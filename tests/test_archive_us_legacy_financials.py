"""tests/test_archive_us_legacy_financials.py

Phase E-0 归档/恢复演练工具单元测试。
规格：docs/core/US_PHASE_E_LEGACY_ARCHIVE_RESTORE_TASK.md §5。

全部为离线单元测试：不连接生产数据库、不创建任何数据库/远端对象。
"""
from __future__ import annotations

import gzip
import json
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import config
import scripts.archive_us_legacy_financials as arch
import scripts.phase_c_baseline as baseline_mod


# ── §5.1 六对象清单复用 ─────────────────────────────────────

class TestObjectListReuse:
    def test_list_is_phase_c_baseline_list(self):
        """工具不得维护第二份对象清单，必须就是 baseline 的同一个对象。"""
        assert arch.RETIRING_OBJECTS is baseline_mod.RETIRING_OBJECTS
        assert len(arch.RETIRING_OBJECTS) == 6

    def test_mv_dependency_check_requires_all_and_rejects_new(self):
        tables = ["us_income_statement", "us_balance_sheet", "us_cash_flow_statement"]
        mvs = ["mv_us_financial_indicator", "mv_us_indicator_ttm", "mv_us_fcf_yield"]
        good = {
            "mv_us_fcf_yield": tables + ["mv_us_indicator_ttm",
                                         "mv_us_financial_indicator",
                                         "stock_info", "daily_quote"],
            "mv_us_indicator_ttm": ["us_income_statement", "us_cash_flow_statement"],
            "mv_us_financial_indicator": tables,
        }
        arch._check_mv_dependencies(good, tables, mvs)  # 不抛

        missing_dep = dict(good, mv_us_fcf_yield=tables)  # 缺 stock_info/daily_quote
        with pytest.raises(arch.PreflightError, match="缺少必需对象"):
            arch._check_mv_dependencies(missing_dep, tables, mvs)

        new_dep = dict(good, mv_us_fcf_yield=good["mv_us_fcf_yield"] + ["stock_share"])
        with pytest.raises(arch.PreflightError, match="新增依赖"):
            arch._check_mv_dependencies(new_dep, tables, mvs)

    def test_mv_refresh_order_respects_dependencies(self):
        deps = {
            "mv_us_financial_indicator": ["us_income_statement"],
            "mv_us_indicator_ttm": ["us_income_statement"],
            "mv_us_fcf_yield": ["mv_us_financial_indicator", "mv_us_indicator_ttm"],
        }
        mvs = sorted(deps)
        order = arch._mv_refresh_order(deps, mvs)
        assert order[-1] == "mv_us_fcf_yield"
        assert set(order) == set(mvs)


# ── §5.2 参数校验与 dry-run ─────────────────────────────────

class TestArgValidation:
    def test_run_id_rejects_bad_values(self):
        for bad in ("", "a", "a b", "../x", "-lead", "x/../y", "x;rm", "x" * 65):
            with pytest.raises(ValueError):
                arch.validate_run_id(bad)
        assert arch.validate_run_id("20260813-legacy.e0") == "20260813-legacy.e0"

    def test_restore_db_must_equal_derived_name(self):
        derived = arch.derive_restore_db("run1")
        assert derived == "stock_data_legacy_restore_run1"
        assert arch.validate_restore_db_arg("run1", derived) == derived
        with pytest.raises(ValueError, match="精确等于"):
            arch.validate_restore_db_arg("run1", "stock_data")
        with pytest.raises(ValueError, match="精确等于"):
            arch.validate_restore_db_arg("run1", "stock_data_legacy_restore_other")

    def test_archive_dir_rejects_repo_internal(self):
        inside = config.PROJECT_ROOT / "build" / "archive_run1"
        with pytest.raises(ValueError, match="仓库内"):
            arch.validate_archive_dir(str(inside), "run1", require_empty=False)

    def test_archive_dir_name_must_contain_run_id(self, tmp_path):
        with pytest.raises(ValueError, match="run id"):
            arch.validate_archive_dir(str(tmp_path / "other_name"), "run1",
                                      require_empty=False)

    def test_archive_dir_must_be_empty_for_archive(self, tmp_path):
        d = tmp_path / "archive_run1"
        d.mkdir()
        (d / "stale.dump").write_text("x")
        with pytest.raises(ValueError, match="空目录"):
            arch.validate_archive_dir(str(d), "run1", require_empty=True)
        # dry-run / restore 不要求存在或为空
        assert arch.validate_archive_dir(str(d), "run1", require_empty=False) == d
        assert arch.validate_archive_dir(
            str(tmp_path / "new_run1"), "run1", require_empty=True
        ) == (tmp_path / "new_run1").resolve()

    def test_dry_run_creates_nothing(self, tmp_path, monkeypatch):
        """dry-run 不创建文件/数据库/远端对象：子进程层全部短路。"""

        def boom(*a, **kw):
            raise AssertionError("dry-run 不得执行任何子进程")

        monkeypatch.setattr(arch.subprocess, "run", boom)
        assert arch._run_cli(["createdb", "x"], dry_run=True) is None
        assert arch._pg_dump("public", ["t1"], tmp_path / "x.dump",
                             schema_only=True, dry_run=True) is None
        assert not (tmp_path / "x.dump").exists()


