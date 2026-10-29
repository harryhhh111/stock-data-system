import getpass
import json
import socket
import subprocess
import tempfile
import threading
from pathlib import Path

import psycopg2
import pytest

from scripts import migrate_us_history_indexes as migration


@pytest.fixture
def postgres_cluster():
    with tempfile.TemporaryDirectory(prefix="stock-index-") as temporary:
        root = Path(temporary)
        data = root / "data"
        socket_dir = root / "socket"
        target = root / "history"
        socket_dir.mkdir()
        target.mkdir()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        subprocess.run(
            ["initdb", "-D", str(data), "-A", "trust", "--no-locale", "-E", "UTF8"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                "pg_ctl",
                "-D",
                str(data),
                "-l",
                str(root / "postgres.log"),
                "-o",
                f"-k {socket_dir} -p {port} -c listen_addresses= -c fsync=off -c full_page_writes=off",
                "-w",
                "start",
            ],
            check=True,
            capture_output=True,
            text=True,
        )

        def connect():
            conn = psycopg2.connect(
                host=str(socket_dir),
                port=port,
                dbname="postgres",
                user=getpass.getuser(),
            )
            conn.autocommit = False
            return conn

        conn = connect()
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("CREATE TABLESPACE history_ts LOCATION %s", (str(target),))
            conn.autocommit = False
            with conn.cursor() as cur:
                for table, index_count in zip(migration.PARENT_TABLES, (6, 5, 6, 5, 5, 3, 5)):
                    cur.execute(
                        f'CREATE TABLE public."{table}" '
                        "(id bigint PRIMARY KEY, sync_batch bigint) TABLESPACE history_ts"
                    )
                    for number in range(index_count - 1):
                        name = (
                            "idx_snapshot_batch"
                            if table == "raw_snapshot" and number == 0
                            else f"ix_{table}_{number}"
                        )
                        cur.execute(
                            f'CREATE INDEX "{name}" ON public."{table}" (sync_batch)'
                        )
                    cur.execute(
                        f'INSERT INTO public."{table}" VALUES (1, 10), (2, 20)'
                    )
                cur.execute("CREATE TABLE unrelated (id bigint)")
                cur.execute("CREATE INDEX unrelated_idx ON unrelated (id)")
            conn.commit()
        finally:
            conn.close()
        try:
            yield {
                "connect": connect,
                "target": target,
                "root": root,
            }
        finally:
            subprocess.run(
                ["pg_ctl", "-D", str(data), "-m", "immediate", "-w", "stop"],
                check=True,
                capture_output=True,
                text=True,
            )


def tablespace(conn, name):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(ts.spcname, 'pg_default') "
            "FROM pg_class c LEFT JOIN pg_tablespace ts ON ts.oid=c.reltablespace "
            "WHERE c.oid=%s::regclass",
            (f"public.{name}",),
        )
        result = cur.fetchone()[0]
    conn.rollback()
    return result


def make_migration(cluster, manifest_path, **kwargs):
    connection_factory = kwargs.pop("connection_factory", cluster["connect"])
    return migration.Migration(
        manifest_path,
        connection_factory=connection_factory,
        system_path=cluster["root"],
        target_path=cluster["target"],
        monitor_interval=0.02,
        **kwargs,
    )


def test_real_migration_rerun_scope_and_single_rollback(postgres_cluster):
    cluster = postgres_cluster
    conn = cluster["connect"]()
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE INDEX later_index ON raw_snapshot (id, sync_batch)")
        conn.commit()
        with pytest.raises(migration.MigrationError, match="found 36"):
            migration.freeze(cluster["root"] / "rejected", cluster["connect"])
        with conn.cursor() as cur:
            cur.execute("DROP INDEX later_index")
        conn.commit()

        manifest_path, manifest = migration.freeze(cluster["root"] / "run", cluster["connect"])
        assert manifest["candidate_count"] == 35
        assert manifest["indexes"][0]["index_name"] == "idx_snapshot_batch"
        assert (manifest_path.parent / "plan.sql").is_file()

        pilot = make_migration(cluster, manifest_path).apply(only="idx_snapshot_batch")
        assert pilot[0]["status"] == "succeeded"
        assert tablespace(conn, "idx_snapshot_batch") == "history_ts"
        assert tablespace(conn, "raw_snapshot") == "history_ts"

        with conn.cursor() as cur:
            cur.execute("CREATE INDEX later_index ON raw_snapshot (id, sync_batch)")
        conn.commit()
        results = make_migration(cluster, manifest_path).apply()
        assert len(results) == 35
        assert results[0]["status"] == "skipped_already_in_place"
        assert all(
            result["status"] in ("succeeded", "skipped_already_in_place")
            for result in results
        )
        assert tablespace(conn, "later_index") == "pg_default"
        assert tablespace(conn, "unrelated_idx") == "pg_default"

        rerun = make_migration(cluster, manifest_path).apply()
        assert all(result["status"] == "skipped_already_in_place" for result in rerun)
        rollback = make_migration(cluster, manifest_path).apply(rollback="idx_snapshot_batch")
        assert rollback[0]["status"] == "succeeded"
        assert tablespace(conn, "idx_snapshot_batch") == "pg_default"
        assert tablespace(conn, "raw_snapshot_pkey") == "history_ts"
    finally:
        conn.close()


