#!/usr/bin/env python3
"""Phase E-0：美股旧财务对象归档与隔离恢复演练工具。

规格：docs/core/US_PHASE_E_LEGACY_ARCHIVE_RESTORE_TASK.md

子命令：
  preflight  → 检查工具、对象、基线、MV 依赖、目标位置，不写生产数据
  archive    → 导出、校验、上传、下载校验；不做 DROP
  restore    → 只恢复到受限命名的隔离数据库并验证

不变约束（§3）：
  - 不执行 DROP/TRUNCATE/VACUUM FULL/--clean，不变更生产对象；  # scan-allow: 规范文本引用
  - 凭证只通过 PG* 环境变量注入子进程，绝不出现在 argv/日志/manifest；
  - restore 目标库名必须精确等于 stock_data_legacy_restore_<run-id>。
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urlunparse

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import psycopg2  # noqa: E402

import config  # noqa: E402
import scripts.phase_c_baseline as baseline_mod  # noqa: E402

logger = logging.getLogger("archive_us_legacy_financials")

# 六对象名单唯一来源：phase_c_baseline（§4.1 禁止维护第二份清单）
RETIRING_OBJECTS = baseline_mod.RETIRING_OBJECTS

# MV refresh 的受限依赖（§4.2）：仅 US 行，不是待退役对象
DEPENDENCY_TABLES = ("stock_info", "daily_quote")

WIDE_DUMP_NAME = "legacy_wide_tables.dump"
MV_DUMP_NAME = "legacy_materialized_views_schema.dump"
DEPS_SQL_GZ_NAME = "us_mv_refresh_dependencies.sql.gz"
MANIFEST_NAME = "manifest.json"
SHA256SUMS_NAME = "SHA256SUMS"
ARTIFACT_NAMES = (WIDE_DUMP_NAME, MV_DUMP_NAME, DEPS_SQL_GZ_NAME)

RESTORE_DB_PREFIX = "stock_data_legacy_restore_"
COSFS_ARCHIVE_ROOT = Path("/lhcos-data")

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$")

# 依赖 SQL 校验的拒绝模式（§4.3.2：不得含 DDL 删除或非白名单写入）
_FORBIDDEN_SQL_RE = re.compile(  # scan-allow: 校验规则定义
    r"\b(DROP|TRUNCATE|VACUUM|DELETE\s+FROM|INSERT\s+INTO|GRANT|REVOKE|"  # scan-allow
    r"CREATE\s+EXTENSION|ALTER\s+DATABASE|ALTER\s+SYSTEM)\b",  # scan-allow
    re.IGNORECASE,
)
# UPDATE 单独处理，避免误伤 "UPDATE ..." 出现在注释中的同时仍拦截真实 UPDATE
_FORBIDDEN_UPDATE_RE = re.compile(  # scan-allow: 校验规则定义
    r"^\s*UPDATE\s+\S+\s+SET\b", re.IGNORECASE | re.MULTILINE,
)


class PreflightError(RuntimeError):
    """预检失败：停止而非降级。"""


class RestoreVerificationError(RuntimeError):
    """恢复演练验证失败：保留隔离库与现场。"""


# ── 参数与命名校验 ─────────────────────────────────────────


def validate_run_id(run_id: str) -> str:
    if not run_id or not _RUN_ID_RE.match(run_id) or ".." in run_id:
        raise ValueError(f"非法 run id: {run_id!r}（仅允许字母数字开头，含 . _ -，长度 3-64）")
    return run_id


def derive_restore_db(run_id: str) -> str:
    name = f"{RESTORE_DB_PREFIX}{validate_run_id(run_id)}"
    if len(name) > 63:
        raise ValueError(f"派生恢复库名超过 63 字符: {name!r}")
    return name


def validate_restore_db_arg(run_id: str, restore_db: str) -> str:
    expected = derive_restore_db(run_id)
    if restore_db != expected:
        raise ValueError(
            f"--restore-db 必须精确等于派生名 {expected!r}，收到 {restore_db!r}"
        )
    return restore_db


def validate_archive_dir(path: str, run_id: str, *, require_empty: bool) -> Path:
    """archive 目录必须在仓库外、目录名含 run id；require_empty 时须为不存在或空目录。

    restore 不需要本地 archive 目录存在（产物从对象存储下载），只校验位置约束。
    """
    p = Path(path).expanduser().resolve()
    root = config.PROJECT_ROOT.resolve()
    if p == root or root in p.parents:
        raise ValueError(f"archive 目录不得在仓库内: {p}")
    if validate_run_id(run_id) not in p.name:
        raise ValueError(f"archive 目录名必须包含本次 run id {run_id!r}: {p.name!r}")
    if require_empty and p.exists() and (not p.is_dir() or any(p.iterdir())):
        raise ValueError(f"archive 目录必须是不存在或空目录: {p}")
    return p


def sanitize_uri(uri: str) -> str:
    """脱敏 URI：去掉 userinfo 与 query/fragment，只保留 scheme://host/path。"""
    parsed = urlparse(uri)
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urlunparse((parsed.scheme, host, parsed.path, "", "", ""))


# ── 子进程执行（凭证只走环境变量）─────────────────────────