# ── §5.3 pg_restore --list 与依赖 SQL 校验 ──────────────────

_WIDE_LIST = """\
;
; Archive created at 2026-08-13
;
215; 1259 16391 TABLE public us_income_statement postgres
216; 1259 16398 TABLE public us_balance_sheet postgres
217; 1259 16405 TABLE public us_cash_flow_statement postgres
301; 0 16391 TABLE DATA public us_income_statement postgres
302; 0 16398 TABLE DATA public us_balance_statement postgres
401; 1259 16410 INDEX public idx_us_income_pk postgres
"""

_MV_LIST = """\
;
; Archive created at 2026-08-13
;
501; 1259 20001 MATERIALIZED VIEW public mv_us_financial_indicator postgres
502; 1259 20010 MATERIALIZED VIEW public mv_us_indicator_ttm postgres
503; 1259 20020 MATERIALIZED VIEW public mv_us_fcf_yield postgres
601; 1259 20030 INDEX public idx_mv_us_fcf_yield_pk postgres
"""

_TABLES = ("us_balance_sheet", "us_cash_flow_statement", "us_income_statement")
_MVS = ("mv_us_fcf_yield", "mv_us_financial_indicator", "mv_us_indicator_ttm")


class TestDumpListing:
    def test_parse_entries(self):
        entries = arch.parse_pg_restore_list(_WIDE_LIST)
        tables = {e["name"] for e in entries if e["desc"] == "TABLE"}
        assert tables == set(_TABLES)
        kinds = {e["desc"] for e in entries}
        assert "TABLE DATA" in kinds and "INDEX" in kinds

    def test_wide_dump_ok(self):
        arch.verify_dump_listing(
            arch.parse_pg_restore_list(_WIDE_LIST), expected_tables=_TABLES)

    def test_wide_dump_missing_table_rejected(self):
        text = _WIDE_LIST.replace(
            "216; 1259 16398 TABLE public us_balance_sheet postgres\n", "")
        with pytest.raises(arch.RestoreVerificationError, match="不符"):
            arch.verify_dump_listing(
                arch.parse_pg_restore_list(text), expected_tables=_TABLES)

    def test_wide_dump_extra_production_object_rejected(self):
        text = _WIDE_LIST + "900; 1259 99999 TABLE public daily_quote postgres\n"
        with pytest.raises(arch.RestoreVerificationError, match="不符"):
            arch.verify_dump_listing(
                arch.parse_pg_restore_list(text), expected_tables=_TABLES)

    def test_wide_dump_with_mv_rejected(self):
        text = _WIDE_LIST + _MV_LIST.splitlines(keepends=True)[4]
        with pytest.raises(arch.RestoreVerificationError, match="物化视图"):
            arch.verify_dump_listing(
                arch.parse_pg_restore_list(text), expected_tables=_TABLES)

    def test_mv_dump_ok_and_missing_mv_rejected(self):
        arch.verify_dump_listing(
            arch.parse_pg_restore_list(_MV_LIST), expected_mvs=_MVS)
        text = _MV_LIST.replace(
            "503; 1259 20020 MATERIALIZED VIEW public mv_us_fcf_yield postgres\n", "")
        with pytest.raises(arch.RestoreVerificationError, match="不符"):
            arch.verify_dump_listing(
                arch.parse_pg_restore_list(text), expected_mvs=_MVS)


