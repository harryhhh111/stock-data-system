from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import threading
import time
import traceback
import uuid

import psycopg2
from psycopg2 import sql

import config


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "build" / "us_history_index_tablespace"
EXPECTED_COUNT = 35
PILOT_INDEX = "idx_snapshot_batch"
SOURCE_TABLESPACE = "pg_default"
TARGET_TABLESPACE = "history_ts"
GIB = 1024**3
PARENT_TABLES = (
    "us_financial_fact_version",
    "us_financial_fact_source",
    "us_fact_version_relation",
    "us_fact_selection_audit",
    "us_financial_fact_staging",
    "raw_snapshot_version",
    "raw_snapshot",
)
IDENTITY_FIELDS = (
    "index_oid",
    "index_schema",
    "index_name",
    "parent_oid",
    "parent_schema",
    "parent_name",
    "owner",
    "index_def",
    "constraint_oids",
    "referencing_constraint_oids",
    "indisvalid",
    "indisready",
    "indislive",
    "indisunique",
    "indisprimary",
    "index_persistence",
    "parent_persistence",
    "parent_tablespace",
)

INDEX_SELECT = """
SELECT
    i.indexrelid AS index_oid,
    ni.nspname AS index_schema,
    ci.relname AS index_name,
    i.indrelid AS parent_oid,
    np.nspname AS parent_schema,
    cp.relname AS parent_name,
    pg_get_userbyid(ci.relowner) AS owner,
    COALESCE(tsi.spcname, 'pg_default') AS index_tablespace,
    COALESCE(tsp.spcname, 'pg_default') AS parent_tablespace,
    pg_relation_size(i.indexrelid) AS size_bytes,
    pg_get_indexdef(i.indexrelid) AS index_def,
    COALESCE((SELECT array_agg(con.oid ORDER BY con.oid)
              FROM pg_constraint con
              WHERE con.conindid=i.indexrelid AND con.conrelid=i.indrelid), '{}') AS constraint_oids,
    COALESCE((SELECT array_agg(con.oid ORDER BY con.oid)
              FROM pg_constraint con
              WHERE con.conindid=i.indexrelid AND con.conrelid<>i.indrelid), '{}') AS referencing_constraint_oids,
    i.indisvalid,
    i.indisready,
    i.indislive,
    i.indisunique,
    i.indisprimary,
    ci.relpersistence AS index_persistence,
    cp.relpersistence AS parent_persistence,
    ci.relkind AS index_kind
FROM pg_index i
JOIN pg_class ci ON ci.oid=i.indexrelid
JOIN pg_namespace ni ON ni.oid=ci.relnamespace
JOIN pg_class cp ON cp.oid=i.indrelid
JOIN pg_namespace np ON np.oid=cp.relnamespace
JOIN pg_database db ON db.datname=current_database()
LEFT JOIN pg_tablespace tsi ON tsi.oid=COALESCE(NULLIF(ci.reltablespace, 0), db.dattablespace)
LEFT JOIN pg_tablespace tsp ON tsp.oid=COALESCE(NULLIF(cp.reltablespace, 0), db.dattablespace)
"""


class MigrationError(RuntimeError):
    pass


def connect_production():
    conn = psycopg2.connect(
        host=config.db.host,
        port=config.db.port,
        dbname=config.db.dbname,
        user=config.db.user,
        password=config.db.password,
        application_name="us_history_index_migration",
        connect_timeout=5,
        options="-c client_encoding=utf8 -c lock_timeout=3000 -c statement_timeout=5000",
    )
    conn.autocommit = False
    return conn


def fetch_one(conn, where, params=()):
    query = INDEX_SELECT + where
    with conn.cursor() as cur:
        cur.execute(query, params)
        columns = [column.name for column in cur.description]
        row = cur.fetchone()
    return dict(zip(columns, row)) if row else None