def _pg_env(dbname: str | None = None) -> dict:
    env = dict(os.environ)
    env["PGHOST"] = str(config.db.host)
    env["PGPORT"] = str(config.db.port)
    env["PGUSER"] = str(config.db.user)
    env["PGPASSWORD"] = str(config.db.password)
    env["PGDATABASE"] = dbname or str(config.db.dbname)
    return env


def _run_cli(
    argv: list[str],
    *,
    env: dict | None = None,
    dry_run: bool = False,
    check_stderr_pattern: re.Pattern | None = None,
) -> subprocess.CompletedProcess | None:
    """执行 CLI。dry-run 只打印 argv（argv 永不包含凭证）。

    check_stderr_pattern 命中时按失败处理（restore 的 warning 不得放行）。
    """
    if dry_run:
        logger.info("[dry-run] 将执行: %s", " ".join(argv))
        return None
    logger.info("执行: %s", " ".join(argv))
    proc = subprocess.run(argv, env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"命令失败(rc={proc.returncode}): {argv[0]} …\nstderr: {proc.stderr[-2000:]}"
        )
    if check_stderr_pattern and proc.stderr and check_stderr_pattern.search(proc.stderr):
        raise RuntimeError(
            f"命令 stderr 出现拒绝模式 {check_stderr_pattern.pattern!r}: "
            f"{argv[0]} …\nstderr: {proc.stderr[-2000:]}"
        )
    return proc


# ── 数据库访问（本工具的生产访问全部是只读 SELECT）─────────


def _connect(dbname: str | None = None, *, readonly: bool = True) -> psycopg2.extensions.connection:
    conn = psycopg2.connect(
        host=config.db.host,
        port=config.db.port,
        dbname=dbname or config.db.dbname,
        user=config.db.user,
        password=config.db.password,
    )
    conn.set_session(readonly=readonly, autocommit=True)
    return conn


def _query(conn, sql: str, params: tuple = ()) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _object_stats_conn(conn, obj: str) -> dict:
    """与 phase_c_baseline._object_stats 等价的全行稳定 hash，可在任意库连接上运行。"""
    rows = _query(
        conn,
        f"SELECT COUNT(*), md5(string_agg(md5(t::text), '' ORDER BY t::text)) "
        f"FROM (SELECT * FROM {obj}) t",
    )
    count, agg = rows[0]
    updated_col = None
    for col in ("updated_at", "sync_time", "created_at"):
        try:
            r = _query(conn, f"SELECT MAX({col}) FROM {obj}")
            updated_col = (col, r[0][0].isoformat() if r and r[0][0] else None)
            break
        except Exception:
            continue
    return {
        "row_count": int(count),
        "content_md5": agg,
        "max_updated_column": updated_col[0] if updated_col else None,
        "max_updated_at": updated_col[1] if updated_col else None,
    }


def _classify_retiring_objects(conn) -> tuple[str, list[str], list[str], dict]:
    """从 pg_class 取得六对象的 schema/relkind/OID；类型断言不通过即停止。"""
    rows = _query(
        conn,
        "SELECT n.nspname, c.relname, c.oid, c.relkind "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.relname = ANY(%s) AND n.nspname NOT IN ('pg_catalog', 'information_schema')",
        (list(RETIRING_OBJECTS),),
    )
    found = {r[1]: r for r in rows}
    missing = set(RETIRING_OBJECTS) - set(found)
    if missing:
        raise PreflightError(f"待退役对象缺失: {sorted(missing)}")
    schemas = {r[0] for r in rows}
    if len(schemas) != 1:
        raise PreflightError(f"六对象不在单一 schema: {schemas}")
    tables = sorted(r[1] for r in rows if r[3] == "r")
    mvs = sorted(r[1] for r in rows if r[3] == "m")
    if len(tables) != 3 or len(mvs) != 3:
        raise PreflightError(
            f"六对象类型断言失败: 普通表 {tables} / 物化视图 {mvs}（应各 3 个）"
        )
    meta = {
        r[1]: {"schema": r[0], "oid": r[2], "relkind": r[3]} for r in rows
    }
    return schemas.pop(), tables, mvs, meta


def _mv_dependencies(conn, mv_oids: dict[str, int]) -> dict[str, list[str]]:
    """枚举每个 MV 的全部直接/间接 relation 依赖（经 pg_rewrite 传递闭包）。"""
    direct_sql = (
        "SELECT DISTINCT cl.relname "
        "FROM pg_depend d "
        "JOIN pg_rewrite r ON r.oid = d.objid "
        "JOIN pg_class cl ON cl.oid = d.refobjid "
        "WHERE d.refclassid = 'pg_class'::regclass AND r.ev_class = %s AND cl.oid <> %s"
    )
    result: dict[str, list[str]] = {}
    for mv, oid in mv_oids.items():
        seen: set[str] = set()
        queue = [(mv, oid)]
        while queue:
            cur_name, cur_oid = queue.pop()
            for (dep,) in _query(conn, direct_sql, (cur_oid, cur_oid)):
                if dep in seen or dep == cur_name:
                    continue
                seen.add(dep)
                if dep in mv_oids:  # 上游 MV：继续向下传递
                    queue.append((dep, mv_oids[dep]))
        seen.discard(mv)
        result[mv] = sorted(seen)
    return result


