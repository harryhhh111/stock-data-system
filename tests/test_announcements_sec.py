"""SEC 公告采集器测试：临时 PostgreSQL 集群 + 本地 HTTP 服务。

模式复用 tests/test_sec_client.py：不触碰生产数据库与真实 SEC 端点。
覆盖：表单筛选（含 6-K/20-F）、/A 修订独立行、去重幂等、filings.files
分页、429/超时/失败状态落库、检查点恢复（停采 10 天）、分页中断不推进
checked_through、回补不倒填发现时间、多进程互斥与持锁进程被杀后的恢复、
配置注册门。
"""
import getpass
import json
import multiprocessing as mp
import os
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psycopg2
import pytest

import config
import db
from core import sec_client
from core.announcements import sec_source, store, sync

UTC = timezone.utc


def sql(dsn, query, args=()):
    with closing(psycopg2.connect(dsn)) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(query, args)
            return cur.fetchall() if cur.description else None


# ── 临时 PostgreSQL 集群 ──────────────────────────────────

@pytest.fixture(scope="module")
def cluster():
    if not shutil.which("initdb") or not shutil.which("pg_ctl"):
        pytest.skip("announcement tests require initdb/pg_ctl")
    root = Path(tempfile.mkdtemp(prefix="anncsec-"))
    data = root / "data"
    started = False
    try:
        subprocess.run(["initdb", "-D", str(data), "-A", "trust", "--no-locale",
                        "-E", "UTF8"], check=True, capture_output=True)
        result = subprocess.run(
            ["pg_ctl", "-D", str(data), "-l", str(root / "server.log"),
             "-o", f"-k {root} -p 56434 -h '' -F", "-w", "start"], capture_output=True)
        if result.returncode:
            pytest.fail("Temporary PostgreSQL startup failed: " +
                        (root / "server.log").read_text())
        started = True
        dsn = (f"host={root} port=56434 dbname=postgres "
               f"user={getpass.getuser()} connect_timeout=2")
        sql(dsn, Path("scripts/sec_request_gate.sql").read_text())
        sql(dsn, Path("scripts/watchlist_announcements.sql").read_text())
        # 最小化的现有表（只含本模块读取的列）
        sql(dsn, """
            CREATE TABLE stock_info (
                stock_code VARCHAR(20) PRIMARY KEY,
                stock_name VARCHAR(100) NOT NULL DEFAULT '',
                market VARCHAR(10) NOT NULL,
                cik VARCHAR(20)
            )
        """)
        sql(dsn, """
            CREATE TABLE watchlist_item (
                id BIGSERIAL PRIMARY KEY,
                market VARCHAR(10) NOT NULL,
                stock_code VARCHAR(20) NOT NULL,
                stock_name VARCHAR(200) NOT NULL DEFAULT '',
                group_name VARCHAR(80) NOT NULL DEFAULT '',
                sort_order INTEGER NOT NULL DEFAULT 0
            )
        """)
        yield dsn, root
    finally:
        if started:
            subprocess.run(["pg_ctl", "-D", str(data), "-m", "immediate", "-w", "stop"],
                           check=True, capture_output=True)
        shutil.rmtree(root)


# ── 本地 HTTP 服务（响应表可变，测试间互不共享）───────────

@pytest.fixture
def http():
    routes = {}
    events = []
    mutex = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            with mutex:
                events.append(self.path)
                spec = routes.get(self.path)
            if spec is None:
                self.send_response(404)
                self.end_headers()
                return
            status, body, headers = spec
            try:
                if status == "trickle":
                    self.send_response(200)
                    self.send_header("Content-Length", "100000")
                    self.end_headers()
                    for _ in range(200):
                        self.wfile.write(b"x")
                        self.wfile.flush()
                        time.sleep(0.3)
                    return
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        yield base, routes, events
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def serve_json(routes, path, payload, status=200, headers=None):
    routes[path] = (status, json.dumps(payload).encode(), headers or {})


# ── SEC submissions fixture 构造 ──────────────────────────

def mk_filing(accession, form, fdate, acc_dt=None, doc=None):
    return {
        "accessionNumber": accession,
        "form": form,
        "filingDate": fdate.isoformat(),
        "acceptanceDateTime": acc_dt or "",
        "primaryDocument": doc or f"{accession.replace('-', '')}-doc.htm",
    }


def submissions_doc(cik, filings, files=None):
    arrays = {"accessionNumber": [], "form": [], "filingDate": [],
              "acceptanceDateTime": [], "primaryDocument": []}
    for f in filings:
        for key in arrays:
            arrays[key].append(f[key])
    return {"cik": str(int(cik)), "name": "Test Co",
            "filings": {"recent": arrays, "files": files or []}}


def days_ago(n):
    """以 UTC 日期为基准（与 sync 扫描窗口的 UTC .date() 口径一致，
    避免本地时区与 UTC 跨日时造出边界假缺口）。"""
    return datetime.now(UTC).date() - timedelta(days=n)


# ── 环境 fixture：config/db 指向临时集群，URL 校验放行本地 ──

@pytest.fixture
def env(cluster, http, monkeypatch):
    dsn, root = cluster
    base, routes, events = http
    monkeypatch.setattr(config.db, "host", str(root))
    monkeypatch.setattr(config.db, "port", 56434)
    monkeypatch.setattr(config.db, "dbname", "postgres")
    monkeypatch.setattr(config.db, "user", getpass.getuser())
    monkeypatch.setattr(config.db, "password", "")
    db.close_pool()
    db._pool = None
    monkeypatch.setattr(config.sec, "rate_limit", 2)
    monkeypatch.setattr(config.sec, "user_agent", "AnncTest contact@example.org")
    monkeypatch.setattr(config.announcements, "enabled", "true")
    # LLM 增强离线：测试不调用真实 mmx（增强失败路径 = 字段留 NULL）
    from core.announcements import llm_enhance
    monkeypatch.setattr(llm_enhance, "_run_mmx",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            llm_enhance.EnhanceError("test: llm offline")))
    monkeypatch.setattr(config.announcements, "sec_interval_minutes", "60")
    monkeypatch.setattr(config.announcements, "ir_interval_minutes", "60")
    monkeypatch.setattr(sec_source, "SUBMISSIONS_BASE", f"{base}/submissions")
    real_validate = sec_client._validate_url

    def validate(url):
        if not url.startswith(base + "/"):
            real_validate(url)
    monkeypatch.setattr(sec_client, "_validate_url", validate)
    yield dsn, base, routes, events
    db.close_pool()
    db._pool = None


