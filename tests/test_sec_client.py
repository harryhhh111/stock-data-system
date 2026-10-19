"""SEC gate tests use a disposable PostgreSQL cluster and localhost HTTP only.

No production DSN, tables, or SEC network endpoint is used by these tests.
"""
from contextlib import closing
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import multiprocessing as mp
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time

import psycopg2
import pytest

from core import sec_client as sec


def sql(dsn, query, args=()):
    with closing(psycopg2.connect(dsn)) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(query, args)
            return cur.fetchall() if cur.description else None


@pytest.fixture(scope="module")
def cluster():
    if not shutil.which("initdb") or not shutil.which("pg_ctl"):
        pytest.skip("isolated SEC integration tests require initdb/pg_ctl")
    root = Path(tempfile.mkdtemp(prefix="secpg-"))
    data = root / "data"
    started = False
    try:
        subprocess.run(["initdb", "-D", str(data), "-A", "trust", "--no-locale",
                        "-E", "UTF8"], check=True, capture_output=True)
        result = subprocess.run(
            ["pg_ctl", "-D", str(data), "-l", str(root / "server.log"),
             "-o", f"-k {root} -p 56432 -h '' -F", "-w", "start"], capture_output=True)
        if result.returncode:
            pytest.fail("Temporary PostgreSQL startup failed: " +
                        (root / "server.log").read_text())
        started = True
        dsn = f"host={root} port=56432 dbname=postgres connect_timeout=2"
        sql(dsn, Path("scripts/sec_request_gate.sql").read_text())
        yield dsn
    finally:
        if started:
            subprocess.run(["pg_ctl", "-D", str(data), "-m", "immediate", "-w", "stop"],
                           check=True, capture_output=True)
        shutil.rmtree(root)


@pytest.fixture
def dsn(cluster, monkeypatch):
    sql(cluster, "UPDATE sec_request_gate SET next_allowed_at=clock_timestamp(), "
                 "blocked_until=clock_timestamp()")
    monkeypatch.setattr(sec, "_connect", lambda: psycopg2.connect(cluster))
    monkeypatch.setattr(sec.config.sec, "rate_limit", 2)
    monkeypatch.setattr(sec.config.sec, "user_agent", "SEC-test contact@example.org")
    return cluster


@pytest.fixture
def http():
    events = []
    mutex = threading.Lock()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            event = {"start": time.monotonic(), "path": self.path,
                     "ua": self.headers.get("User-Agent"), "end": None}
            with mutex:
                events.append(event)
            try:
                if self.path == "/slow":
                    self.send_response(200)
                    self.end_headers()
                    for _ in range(200):
                        self.wfile.write(b"x")
                        self.wfile.flush()
                        time.sleep(0.05)
                    return
                status = {"/429": 429, "/403": 403, "/redirect": 302}.get(self.path, 200)
                self.send_response(status)
                if status == 429:
                    self.send_header("Retry-After", "2")
                if status == 302:
                    self.send_header("Location", "/ok")
                body = b'{"sicDescription":"Software","cik":1,"facts":{}}'
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                event["end"] = time.monotonic()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", events
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def allow_test_server(monkeypatch, base):
    real_validate = sec._validate_url
    def validate(url):
        if not url.startswith(base + "/"):
            real_validate(url)
    monkeypatch.setattr(sec, "_validate_url", validate)


def request_process(dsn, url, output, kwargs):
    # The production allowlist is never expanded; this test injects a local
    # server only in the isolated child process under test.
    sec._connect = lambda: psycopg2.connect(dsn)
    sec._validate_url = lambda _url: None
    sec.config.sec.rate_limit = 2
    sec.config.sec.user_agent = "SEC-test contact@example.org"
    try:
        if kwargs.pop("fetchers", False):
            from core.fetchers.us_financial import USFinancialFetcher
            for _ in range(2):
                r = USFinancialFetcher()._request_sec(url)
        else:
            r = sec.get(url, task="process_test", **kwargs)
        output.put(("ok", r.status_code))
    except Exception as exc:
        output.put((type(exc).__name__, str(exc)))


def process(ctx, dsn, url, **kwargs):
    output = ctx.Queue()
    child = ctx.Process(target=request_process, args=(dsn, url, output, kwargs))
    child.start()
    return child, output


def join(child, output):
    child.join(15)
    if child.is_alive():
        child.kill()
        child.join()
        pytest.fail("SEC test process hung")
    assert child.exitcode == 0
    return output.get(timeout=2)


def test_multiple_processes_share_actual_send_spacing_and_ua(dsn, http):
    base, events = http
    ctx = mp.get_context("spawn")
    children = [process(ctx, dsn, base + "/ok", fetchers=True) for _ in range(3)]
    assert [join(*item) for item in children] == [("ok", 200)] * 3
    assert len(events) == 6
    assert all(e["ua"] == "SEC-test contact@example.org" for e in events)
    for left, right in zip(events, events[1:]):
        assert right["start"] - left["end"] >= 0.5