def test_lock_timeout_stops_remaining_indexes(postgres_cluster):
    cluster = postgres_cluster
    manifest_path, _ = migration.freeze(cluster["root"] / "run", cluster["connect"])
    blocker = cluster["connect"]()
    try:
        with blocker.cursor() as cur:
            cur.execute("LOCK TABLE raw_snapshot IN ACCESS EXCLUSIVE MODE")
        results = make_migration(
            cluster,
            manifest_path,
            lock_timeout="100ms",
        ).apply()
        assert len(results) == 1
        assert results[0]["status"] == "failed"
        assert results[0]["observed_tablespace"] == "pg_default"
        assert "LockNotAvailable" in results[0]["error"]
        assert tablespace(blocker, "raw_snapshot_pkey") == "pg_default"
    finally:
        blocker.rollback()
        blocker.close()


def test_commit_result_loss_is_reconciled(postgres_cluster):
    cluster = postgres_cluster
    manifest_path, _ = migration.freeze(cluster["root"] / "run", cluster["connect"])
    calls = 0

    class CommitThenReportDisconnect:
        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def commit(self):
            nonlocal calls
            self._conn.commit()
            calls += 1
            if calls == 1:
                raise psycopg2.OperationalError("simulated lost commit result")

    def connection_factory():
        return CommitThenReportDisconnect(cluster["connect"]())

    results = make_migration(
        cluster,
        manifest_path,
        connection_factory=connection_factory,
    ).apply(only="idx_snapshot_batch")
    assert results[0]["status"] == "succeeded_after_reconnect"
    conn = cluster["connect"]()
    try:
        assert tablespace(conn, "idx_snapshot_batch") == "history_ts"
    finally:
        conn.close()


def test_space_and_identity_guards(tmp_path, monkeypatch):
    monkeypatch.setattr(migration, "disk_free", lambda path: 100)
    with pytest.raises(migration.MigrationError, match="insufficient source space"):
        migration.check_space(tmp_path, tmp_path, 10, 10, 10)
    source = tmp_path / "source"
    target = tmp_path / "target"
    monkeypatch.setattr(
        migration,
        "disk_free",
        lambda path: 6 * migration.GIB if path == source else 100,
    )
    with pytest.raises(migration.MigrationError, match="insufficient target space"):
        migration.check_space(source, target, 0, 6 * migration.GIB, 0)

    frozen = {field: 1 for field in migration.IDENTITY_FIELDS}
    frozen["constraint_oids"] = [2, 3]
    current = dict(frozen)
    current["constraint_oids"] = (2, 3)
    migration.validate_identity(frozen, current)
    current["index_def"] = "changed"
    with pytest.raises(migration.MigrationError, match="index_def"):
        migration.validate_identity(frozen, current)


def test_execution_records_are_valid_json_lines(postgres_cluster):
    cluster = postgres_cluster
    manifest_path, _ = migration.freeze(cluster["root"] / "run", cluster["connect"])
    make_migration(cluster, manifest_path).apply(only="idx_snapshot_batch")
    records = [
        json.loads(line)
        for line in (manifest_path.parent / "execution.jsonl").read_text().splitlines()
    ]
    assert [record["status"] for record in records] == ["started", "succeeded"]
    assert records[0]["space"]["source_required_bytes"] >= 6 * migration.GIB


def test_monitor_startup_failure_stops_batch_without_moving(postgres_cluster):
    cluster = postgres_cluster
    manifest_path, manifest = migration.freeze(cluster["root"] / "run", cluster["connect"])

    def factory():
        if threading.current_thread().name == "index-migration-monitor":
            raise psycopg2.OperationalError("monitor connection unavailable")
        return cluster["connect"]()

    results = make_migration(cluster, manifest_path, connection_factory=factory).apply()
    assert len(results) == 1
    assert results[0]["status"] == "failed"
    assert "monitor failed" in results[0]["monitor_reason"]
    conn = cluster["connect"]()
    try:
        assert all(tablespace(conn, row["index_name"]) == "pg_default" for row in manifest["indexes"])
    finally:
        conn.close()