def fetch_candidates(conn):
    with conn.cursor() as cur:
        cur.execute(
            INDEX_SELECT
            + "WHERE cp.relnamespace='public'::regnamespace "
            + "AND cp.relname = ANY(%s) AND tsi.spcname=%s AND ci.relkind='i' "
            + "ORDER BY pg_relation_size(i.indexrelid), i.indexrelid",
            (list(PARENT_TABLES), SOURCE_TABLESPACE),
        )
        columns = [column.name for column in cur.description]
        rows = [dict(zip(columns, row)) for row in cur.fetchall()]
    conn.rollback()
    return rows


def fetch_by_oid(conn, oid):
    return fetch_one(conn, "WHERE i.indexrelid=%s", (oid,))


def tablespace_location(conn, name):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_tablespace_location(oid) FROM pg_tablespace WHERE spcname=%s",
            (name,),
        )
        row = cur.fetchone()
    if not row or not row[0]:
        raise MigrationError(f"tablespace location unavailable: {name}")
    return row[0]


def validate_identity(frozen, current):
    if not current:
        raise MigrationError(f"index missing: oid={frozen['index_oid']}")

    def normalized(index, field):
        value = index[field]
        return list(value) if isinstance(value, (list, tuple)) else value

    differences = [
        field
        for field in IDENTITY_FIELDS
        if normalized(frozen, field) != normalized(current, field)
    ]
    if differences:
        raise MigrationError(
            f"identity changed for {frozen['index_name']}: {', '.join(differences)}"
        )


def assert_valid(index):
    if not all(index[field] for field in ("indisvalid", "indisready", "indislive")):
        raise MigrationError(f"index is not valid/ready/live: {index['index_name']}")
    if index["index_persistence"] != "p" or index["parent_persistence"] != "p":
        raise MigrationError(f"only permanent indexes are allowed: {index['index_name']}")
    if index.get("index_kind", "i") != "i":
        raise MigrationError(f"only ordinary indexes are allowed: {index['index_name']}")


def assert_scope(index):
    if (index["index_schema"] != "public"
            or index["parent_schema"] != "public"
            or index["parent_name"] not in PARENT_TABLES
            or index["parent_tablespace"] != TARGET_TABLESPACE):
        raise MigrationError(f"index outside approved history scope: {index['index_name']}")


def validate_context(conn, manifest, target_path, lock_timeout="3s"):
    with conn.cursor() as cur:
        cur.execute("SET LOCAL lock_timeout = %s", (lock_timeout,))
        cur.execute("SET LOCAL statement_timeout = '5s'")
        cur.execute("SELECT current_database(), session_user")
        database, user = cur.fetchone()
    if database != manifest["database"] or user != manifest["user"]:
        raise MigrationError("database/user does not match frozen manifest")
    actual = Path(tablespace_location(conn, TARGET_TABLESPACE)).resolve()
    if (actual != Path(manifest["target_location"]).resolve()
            or actual != Path(target_path).resolve()):
        raise MigrationError("actual target tablespace location does not match manifest/path")


def observe_tablespace(conn, index_oid):
    # Catalog-only lookup: pg_get_indexdef can itself wait for a parent lock.
    with conn.cursor() as cur:
        cur.execute("SET LOCAL lock_timeout = '3s'")
        cur.execute("SET LOCAL statement_timeout = '5s'")
        cur.execute(
            "SELECT ts.spcname FROM pg_class c "
            "JOIN pg_database db ON db.datname=current_database() "
            "JOIN pg_tablespace ts ON ts.oid=COALESCE(NULLIF(c.reltablespace,0),db.dattablespace) "
            "WHERE c.oid=%s", (index_oid,),
        )
        row = cur.fetchone()
    return row[0] if row else None


def disk_free(path):
    return shutil.disk_usage(path).free