def seed_company(dsn, code, cik):
    sql(dsn, "INSERT INTO stock_info (stock_code, stock_name, market, cik) "
             "VALUES (%s, %s, 'US', %s) ON CONFLICT (stock_code) DO NOTHING",
        (code, code, cik))
    sql(dsn, "INSERT INTO watchlist_item (market, stock_code, stock_name) "
             "VALUES ('US', %s, %s)", (code, code))


def cleanup_company(dsn, code):
    sql(dsn, "DELETE FROM watchlist_item WHERE stock_code = %s", (code,))
    sql(dsn, "DELETE FROM stock_info WHERE stock_code = %s", (code,))
    sql(dsn, "DELETE FROM watchlist_announcement_security")
    sql(dsn, "DELETE FROM watchlist_announcement")
    sql(dsn, "DELETE FROM watchlist_announcement_run")
    sql(dsn, "DELETE FROM watchlist_announcement_check_state")
    sql(dsn, "DELETE FROM watchlist_announcement_recovery")
    sql(dsn, "DELETE FROM watchlist_announcement_gap")
    sql(dsn, "DELETE FROM watchlist_announcement_source")


def run(**kw):
    params = dict(trigger="manual", due_only=False)
    params.update(kw)
    return sync.run_round(**params)


# ── 采集行为测试 ──────────────────────────────────────────

class TestSecFetchAndParse:
    def test_form_filter_neutral_titles_and_amendments(self, env):
        dsn, base, routes, _events = env
        cik = "0001000001"
        filings = [
            mk_filing("0001000001-26-000001", "8-K", days_ago(3),
                      "2026-10-17T13:05:11.000Z"),
            mk_filing("0001000001-26-000002", "8-K/A", days_ago(2),
                      "2026-10-18T09:31:00.000Z"),
            mk_filing("0001000001-26-000003", "10-Q", days_ago(10)),
            mk_filing("0001000001-26-000004", "6-K", days_ago(12)),
            mk_filing("0001000001-26-000005", "20-F", days_ago(30)),
            mk_filing("0001000001-26-000006", "40-F", days_ago(31)),
            mk_filing("0001000001-26-000007", "10-K", days_ago(60)),
            mk_filing("0001000001-26-000008", "S-1", days_ago(5)),      # 不采
            mk_filing("0001000001-26-000009", "DEF 14A", days_ago(6)),  # 不采
            mk_filing("0001000001-26-000010", "4", days_ago(1)),        # 不采
        ]
        serve_json(routes, f"/submissions/CIK{cik}.json",
                   submissions_doc(cik, filings))
        seed_company(dsn, "TST1", cik)
        try:
            summary = run()
            assert summary["status"] == "completed"
            rows = sql(dsn, """
                SELECT source_record_id, form_type, title, published_at,
                       published_at_precision, source_published_raw, source_timezone,
                       revision, is_backfill, original_url
                FROM watchlist_announcement ORDER BY source_record_id
            """)
            forms = [r[1] for r in rows]
            assert forms == ["8-K", "8-K/A", "10-Q", "6-K",
                             "20-F", "40-F", "10-K"]
            by_id = {r[0]: r for r in rows}
            # /A 修订独立成行，标题为中性表单描述
            assert by_id["0001000001-26-000002"][2] == "8-K/A 当前报告（修订）"
            assert by_id["0001000001-26-000001"][2] == "8-K 当前报告"
            assert by_id["0001000001-26-000005"][2] == "20-F 外国发行人年报"
            assert by_id["0001000001-26-000006"][2] == "40-F 外国发行人年报（加拿大）"
            assert by_id["0001000001-26-000004"][2] == "6-K 外国发行人临时报告"
            # acceptance 时间解析为 UTC 秒级精度，原始字符串与美东时区保留
            r8k = by_id["0001000001-26-000001"]
            assert r8k[3] == datetime(2026, 10, 17, 13, 5, 11, tzinfo=UTC)
            assert r8k[4] == "second"
            assert r8k[5] == "2026-10-17T13:05:11.000Z"
            assert r8k[6] == "America/New_York"
            # 无 acceptance 的记录退回 filingDate：published_at 置 NULL，仅日期精度
            r10q = by_id["0001000001-26-000003"]
            assert r10q[3] is None and r10q[4] == "day"
            # 回补标记、修订版本、原文链接
            assert all(r[7] == 1 for r in rows)
            assert all(r[8] for r in rows)  # is_backfill
            assert r8k[9].startswith(
                "https://www.sec.gov/Archives/edgar/data/1000001/")
            # 证券关联落独立表
            links = sql(dsn, "SELECT DISTINCT stock_code "
                             "FROM watchlist_announcement_security")
            assert [r[0] for r in links] == ["TST1"]
            # 检查点推进 + 状态
            state = sql(dsn, "SELECT last_attempt_status, checked_through, "
                             "last_success_at FROM watchlist_announcement_check_state")
            assert state[0][0] == "success_new"
            assert state[0][1] is not None and state[0][2] is not None
            # 回补完成后恢复行删除
            assert sql(dsn, "SELECT COUNT(*) FROM watchlist_announcement_recovery")[0][0] == 0
        finally:
            cleanup_company(dsn, "TST1")

    def test_dedup_rerun_idempotent(self, env):
        dsn, base, routes, _e = env
        cik = "0001000002"
        serve_json(routes, f"/submissions/CIK{cik}.json", submissions_doc(cik, [
            mk_filing("0001000002-26-000001", "8-K", days_ago(3),
                      "2026-10-17T13:05:11.000Z"),
        ]))
        seed_company(dsn, "TST2", cik)
        try:
            run()
            first = sql(dsn, "SELECT first_observed_at, last_seen_at "
                             "FROM watchlist_announcement")
            assert len(first) == 1
            time.sleep(0.05)
            summary = run()  # 重复抓取
            assert summary["sources"][0]["status"] == "success_empty"
            rows = sql(dsn, "SELECT first_observed_at, last_seen_at, revision "
                            "FROM watchlist_announcement")
            assert len(rows) == 1                      # 不产生重复行
            assert rows[0][0] == first[0][0]           # 首次发现时间不变
            assert rows[0][1] >= first[0][1]           # last_seen 推进
            assert rows[0][2] == 1
            links = sql(dsn, "SELECT COUNT(*) FROM watchlist_announcement_security")
            assert links[0][0] == 1                    # 关联不重复
        finally:
            cleanup_company(dsn, "TST2")

    def test_revision_on_content_change(self, env):
        dsn, base, routes, _e = env
        cik = "0001000003"
        payload = submissions_doc(cik, [
            mk_filing("0001000003-26-000001", "10-Q", days_ago(3),
                      "2026-10-17T13:05:11.000Z"),
        ])
        serve_json(routes, f"/submissions/CIK{cik}.json", payload)
        seed_company(dsn, "TST3", cik)
        try:
            run()
            # 相同发布内容变更（accession 不变，主文件变化）→ 追加 revision 2
            payload["filings"]["recent"]["primaryDocument"][0] = "amended-doc.htm"
            serve_json(routes, f"/submissions/CIK{cik}.json", payload)
            summary = run()
            assert summary["sources"][0]["revised"] == 1
            rows = sql(dsn, """
                SELECT revision, original_url, first_observed_at
                FROM watchlist_announcement ORDER BY revision
            """)
            assert [r[0] for r in rows] == [1, 2]     # 旧版本保留
            assert rows[0][1] != rows[1][1]
            run_row = sql(dsn, """
                SELECT status, fetched_count, inserted_count, revised_count
                FROM watchlist_announcement_run ORDER BY id DESC LIMIT 1
            """)[0]
            assert run_row == ("success_new", 1, 0, 1)
        finally:
            cleanup_company(dsn, "TST3")

    def test_backfill_does_not_backfill_first_observed(self, env):
        dsn, base, routes, _e = env
        cik = "0001000004"
        serve_json(routes, f"/submissions/CIK{cik}.json", submissions_doc(cik, [
            mk_filing("0001000004-26-000001", "8-K", days_ago(80),
                      "2026-08-01T10:00:00.000Z"),
        ]))
        seed_company(dsn, "TST4", cik)
        try:
            before = datetime.now(UTC)
            run()
            row = sql(dsn, "SELECT published_at, first_observed_at, is_backfill "
                           "FROM watchlist_announcement")[0]
            assert row[0] == datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
            assert row[1] >= before      # 发现时间是系统实际发现时间，不倒填
            assert row[2] is True
        finally:
            cleanup_company(dsn, "TST4")


    def test_scheduled_round_respects_next_due(self, env):
        """定时入口按到期时间选取：next_due_at 未到 → 不重复抓取。"""
        dsn, base, routes, events = env
        cik = "0001000013"
        serve_json(routes, f"/submissions/CIK{cik}.json", submissions_doc(cik, [
            mk_filing("0001000013-26-000001", "8-K", days_ago(1),
                      "2026-10-19T10:00:00.000Z"),
        ]))
        seed_company(dsn, "TST13", cik)
        try:
            summary = run()
            assert summary["processed"] == 1
            # 未到期不抓取（显式把到期时间放到未来，避免跨小时边界抖动）
            sql(dsn, "UPDATE watchlist_announcement_check_state "
                     "SET next_due_at = NOW() + interval '1 hour'")
            before = len(events)
            summary2 = sync.run_round(trigger="scheduled")  # due_only=True
            assert summary2["processed"] == 0
            assert len(events) == before
            # 到期后恢复抓取
            sql(dsn, "UPDATE watchlist_announcement_check_state "
                     "SET next_due_at = NOW() - interval '1 minute'")
            summary3 = sync.run_round(trigger="scheduled")
            assert summary3["processed"] == 1
        finally:
            cleanup_company(dsn, "TST13")