class TestDependencySql:
    GOOD_SQL = """\
CREATE TABLE public.stock_info (stock_code text, market text);
CREATE TABLE public.daily_quote (stock_code text, trade_date date, market text);
ALTER TABLE ONLY public.stock_info ADD CONSTRAINT stock_info_pkey PRIMARY KEY (stock_code);
CREATE INDEX idx_daily_quote_market ON public.daily_quote (market);
COPY public.stock_info (stock_code, market) FROM stdin;
AAPL\tUS
\\.
COPY public.daily_quote (stock_code, trade_date, market) FROM stdin;
AAPL\t2026-08-12\tUS
\\.
"""

    def test_good_sql_passes(self):
        arch.verify_dependency_sql(self.GOOD_SQL)

    def test_drop_rejected(self):
        with pytest.raises(arch.RestoreVerificationError, match="DROP"):
            arch.verify_dependency_sql(self.GOOD_SQL + "\nDROP TABLE daily_quote;\n")

    def test_insert_delete_update_rejected(self):
        for stmt in ("INSERT INTO stock_info VALUES ('X');",
                     "DELETE FROM stock_info;",
                     "UPDATE stock_info SET market='CN_A';"):
            with pytest.raises(arch.RestoreVerificationError):
                arch.verify_dependency_sql(self.GOOD_SQL + "\n" + stmt + "\n")

    def test_non_whitelisted_object_rejected(self):
        bad = self.GOOD_SQL + "COPY public.us_income_statement (stock_code) FROM stdin;\n"
        with pytest.raises(arch.RestoreVerificationError, match="白名单"):
            arch.verify_dependency_sql(bad)

    def test_missing_table_rejected(self):
        only_one = "CREATE TABLE public.stock_info (a text);\n" \
                   "COPY public.stock_info (a) FROM stdin;\nx\n\\.\n"
        with pytest.raises(arch.RestoreVerificationError, match="缺少"):
            arch.verify_dependency_sql(only_one)


# ── §5.5 上传后下载副本 checksum 复核 ───────────────────────

class TestUploadDownloadVerification:
    def _store_and_files(self, tmp_path):
        store_root = tmp_path / "remote"
        store_root.mkdir()
        local = tmp_path / "archive_run1"
        local.mkdir()
        for name in arch.ARTIFACT_NAMES:
            (local / name).write_bytes(f"content-of-{name}".encode())
        return arch.FileArchiveStore(f"file://{store_root}"), local

    def test_roundtrip_passes(self, tmp_path):
        store, local = self._store_and_files(tmp_path)
        arch._upload_and_verify(store, local, "run1", arch.ARTIFACT_NAMES)

    def test_tampered_download_detected(self, tmp_path, monkeypatch):
        store, local = self._store_and_files(tmp_path)
        original_download = store.download

        def tampered(run_id, name, dest_dir):
            dest = original_download(run_id, name, dest_dir)
            if name == arch.WIDE_DUMP_NAME:
                dest.write_bytes(b"corrupted")
            return dest

        monkeypatch.setattr(store, "download", tampered)
        with pytest.raises(RuntimeError, match="checksum 不符"):
            arch._upload_and_verify(store, local, "run1", arch.ARTIFACT_NAMES)

    def test_download_verified_against_sha256sums(self, tmp_path):
        store, local = self._store_and_files(tmp_path)
        sums = arch._write_checksums(local)
        for name in arch.ARTIFACT_NAMES:
            store.upload(local / name, "run1", name)
        (local / arch.SHA256SUMS_NAME).write_text(
            "".join(f"{sums[n]}  {n}\n" for n in sorted(sums)))
        store.upload(local / arch.SHA256SUMS_NAME, "run1", arch.SHA256SUMS_NAME)
        (local / arch.MANIFEST_NAME).write_text("{}")
        store.upload(local / arch.MANIFEST_NAME, "run1", arch.MANIFEST_NAME)

        dest = tmp_path / "dl"
        dest.mkdir()
        paths = arch._download_verified(store, "run1", dest)
        assert set(paths) == set(arch.ARTIFACT_NAMES) | {arch.MANIFEST_NAME,
                                                         arch.SHA256SUMS_NAME}
        # 篡改远端后再下载必须失败
        (store.root / "run1" / arch.MV_DUMP_NAME).write_bytes(b"evil")
        with pytest.raises(arch.RestoreVerificationError, match="checksum 不符"):
            arch._download_verified(store, "run1", dest)