def check_space(system_path, target_path, current_bytes, remaining_bytes, max_bytes):
    source_required = 6 * GIB + 2 * current_bytes
    target_required = remaining_bytes + max_bytes + 5 * GIB
    source_free = disk_free(system_path)
    target_free = disk_free(target_path)
    if source_free < source_required:
        raise MigrationError(
            f"insufficient source space: free={source_free} required={source_required}"
        )
    if target_free < target_required:
        raise MigrationError(
            f"insufficient target space: free={target_free} required={target_required}"
        )
    return {
        "source_free_bytes": source_free,
        "source_required_bytes": source_required,
        "target_free_bytes": target_free,
        "target_required_bytes": target_required,
    }


def index_sql(index, destination, conn):
    return sql.SQL("ALTER INDEX {}.{} SET TABLESPACE {}").format(
        sql.Identifier(index["index_schema"]),
        sql.Identifier(index["index_name"]),
        sql.Identifier(destination),
    )


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def append_record(path, value):
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def load_manifest(path):
    manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1:
        raise MigrationError("unsupported manifest schema")
    indexes = manifest.get("indexes", [])
    if len(indexes) != EXPECTED_COUNT:
        raise MigrationError(f"frozen manifest must contain {EXPECTED_COUNT} indexes")
    if len({index["index_oid"] for index in indexes}) != EXPECTED_COUNT:
        raise MigrationError("duplicate index OID in frozen manifest")
    if not any(index["index_name"] == PILOT_INDEX for index in indexes):
        raise MigrationError(f"pilot index missing: {PILOT_INDEX}")
    if any(index["source_tablespace"] != SOURCE_TABLESPACE for index in indexes):
        raise MigrationError("frozen source tablespace must be pg_default")
    if manifest.get("target_tablespace") != TARGET_TABLESPACE:
        raise MigrationError("frozen target tablespace must be history_ts")
    if (not manifest.get("database") or not manifest.get("user")
            or not Path(manifest.get("target_location", "")).is_absolute()):
        raise MigrationError("manifest requires database, user and absolute target location")
    for index in indexes:
        assert_scope(index)
        assert_valid(index)
    return manifest


class OperationMonitor:
    def __init__(
        self,
        connection_factory,
        system_path,
        target_path,
        interval=1.0,
        blocked_grace_seconds=5.0,
        cancel=None,
    ):
        self.connection_factory = connection_factory
        self.system_path = system_path
        self.target_path = target_path
        self.interval = interval
        self.blocked_grace_seconds = blocked_grace_seconds
        self.reason = None
        self.min_source_free = None
        self.min_target_free = None
        self.max_blocked = 0
        self._stop = threading.Event()
        self._thread = None
        self._ready = threading.Event()
        self._cancel = cancel

    def start(self, migration_pid):
        self._thread = threading.Thread(
            target=self._run,
            args=(migration_pid,),
            name="index-migration-monitor",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=5.0):
            self._fail("monitor startup timed out")
        self.assert_healthy()

    def assert_healthy(self):
        if self.reason:
            raise MigrationError(self.reason)
        if self._thread is None or (not self._thread.is_alive() and not self._stop.is_set()):
            raise MigrationError("monitor is not running")

    def _fail(self, reason):
        self.reason = self.reason or reason
        # Cancel only the owned connection, including when the monitor cannot
        # connect to PostgreSQL. Never cancel a backend discovered by PID lookup.
        if self._cancel is not None and not self._stop.is_set():
            try:
                self._cancel()
            except Exception:
                pass  # reason remains sticky; caller must stop this batch.

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=max(5.0, self.interval * 3))
            if self._thread.is_alive():
                self.reason = self.reason or "monitor did not stop within timeout"

    def _run(self, migration_pid):
        monitor_conn = None
        blocked_since = None
        try:
            monitor_conn = self.connection_factory()
            while not self._stop.is_set():
                source_free = disk_free(self.system_path)
                target_free = disk_free(self.target_path)
                self.min_source_free = (
                    source_free
                    if self.min_source_free is None
                    else min(self.min_source_free, source_free)
                )
                self.min_target_free = (
                    target_free
                    if self.min_target_free is None
                    else min(self.min_target_free, target_free)
                )
                if source_free < 6 * GIB:
                    self.reason = f"source free space below 6 GiB: {source_free}"
                elif target_free < 5 * GIB:
                    self.reason = f"target free space below 5 GiB: {target_free}"
                with monitor_conn.cursor() as cur:
                    cur.execute("SET LOCAL statement_timeout = '2s'")
                    cur.execute(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE pid<>%s AND %s=ANY(pg_blocking_pids(pid))",
                        (migration_pid, migration_pid),
                    )
                    blocked = cur.fetchone()[0]
                monitor_conn.rollback()
                self.max_blocked = max(self.max_blocked, blocked)
                if blocked:
                    blocked_since = blocked_since or time.monotonic()
                    if time.monotonic() - blocked_since >= self.blocked_grace_seconds:
                        self.reason = f"business sessions blocked by migration: {blocked}"
                else:
                    blocked_since = None
                if self.reason:
                    self._fail(self.reason)
                    return
                self._ready.set()
                if self._stop.wait(self.interval):
                    return
        except Exception as exc:
            self._fail(f"monitor failed: {type(exc).__name__}: {exc}")
        finally:
            self._ready.set()
            if monitor_conn:
                monitor_conn.close()