def _check_mv_dependencies(deps: dict[str, list[str]], tables: list[str], mvs: list[str]) -> None:
    """§4.3.1：依赖至少含三宽表+两上游 MV+stock_info+daily_quote；出现白名单外依赖即停止。"""
    all_deps = set().union(*(set(v) for v in deps.values())) if deps else set()
    required = set(tables) | set(DEPENDENCY_TABLES)
    missing = required - all_deps
    if missing:
        raise PreflightError(f"MV 依赖枚举缺少必需对象: {sorted(missing)}")
    allowed = set(tables) | set(mvs) | set(DEPENDENCY_TABLES)
    unexpected = all_deps - allowed
    if unexpected:
        raise PreflightError(
            f"MV 出现本任务未纳入的新增依赖: {sorted(unexpected)} —— 停止，不得以空表/NULL 降级"
        )


def _mv_refresh_order(deps: dict[str, list[str]], mvs: list[str]) -> list[str]:
    """按依赖拓扑排序 MV refresh 顺序（同层按名称字典序，确定可重放）。"""
    remaining = set(mvs)
    order: list[str] = []
    while remaining:
        ready = sorted(
            mv for mv in remaining
            if not (set(deps.get(mv, [])) & remaining)
        )
        if not ready:
            raise PreflightError(f"MV 依赖存在环: {sorted(remaining)}")
        order.extend(ready)
        remaining -= set(ready)
    return order


# ── pg_restore --list 解析与归档内容白名单校验 ─────────────

_LIST_IDS_RE = re.compile(r"^\d+;\s+\d+\s+\d+\s+(?P<body>.+)$")

# pg_restore --list 已知条目前缀（长的在前，避免 TABLE 吃掉 TABLE DATA）
_KNOWN_DESCS = sorted(
    (
        "MATERIALIZED VIEW DATA", "MATERIALIZED VIEW", "SEQUENCE OWNED BY",
        "SEQUENCE SET", "TABLE DATA", "FK CONSTRAINT", "CHECK CONSTRAINT",
        "CONSTRAINT", "DEFAULT", "INDEX", "TABLE", "SEQUENCE", "ACL",
        "COMMENT", "RULE", "TRIGGER", "TYPE", "FUNCTION", "VIEW",
    ),
    key=len,
    reverse=True,
)


def parse_pg_restore_list(text: str) -> list[dict]:
    """解析 pg_restore --list 输出为 {desc, namespace, name} 条目。"""
    entries = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(";"):
            continue
        m = _LIST_IDS_RE.match(line)
        if not m:
            continue
        body = m.group("body")
        desc, rest = None, None
        for known in _KNOWN_DESCS:
            if body.startswith(known + " "):
                desc, rest = known, body[len(known) + 1:].split()
                break
        if desc is None:
            tokens = body.split()
            desc, rest = tokens[0], tokens[1:]
        if not rest or len(rest) < 2:
            continue
        entries.append({
            "desc": desc,
            "namespace": rest[0],
            "name": rest[1],
        })
    return entries


def verify_dump_listing(
    entries: list[dict],
    *,
    expected_tables: tuple[str, ...] = (),
    expected_mvs: tuple[str, ...] = (),
) -> None:
    """缺任一目标对象、或出现非预期 TABLE/MATERIALIZED VIEW 即拒绝。"""
    tables = {e["name"] for e in entries if e["desc"] == "TABLE"}
    mvs = {e["name"] for e in entries if e["desc"] == "MATERIALIZED VIEW"}
    if expected_tables:
        if tables != set(expected_tables):
            raise RestoreVerificationError(
                f"宽表 dump 对象不符: 期望 {sorted(expected_tables)}，实际 {sorted(tables)}"
            )
        if mvs:
            raise RestoreVerificationError(f"宽表 dump 出现物化视图: {sorted(mvs)}")
    if expected_mvs:
        if mvs != set(expected_mvs):
            raise RestoreVerificationError(
                f"MV dump 对象不符: 期望 {sorted(expected_mvs)}，实际 {sorted(mvs)}"
            )
        if tables:
            raise RestoreVerificationError(f"MV dump 出现普通表: {sorted(tables)}")


def verify_dependency_sql(text: str) -> None:
    """§4.3.2：依赖 SQL 只含两张白名单表的 schema/US 数据，无删除/写入其他对象。"""
    m = _FORBIDDEN_SQL_RE.search(text)
    if m:
        raise RestoreVerificationError(f"依赖 SQL 含拒绝语句 {m.group(0)!r}")
    m = _FORBIDDEN_UPDATE_RE.search(text)
    if m:
        raise RestoreVerificationError("依赖 SQL 含 UPDATE 写入语句")
    allowed = set(DEPENDENCY_TABLES)
    targets: set[str] = set()
    for pattern in (
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:public\.)?(\w+)",
        r"COPY\s+(?:public\.)?(\w+)\s*\(",
        r"ALTER\s+TABLE\s+(?:ONLY\s+)?(?:public\.)?(\w+)",
        r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+\S+\s+ON\s+(?:public\.)?(\w+)",
    ):
        targets.update(re.findall(pattern, text, re.IGNORECASE))
    unexpected = targets - allowed
    if unexpected:
        raise RestoreVerificationError(
            f"依赖 SQL 涉及白名单外对象: {sorted(unexpected)}"
        )
    if not {"stock_info", "daily_quote"} <= targets:
        raise RestoreVerificationError("依赖 SQL 缺少 stock_info 或 daily_quote")