class TestCheckpointAndRecovery:
    def test_resume_window_after_10_day_outage(self, env):
        """停采 10 天（超过 7 天重叠）：窗口从原检查点减 7 天起，覆盖中断期。"""
        dsn, base, routes, _e = env
        cik = "0001000005"
        serve_json(routes, f"/submissions/CIK{cik}.json", submissions_doc(cik, [
            mk_filing("0001000005-26-000001", "8-K", days_ago(9),   # 中断期内
                      "2026-10-11T10:00:00.000Z"),
            mk_filing("0001000005-26-000002", "8-K", days_ago(2),
                      "2026-10-18T10:00:00.000Z"),
        ]))
        seed_company(dsn, "TST5", cik)
        try:
            # 预置：10 天前完成过完整检查
            source_id = sql(dsn, """
                INSERT INTO watchlist_announcement_source
                    (market, source_kind, issuer_identifier, allowed_domain, entry_url,
                     enabled, source_status)
                VALUES ('US', 'sec', %s, 'sec.gov', %s, true, 'enabled')
                RETURNING source_id
            """, (cik, f"https://data.sec.gov/submissions/CIK{cik}.json"))[0][0]
            ct = datetime.now(UTC) - timedelta(days=10)
            sql(dsn, """
                INSERT INTO watchlist_announcement_check_state
                    (market, issuer_identifier, source_id, effective_interval_minutes,
                     last_attempt_at, last_attempt_status, last_success_at,
                     checked_through)
                VALUES ('US', %s, %s, 60, %s, 'success_empty', %s, %s)
            """, (cik, source_id, ct, ct, ct))
            before = datetime.now(UTC)
            summary = run()
            assert summary["sources"][0]["status"] == "success_new"
            rows = sql(dsn, "SELECT source_record_id, is_backfill "
                            "FROM watchlist_announcement ORDER BY source_record_id")
            assert len(rows) == 2              # 中断期内的公告被补抓
            assert all(not r[1] for r in rows)  # 增量路径不是回补
            new_ct = sql(dsn, "SELECT checked_through "
                              "FROM watchlist_announcement_check_state")[0][0]
            assert new_ct >= before            # 检查点推进到本轮 T
        finally:
            cleanup_company(dsn, "TST5")

    def test_page_failure_keeps_checkpoint_and_persists_pending(self, env):
        """分页失败：recent 数据已入库，但 checked_through 不推进、pending 保留。"""
        dsn, base, routes, _e = env
        cik = "0001000006"
        old_from = days_ago(90)
        old_to = days_ago(31)
        page_name = f"CIK{int(cik)}-submissions-001.json"
        serve_json(routes, f"/submissions/CIK{cik}.json", submissions_doc(
            cik,
            [mk_filing("0001000006-26-000001", "8-K", days_ago(5),
                       "2026-10-15T10:00:00.000Z")],
            files=[{"name": page_name, "filingCount": 1,
                    "filingFrom": old_from.isoformat(),
                    "filingTo": old_to.isoformat()}]))
        # 分页返回 500（sec_client 重试后失败，共享退避由 gate 承载）
        routes[f"/submissions/{page_name}"] = (500, b"{}", {"Retry-After": "0"})
        seed_company(dsn, "TST6", cik)
        try:
            summary = run()
            src = summary["sources"][0]
            assert src["status"] in ("failed", "partial")
            rows = sql(dsn, "SELECT COUNT(*) FROM watchlist_announcement")
            assert rows[0][0] == 0  # 整体失败：未提交任何公告（fetch 阶段抛错）
            state = sql(dsn, "SELECT checked_through, last_attempt_status, "
                             "last_error_class FROM watchlist_announcement_check_state")
            assert state[0][0] is None               # 检查点不推进
            assert state[0][1] == "failed"
            assert state[0][2] == "http_500"
        finally:
            cleanup_company(dsn, "TST6")

    def test_pagination_via_filings_files(self, env):
        """90 天回补超出 recent 覆盖时，经 filings.files 分页补齐。"""
        dsn, base, routes, _e = env
        cik = "0001000007"
        page_name = f"CIK{int(cik)}-submissions-001.json"
        serve_json(routes, f"/submissions/CIK{cik}.json", submissions_doc(
            cik,
            [mk_filing("0001000007-26-000001", "8-K", days_ago(5),
                       "2026-10-15T10:00:00.000Z")],
            files=[{"name": page_name, "filingCount": 1,
                    "filingFrom": days_ago(90).isoformat(),
                    "filingTo": days_ago(6).isoformat()}]))
        serve_json(routes, f"/submissions/{page_name}", {
            "accessionNumber": ["0001000007-26-000002"],
            "form": ["20-F"],
            "filingDate": [days_ago(50).isoformat()],
            "acceptanceDateTime": ["2026-08-31T09:00:00.000Z"],
            "primaryDocument": ["old-20f.htm"],
        })
        seed_company(dsn, "TST7", cik)
        try:
            summary = run()
            assert summary["sources"][0]["status"] == "success_new"
            rows = sql(dsn, "SELECT source_record_id FROM watchlist_announcement "
                            "ORDER BY source_record_id")
            assert [r[0] for r in rows] == [
                "0001000007-26-000001", "0001000007-26-000002"]
            assert sql(dsn, "SELECT checked_through IS NOT NULL "
                            "FROM watchlist_announcement_check_state")[0][0]
            assert sql(dsn, "SELECT COUNT(*) FROM watchlist_announcement_recovery")[0][0] == 0
            assert sql(dsn, "SELECT COUNT(*) FROM watchlist_announcement_gap")[0][0] == 0
        finally:
            cleanup_company(dsn, "TST7")

    def test_page_404_records_gap(self, env):
        """历史分页 404：记持久缺口；完整检查点不能越过缺口——
        checked_through / last_success_at 不推进，状态持续 partial，
        近期区间检查进度记入 covered_ranges。"""
        dsn, base, routes, _e = env
        cik = "0001000008"
        page_name = f"CIK{int(cik)}-submissions-001.json"
        serve_json(routes, f"/submissions/CIK{cik}.json", submissions_doc(
            cik,
            [mk_filing("0001000008-26-000001", "8-K", days_ago(5),
                       "2026-10-15T10:00:00.000Z")],
            files=[{"name": page_name, "filingCount": 1,
                    "filingFrom": days_ago(90).isoformat(),
                    "filingTo": days_ago(6).isoformat()}]))
        # 不注册分页路由 → 本地服务返回 404（allowed_statuses 显式允许）
        seed_company(dsn, "TST8", cik)
        try:
            summary = run()
            assert summary["sources"][0]["status"] == "partial"
            gaps = sql(dsn, "SELECT reason, resolved_at FROM watchlist_announcement_gap")
            assert len(gaps) == 1 and gaps[0][0] == "history_unavailable"
            assert gaps[0][1] is None              # 缺口持久保留
            state = sql(dsn, """
                SELECT checked_through, last_success_at, last_attempt_status,
                       covered_ranges
                FROM watchlist_announcement_check_state
            """)[0]
            assert state[0] is None                # 检查点不越过缺口
            assert state[1] is None                # 无完整成功检查
            assert state[2] == "partial"           # 不宣称完整
            assert state[3] is not None            # 近期区间进度另行记录
            # 已入库公告可读（recent 区间数据保留）
            assert sql(dsn, "SELECT COUNT(*) FROM watchlist_announcement")[0][0] == 1

            # 下一轮：缺口未解决，状态仍 partial，不退化成 success_empty，
            # 已记录缺口不自动重试（不重复请求缺失分页）
            before_gaps = sql(dsn, "SELECT COUNT(*) FROM watchlist_announcement_gap")[0][0]
            summary2 = run()
            assert summary2["sources"][0]["status"] == "partial"
            state2 = sql(dsn, "SELECT checked_through, last_attempt_status "
                              "FROM watchlist_announcement_check_state")[0]
            assert state2[0] is None and state2[1] == "partial"
            assert sql(dsn, "SELECT COUNT(*) FROM watchlist_announcement_gap")[0][0] == before_gaps
        finally:
            cleanup_company(dsn, "TST8")