class Migration:
    def __init__(
        self,
        manifest_path,
        connection_factory=connect_production,
        system_path="/",
        target_path=None,
        lock_timeout="3s",
        statement_timeout="15min",
        monitor_interval=1.0,
    ):
        self.manifest_path = Path(manifest_path).resolve()
        self.manifest = load_manifest(self.manifest_path)
        self.connection_factory = connection_factory
        self.system_path = str(system_path)
        self.target_path = str(target_path or self.manifest["target_location"])
        self.lock_timeout = lock_timeout
        self.statement_timeout = statement_timeout
        self.monitor_interval = monitor_interval
        self.record_path = self.manifest_path.parent / "execution.jsonl"

    def _record(self, index, status, started, **extra):
        record = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "index_oid": index["index_oid"],
            "index_schema": index["index_schema"],
            "index_name": index["index_name"],
            "status": status,
            "started_at": started,
            "duration_seconds": extra.pop("duration_seconds", None),
            **extra,
        }
        append_record(self.record_path, record)
        return record

    def _reconcile_connection_failure(self, index, destination, started, exc, monitor_reason=None):
        try:
            conn = self.connection_factory()
            try:
                validate_context(conn, self.manifest, self.target_path, self.lock_timeout)
                current = fetch_by_oid(conn, index["index_oid"])
                validate_identity(index, current)
                assert_scope(current)
                assert_valid(current)
                if current["index_tablespace"] == destination:
                    return self._record(
                        index,
                        "committed_monitor_failed" if monitor_reason else "succeeded_after_reconnect",
                        started,
                        destination=destination,
                        monitor_reason=monitor_reason,
                        original_error=f"{type(exc).__name__}: {exc}",
                    )
                return self._record(
                    index,
                    "failed",
                    started,
                    observed_tablespace=current["index_tablespace"],
                    error=f"{type(exc).__name__}: {exc}",
                    monitor_reason=monitor_reason,
                )
            finally:
                conn.close()
        except Exception as reconcile_exc:
            return self._record(
                index,
                "outcome_unknown",
                started,
                error=f"{type(exc).__name__}: {exc}",
                reconcile_error=f"{type(reconcile_exc).__name__}: {reconcile_exc}",
                monitor_reason=monitor_reason,
            )

    def migrate_one(self, index, destination=TARGET_TABLESPACE):
        started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        started_monotonic = time.monotonic()
        conn = None
        monitor = None
        monitor_pid = None
        try:
            conn = self.connection_factory()
            validate_context(conn, self.manifest, self.target_path, self.lock_timeout)
            current = fetch_by_oid(conn, index["index_oid"])
            validate_identity(index, current)
            assert_scope(current)
            assert_valid(current)
            if current["index_tablespace"] == destination:
                conn.rollback()
                return self._record(index, "skipped_already_in_place", started)
            conn.rollback()
            monitor_pid = conn.get_backend_pid()
            monitor = OperationMonitor(
                self.connection_factory,
                self.system_path if destination == TARGET_TABLESPACE else self.target_path,
                self.target_path if destination == TARGET_TABLESPACE else self.system_path,
                interval=self.monitor_interval,
                cancel=conn.cancel,
            )
            monitor.start(monitor_pid)
            with conn.cursor() as cur:
                cur.execute("BEGIN")
                cur.execute("SET LOCAL lock_timeout = %s", (self.lock_timeout,))
                cur.execute("SET LOCAL statement_timeout = %s", (self.statement_timeout,))
                cur.execute(
                    sql.SQL("LOCK TABLE {}.{} IN ACCESS EXCLUSIVE MODE").format(
                        sql.Identifier(index["parent_schema"]),
                        sql.Identifier(index["parent_name"]),
                    )
                )
            current = fetch_by_oid(conn, index["index_oid"])
            validate_identity(index, current)
            assert_scope(current)
            assert_valid(current)
            monitor.assert_healthy()
            if current["index_tablespace"] == destination:
                conn.rollback()
                monitor.stop()
                monitor.assert_healthy()
                return self._record(index, "skipped_already_in_place", started)
            expected_source = (
                index["source_tablespace"]
                if destination == TARGET_TABLESPACE
                else TARGET_TABLESPACE
            )
            if current["index_tablespace"] != expected_source:
                raise MigrationError(
                    f"unexpected source tablespace for {index['index_name']}: "
                    f"{current['index_tablespace']}"
                )
            with conn.cursor() as cur:
                cur.execute(index_sql(index, destination, conn))
            monitor.assert_healthy()
            conn.commit()
            monitor.stop()
            current = fetch_by_oid(conn, index["index_oid"])
            validate_identity(index, current)
            assert_valid(current)
            if current["index_tablespace"] != destination:
                raise MigrationError(f"post-commit tablespace mismatch: {index['index_name']}")
            with conn.cursor() as cur:
                cur.execute("SELECT pg_relation_filepath(%s)", (index["index_oid"],))
                filepath = cur.fetchone()[0]
            if destination == TARGET_TABLESPACE and not filepath.startswith("pg_tblspc/"):
                raise MigrationError(f"index filepath is not in target tablespace: {filepath}")
            conn.rollback()
            return self._record(
                index,
                "committed_monitor_failed" if monitor.reason else "succeeded",
                started,
                duration_seconds=round(time.monotonic() - started_monotonic, 3),
                destination=destination,
                relation_filepath=filepath,
                monitor_reason=monitor.reason,
                monitor={
                    "min_source_free_bytes": monitor.min_source_free if monitor else None,
                    "min_target_free_bytes": monitor.min_target_free if monitor else None,
                    "max_blocked": monitor.max_blocked if monitor else None,
                },
            )
        except (psycopg2.InterfaceError, psycopg2.OperationalError) as exc:
            if monitor:
                monitor.stop()
            if conn is not None and not conn.closed and exc.pgcode and not exc.pgcode.startswith("08"):
                try:
                    conn.rollback()
                    observed = observe_tablespace(conn, index["index_oid"])
                    conn.rollback()
                except Exception:
                    observed = None
                return self._record(
                    index,
                    "failed",
                    started,
                    duration_seconds=round(time.monotonic() - started_monotonic, 3),
                    observed_tablespace=observed,
                    error=f"{type(exc).__name__}: {exc}",
                    monitor_reason=monitor.reason if monitor else None,
                )
            return self._reconcile_connection_failure(
                index, destination, started, exc, monitor.reason if monitor else None
            )
        except Exception as exc:
            if conn is not None and not conn.closed:
                try:
                    conn.rollback()
                except Exception:
                    pass
            return self._record(
                index,
                "failed",
                started,
                duration_seconds=round(time.monotonic() - started_monotonic, 3),
                error=f"{type(exc).__name__}: {exc}",
                monitor_reason=monitor.reason if monitor else None,
            )
        finally:
            if monitor:
                monitor.stop()
            if conn:
                conn.close()

    def apply(self, only=None, rollback=None):
        if only and rollback:
            raise MigrationError("--only and --rollback are mutually exclusive")
        indexes = sorted(
            self.manifest["indexes"],
            key=lambda index: (index["index_name"] != PILOT_INDEX, index["size_bytes"], index["index_oid"]),
        )
        if rollback:
            indexes = [index for index in indexes if index["index_name"] == rollback]
            if len(indexes) != 1:
                raise MigrationError(f"rollback requires one frozen index: {rollback}")
            destination = SOURCE_TABLESPACE
        else:
            destination = TARGET_TABLESPACE
        if only:
            indexes = [index for index in indexes if index["index_name"] == only]
            if len(indexes) != 1:
                raise MigrationError(f"index not in frozen manifest: {only}")

        # Validate the live connection and full frozen scope before any DDL or
        # allocation checks. Completed items need neither write locks nor space.
        conn = self.connection_factory()
        try:
            validate_context(conn, self.manifest, self.target_path, self.lock_timeout)
            current_indexes = {}
            for index in self.manifest["indexes"]:
                try:
                    current = fetch_by_oid(conn, index["index_oid"])
                except psycopg2.Error as exc:
                    conn.rollback()
                    try:
                        observed = observe_tablespace(conn, index["index_oid"])
                    except psycopg2.Error:
                        observed = None
                    return [self._record(
                        index, "failed", time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                        phase="read_only_preflight", observed_tablespace=observed,
                        error=f"{type(exc).__name__}: {exc}",
                    )]
                validate_identity(index, current)
                assert_scope(current)
                assert_valid(current)
                if current["index_tablespace"] not in (SOURCE_TABLESPACE, TARGET_TABLESPACE):
                    raise MigrationError(f"unexpected tablespace: {index['index_name']}")
                current_indexes[index["index_oid"]] = current
        finally:
            conn.close()
        remaining_indexes = [
            current_indexes[index["index_oid"]] for index in indexes
            if current_indexes[index["index_oid"]]["index_tablespace"] != destination
        ]
        remaining_bytes = sum(index["size_bytes"] for index in remaining_indexes)
        max_bytes = max((index["size_bytes"] for index in remaining_indexes), default=0)
        results = []
        for position, index in enumerate(indexes):
            current = current_indexes[index["index_oid"]]
            if current["index_tablespace"] == destination:
                results.append(self._record(
                    index, "skipped_already_in_place", time.strftime("%Y-%m-%dT%H:%M:%S%z")
                ))
                continue
            current_bytes = current["size_bytes"]
            if destination == TARGET_TABLESPACE:
                space = check_space(
                    self.system_path,
                    self.target_path,
                    current_bytes,
                    remaining_bytes,
                    max_bytes,
                )
            else:
                space = check_space(
                    self.target_path,
                    self.system_path,
                    current_bytes,
                    remaining_bytes,
                    max_bytes,
                )
            append_record(
                self.record_path,
                {
                    "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "index_oid": index["index_oid"],
                    "index_name": index["index_name"],
                    "status": "started",
                    "destination": destination,
                    "space": space,
                },
            )
            result = self.migrate_one(index, destination)
            results.append(result)
            if not result["status"].startswith("succeeded") and result["status"] != "skipped_already_in_place":
                break
            remaining_bytes -= current_bytes
            if max_bytes == current_bytes:
                max_bytes = max(
                    (current_indexes[item["index_oid"]]["size_bytes"]
                     for item in indexes[position + 1 :]
                     if current_indexes[item["index_oid"]]["index_tablespace"] != destination),
                    default=0,
                )
        return results