# ── 对象存储 ───────────────────────────────────────────────


def _assert_cosfs_mount(root: Path) -> str:
    """拒绝把掉线后的 /lhcos-data 本地目录当作 COS 归档。"""
    for tool in ("mountpoint", "findmnt"):
        if not shutil.which(tool):
            raise PreflightError(f"COS 归档挂载校验需要 {tool}，但当前未找到")

    try:
        mounted = subprocess.run(
            ["mountpoint", "-q", str(root)], capture_output=True, text=True, check=False
        )
    except OSError as exc:
        raise PreflightError(f"无法检查 COS 挂载点 {root}: {exc}") from exc
    if mounted.returncode != 0:
        raise PreflightError(f"COS 归档挂载点未在线: {root}")

    try:
        fstype_result = subprocess.run(
            ["findmnt", "--noheadings", "--output", "FSTYPE", "--target", str(root)],
            capture_output=True, text=True, check=False,
        )
    except OSError as exc:
        raise PreflightError(f"无法读取 COS 挂载类型 {root}: {exc}") from exc
    fstype = fstype_result.stdout.strip()
    if fstype_result.returncode != 0 or fstype != "fuse.cosfs":
        observed = fstype or "<unknown>"
        raise PreflightError(
            f"COS 归档挂载类型错误: {root} 期望 fuse.cosfs，实际 {observed}"
        )
    return fstype


class FileArchiveStore:
    """file:// URI：本地目录后端（演练/测试用）。"""

    def __init__(self, uri: str):
        self.root = Path(urlparse(uri).path).expanduser().resolve()

    def _key(self, run_id: str, name: str) -> Path:
        return self.root / run_id / name

    def probe(self, run_id: str) -> None:
        # `file:///lhcos-data` 是生产 COS 挂载，而不是通用本地文件后端；必须防止挂载
        # 掉线后把写入同名本地目录误报为远端归档。其他 file:// URI 保留给离线测试。
        if self.root == COSFS_ARCHIVE_ROOT:
            _assert_cosfs_mount(self.root)
        if not self.root.is_dir():
            raise PreflightError(f"archive URI 根目录不存在: {self.root}")
        probe = self.root / f".probe_{run_id}"
        probe.write_text("probe")
        if probe.read_text() != "probe":
            raise PreflightError(f"archive URI 读回校验失败: {self.root}")
        probe.unlink()

    def upload(self, local: Path, run_id: str, name: str) -> None:
        dest = self._key(run_id, name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local, dest)

    def download(self, run_id: str, name: str, dest_dir: Path) -> Path:
        src = self._key(run_id, name)
        if not src.is_file():
            raise RuntimeError(f"对象存储中不存在: {sanitize_uri('file://' + str(src))}")
        dest = dest_dir / name
        shutil.copy2(src, dest)
        return dest


class S3ArchiveStore:
    """s3:// URI：经 aws CLI（凭证由 aws 配置/实例角色管理，不入命令行）。"""

    def __init__(self, uri: str):
        if not shutil.which("aws"):
            raise PreflightError("s3:// URI 需要 aws CLI，未找到")
        self.base = uri.rstrip("/")

    def _key(self, run_id: str, name: str) -> str:
        return f"{self.base}/{run_id}/{name}"

    def probe(self, run_id: str) -> None:
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("probe")
            tmp = f.name
        try:
            key = self._key(run_id, ".probe")
            _run_cli(["aws", "s3", "cp", tmp, key, "--quiet"])
            _run_cli(["aws", "s3", "cp", key, tmp + ".back", "--quiet"])
            if Path(tmp + ".back").read_text() != "probe":
                raise PreflightError("s3 读回校验失败")
            _run_cli(["aws", "s3", "rm", key, "--quiet"])
        finally:
            Path(tmp).unlink(missing_ok=True)
            Path(tmp + ".back").unlink(missing_ok=True)

    def upload(self, local: Path, run_id: str, name: str) -> None:
        _run_cli(["aws", "s3", "cp", str(local), self._key(run_id, name), "--quiet"])

    def download(self, run_id: str, name: str, dest_dir: Path) -> Path:
        dest = dest_dir / name
        _run_cli(["aws", "s3", "cp", self._key(run_id, name), str(dest), "--quiet"])
        return dest


def make_store(uri: str):
    parsed = urlparse(uri)
    if parsed.scheme == "file":
        return FileArchiveStore(uri)
    if parsed.scheme == "s3":
        return S3ArchiveStore(uri)
    raise ValueError(
        f"不支持的 archive URI scheme: {parsed.scheme!r}（支持 file/s3；"
        "其他对象存储请由项目所有者提供凭证方式后扩展）"
    )


# ── 校验和 ─────────────────────────────────────────────────


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ── preflight ──────────────────────────────────────────────

_REQUIRED_TOOLS = ("pg_dump", "pg_restore", "psql", "createdb", "dropdb")