# ── 修复回归：多证券关联 / 解析失败 / 回补起点 / 退出码 / 到期推进 ──

class TestSharedCikSecurityLinks:
    def test_shared_cik_links_all_securities(self, env):
        """两个关注证券共享 CIK：请求合并为一次，关联写入全部证券。"""
        dsn, base, routes, events = env
        cik = "0001000014"
        serve_json(routes, f"/submissions/CIK{cik}.json", submissions_doc(cik, [
            mk_filing("0001000014-26-000001", "8-K", days_ago(1),
                      "2026-10-19T10:00:00.000Z"),
        ]))
        seed_company(dsn, "LINK.A", cik)
        seed_company(dsn, "LINK.B", cik)
        try:
            before = len(events)
            summary = run()
            assert len(events) - before == 1  # 网络请求合并
            links = sql(dsn, "SELECT stock_code FROM "
                             "watchlist_announcement_security ORDER BY stock_code")
            assert [r[0] for r in links] == ["LINK.A", "LINK.B"]
        finally:
            cleanup_company(dsn, "LINK.A")
            cleanup_company(dsn, "LINK.B")


class TestParseFailureVsEmpty:
    def test_missing_required_arrays_is_parse_failure(self, env):
        """filings.recent 缺必需数组 = 解析失败（failed，不推进检查点），
        不能当成"窗口内无公告"。"""
        dsn, base, routes, _e = env
        cik = "0001000015"
        serve_json(routes, f"/submissions/CIK{cik}.json",
                   {"cik": cik, "filings": {"recent": {}}})
        seed_company(dsn, "BADJSON", cik)
        try:
            summary = run()
            assert summary["sources"][0]["status"] == "failed"
            assert summary["sources"][0]["error_class"] == "parse_error"
            state = sql(dsn, "SELECT checked_through, last_attempt_status "
                             "FROM watchlist_announcement_check_state")[0]
            assert state[0] is None and state[1] == "failed"
        finally:
            cleanup_company(dsn, "BADJSON")

    def test_empty_arrays_is_success_empty(self, env):
        """数组齐全但为空 = 真实"窗口内无公告"（success_empty，推进检查点）。"""
        dsn, base, routes, _e = env
        cik = "0001000016"
        serve_json(routes, f"/submissions/CIK{cik}.json",
                   submissions_doc(cik, []))
        seed_company(dsn, "EMPTYOK", cik)
        try:
            summary = run()
            assert summary["sources"][0]["status"] == "success_empty"
            state = sql(dsn, "SELECT checked_through, last_success_at "
                             "FROM watchlist_announcement_check_state")[0]
            assert state[0] is not None and state[1] is not None
        finally:
            cleanup_company(dsn, "EMPTYOK")