def freeze(output_root=DEFAULT_OUTPUT, connection_factory=connect_production):
    conn = connection_factory()
    try:
        indexes = fetch_candidates(conn)
        if len(indexes) != EXPECTED_COUNT:
            raise MigrationError(
                f"expected {EXPECTED_COUNT} pg_default indexes, found {len(indexes)}"
            )
        for index in indexes:
            assert_scope(index)
            assert_valid(index)
            if index["parent_tablespace"] != TARGET_TABLESPACE:
                raise MigrationError(f"parent not in history_ts: {index['parent_name']}")
            index["source_tablespace"] = index["index_tablespace"]
        target_location = tablespace_location(conn, TARGET_TABLESPACE)
        with conn.cursor() as cur:
            cur.execute("SELECT current_database(), session_user")
            database, user = cur.fetchone()
        conn.rollback()
        indexes.sort(key=lambda index: (index["index_name"] != PILOT_INDEX, index["size_bytes"], index["index_oid"]))
        manifest = {
            "schema_version": 1,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "database": database,
            "user": user,
            "source_tablespace": SOURCE_TABLESPACE,
            "target_tablespace": TARGET_TABLESPACE,
            "target_location": target_location,
            "candidate_count": len(indexes),
            "total_bytes": sum(index["size_bytes"] for index in indexes),
            "max_bytes": max(index["size_bytes"] for index in indexes),
            "indexes": indexes,
        }
        check_space("/", target_location, manifest["max_bytes"], manifest["total_bytes"], manifest["max_bytes"])
        run_dir = Path(output_root) / (
            time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
        )
        run_dir.mkdir(parents=True, exist_ok=False)
        manifest_path = run_dir / "manifest.json"
        write_json(manifest_path, manifest)
        (run_dir / "plan.sql").write_text(
            "\n\n".join(
                index_sql(index, TARGET_TABLESPACE, conn).as_string(conn) + ";"
                for index in indexes
            )
            + "\n",
            encoding="utf-8",
        )
        return manifest_path, manifest
    finally:
        conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Move frozen US history indexes between tablespaces")
    parser.add_argument("--apply", action="store_true", help="execute DDL; default is dry-run")
    parser.add_argument("--manifest", type=Path, help="frozen manifest from the first dry-run")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--only", help="apply only one named frozen index")
    parser.add_argument("--rollback", help="move one named frozen index back to pg_default")
    args = parser.parse_args(argv)
    lock_root = args.output_root
    lock_root.mkdir(parents=True, exist_ok=True)
    with (lock_root / ".migration.lock").open("w") as lock_handle:
        try:
            fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MigrationError("another index migration process holds the local lock") from exc
        if not args.apply:
            if args.only or args.rollback:
                parser.error("--only/--rollback require --apply")
            if args.manifest:
                manifest = load_manifest(args.manifest)
                print(json.dumps({"dry_run": True, "manifest": str(args.manifest), "candidate_count": len(manifest["indexes"])}, ensure_ascii=False))
                return 0
            manifest_path, manifest = freeze(args.output_root)
            print(json.dumps({
                "dry_run": True,
                "manifest": str(manifest_path),
                "candidate_count": manifest["candidate_count"],
                "total_bytes": manifest["total_bytes"],
                "max_bytes": manifest["max_bytes"],
            }, ensure_ascii=False))
            return 0
        if not args.manifest:
            parser.error("--apply requires --manifest from the first dry-run")
        migration = Migration(args.manifest)
        results = migration.apply(only=args.only, rollback=args.rollback)
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0 if all(
            result["status"].startswith("succeeded") or result["status"] == "skipped_already_in_place"
            for result in results
        ) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except MigrationError as exc:
        raise SystemExit(f"migration stopped: {exc}")
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