def run_preflight(args) -> dict:
    report: dict = {"run_id": args.run_id, "checked_at": datetime.now(timezone.utc).isoformat()}

    for tool in _REQUIRED_TOOLS:
        if not shutil.which(tool):
            raise PreflightError(f"缺少工具: {tool}")
    versions = {}
    for tool in ("pg_dump", "pg_restore"):
        proc = _run_cli([tool, "--version"])
        versions[tool] = proc.stdout.strip()
    report["tool_versions"] = versions

    conn = _connect()
    try:
        report["server_version"] = _query(conn, "SHOW server_version")[0][0]
        report["database"] = config.db.dbname  # 只记录库名，不含 host/凭证

        schema, tables, mvs, meta = _classify_retiring_objects(conn)
        report["schema"] = schema
        report["objects"] = meta
        report["wide_tables"] = tables
        report["materialized_views"] = mvs

        mv_oids = {mv: meta[mv]["oid"] for mv in mvs}
        deps = _mv_dependencies(conn, mv_oids)
        _check_mv_dependencies(deps, tables, mvs)
        report["mv_dependencies"] = deps
        report["mv_refresh_order"] = _mv_refresh_order(deps, mvs)

        # MV 定义与唯一索引（restore 验证的参照）
        mv_meta = {}
        for mv in mvs:
            viewdef = _query(conn, "SELECT pg_get_viewdef(%s)", (meta[mv]["oid"],))[0][0]
            indexes = _query(
                conn,
                "SELECT i.indexname, ix.indisunique FROM pg_indexes i "
                "JOIN pg_class c ON c.relname = i.indexname "
                "JOIN pg_index ix ON ix.indexrelid = c.oid "
                "WHERE i.schemaname = %s AND i.tablename = %s",
                (schema, mv),
            )
            mv_meta[mv] = {
                "viewdef": viewdef,
                "indexes": {name: {"unique": bool(is_unique)} for name, is_unique in indexes},
            }
        report["mv_meta"] = mv_meta

        # 依赖表行数（仅 US 子集）
        report["dependency_rows"] = {
            t: _query(conn, f"SELECT COUNT(*) FROM {schema}.{t} WHERE market = 'US'")[0][0]
            for t in DEPENDENCY_TABLES
        }
    finally:
        conn.close()

    # Phase C 零写入基线必须存在且通过
    if not baseline_mod.OUT.exists():
        raise PreflightError(f"Phase C 基线不存在: {baseline_mod.OUT} —— 先运行 record")
    violations = baseline_mod.find_violations()
    if violations:
        raise PreflightError(f"零写入基线检查未通过: {json.dumps(violations, ensure_ascii=False)}")
    report["baseline_stats"] = {obj: baseline_mod._object_stats(obj) for obj in RETIRING_OBJECTS}

    # archive 目标位置
    store = make_store(args.archive_uri)
    if not args.dry_run:
        store.probe(args.run_id)
    report["archive_uri"] = sanitize_uri(args.archive_uri)

    if args.subcommand == "restore":
        validate_restore_db_arg(args.run_id, args.restore_db)
        conn = _connect()
        try:
            exists = _query(
                conn, "SELECT 1 FROM pg_database WHERE datname = %s", (args.restore_db,)
            )
        finally:
            conn.close()
        if exists:
            raise PreflightError(f"恢复库已存在: {args.restore_db}")

    return report


# ── archive ────────────────────────────────────────────────

_STDERR_REJECT_RE = re.compile(r"\b(ERROR|FATAL|WARNING)\b")


def _pg_dump(schema: str, objects: list[str], out: Path, *, schema_only: bool, dry_run: bool) -> None:
    argv = ["pg_dump", "--format=custom", "--file", str(out)]
    if schema_only:
        argv.append("--schema-only")
    for obj in objects:
        argv += ["--table", f"{schema}.{obj}"]
    _run_cli(argv, env=_pg_env(), dry_run=dry_run, check_stderr_pattern=_STDERR_REJECT_RE)


def _export_dependency_sql(conn, schema: str, out_gz: Path) -> dict:
    """生成受限 US 依赖数据集：pg_dump schema-only + 显式列 COPY（仅 market='US' 行）。

    数据经 copy_expert 流式写入 gzip，避免大结果集驻留内存。
    """
    schema_sql = subprocess.run(
        ["pg_dump", "--schema-only", "--table", f"{schema}.{DEPENDENCY_TABLES[0]}",
         "--table", f"{schema}.{DEPENDENCY_TABLES[1]}"],
        env=_pg_env(), capture_output=True, text=True, check=True,
    ).stdout

    counts = {}
    copy_sections = []
    for table in DEPENDENCY_TABLES:
        cols = [
            r[0]
            for r in _query(
                conn,
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
                (schema, table),
            )
        ]
        col_list = ", ".join(cols)
        order_by = "stock_code, trade_date" if table == "daily_quote" else "stock_code"
        select_sql = (
            f"SELECT {col_list} FROM {schema}.{table} "
            f"WHERE market = 'US' ORDER BY {order_by}"
        )
        counts[table] = _query(
            conn, f"SELECT COUNT(*) FROM {schema}.{table} WHERE market = 'US'"
        )[0][0]
        copy_sections.append((table, col_list, select_sql))

    # 自检：schema 与 COPY 头部（不含数据行）必须通过自己的白名单校验
    header_text = schema_sql + "\n" + "\n".join(
        f"COPY {schema}.{t} ({cols}) FROM stdin;" for t, cols, _ in copy_sections
    )
    verify_dependency_sql(header_text)

    with gzip.open(out_gz, "wt", encoding="utf-8") as f:
        f.write(schema_sql.rstrip())
        f.write("\n")
        for table, col_list, select_sql in copy_sections:
            f.write(f"COPY {schema}.{table} ({col_list}) FROM stdin;\n")
            with conn.cursor() as cur:
                cur.copy_expert(f"COPY ({select_sql}) TO STDOUT", f)
            f.write("\\.\n\n")
    return counts