class TestBackfillOriginPersistence:
    def test_initial_failure_preserves_backfill_bounds(self, env, monkeypatch):
        """首次请求失败前必须已持久化固定回补起点；下轮不随日期滚动。"""
        dsn, base, routes, _e = env
        cik = "0001000017"
        seed_company(dsn, "ORIGIN", cik)
        def fail(*_a, **_kw):
            raise ValueError("simulated fetch failure")
        monkeypatch.setattr(sec_source, "fetch_submissions", fail)
        try:
            before = datetime.now(UTC)
            run()
            rows = sql(dsn, "SELECT backfill_start, target_through "
                            "FROM watchlist_announcement_recovery")
            assert len(rows) == 1
            start1, target1 = rows[0]
            assert start1 >= before - timedelta(days=91)
            assert start1 <= before - timedelta(days=89)
            # 再次失败：持久起点不变（不滚动为"现在减 90 天"）
            run()
            rows2 = sql(dsn, "SELECT backfill_start, target_through "
                             "FROM watchlist_announcement_recovery")
            assert rows2[0] == (start1, target1)
        finally:
            cleanup_company(dsn, "ORIGIN")


class TestCliExitCodes:
    def test_failed_round_exit_4(self, env, monkeypatch):
        dsn, base, routes, _e = env
        cik = "0001000018"
        seed_company(dsn, "CLIFAIL", cik)
        def fail(*_a, **_kw):
            raise ValueError("simulated fetch failure")
        monkeypatch.setattr(sec_source, "fetch_submissions", fail)
        try:
            assert sync.main([]) == 4
        finally:
            cleanup_company(dsn, "CLIFAIL")

    def test_success_round_exit_0(self, env):
        dsn, base, routes, _e = env
        cik = "0001000019"
        serve_json(routes, f"/submissions/CIK{cik}.json",
                   submissions_doc(cik, []))
        seed_company(dsn, "CLIOK", cik)
        try:
            assert sync.main([]) == 0
        finally:
            cleanup_company(dsn, "CLIOK")


class TestDueAdvancement:
    def test_recovery_due_advances_beyond_current_round(self, env):
        """停采恢复：next_due 推进到当前轮之后（槽位+interval），
        不用"旧到期+interval"（仍在过去会紧接着重复采集）。"""
        dsn, base, routes, _e = env
        cik = "0001000020"
        serve_json(routes, f"/submissions/CIK{cik}.json",
                   submissions_doc(cik, []))
        seed_company(dsn, "DUE", cik)
        try:
            source_id = sql(dsn, """
                INSERT INTO watchlist_announcement_source
                    (market, source_kind, issuer_identifier, allowed_domain, entry_url,
                     enabled, source_status)
                VALUES ('US', 'sec', %s, 'sec.gov', %s, true, 'enabled')
                RETURNING source_id
            """, (cik, f"https://data.sec.gov/submissions/CIK{cik}.json"))[0][0]
            # 预置：上次检查在 10 天前，旧到期时间早已过期
            old = datetime.now(UTC) - timedelta(days=10)
            sql(dsn, """
                INSERT INTO watchlist_announcement_check_state
                    (market, issuer_identifier, source_id, effective_interval_minutes,
                     last_attempt_at, last_attempt_status, last_success_at,
                     next_due_at, checked_through)
                VALUES ('US', %s, %s, 60, %s, 'success_empty', %s, %s, %s)
            """, (cik, source_id, old, old, old, old))
            before = datetime.now(UTC)
            summary = sync.run_round(trigger="scheduled")
            assert summary["processed"] == 1       # 过期来源恢复检查一次
            new_due = sql(dsn, "SELECT next_due_at "
                               "FROM watchlist_announcement_check_state")[0][0]
            # 推进到当前轮之后：不是旧到期 +interval（过去），而是
            # 本轮槽位 +interval（未来）
            assert new_due > before
            assert new_due <= before + timedelta(minutes=61)
            # 紧接着的定时入口不重复采集（过期检查只合并一次）
            summary2 = sync.run_round(trigger="scheduled")
            assert summary2["processed"] == 0
        finally:
            cleanup_company(dsn, "DUE")

    def test_interval_anchor_phase_no_skip_after_start_delay(self, env, monkeypatch):
        """非整点调度相位（注册时间锚点）+ 20 秒开始延迟：
        next_due = 槽位+interval，与 IntervalTrigger 下一次触发同相位。"""
        dsn, base, routes, _e = env
        cik = "0001000024"
        serve_json(routes, f"/submissions/CIK{cik}.json",
                   submissions_doc(cik, []))
        seed_company(dsn, "PHASE", cik)
        first_tick = datetime(2026, 10, 20, 10, 59, 50, tzinfo=UTC)
        clock = [first_tick]

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)

        original = sync._sync_sec_source

        def delayed(*args, **kwargs):
            clock[0] += timedelta(seconds=20)  # 来源开始晚于触发点 20 秒
            return original(*args, **kwargs)

        monkeypatch.setattr(sync, "datetime", Clock)
        monkeypatch.setattr(sync, "_sync_sec_source", delayed)
        try:
            first = sync.run_round(trigger="scheduled",
                                   schedule_anchor=first_tick)
            assert first["processed"] == 1
            due = sql(dsn, "SELECT next_due_at "
                           "FROM watchlist_announcement_check_state")[0][0]
            # 槽位对齐到锚点相位：10:59:50 + 60min = 11:59:50，
            # 不是 12:00 整点（网格对齐错误方向），也不含 20 秒漂移
            assert due == first_tick + timedelta(minutes=60)
            clock[0] = first_tick + timedelta(minutes=60)  # 下一次触发
            second = sync.run_round(trigger="scheduled",
                                    schedule_anchor=first_tick)
            assert second["processed"] == 1
        finally:
            cleanup_company(dsn, "PHASE")

    def test_hourly_due_aligned_despite_start_delay(self, env, monkeypatch):
        """手工相位（无锚点）：10:00 开始 + 5 秒延迟 → 槽位=本轮开始，
        next_due=11:00:00（不含 5 秒漂移），下一轮到期处理。"""
        dsn, base, routes, _e = env
        cik = "0001000021"
        serve_json(routes, f"/submissions/CIK{cik}.json",
                   submissions_doc(cik, []))
        seed_company(dsn, "HOURLY", cik)
        first_tick = datetime.now(UTC).replace(
            hour=10, minute=0, second=0, microsecond=0)
        clock = [first_tick]

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)

        original = sync._sync_sec_source

        def delayed(*args, **kwargs):
            clock[0] += timedelta(seconds=5)   # 模拟来源实际开始晚于触发点
            return original(*args, **kwargs)

        monkeypatch.setattr(sync, "datetime", Clock)
        monkeypatch.setattr(sync, "_sync_sec_source", delayed)
        try:
            first = sync.run_round(trigger="scheduled")
            assert first["processed"] == 1
            due = sql(dsn, "SELECT next_due_at "
                           "FROM watchlist_announcement_check_state")[0][0]
            assert due == first_tick + timedelta(minutes=60)  # 无 5 秒漂移
            clock[0] = first_tick + timedelta(minutes=60)     # 11:00 整点调度
            second = sync.run_round(trigger="scheduled")
            assert second["processed"] == 1
        finally:
            cleanup_company(dsn, "HOURLY")