def test_shared_429_cooldown_releases_lock_and_survives_caller_timeout(dsn, http):
    base, events = http
    ctx = mp.get_context("spawn")
    first = process(ctx, dsn, base + "/429", total_timeout=1.5)
    assert join(*first)[0] == "SECWaitTimeout"
    assert sql(dsn, "SELECT pg_try_advisory_lock(%s)", (sec.LOCK_KEY,)) == [(True,)]
    second = process(ctx, dsn, base + "/ok")
    assert join(*second) == ("ok", 200)
    assert len(events) == 2
    assert events[1]["start"] - events[0]["end"] >= 2


def test_redirect_counts_as_another_request(dsn, http, monkeypatch):
    base, events = http
    allow_test_server(monkeypatch, base)
    assert sec.get(base + "/redirect", task="redirect_test").status_code == 200
    assert [e["path"] for e in events] == ["/redirect", "/ok"]
    assert events[1]["start"] - events[0]["end"] >= 0.5


def test_trickle_response_is_terminated_without_background_download(dsn, http, monkeypatch):
    base, events = http
    allow_test_server(monkeypatch, base)
    started = time.monotonic()
    with pytest.raises(sec.SECWaitTimeout):
        sec.get(base + "/slow", task="timeout_test", timeout=0.8, total_timeout=1.8)
    assert time.monotonic() - started < 2.5
    for _ in range(30):
        if events and events[0]["end"] is not None:
            break
        time.sleep(0.05)
    assert len(events) == 1
    assert events[0]["end"] is not None  # server saw its socket close


def test_killed_coordinator_leaves_protection_and_kills_http_child(dsn, http):
    base, events = http
    ctx = mp.get_context("spawn")
    child, output = process(ctx, dsn, base + "/slow", timeout=2)
    try:
        for _ in range(100):
            if events:
                break
            time.sleep(0.05)
        assert events
        child.kill()
        child.join(3)
        assert child.exitcode != 0
        remaining = sql(dsn, "SELECT EXTRACT(EPOCH FROM next_allowed_at-clock_timestamp()) "
                             "FROM sec_request_gate")[0][0]
        assert remaining > 0
        assert join(*process(ctx, dsn, base + "/ok", total_timeout=0.3))[0] == "SECWaitTimeout"
        assert len(events) == 1
        for _ in range(30):
            if events[0]["end"] is not None:
                break
            time.sleep(0.05)
        assert events[0]["end"] is not None
        assert join(*process(ctx, dsn, base + "/ok")) == ("ok", 200)
    finally:
        if child.is_alive():
            child.kill()
            child.join()
        output.close()


def test_database_disconnect_cancels_inflight_http(dsn, http, monkeypatch):
    base, events = http
    allow_test_server(monkeypatch, base)
    original = sec._connect
    connection = original()
    monkeypatch.setattr(sec, "_connect", lambda: connection)
    def disconnect():
        while not events:
            time.sleep(0.02)
        sql(dsn, "SELECT pg_terminate_backend(%s)", (connection.get_backend_pid(),))
    killer = threading.Thread(target=disconnect, daemon=True)
    killer.start()
    with pytest.raises(sec.SECCoordinationError):
        sec.get(base + "/slow", task="lost_connection", timeout=3)
    killer.join(2)
    assert sql(dsn, "SELECT next_allowed_at > clock_timestamp() FROM sec_request_gate") == [(True,)]


def test_missing_state_blocks_without_network(dsn, monkeypatch):
    sql(dsn, "DELETE FROM sec_request_gate")
    monkeypatch.setattr(sec, "_physical_get", lambda *_: pytest.fail("network bypass"))
    try:
        with pytest.raises(sec.SECCoordinationError):
            sec.get("https://data.sec.gov/test", task="missing_migration")
    finally:
        sql(dsn, Path("scripts/sec_request_gate.sql").read_text())


def test_state_write_failure_preserves_reservation(dsn, monkeypatch):
    def fail_finish(*_):
        raise psycopg2.OperationalError("connection lost at state write")
    monkeypatch.setattr(sec._Gate, "finish", fail_finish)
    monkeypatch.setattr(sec, "_physical_get", lambda *_: response(200))
    with pytest.raises(sec.SECCoordinationError):
        sec.get("https://data.sec.gov/test", task="write_failure")
    assert sql(dsn, "SELECT next_allowed_at > clock_timestamp() FROM sec_request_gate") == [(True,)]