def _git_commit() -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=_PROJECT_ROOT
        )
        return proc.stdout.strip() if proc.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def _write_checksums(archive_dir: Path) -> dict:
    sums = {}
    for name in ARTIFACT_NAMES:
        sums[name] = sha256_file(archive_dir / name)
    lines = [f"{digest}  {name}" for name, digest in sorted(sums.items())]
    (archive_dir / SHA256SUMS_NAME).write_text("\n".join(lines) + "\n")
    return sums


def _upload_and_verify(store, archive_dir: Path, run_id: str, names: tuple[str, ...]) -> None:
    """上传后下载到独立临时目录，重新计算 SHA-256 并精确比对（§4.2）。"""
    for name in names:
        store.upload(archive_dir / name, run_id, name)
    expected = {name: sha256_file(archive_dir / name) for name in names}
    with tempfile.TemporaryDirectory(prefix=f"dlverify_{run_id}_") as tmp:
        for name in names:
            downloaded = store.download(run_id, name, Path(tmp))
            actual = sha256_file(downloaded)
            if actual != expected[name]:
                raise RuntimeError(
                    f"下载副本 checksum 不符: {name} 本地 {expected[name]} ≠ 下载 {actual}"
                )


def run_archive(args) -> dict:
    report = run_preflight(args)
    schema = report["schema"]
    tables = report["wide_tables"]
    mvs = report["materialized_views"]

    if args.dry_run:
        _pg_dump(schema, tables, Path("<archive-dir>") / WIDE_DUMP_NAME, schema_only=False, dry_run=True)
        _pg_dump(schema, mvs, Path("<archive-dir>") / MV_DUMP_NAME, schema_only=True, dry_run=True)
        logger.info("[dry-run] 将生成依赖数据集 %s（stock_info/daily_quote 仅 US 行）", DEPS_SQL_GZ_NAME)
        logger.info("[dry-run] 将上传并下载校验: %s", sorted(ARTIFACT_NAMES))
        return report

    archive_dir = validate_archive_dir(args.archive_dir, args.run_id, require_empty=True)
    archive_dir.mkdir(parents=True, exist_ok=True)

    _pg_dump(schema, tables, archive_dir / WIDE_DUMP_NAME, schema_only=False, dry_run=False)
    _pg_dump(schema, mvs, archive_dir / MV_DUMP_NAME, schema_only=True, dry_run=False)

    conn = _connect()
    try:
        dep_counts = _export_dependency_sql(conn, schema, archive_dir / DEPS_SQL_GZ_NAME)
    finally:
        conn.close()

    # 导出后零写入复查（§3.2：任何差异立即停止）
    violations = baseline_mod.find_violations()
    if violations:
        raise RuntimeError(
            f"导出后零写入检查未通过，保留现场: {json.dumps(violations, ensure_ascii=False)}"
        )

    sums = _write_checksums(archive_dir)

    listings = {}
    for name in (WIDE_DUMP_NAME, MV_DUMP_NAME):
        proc = _run_cli(["pg_restore", "--list", str(archive_dir / name)])
        listings[name] = proc.stdout

    manifest = {
        "run_id": args.run_id,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "database": config.db.dbname,
        "tool_versions": report["tool_versions"],
        "server_version": report["server_version"],
        "objects": report["objects"],
        "mv_dependencies": report["mv_dependencies"],
        "mv_refresh_order": report["mv_refresh_order"],
        "mv_meta": report["mv_meta"],
        "baseline_stats_pre_export": report["baseline_stats"],
        "dependency_dataset": {
            "market_filter": "US",
            "row_counts": dep_counts,
            "exported_at_utc": datetime.now(timezone.utc).isoformat(),
        },
        "files": {
            name: {"size": (archive_dir / name).stat().st_size, "sha256": sums[name]}
            for name in ARTIFACT_NAMES
        },
        "pg_restore_listings": listings,
        "archive_uri": sanitize_uri(args.archive_uri),
        "retention": getattr(args, "retention", "") or None,
        "executor": os.environ.get("USER", "unknown"),
        "git_commit": _git_commit(),
        "restore_verification": None,
    }
    (archive_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    )

    store = make_store(args.archive_uri)
    _upload_and_verify(store, archive_dir, args.run_id, ARTIFACT_NAMES + (MANIFEST_NAME, SHA256SUMS_NAME))
    logger.info("归档完成并已通过下载校验: %s", sanitize_uri(args.archive_uri))
    return manifest


# ── restore ────────────────────────────────────────────────


def _createdb_argv(restore_db: str) -> list[str]:
    assert restore_db.startswith(RESTORE_DB_PREFIX), restore_db
    return ["createdb", "-T", "template0", restore_db]


def _dropdb_argv(restore_db: str) -> list[str]:
    assert restore_db.startswith(RESTORE_DB_PREFIX), restore_db
    return ["dropdb", restore_db]