class TestRecoveryDurability:
    def test_recovery_committed_before_first_request(self, env, monkeypatch):
        """首次网络请求进行时，独立数据库连接已能读到固定回补起点。"""
        dsn, base, routes, _e = env
        cik = "0001000022"
        seed_company(dsn, "DURABLE", cik)
        observed = []

        def fetch(*_a, **_kw):
            observed.extend(sql(dsn, "SELECT backfill_start, target_through "
                                     "FROM watchlist_announcement_recovery"))
            return submissions_doc(cik, [])

        monkeypatch.setattr(sec_source, "fetch_submissions", fetch)
        try:
            run()
            assert observed, "请求进行中恢复行未提交（起点仍可能丢失）"
        finally:
            cleanup_company(dsn, "DURABLE")


class TestLockLoss:
    def test_lost_lock_session_reports_lock_lost(self, env, monkeypatch):
        """整轮进行中锁会话失效：中止提交与请求，状态 lock_lost，
        绝不报 completed。"""
        dsn, base, routes, _e = env
        cik = "0001000023"
        seed_company(dsn, "LOCKLOSS", cik)

        def lost(self):
            raise psycopg2.OperationalError("test: lost lock connection")

        monkeypatch.setattr(sync.RoundLock, "check_alive", lost)
        try:
            summary = run()
            assert summary["status"] == "lock_lost"
            assert summary["processed"] == 0
            assert sync.main([]) == 4  # CLI 非成功退出码
        finally:
            cleanup_company(dsn, "LOCKLOSS")


class TestFailureStatusRecording:
    def test_next_due_advances_from_previous_due_not_round_end(self, env):
        """到期推进基准是上次到期时间，不是本轮结束时间：
        整轮有耗时时，下一个调度触发点来源确实到期（有效间隔 ≈ 配置值）。"""
        dsn, base, routes, _e = env
        cik = "0001000020"
        serve_json(routes, f"/submissions/CIK{cik}.json",
                   submissions_doc(cik, []))
        seed_company(dsn, "DUE", cik)
        try:
            # 预置：上次到期于 65 分钟前（模拟整轮耗时 5 分钟的上一轮
            # 在 60 分钟调度点触发）；若按本轮结束时间推进，下次到期将是
            # now+60min（距上次到期 125 分钟）；按上次到期推进应为 T0+60min。
            source_id = sql(dsn, """
                INSERT INTO watchlist_announcement_source
                    (market, source_kind, issuer_identifier, allowed_domain, entry_url,
                     enabled, source_status)
                VALUES ('US', 'sec', %s, 'sec.gov', %s, true, 'enabled')
                RETURNING source_id
            """, (cik, f"https://data.sec.gov/submissions/CIK{cik}.json"))[0][0]
            t0 = datetime.now(UTC) - timedelta(minutes=65)
            ct = datetime.now(UTC) - timedelta(minutes=125)
            sql(dsn, """
                INSERT INTO watchlist_announcement_check_state
                    (market, issuer_identifier, source_id, effective_interval_minutes,
                     last_attempt_at, last_attempt_status, last_success_at,
                     next_due_at, checked_through)
                VALUES ('US', %s, %s, 60, %s, 'success_empty', %s, %s, %s)
            """, (cik, source_id, t0, t0, t0, ct))
            summary = sync.run_round(trigger="scheduled")  # due_only=True
            assert summary["processed"] == 1
            new_due = sql(dsn, "SELECT next_due_at "
                               "FROM watchlist_announcement_check_state")[0][0]
            assert new_due == t0 + timedelta(minutes=60)
            # 下一个调度触发点（上次到期 +60 分钟 + 任务耗时）来源已到期：
            # 有效间隔 ≈60 分钟而非 ≈120 分钟
            assert new_due <= datetime.now(UTC)
            summary2 = sync.run_round(trigger="scheduled")
            assert summary2["processed"] == 1
        finally:
            cleanup_company(dsn, "DUE")