def test_monitor_query_failure_cancels_owned_operation(postgres_cluster):
    cluster = postgres_cluster
    manifest_path, _ = migration.freeze(cluster["root"] / "run", cluster["connect"])
    counts = {"polls": 0, "cancels": 0}

    class Cursor:
        def __init__(self, cursor, monitor):
            self.cursor, self.monitor = cursor, monitor

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.cursor.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.cursor, name)

        def execute(self, query, params=None):
            text = query.as_string(self.cursor.connection) if hasattr(query, "as_string") else query
            if self.monitor and "FROM pg_stat_activity" in text:
                counts["polls"] += 1
                if counts["polls"] > 1:
                    raise psycopg2.OperationalError("monitor query unavailable")
            if not self.monitor and text.startswith("ALTER INDEX"):
                # A real server statement is in flight when the monitor fails.
                self.cursor.execute("SELECT pg_sleep(10)")
            return self.cursor.execute(query, params)

    class Connection:
        def __init__(self, conn):
            self.conn = conn
            self.monitor = threading.current_thread().name == "index-migration-monitor"

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def cursor(self):
            return Cursor(self.conn.cursor(), self.monitor)

        def cancel(self):
            counts["cancels"] += 1
            self.conn.cancel()

    result = make_migration(cluster, manifest_path,
                            connection_factory=lambda: Connection(cluster["connect"]())).apply()
    assert len(result) == 1 and result[0]["status"] == "failed"
    assert "monitor query unavailable" in result[0]["monitor_reason"]
    assert counts["cancels"] == 1
    conn = cluster["connect"]()
    try:
        assert tablespace(conn, "idx_snapshot_batch") == "pg_default"
    finally:
        conn.close()


@pytest.mark.parametrize("lost_commit_result", [False, True])
def test_late_monitor_failure_records_commit_but_stops_batch(postgres_cluster, monkeypatch, lost_commit_result):
    cluster = postgres_cluster
    manifest_path, _ = migration.freeze(cluster["root"] / "run", cluster["connect"])
    original_stop = migration.OperationMonitor.stop

    def stop_with_late_failure(self):
        original_stop(self)
        self.reason = "monitor failed at commit boundary"

    monkeypatch.setattr(migration.OperationMonitor, "stop", stop_with_late_failure)

    class Connection:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def commit(self):
            self.conn.commit()
            if lost_commit_result:
                raise psycopg2.OperationalError("lost commit reply")

    results = make_migration(cluster, manifest_path,
                             connection_factory=lambda: Connection(cluster["connect"]())).apply()
    assert len(results) == 1
    assert results[0]["status"] == "committed_monitor_failed"
    assert results[0]["monitor_reason"] == "monitor failed at commit boundary"
    conn = cluster["connect"]()
    try:
        assert tablespace(conn, "idx_snapshot_batch") == "history_ts"
        assert tablespace(conn, "raw_snapshot_pkey") == "pg_default"
    finally:
        conn.close()


def test_out_of_scope_manifest_rejected_before_connecting(postgres_cluster):
    cluster = postgres_cluster
    manifest_path, manifest = migration.freeze(cluster["root"] / "run", cluster["connect"])
    conn = cluster["connect"]()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 'unrelated_idx'::regclass::oid")
            oid = cur.fetchone()[0]
        outsider = migration.fetch_by_oid(conn, oid)
    finally:
        conn.close()
    outsider["source_tablespace"] = "pg_default"
    position = next(i for i, row in enumerate(manifest["indexes"])
                    if row["index_name"] != migration.PILOT_INDEX)
    manifest["indexes"][position] = outsider
    migration.write_json(manifest_path, manifest)
    with pytest.raises(migration.MigrationError, match="outside approved history scope"):
        make_migration(cluster, manifest_path, connection_factory=lambda: pytest.fail("must not connect"))


@pytest.mark.parametrize("mismatch", ["database", "user", "location", "override"])
def test_runtime_context_mismatch_stops_before_ddl(postgres_cluster, mismatch):
    cluster = postgres_cluster
    manifest_path, manifest = migration.freeze(cluster["root"] / "run", cluster["connect"])
    kwargs = {}
    if mismatch in ("database", "user"):
        manifest[mismatch] = "other_" + manifest[mismatch]
    elif mismatch == "location":
        manifest["target_location"] = str(cluster["root"] / "wrong")
    else:
        kwargs["target_path"] = str(cluster["root"] / "wrong")
    migration.write_json(manifest_path, manifest)
    # Use constructor directly to exercise its path override as well.
    runner = migration.Migration(manifest_path, connection_factory=cluster["connect"],
                                 system_path=cluster["root"], **kwargs)
    with pytest.raises(migration.MigrationError, match="does not match"):
        runner.apply()
    conn = cluster["connect"]()
    try:
        assert tablespace(conn, "idx_snapshot_batch") == "pg_default"
    finally:
        conn.close()


def test_completed_retry_is_read_only_even_with_reader_and_no_free_space(postgres_cluster, monkeypatch):
    cluster = postgres_cluster
    manifest_path, manifest = migration.freeze(cluster["root"] / "run", cluster["connect"])
    make_migration(cluster, manifest_path).apply()
    reader = cluster["connect"]()
    try:
        with reader.cursor() as cur:
            cur.execute("SELECT id FROM raw_snapshot LIMIT 1")
        monkeypatch.setattr(migration, "disk_free", lambda _: 0)
        results = make_migration(cluster, manifest_path, lock_timeout="100ms").apply()
        assert len(results) == 35
        assert all(row["status"] == "skipped_already_in_place" for row in results)
        single = make_migration(cluster, manifest_path, lock_timeout="100ms").migrate_one(manifest["indexes"][0])
        assert single["status"] == "skipped_already_in_place"
    finally:
        reader.rollback()
        reader.close()