def _psql_restore_sql(restore_db: str, sql_path: Path, dry_run: bool) -> None:
    _run_cli(
        ["psql", "-X", "-v", "ON_ERROR_STOP=1", "-d", restore_db, "-f", str(sql_path)],
        env=_pg_env(restore_db), dry_run=dry_run, check_stderr_pattern=_STDERR_REJECT_RE,
    )


def _pg_restore(restore_db: str, dump: Path, dry_run: bool) -> None:
    _run_cli(
        ["pg_restore", "--exit-on-error", "-d", restore_db, str(dump)],
        env=_pg_env(restore_db), dry_run=dry_run, check_stderr_pattern=_STDERR_REJECT_RE,
    )


def _download_verified(store, run_id: str, dest_dir: Path) -> dict[str, Path]:
    """从对象存储下载全部文件并按 SHA256SUMS 精确校验。"""
    paths = {name: store.download(run_id, name, dest_dir)
             for name in ARTIFACT_NAMES + (MANIFEST_NAME, SHA256SUMS_NAME)}
    expected = {}
    for line in paths[SHA256SUMS_NAME].read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        digest, name = line.split(None, 1)
        expected[name.strip()] = digest
    for name in ARTIFACT_NAMES:
        if name not in expected:
            raise RestoreVerificationError(f"SHA256SUMS 缺少条目: {name}")
        actual = sha256_file(paths[name])
        if actual != expected[name]:
            raise RestoreVerificationError(
                f"下载归档 checksum 不符: {name} 期望 {expected[name]} 实际 {actual}"
            )
    return paths