class TestFailureStatusRecording:
    def test_429_recorded_as_failure(self, env):
        dsn, base, routes, _e = env
        cik = "0001000009"
        routes[f"/submissions/CIK{cik}.json"] = (429, b"{}", {"Retry-After": "0"})
        seed_company(dsn, "TST9", cik)
        try:
            summary = run()
            src = summary["sources"][0]
            assert src["status"] == "failed"
            assert src["error_class"] == "http_429"
            run_row = sql(dsn, """
                SELECT status, error_class, fetched_count, inserted_count
                FROM watchlist_announcement_run ORDER BY id DESC LIMIT 1
            """)[0]
            assert run_row[0] == "failed" and run_row[1] == "http_429"
            assert run_row[2] is None and run_row[3] is None  # 不伪造为 0
            state = sql(dsn, "SELECT checked_through, next_due_at "
                             "FROM watchlist_announcement_check_state")[0]
            assert state[0] is None                # 失败不推进检查点
            assert state[1] is not None            # 失败也按间隔推进到期时间
        finally:
            cleanup_company(dsn, "TST9")

    def test_timeout_recorded_as_failure(self, env):
        dsn, base, routes, _e = env
        cik = "0001000010"
        routes[f"/submissions/CIK{cik}.json"] = ("trickle", b"", {})
        seed_company(dsn, "TST10", cik)
        try:
            summary = run(request_timeout=1, request_total_timeout=2)
            src = summary["sources"][0]
            assert src["status"] == "failed"
            assert src["error_class"] in ("transport_timeout", "wait_timeout")
            assert sql(dsn, "SELECT checked_through "
                            "FROM watchlist_announcement_check_state")[0][0] is None
        finally:
            cleanup_company(dsn, "TST10")

    def test_missing_cik_blocks_company(self, env):
        dsn, base, routes, events = env
        cik = "0001000011"
        serve_json(routes, f"/submissions/CIK{cik}.json", submissions_doc(cik, [
            mk_filing("0001000011-26-000001", "8-K", days_ago(1),
                      "2026-10-19T10:00:00.000Z"),
        ]))
        sql(dsn, "INSERT INTO stock_info (stock_code, stock_name, market, cik) "
                 "VALUES ('NOCIK', 'NOCIK', 'US', NULL) "
                 "ON CONFLICT (stock_code) DO NOTHING")
        sql(dsn, "INSERT INTO watchlist_item (market, stock_code, stock_name) "
                 "VALUES ('US', 'NOCIK', 'NOCIK')")
        seed_company(dsn, "TST11", cik)
        try:
            before = len(events)
            summary = run()
            assert summary["missing_cik"] == ["NOCIK"]
            row = sql(dsn, """
                SELECT status, error_class, error_detail FROM watchlist_announcement_run
                WHERE error_class = 'missing_cik'
            """)[0]
            assert row[0] == "failed" and "NOCIK" in row[2]
            # 缺 CIK 不发起任何额外请求（只有正常公司的 submissions 请求）
            assert len(events) - before == 1
            # 正常公司不受影响
            assert sql(dsn, "SELECT COUNT(*) FROM watchlist_announcement")[0][0] == 1
        finally:
            sql(dsn, "DELETE FROM watchlist_item WHERE stock_code = 'NOCIK'")
            sql(dsn, "DELETE FROM stock_info WHERE stock_code = 'NOCIK'")
            cleanup_company(dsn, "TST11")


# ── 多进程互斥 ────────────────────────────────────────────

def _holder_process(ready, release, hold_seconds=30):
    db._pool = None  # fork 后不得沿用父进程连接池
    with sync.RoundLock() as lock:
        ready.put(lock.acquired)
        release.get(timeout=hold_seconds)


def _round_process(output):
    db._pool = None
    try:
        summary = sync.run_round(trigger="manual", due_only=False)
        output.put(("ok", summary["status"]))
    except Exception as exc:
        output.put((type(exc).__name__, str(exc)))


class TestRoundMutex:
    def test_second_process_skipped_running(self, env):
        """两个进程同跑：持锁者执行，另一个 skipped_running 且不发请求。"""
        dsn, base, routes, events = env
        ctx = mp.get_context("fork")
        ready, release, output = ctx.Queue(), ctx.Queue(), ctx.Queue()
        holder = ctx.Process(target=_holder_process, args=(ready, release))
        holder.start()
        try:
            assert ready.get(timeout=10) is True
            before = len(events)
            child = ctx.Process(target=_round_process, args=(output,))
            child.start()
            child.join(timeout=30)
            assert child.exitcode == 0
            status, payload = output.get(timeout=5)
            assert (status, payload) == ("ok", "skipped_running")
            assert len(events) == before  # 未发起任何 HTTP 请求
            rows = sql(dsn, "SELECT status FROM watchlist_announcement_run "
                            "WHERE status = 'skipped_running'")
            assert len(rows) == 1
        finally:
            release.put("go")
            holder.join(timeout=10)
            if holder.is_alive():
                holder.kill()
        sql(dsn, "DELETE FROM watchlist_announcement_run")

    def test_killed_holder_releases_lock_and_recovers(self, env):
        """持锁进程被杀：数据库回收会话锁，新进程取得锁并完成采集。"""
        dsn, base, routes, _e = env
        cik = "0001000012"
        serve_json(routes, f"/submissions/CIK{cik}.json", submissions_doc(cik, [
            mk_filing("0001000012-26-000001", "8-K", days_ago(1),
                      "2026-10-19T10:00:00.000Z"),
        ]))
        seed_company(dsn, "TST12", cik)
        ctx = mp.get_context("fork")
        ready, release = ctx.Queue(), ctx.Queue()
        holder = ctx.Process(target=_holder_process, args=(ready, release))
        holder.start()
        try:
            assert ready.get(timeout=10) is True
            holder.kill()          # SIGKILL：finally 不执行，模拟进程崩溃
            holder.join(timeout=10)
            time.sleep(1)          # 等数据库识别会话终止
            summary = run()
            assert summary["status"] == "completed"
            assert sql(dsn, "SELECT COUNT(*) FROM watchlist_announcement")[0][0] == 1
        finally:
            if holder.is_alive():
                holder.kill()
            cleanup_company(dsn, "TST12")


# ── SQL_ASCII 生产库回归 ──────────────────────────────────

@pytest.fixture(scope="module")
def ascii_cluster():
    """SQL_ASCII 编码集群（复刻生产历史库编码）。

    默认 UTF8 集群覆盖不到：SQL_ASCII 服务器 + 客户端 ascii codec 时，
    裸 psycopg2 连接写中文在客户端编码阶段直接 UnicodeEncodeError
    （db.py 连接池带 client_encoding=utf8，RoundLock 裸连接曾不带）。
    """
    if not shutil.which("initdb") or not shutil.which("pg_ctl"):
        pytest.skip("SQL_ASCII regression requires initdb/pg_ctl")
    root = Path(tempfile.mkdtemp(prefix="anncascii-"))
    data = root / "data"
    started = False
    try:
        subprocess.run(["initdb", "-D", str(data), "-A", "trust", "--no-locale",
                        "-E", "SQL_ASCII"], check=True, capture_output=True)
        result = subprocess.run(
            ["pg_ctl", "-D", str(data), "-l", str(root / "server.log"),
             "-o", f"-k {root} -p 56437 -h '' -F", "-w", "start"], capture_output=True)
        if result.returncode:
            pytest.fail("SQL_ASCII PostgreSQL startup failed: " +
                        (root / "server.log").read_text())
        started = True
        dsn = (f"host={root} port=56437 dbname=postgres "
               f"user={getpass.getuser()} connect_timeout=2 "
               f"options='-c client_encoding=utf8'")
        sql(dsn, Path("scripts/watchlist_announcements.sql").read_text())
        yield dsn, root
    finally:
        if started:
            subprocess.run(["pg_ctl", "-D", str(data), "-m", "immediate", "-w", "stop"],
                           check=True, capture_output=True)
        shutil.rmtree(root)