def response(status, headers=None):
    r = sec.requests.Response()
    r.status_code = status
    r.headers.update(headers or {})
    r._content = b'{}'
    r.url = "https://data.sec.gov/test"
    return r


def test_http_retry_is_bounded_and_leaves_shared_cooldown(dsn, monkeypatch):
    starts = []
    def fail(*_):
        starts.append(time.monotonic())
        return response(503)
    monkeypatch.setattr(sec, "_physical_get", fail)
    with pytest.raises(sec.SECHTTPError):
        sec.get("https://data.sec.gov/test", task="retry_test", total_timeout=15)
    assert len(starts) == 3
    assert starts[1] - starts[0] >= 2
    assert starts[2] - starts[1] >= 4
    assert sql(dsn, "SELECT blocked_until > clock_timestamp() FROM sec_request_gate") == [(True,)]


@pytest.mark.parametrize("rate", [10, 0, -1, "invalid", "2", True])
def test_invalid_settings_fail_before_db_or_http(rate, monkeypatch):
    monkeypatch.setattr(sec.config.sec, "rate_limit", rate)
    monkeypatch.setattr(sec, "_connect", lambda: pytest.fail("unexpected DB"))
    with pytest.raises(sec.SECError, match="RATE_LIMIT"):
        sec.get("https://data.sec.gov/test", task="invalid_config")


@pytest.mark.parametrize("url", ["http://data.sec.gov/test", "https://data.sec.gov.evil/test",
                                 "https://user@data.sec.gov/test", "https://www.sec.gov:444/a"])
def test_untrusted_targets_rejected(url):
    with pytest.raises(sec.SECError):
        sec._validate_url(url)


def test_retry_after_date_and_malformed_header():
    assert sec._retry_after("120") == 120
    future = datetime.now(timezone.utc) + timedelta(seconds=120)
    assert 118 <= sec._retry_after(format_datetime(future)) <= 120
    assert sec._retry_after("nonsense") is None
    assert sec._retry_after("-10") is None


def test_unavailable_database_is_not_an_empty_response(monkeypatch):
    def unavailable():
        raise psycopg2.OperationalError("offline")
    monkeypatch.setattr(sec.config.sec, "rate_limit", 2)
    monkeypatch.setattr(sec, "_connect", unavailable)
    with pytest.raises(sec.SECCoordinationError):
        sec.get("https://data.sec.gov/test", task="db_failure")


def test_industry_and_equity_use_the_configured_user_agent(dsn, http, monkeypatch):
    from core.fetchers.industry import fetch_us_industry
    from scripts.backfill_us_equity import fetch_company_facts
    base, events = http
    real_transport = sec._physical_get
    monkeypatch.setattr(sec, "_physical_get", lambda url, headers, deadline, check:
                        real_transport(base + "/ok", headers, deadline, check))
    fetch_us_industry([{"stock_code": "TEST", "cik": "1"}])
    fetch_company_facts("0000000001")
    assert [e["ua"] for e in events] == ["SEC-test contact@example.org"] * 2


def test_existing_consumers_propagate_coordination_failure(monkeypatch):
    from core.fetchers import us_adt_cogs_filing as adt
    from core import us_financial_xbrl_fallback as fallback
    from core.financial_review_agent import SECEvidenceCollector
    from scripts import audit_adt_consolidated_cogs as audit
    from scripts import backfill_us_equity as equity
    def fail(*args, **kwargs):
        raise sec.SECCoordinationError("test gate unavailable")
    monkeypatch.setattr(sec, "get", fail)
    fallback._discover_instance_url.cache_clear()
    fallback._load_instance.cache_clear()
    for call in [lambda: adt._http_get("https://www.sec.gov/test"),
                 lambda: audit._http_get("https://www.sec.gov/test"),
                 lambda: fallback._discover_instance_url("test", "1"),
                 lambda: fallback._load_instance("https://www.sec.gov/test"),
                 lambda: SECEvidenceCollector()._fetch_filing("1", "test"),
                 equity.fetch_ticker_to_cik,
                 lambda: equity.fetch_company_facts("1")]:
        with pytest.raises(sec.SECCoordinationError):
            call()


def test_fetcher_construction_and_local_parsing_do_not_need_gate(monkeypatch):
    from core.fetchers.us_financial import USFinancialFetcher
    monkeypatch.setattr(sec, "_connect", lambda: pytest.fail("offline parsing touched DB"))
    monkeypatch.setattr(sec, "get", lambda *a, **k: pytest.fail("offline parsing touched HTTP"))
    # Mirrors per-statement construction in backfill_us_financial_versions.
    for _ in range(3):
        records, invalid, facts = USFinancialFetcher()._extract_facts(
            {"facts": {}}, {}, statement="income", stock_code="TEST")
        assert records == invalid == facts == []