class TestCosfsMountGuard:
    def _cosfs_store(self, tmp_path, monkeypatch):
        root = tmp_path / "lhcos-data"
        root.mkdir()
        monkeypatch.setattr(arch, "COSFS_ARCHIVE_ROOT", root.resolve())
        return arch.FileArchiveStore(f"file://{root}")

    def test_cosfs_archive_requires_online_cosfs_mount(self, tmp_path, monkeypatch):
        store = self._cosfs_store(tmp_path, monkeypatch)
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            if argv[0] == "mountpoint":
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="fuse.cosfs\n", stderr="")

        monkeypatch.setattr(arch.shutil, "which", lambda _: "/usr/bin/tool")
        monkeypatch.setattr(arch.subprocess, "run", fake_run)
        store.probe("run1")
        assert calls == [
            ["mountpoint", "-q", str(store.root)],
            ["findmnt", "--noheadings", "--output", "FSTYPE", "--target", str(store.root)],
        ]

    def test_cosfs_archive_rejects_unmounted_local_directory(self, tmp_path, monkeypatch):
        store = self._cosfs_store(tmp_path, monkeypatch)
        monkeypatch.setattr(arch.shutil, "which", lambda _: "/usr/bin/tool")
        monkeypatch.setattr(
            arch.subprocess, "run",
            lambda *args, **kwargs: MagicMock(returncode=1, stdout="", stderr=""),
        )
        with pytest.raises(arch.PreflightError, match="未在线"):
            store.probe("run1")

    def test_cosfs_archive_rejects_wrong_filesystem_type(self, tmp_path, monkeypatch):
        store = self._cosfs_store(tmp_path, monkeypatch)

        def fake_run(argv, **kwargs):
            if argv[0] == "mountpoint":
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="ext4\n", stderr="")

        monkeypatch.setattr(arch.shutil, "which", lambda _: "/usr/bin/tool")
        monkeypatch.setattr(arch.subprocess, "run", fake_run)
        with pytest.raises(arch.PreflightError, match="类型错误"):
            store.probe("run1")


# ── 命名断言与静态禁扫（§5.6）───────────────────────────────

class TestNamingAndStaticScan:
    def test_createdb_dropdb_only_derived_name(self):
        good = "stock_data_legacy_restore_run1"
        assert arch._createdb_argv(good)[-1] == good
        assert arch._dropdb_argv(good)[-1] == good
        for bad in ("stock_data", "postgres", "template1", "x_stock_data_legacy_restore_"):
            with pytest.raises(AssertionError):
                arch._createdb_argv(bad)
            with pytest.raises(AssertionError):
                arch._dropdb_argv(bad)

    def test_no_forbidden_production_ddl_in_tool_source(self):
        """§5.6：工具不得含生产 DROP/TRUNCATE/VACUUM FULL/--clean。

        例外行以 scan-allow 标记（校验正则定义与错误消息文本）。
        """
        src = Path("scripts/archive_us_legacy_financials.py").read_text()
        forbidden = re.compile(
            r"DROP\s+TABLE|DROP\s+MATERIALIZED\s+VIEW|TRUNCATE|VACUUM\s+FULL|--clean",
            re.IGNORECASE,
        )
        offenders = [
            line for line in src.splitlines()
            if forbidden.search(line) and "scan-allow" not in line
        ]
        assert offenders == [], f"工具源码出现未豁免的禁止语句: {offenders}"

    def test_password_only_injected_via_env_or_connect(self):
        """凭证只能经 PG* 环境变量或 psycopg2 connect 注入，不得进入 argv/日志。"""
        src = Path("scripts/archive_us_legacy_financials.py").read_text()
        for i, line in enumerate(src.splitlines(), 1):
            if "config.db.password" in line:
                assert "env[" in line or "password=" in line, \
                    f"第 {i} 行密码用途异常: {line}"


# ── URI 脱敏 ───────────────────────────────────────────────

class TestUriSanitize:
    def test_strips_credentials_and_query(self):
        assert arch.sanitize_uri("s3://key:secret@bucket/path/p?token=abc") == \
            "s3://bucket/path/p"
        assert arch.sanitize_uri("file:///data/archive") == "file:///data/archive"

    def test_unsupported_scheme_rejected(self):
        with pytest.raises(ValueError, match="不支持"):
            arch.make_store("ftp://example.com/x")