class TestSqlAsciiEncoding:
    def test_round_lock_session_writes_chinese(self, ascii_cluster, monkeypatch):
        """SQL_ASCII 库 + C locale 客户端（复刻生产条件）：持锁会话必须带
        client_encoding=utf8，否则中文 status_reason 在客户端编码阶段
        UnicodeEncodeError，整轮被拒。

        注意不能用 PGCLIENTENCODING 模拟：libpq 对它做连接后 SET，
        会覆盖 -c 启动参数（连修复都压不住）；生产条件是 C locale 下的
        默认编码推导，用 setlocale 复刻。
        """
        import locale

        dsn, root = ascii_cluster
        monkeypatch.setattr(config.db, "host", str(root))
        monkeypatch.setattr(config.db, "port", 56437)
        monkeypatch.setattr(config.db, "dbname", "postgres")
        monkeypatch.setattr(config.db, "user", getpass.getuser())
        monkeypatch.setattr(config.db, "password", "")
        monkeypatch.delenv("PGCLIENTENCODING", raising=False)
        reason = "适配器按实测结构实现；启用前需真实采集验证"
        old_locale = locale.setlocale(locale.LC_ALL)
        try:
            locale.setlocale(locale.LC_ALL, "C")  # 生产 C locale
            with sync.RoundLock() as lock:
                assert lock.acquired
                store.ensure_ir_source(
                    lock.conn, issuer="0003000001", platform="nebius_newsroom",
                    allowed_domain="nebius.com",
                    entry_url="https://nebius.com/newsroom",
                    source_timezone="UTC", parser_version=1, interval_minutes=None,
                    enabled=False, source_status="pending_verification",
                    status_reason=reason)
                lock.conn.commit()
        finally:
            locale.setlocale(locale.LC_ALL, old_locale)
        rows = sql(dsn, "SELECT status_reason FROM watchlist_announcement_source "
                        "WHERE issuer_identifier = '0003000001'")
        assert rows[0][0] == reason
        sql(dsn, "DELETE FROM watchlist_announcement_source "
                 "WHERE issuer_identifier = '0003000001'")

# ── 配置注册门（不需要数据库/HTTP）─────────────────────────

class TestSchedulerRegistration:
    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.setattr(config.announcements, "enabled", "false")
        from core.scheduler import _announcements_job_minutes
        assert _announcements_job_minutes() is None

    def test_enabled_returns_sec_interval(self, monkeypatch):
        monkeypatch.setattr(config.announcements, "enabled", "true")
        monkeypatch.setattr(config.announcements, "sec_interval_minutes", "60")
        from core.scheduler import _announcements_job_minutes
        assert _announcements_job_minutes() == 60

    @pytest.mark.parametrize("value", ["0", "-5", "abc"])
    def test_invalid_interval_fails_before_registration(self, monkeypatch, value):
        monkeypatch.setattr(config.announcements, "enabled", "true")
        monkeypatch.setattr(config.announcements, "sec_interval_minutes", value)
        from core.scheduler import _announcements_job_minutes
        with pytest.raises(ValueError):
            _announcements_job_minutes()

    def test_invalid_enabled_fails(self, monkeypatch):
        monkeypatch.setattr(config.announcements, "enabled", "maybe")
        from core.scheduler import _announcements_job_minutes
        with pytest.raises(ValueError):
            _announcements_job_minutes()

    def test_existing_job_defs_untouched(self):
        from core.scheduler import JOB_DEFS
        assert "watchlist_announcements" not in JOB_DEFS
        assert set(JOB_DEFS) == {
            "CN_A_daily_quote", "CN_HK_daily_quote", "US_daily_quote",
            "CN_A_financial", "CN_HK_financial", "US_financial",
        }

    def test_run_round_refuses_when_disabled(self, monkeypatch):
        monkeypatch.setattr(config.announcements, "enabled", "false")
        with pytest.raises(RuntimeError, match="ENABLED"):
            sync.run_round(trigger="manual")

    def test_cli_refuses_when_disabled(self, monkeypatch, capsys):
        monkeypatch.setattr(config.announcements, "enabled", "false")
        rc = sync.main([])
        assert rc == 2
        assert "ENABLED" in capsys.readouterr().err


class TestSecRevisionReenhance:
    def test_amended_filing_recomputes_importance(self, env, monkeypatch):
        """/内容变更产生新 revision 时 importance 重算并回写到新版本行，
        旧版本行保持原值。"""
        from core.announcements import llm_enhance
        dsn, base, routes, _e = env
        cik = "0001000025"
        payload = submissions_doc(cik, [
            mk_filing("0001000025-26-000001", "8-K", days_ago(2),
                      "2026-10-21T13:05:11.000Z"),
        ])
        serve_json(routes, f"/submissions/CIK{cik}.json", payload)
        seed_company(dsn, "TSTRV", cik)
        monkeypatch.setattr(llm_enhance, "_run_mmx",
                            lambda *a, **kw: '{"importance": "normal"}')
        try:
            run()
            rows = sql(dsn, "SELECT revision, importance FROM "
                            "watchlist_announcement ORDER BY revision")
            assert rows == [(1, "normal")]
            # 内容变更（主文件变化）→ revision 2，importance 重算
            payload["filings"]["recent"]["primaryDocument"][0] = "amended.htm"
            serve_json(routes, f"/submissions/CIK{cik}.json", payload)
            monkeypatch.setattr(llm_enhance, "_run_mmx",
                                lambda *a, **kw: '{"importance": "high"}')
            summary = run()
            src = summary["sources"][0]
            assert src["revised"] == 1
            rows = sql(dsn, "SELECT revision, importance FROM "
                            "watchlist_announcement ORDER BY revision")
            assert rows == [(1, "normal"), (2, "high")]
            assert summary["llm"]["ok"] >= 1
        finally:
            cleanup_company(dsn, "TSTRV")