def run_restore(args) -> dict:
    restore_db = validate_restore_db_arg(args.run_id, args.restore_db)
    report = run_preflight(args)
    schema = report["schema"]
    tables = report["wide_tables"]
    mvs = report["materialized_views"]

    if args.dry_run:
        logger.info("[dry-run] 将下载并校验: %s", sorted(ARTIFACT_NAMES))
        _createdb_argv(restore_db)  # 仅断言命名
        logger.info("[dry-run] 将执行: createdb -T template0 %s", restore_db)
        logger.info("[dry-run] restore 顺序: 依赖SQL → 宽表 dump → MV schema dump")
        logger.info("[dry-run] MV refresh 顺序: %s", report["mv_refresh_order"])
        return report

    store = make_store(args.archive_uri)
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"restore_{args.run_id}_"))
    logger.info("下载目录（与 archive 目录无关）: %s", tmp_dir)

    paths = _download_verified(store, args.run_id, tmp_dir)
    manifest = json.loads(paths[MANIFEST_NAME].read_text())

    # 归档内容白名单校验（§4.3.2）
    wide_entries = parse_pg_restore_list(
        _run_cli(["pg_restore", "--list", str(paths[WIDE_DUMP_NAME])]).stdout
    )
    verify_dump_listing(wide_entries, expected_tables=tuple(tables))
    mv_entries = parse_pg_restore_list(
        _run_cli(["pg_restore", "--list", str(paths[MV_DUMP_NAME])]).stdout
    )
    verify_dump_listing(mv_entries, expected_mvs=tuple(mvs))

    deps_sql_path = tmp_dir / DEPS_SQL_GZ_NAME[: -len(".gz")]
    with gzip.open(paths[DEPS_SQL_GZ_NAME], "rt", encoding="utf-8") as f:
        deps_sql = f.read()
    verify_dependency_sql(deps_sql)
    deps_sql_path.write_text(deps_sql)

    _run_cli(_createdb_argv(restore_db), env=_pg_env("postgres"))
    verification: dict = {"restore_db": restore_db, "ok": False}
    try:
        _psql_restore_sql(restore_db, deps_sql_path, dry_run=False)
        _pg_restore(restore_db, paths[WIDE_DUMP_NAME], dry_run=False)
        _pg_restore(restore_db, paths[MV_DUMP_NAME], dry_run=False)

        rconn = _connect(restore_db, readonly=False)
        try:
            # 宽表：与归档前基线精确一致（§4.3.5）
            wide_stats = {}
            for t in tables:
                stats = _object_stats_conn(rconn, f"{schema}.{t}")
                base = manifest["baseline_stats_pre_export"][t]
                if stats != base:
                    raise RestoreVerificationError(
                        f"恢复后宽表与基线不一致: {t}\n恢复 {stats}\n基线 {base}"
                    )
                wide_stats[t] = stats
            verification["wide_tables"] = wide_stats

            # 依赖数据集：行数一致且全部 market='US'
            dep_check = {}
            for t in DEPENDENCY_TABLES:
                total = _query(rconn, f"SELECT COUNT(*) FROM {schema}.{t}")[0][0]
                non_us = _query(
                    rconn,
                    f"SELECT COUNT(*) FROM {schema}.{t} WHERE market IS DISTINCT FROM 'US'",
                )[0][0]
                expected = manifest["dependency_dataset"]["row_counts"][t]
                if total != expected or non_us != 0:
                    raise RestoreVerificationError(
                        f"依赖数据集不符: {t} 行数 {total}/{expected}，非 US 行 {non_us}"
                    )
                dep_check[t] = {"rows": total, "non_us_rows": non_us}
            verification["dependency_dataset"] = dep_check

            # MV：定义/唯一索引与生产一致，按依赖顺序非并发 refresh
            mv_check = {}
            for mv in report["mv_refresh_order"]:
                restored_viewdef = _query(
                    rconn, "SELECT pg_get_viewdef(%s::regclass)", (f"{schema}.{mv}",)
                )[0][0]
                # restore 会重解析定义文本，解析器可能规范化表达式（如数组整体
                # 强转被改写为逐元素强转），逐字比对会误报。把生产定义在同一
                # 隔离库重解析归一化后再比对，语义相同则文本必然相同。
                prod_viewdef = report["mv_meta"][mv]["viewdef"].rstrip().rstrip(";")
                with rconn.cursor() as cur:
                    cur.execute(
                        f"CREATE TEMP VIEW _e0_viewdef_check AS {prod_viewdef}"
                    )
                    cur.execute(
                        "SELECT pg_get_viewdef('_e0_viewdef_check'::regclass)"
                    )
                    prod_normalized = cur.fetchone()[0]
                    cur.execute("DROP VIEW _e0_viewdef_check")
                if restored_viewdef != prod_normalized:
                    raise RestoreVerificationError(
                        f"MV 定义与生产语义不一致: {mv}\n"
                        f"恢复(归一化前): {restored_viewdef[:500]}\n"
                        f"生产(归一化后): {prod_normalized[:500]}"
                    )
                prod_indexes = report["mv_meta"][mv]["indexes"]
                got = _query(
                    rconn,
                    "SELECT i.indexname, ix.indisunique FROM pg_indexes i "
                    "JOIN pg_class c ON c.relname = i.indexname "
                    "JOIN pg_index ix ON ix.indexrelid = c.oid "
                    "WHERE i.schemaname = %s AND i.tablename = %s",
                    (schema, mv),
                )
                got_map = {name: bool(u) for name, u in got}
                for idx, imeta in prod_indexes.items():
                    if idx not in got_map:
                        raise RestoreVerificationError(f"MV 缺索引: {mv}.{idx}")
                    if imeta["unique"] and not got_map[idx]:
                        raise RestoreVerificationError(f"MV 唯一索引退化为非唯一: {mv}.{idx}")
                with rconn.cursor() as cur:
                    cur.execute(f"REFRESH MATERIALIZED VIEW {schema}.{mv}")
                stats = _object_stats_conn(rconn, f"{schema}.{mv}")
                mv_check[mv] = stats
            verification["materialized_views"] = mv_check
        finally:
            rconn.close()

        # 演练全程生产零写入复查
        violations = baseline_mod.find_violations()
        if violations:
            raise RestoreVerificationError(
                f"演练后生产零写入检查未通过: {json.dumps(violations, ensure_ascii=False)}"
            )
        verification["ok"] = True
    except Exception as exc:
        logger.error(
            "恢复演练失败，隔离库 %s 已保留供排查，禁止清理证据。", restore_db
        )
        verification["failure_kept_db"] = restore_db
        verification["failure_reason"] = f"{type(exc).__name__}: {exc}"
        failure_note = tmp_dir / "restore_failure.json"
        failure_note.write_text(
            json.dumps(verification, indent=2, ensure_ascii=False, default=str) + "\n"
        )
        logger.error("失败记录: %s", failure_note)
        raise
    finally:
        verification["finished_at_utc"] = datetime.now(timezone.utc).isoformat()

    # 成功：按记录删除隔离库（§3.5）
    _run_cli(_dropdb_argv(restore_db), env=_pg_env("postgres"))
    logger.info("隔离库已按策略删除: %s", restore_db)

    # 回写 manifest 的恢复验证结果并重新上传
    archive_dir = Path(args.archive_dir).expanduser().resolve()
    if archive_dir.is_dir() and (archive_dir / MANIFEST_NAME).is_file():
        local_manifest = json.loads((archive_dir / MANIFEST_NAME).read_text())
        local_manifest["restore_verification"] = verification
        (archive_dir / MANIFEST_NAME).write_text(
            json.dumps(local_manifest, indent=2, ensure_ascii=False) + "\n"
        )
        store.upload(archive_dir / MANIFEST_NAME, args.run_id, MANIFEST_NAME)
    return verification


# ── CLI ────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Phase E-0 美股旧财务对象归档与隔离恢复演练（不授权任何删除）"
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)
    for name in ("preflight", "archive", "restore"):
        p = sub.add_parser(name)
        p.add_argument("--run-id", required=True)
        p.add_argument("--archive-dir", required=True)
        p.add_argument("--archive-uri", required=True)
        p.add_argument("--dry-run", action="store_true")
        if name == "archive":
            p.add_argument("--retention", default="",
                           help="归档保留期说明（由项目所有者指定，记入 manifest）")
        if name == "restore":
            p.add_argument("--restore-db", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    args = build_parser().parse_args(argv)
    validate_run_id(args.run_id)
    if args.subcommand in ("archive", "restore"):
        validate_archive_dir(
            args.archive_dir,
            args.run_id,
            require_empty=args.subcommand == "archive" and not args.dry_run,
        )

    if args.subcommand == "preflight":
        report = run_preflight(args)
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    elif args.subcommand == "archive":
        run_archive(args)
    else:
        run_restore(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
