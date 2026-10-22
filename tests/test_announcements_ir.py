"""IR 适配框架测试：离线 fixture + 临时 PostgreSQL 集群。

不访问真实网络：http_get 全部 monkeypatch 到内存 fixture。
覆盖：平台适配器解析（nebius_newsroom 实测结构、q4_press_release 未验证
结构）、受阻来源一等状态、覆盖不足标 partial + 缺口、去重幂等、注册表
单源间隔覆盖、持续失败退避、未知平台失败。
"""
import getpass
import json
import shutil
import subprocess
import tempfile
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import psycopg2
import pytest

import config
import db
from core.announcements import ir_source, sec_source, store, sync

UTC = timezone.utc


def sql(dsn, query, args=()):
    with closing(psycopg2.connect(dsn)) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(query, args)
            return cur.fetchall() if cur.description else None


# ── 离线 fixture 构造 ─────────────────────────────────────

def nebius_page(press_releases, blog_posts=()):
    """按 2026-10-21 实测 __NEXT_DATA__ Apollo 归一化缓存结构构造。"""
    cache = {}
    for pr in press_releases:
        entry = {
            "__typename": "press_releases", "id": pr["id"],
            "name": pr["slug"], "title": pr["title"], "date": pr["date"],
        }
        if pr.get("publication_datetime"):
            entry["publication_datetime"] = pr["publication_datetime"]
        cache[f'press_releases:{{"id":"{pr["id"]}"}}'] = entry
    for bp in blog_posts:
        cache[f'blog_posts:{{"id":"{bp["id"]}"}}'] = {
            "__typename": "blog_posts", "id": bp["id"],
            "name": bp["slug"], "title": bp["title"], "date": bp["date"],
        }
    next_data = {"props": {"pageProps": {"_apolloCache": cache}}}
    return ("<html><body><script id=\"__NEXT_DATA__\" "
            "type=\"application/json\">"
            + json.dumps(next_data) + "</script></body></html>").encode()


def q4_page(items):
    """Q4 GetPressReleaseList 结构（未真实验证，按平台公开结构构造）。"""
    return json.dumps({"GetPressReleaseListResult": items}).encode()


def days_ago(n):
    return datetime.now(UTC).date() - timedelta(days=n)


# ── 适配器单元测试（无数据库）──────────────────────────────

class TestNebiusAdapter:
    adapter = ir_source.NebiusNewsroomAdapter()
    entry = "https://nebius.com/newsroom"

    def test_parse_press_releases_excludes_blog(self):
        body = nebius_page(
            [{"id": "1", "slug": "nbis-q3-results", "title": "Q3 Results",
              "date": "2026-10-15",
              "publication_datetime": "2026-10-15T11:00:00.000Z"},
             {"id": "2", "slug": "nbis-event", "title": "Event",
              "date": "2026-10-10"}],
            blog_posts=[{"id": "9", "slug": "academy-course",
                         "title": "Academy", "date": "2026-10-16"}])
        items = self.adapter.parse_listing(body, self.entry)
        assert len(items) == 2                      # blog_posts 不属于官方新闻发布
        by_slug = {it.source_record_id: it for it in items}
        first = by_slug["nbis-q3-results"]
        assert first.url == "https://nebius.com/newsroom/nbis-q3-results"
        assert first.published_at == datetime(2026, 10, 15, 11, 0, tzinfo=UTC)
        assert first.precision == "second"
        assert first.published_raw == "2026-10-15T11:00:00.000Z"
        # 仅日期条目：published_at 置 NULL，精度 day，不补午夜
        second = by_slug["nbis-event"]
        assert second.published_at is None
        assert second.precision == "day"
        assert second.published_raw == "2026-10-10"

    def test_missing_next_data_is_parse_error(self):
        with pytest.raises(ir_source.IrFetchError) as exc:
            self.adapter.parse_listing(b"<html>no data island</html>", self.entry)
        assert exc.value.error_class == "parse_error"


class TestQ4Adapter:
    adapter = ir_source.Q4PressReleaseAdapter()
    entry = "https://ir.example.com/feed/PressRelease.svc/GetPressReleaseList"

    def test_parse_items(self):
        body = q4_page([
            {"PressReleaseId": "p-100", "Title": "Results",
             "LinkToDetailPage": "https://ir.example.com/news/p-100",
             "PressReleaseDate": "/Date(1760572800000)/"},   # 2025-10-16 UTC
            {"PressReleaseId": "p-101", "Title": "Guidance",
             "LinkToDetailPage": "https://ir.example.com/news/p-101",
             "PressReleaseDate": "2026-10-01T12:30:00Z"},
        ])
        items = self.adapter.parse_listing(body, self.entry)
        assert [it.source_record_id for it in items] == ["p-100", "p-101"]
        assert items[0].published_at == datetime(2025, 10, 16, tzinfo=UTC)
        assert items[1].published_at == datetime(2026, 10, 1, 12, 30, tzinfo=UTC)

    def test_missing_result_key_is_parse_error(self):
        with pytest.raises(ir_source.IrFetchError) as exc:
            self.adapter.parse_listing(b"{}", self.entry)
        assert exc.value.error_class == "parse_error"

    def test_pagination_urls(self):
        assert self.adapter.page_url(self.entry, 0) == self.entry
        assert "pageNumber=2" in self.adapter.page_url(self.entry, 1)


class TestUrlValidationAndNormalize:
    def test_rejects_non_https_and_foreign_hosts(self):
        for bad in ("http://nebius.com/newsroom",
                    "https://evil.com/newsroom",
                    "https://127.0.0.1/x",
                    "https://nebius.com.evil.com/x"):
            with pytest.raises(ir_source.IrFetchError) as exc:
                ir_source._validate_url(bad, "nebius.com")
            assert exc.value.error_class == "url_rejected"

    def test_accepts_subdomain(self):
        ir_source._validate_url("https://www.nebius.com/newsroom", "nebius.com")

    def test_normalize_url_strips_tracking(self):
        assert ir_source.normalize_url(
            "HTTPS://Nebius.com/newsroom/x/?utm_source=rss&b=1#frag"
        ) == "https://nebius.com/newsroom/x?b=1"


# ── 集成测试（临时 PG 集群 + monkeypatch http_get）─────────

@pytest.fixture(scope="module")
def cluster():
    if not shutil.which("initdb") or not shutil.which("pg_ctl"):
        pytest.skip("IR tests require initdb/pg_ctl")
    root = Path(tempfile.mkdtemp(prefix="anncir-"))
    data = root / "data"
    started = False
    try:
        subprocess.run(["initdb", "-D", str(data), "-A", "trust", "--no-locale",
                        "-E", "UTF8"], check=True, capture_output=True)
        result = subprocess.run(
            ["pg_ctl", "-D", str(data), "-l", str(root / "server.log"),
             "-o", f"-k {root} -p 56435 -h '' -F", "-w", "start"], capture_output=True)
        if result.returncode:
            pytest.fail("Temporary PostgreSQL startup failed: " +
                        (root / "server.log").read_text())
        started = True
        dsn = (f"host={root} port=56435 dbname=postgres "
               f"user={getpass.getuser()} connect_timeout=2")
        sql(dsn, Path("scripts/watchlist_announcements.sql").read_text())
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
        yield dsn
    finally:
        if started:
            subprocess.run(["pg_ctl", "-D", str(data), "-m", "immediate", "-w", "stop"],
                           check=True, capture_output=True)
        shutil.rmtree(root)


@pytest.fixture
def env(cluster, monkeypatch):
    dsn = cluster
    host = dsn.split()[0].split("=")[1]
    monkeypatch.setattr(config.db, "host", host)
    monkeypatch.setattr(config.db, "port", 56435)
    monkeypatch.setattr(config.db, "dbname", "postgres")
    monkeypatch.setattr(config.db, "user", getpass.getuser())
    monkeypatch.setattr(config.db, "password", "")
    db.close_pool()
    db._pool = None
    monkeypatch.setattr(config.announcements, "enabled", "true")
    # LLM 增强离线：测试不调用真实 mmx（增强失败路径 = 字段留 NULL）
    from core.announcements import llm_enhance
    monkeypatch.setattr(llm_enhance, "_run_mmx",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            llm_enhance.EnhanceError("test: llm offline")))
    monkeypatch.setattr(config.announcements, "sec_interval_minutes", "60")
    monkeypatch.setattr(config.announcements, "ir_interval_minutes", "60")

    routes = {}
    calls = []

    def fake_http_get(url, *, allowed_domain, **_kw):
        calls.append(url)
        body = routes.get(url)
        if body is None:
            raise ir_source.IrFetchError(f"IR HTTP 403（受阻）: {url}",
                                         error_class="http_403")
        return body

    monkeypatch.setattr(ir_source, "http_get", fake_http_get)
    # IR 测试聚焦 IR 路径：SEC submissions 打桩为空列表（本集群无 SEC gate）
    monkeypatch.setattr(
        sec_source, "fetch_submissions",
        lambda cik, **_kw: {"cik": str(int(cik)), "filings": {"recent": {
            "accessionNumber": [], "form": [], "filingDate": [],
            "acceptanceDateTime": [], "primaryDocument": []}, "files": []}})
    yield dsn, routes, calls
    db.close_pool()
    db._pool = None


def seed_company(dsn, code, cik):
    sql(dsn, "INSERT INTO stock_info (stock_code, stock_name, market, cik) "
             "VALUES (%s, %s, 'US', %s) ON CONFLICT (stock_code) DO NOTHING",
        (code, code, cik))
    sql(dsn, "INSERT INTO watchlist_item (market, stock_code, stock_name) "
             "VALUES ('US', %s, %s)", (code, code))


def cleanup(dsn):
    for table in ("watchlist_announcement_security", "watchlist_announcement",
                  "watchlist_announcement_run", "watchlist_announcement_check_state",
                  "watchlist_announcement_recovery", "watchlist_announcement_gap",
                  "watchlist_announcement_source", "watchlist_item", "stock_info"):
        sql(dsn, f"DELETE FROM {table}")


def enable_nbis(dsn):
    sql(dsn, "UPDATE watchlist_announcement_source SET enabled = true, "
             "source_status = 'enabled' WHERE platform = 'nebius_newsroom'")


def nbis_items(days_list):
    return [{"id": str(i), "slug": f"nbis-news-{i}", "title": f"News {i}",
             "date": days_ago(d).isoformat(),
             "publication_datetime": None}
            for i, d in enumerate(days_list)]


def run(**kw):
    params = dict(trigger="manual", due_only=False)
    params.update(kw)
    return sync.run_round(**params)


class TestIrRoundIntegration:
    def test_blocked_sources_are_first_class(self, env):
        """MU/WDC 注册为 blocked + 原因，不抓取、不计入成功率分母。"""
        dsn, routes, calls = env
        seed_company(dsn, "MU", "0000723450")
        seed_company(dsn, "WDC", "0000108040")
        try:
            summary = run()
            inactive = {s["issuer"]: s for s in summary["ir_inactive"]}
            assert set(inactive) == {"0000723450", "0000108040"}
            assert all(s["status"] == "blocked" for s in inactive.values())
            assert all(s["reason"] for s in inactive.values())
            assert not calls                       # 受阻来源零请求
            assert summary["status"] == "completed"  # 不进成功率分母
            sources = sql(dsn, "SELECT source_status, enabled FROM "
                               "watchlist_announcement_source WHERE source_kind='ir' "
                               "ORDER BY issuer_identifier")
            assert [(r[0], r[1]) for r in sources] == [("blocked", False)] * 2
        finally:
            cleanup(dsn)

    def test_nbis_backfill_full_coverage(self, env):
        dsn, routes, _calls = env
        seed_company(dsn, "NBIS", "0001713445")
        routes["https://nebius.com/newsroom"] = nebius_page(
            nbis_items([1, 10, 45, 95]))   # 最旧 95 天 > 90 天回补窗口
        try:
            summary = run()
            # 首轮自动注册为 pending_verification，不抓取
            assert summary["ir_inactive"][0]["status"] == "pending_verification"
            enable_nbis(dsn)
            summary = run()
            assert summary["status"] == "completed"
            src = [s for s in summary["sources"] if s.get("platform")][0]
            assert src["status"] == "success_new" and src["inserted"] == 3  # 95 天条目在窗口外
            rows = sql(dsn, """
                SELECT source_record_id, title, form_type, published_at,
                       published_at_precision, source_timezone, original_url
                FROM watchlist_announcement ORDER BY source_record_id
            """)
            assert len(rows) == 3
            assert all(r[2] == "press_release" for r in rows)
            assert all(r[4] == "day" and r[3] is None for r in rows)  # 仅日期
            assert all(r[5] == "UTC" for r in rows)
            assert rows[0][6].startswith("https://nebius.com/newsroom/")
            state = sql(dsn, "SELECT checked_through, last_attempt_status FROM "
                             "watchlist_announcement_check_state WHERE source_id IN "
                             "(SELECT source_id FROM watchlist_announcement_source "
                             "WHERE source_kind='ir')")[0]
            assert state[0] is not None and state[1] == "success_new"
            assert sql(dsn, "SELECT COUNT(*) FROM watchlist_announcement_gap")[0][0] == 0
        finally:
            cleanup(dsn)

    def test_nbis_backfill_shallow_history_marks_partial(self, env):
        """列表历史不足 90 天：记实际覆盖范围 + 缺口，标 partial，
        检查点不越过缺口；下轮仍 partial。"""
        dsn, routes, _calls = env
        seed_company(dsn, "NBIS", "0001713445")
        routes["https://nebius.com/newsroom"] = nebius_page(
            nbis_items([1, 10, 30]))       # 最旧 30 天 < 90 天窗口
        try:
            run()
            enable_nbis(dsn)
            summary = run()
            src = [s for s in summary["sources"] if s.get("platform")][0]
            assert src["status"] == "partial"
            gaps = sql(dsn, "SELECT reason, resolved_at FROM "
                            "watchlist_announcement_gap")
            assert len(gaps) == 1 and gaps[0][0] == "history_unavailable"
            state = sql(dsn, "SELECT checked_through, last_success_at, "
                             "last_attempt_status, covered_ranges FROM "
                             "watchlist_announcement_check_state WHERE source_id IN "
                             "(SELECT source_id FROM watchlist_announcement_source "
                             "WHERE source_kind='ir')")[0]
            assert state[0] is None and state[1] is None
            assert state[2] == "partial"
            assert state[3] is not None    # 实际覆盖范围已记录
            summary2 = run()
            src2 = [s for s in summary2["sources"] if s.get("platform")][0]
            assert src2["status"] == "partial"   # 不退化成 success_empty
        finally:
            cleanup(dsn)

    def test_empty_listing_backfill_has_no_coverage_evidence(self, env):
        dsn, routes, _calls = env
        seed_company(dsn, "NBIS", "0001713445")
        routes["https://nebius.com/newsroom"] = nebius_page([])
        try:
            run()
            enable_nbis(dsn)
            summary = run()
            src = [s for s in summary["sources"] if s.get("platform")][0]
            assert src["status"] == "partial"    # 空列表不能宣称完整覆盖
            assert sql(dsn, "SELECT COUNT(*) FROM watchlist_announcement_gap")[0][0] == 1
        finally:
            cleanup(dsn)

    def test_ir_dedup_rerun_and_incremental(self, env):
        dsn, routes, _calls = env
        seed_company(dsn, "NBIS", "0001713445")
        routes["https://nebius.com/newsroom"] = nebius_page(
            nbis_items([1, 5, 95]))
        try:
            run()
            enable_nbis(dsn)
            run()
            first = sql(dsn, "SELECT first_observed_at FROM watchlist_announcement "
                             "ORDER BY source_record_id")
            assert len(first) == 2  # 95 天条目在 90 天回补窗口外
            summary = run()   # 增量重抓
            src = [s for s in summary["sources"] if s.get("platform")][0]
            assert src["status"] == "success_empty"
            second = sql(dsn, "SELECT first_observed_at FROM watchlist_announcement "
                              "ORDER BY source_record_id")
            assert [r[0] for r in second] == [r[0] for r in first]
            # 新增一条新闻 → success_new
            routes["https://nebius.com/newsroom"] = nebius_page(
                nbis_items([0, 1, 5, 95]))
            summary = run()
            src = [s for s in summary["sources"] if s.get("platform")][0]
            assert src["status"] == "success_new" and src["inserted"] == 1
        finally:
            cleanup(dsn)

    def test_failure_backoff_extends_next_due(self, env):
        """持续失败明确退避：连续失败次数越多，下次到期推得越远。"""
        dsn, routes, _calls = env
        seed_company(dsn, "NBIS", "0001713445")
        # 不注册路由 → fake http_get 抛 http_403
        try:
            run()
            enable_nbis(dsn)
            before = datetime.now(UTC)
            run()
            due1 = sql(dsn, "SELECT next_due_at, last_attempt_status, "
                            "last_error_class FROM watchlist_announcement_check_state "
                            "WHERE source_id IN (SELECT source_id FROM "
                            "watchlist_announcement_source WHERE source_kind='ir')")[0]
            assert due1[1] == "failed" and due1[2] == "http_403"
            assert due1[0] >= before + timedelta(minutes=55)   # 首次失败 ≈ 1×间隔
            run()
            due2 = sql(dsn, "SELECT next_due_at FROM "
                            "watchlist_announcement_check_state WHERE source_id IN "
                            "(SELECT source_id FROM watchlist_announcement_source "
                            "WHERE source_kind='ir')")[0][0]
            assert due2 >= before + timedelta(minutes=115)     # 连续失败退避 ≈ 2×
            assert sql(dsn, "SELECT checked_through FROM "
                            "watchlist_announcement_check_state WHERE source_id IN "
                            "(SELECT source_id FROM watchlist_announcement_source "
                            "WHERE source_kind='ir')")[0][0] is None
        finally:
            cleanup(dsn)

    def test_registry_interval_override(self, env):
        """注册表单源覆盖间隔优先于配置默认值。"""
        dsn, routes, _calls = env
        seed_company(dsn, "NBIS", "0001713445")
        routes["https://nebius.com/newsroom"] = nebius_page(nbis_items([95]))
        try:
            run()
            enable_nbis(dsn)
            sql(dsn, "UPDATE watchlist_announcement_source "
                     "SET interval_minutes = 15 WHERE platform = 'nebius_newsroom'")
            before = datetime.now(UTC)
            run()
            state = sql(dsn, "SELECT effective_interval_minutes, next_due_at FROM "
                             "watchlist_announcement_check_state WHERE source_id IN "
                             "(SELECT source_id FROM watchlist_announcement_source "
                             "WHERE source_kind='ir')")[0]
            assert state[0] == 15
            assert state[1] <= before + timedelta(minutes=16)
        finally:
            cleanup(dsn)

    def test_unknown_platform_fails_loudly(self, env):
        """注册表指向无适配器的平台：落 failed/unknown_platform，不抓取。"""
        dsn, routes, calls = env
        seed_company(dsn, "NBIS", "0001713445")
        routes["https://nebius.com/newsroom"] = nebius_page(nbis_items([1]))
        try:
            run()
            sql(dsn, "UPDATE watchlist_announcement_source SET enabled = true, "
                     "source_status = 'enabled', platform = 'unknown_platform' "
                     "WHERE platform = 'nebius_newsroom'")
            before = len(calls)
            summary = run()
            src = [s for s in summary["sources"]
                   if s.get("platform") == "unknown_platform"][0]
            assert src["status"] == "failed"
            assert src["error_class"] == "unknown_platform"
            assert len(calls) == before
        finally:
            cleanup(dsn)

# ── 边界修复回归（对应 codex 复核七项）─────────────────────

class TestIrWatchRemoval:
    def test_unwatch_stops_ir_polling(self, env):
        """取消关注后 IR 来源停止主动轮询，历史记录保留。"""
        dsn, routes, calls = env
        seed_company(dsn, "NBIS", "0001713445")
        routes["https://nebius.com/newsroom"] = nebius_page(nbis_items([1, 95]))
        try:
            run()
            enable_nbis(dsn)
            run()
            assert sql(dsn, "SELECT COUNT(*) FROM watchlist_announcement")[0][0] == 1
            sql(dsn, "DELETE FROM watchlist_item WHERE stock_code = 'NBIS'")
            before = len(calls)
            summary = run()
            assert len(calls) == before              # 不再轮询
            assert sql(dsn, "SELECT COUNT(*) FROM watchlist_announcement")[0][0] == 1
        finally:
            cleanup(dsn)


class TestIrBoundaryFixes:
    def test_partial_backfill_still_collects_new_publications(self, env, monkeypatch):
        """P1：回补 partial 后新公告照常入库——扫描上界始终为当前时间。"""
        dsn, routes, _ = env
        seed_company(dsn, "NBIS", "0001713445")
        routes["https://nebius.com/newsroom"] = nebius_page(nbis_items([1, 30]))
        try:
            run()
            enable_nbis(dsn)
            assert [s for s in run()["sources"]
                    if s.get("platform")][0]["status"] == "partial"
            later = datetime.now(UTC) + timedelta(days=5)

            class Clock(datetime):
                @classmethod
                def now(cls, tz=None):
                    return later.astimezone(tz) if tz else later.replace(tzinfo=None)
            monkeypatch.setattr(sync, "datetime", Clock)
            rows = nbis_items([1, 30])
            rows.append({"id": "new", "slug": "new-after-backfill",
                         "title": "New", "date": later.date().isoformat()})
            routes["https://nebius.com/newsroom"] = nebius_page(rows)
            run()
            assert sql(dsn, "SELECT 1 FROM watchlist_announcement "
                            "WHERE source_record_id='new-after-backfill'")
        finally:
            cleanup(dsn)

    def test_shallow_incremental_keeps_checkpoint(self, env):
        """P1：停采 10 天、列表只返回今天 → partial，checked_through 不动。"""
        dsn, routes, _ = env
        seed_company(dsn, "NBIS", "0001713445")
        routes["https://nebius.com/newsroom"] = nebius_page(nbis_items([1, 95]))
        try:
            run()
            enable_nbis(dsn)
            run()  # 完整回补
            old = datetime.now(UTC) - timedelta(days=10)
            sql(dsn, "UPDATE watchlist_announcement_check_state "
                     "SET checked_through = %s WHERE source_id IN "
                     "(SELECT source_id FROM watchlist_announcement_source "
                     "WHERE source_kind='ir')", (old,))
            routes["https://nebius.com/newsroom"] = nebius_page(nbis_items([0]))
            src = [s for s in run()["sources"] if s.get("platform")][0]
            assert src["status"] == "partial"
            state = sql(dsn, "SELECT checked_through FROM "
                             "watchlist_announcement_check_state WHERE source_id IN "
                             "(SELECT source_id FROM watchlist_announcement_source "
                             "WHERE source_kind='ir')")[0][0]
            assert state == old
            assert sql(dsn, "SELECT COUNT(*) FROM watchlist_announcement_gap "
                            "WHERE resolved_at IS NULL")[0][0] == 1
        finally:
            cleanup(dsn)

    def test_open_gap_prevents_success_even_if_listing_deepens(self, env):
        """P2：缺口未解决时，覆盖变完整也只报 partial。"""
        dsn, routes, _ = env
        seed_company(dsn, "NBIS", "0001713445")
        routes["https://nebius.com/newsroom"] = nebius_page(nbis_items([1, 30]))
        try:
            run()
            enable_nbis(dsn)
            run()
            routes["https://nebius.com/newsroom"] = nebius_page(nbis_items([1, 95]))
            src = [s for s in run()["sources"] if s.get("platform")][0]
            gaps = sql(dsn, "SELECT COUNT(*) FROM watchlist_announcement_gap "
                            "WHERE resolved_at IS NULL")[0][0]
            assert gaps == 1
            assert not src["status"].startswith("success")
            assert sql(dsn, "SELECT checked_through FROM "
                            "watchlist_announcement_check_state WHERE source_id IN "
                            "(SELECT source_id FROM watchlist_announcement_source "
                            "WHERE source_kind='ir')")[0][0] is None
        finally:
            cleanup(dsn)

    def test_pagination_respects_round_budget(self, env, monkeypatch):
        """P2：整轮预算耗尽即停止分页；页数不超上限。"""
        from types import SimpleNamespace
        dsn, _routes, _calls = env
        seed_company(dsn, "NBIS", "0001713445")
        run()
        sql(dsn, "UPDATE watchlist_announcement_source SET enabled = true, "
                 "source_status = 'enabled', platform = 'q4_press_release' "
                 "WHERE source_kind = 'ir'")
        clock, fetched = [0.0], []
        monkeypatch.setattr(sync, "time",
                            SimpleNamespace(monotonic=lambda: clock[0]))

        def fetch(url, **_kw):
            fetched.append(url)
            clock[0] += 3.0
            return q4_page([{"PressReleaseId": str(len(fetched)), "Title": "R",
                             "PressReleaseDate": datetime.now(UTC).isoformat(),
                             "LinkToDetailPage": "https://ir.example.com/x"}])
        monkeypatch.setattr(ir_source, "http_get", fetch)
        try:
            run(round_max_seconds=2)
            assert len(fetched) == 1
        finally:
            cleanup(dsn)

    def test_blocked_with_enabled_flag_never_fetches(self, env):
        """P2：enabled=true + source_status='blocked' → 零请求且入 ir_inactive。"""
        dsn, routes, calls = env
        seed_company(dsn, "NBIS", "0001713445")
        routes["https://nebius.com/newsroom"] = nebius_page(nbis_items([1, 95]))
        try:
            run()
            sql(dsn, "UPDATE watchlist_announcement_source SET enabled = true, "
                     "source_status = 'blocked' WHERE source_kind = 'ir'")
            before = len(calls)
            summary = run()
            assert len(calls) == before
            assert summary["ir_inactive"]
            assert summary["ir_inactive"][0]["status"] == "blocked"
        finally:
            cleanup(dsn)


class TestSchedulerIrInterval:
    def test_wakeup_covers_shortest_source_interval(self, monkeypatch):
        """P2：调度唤醒间隔 = min(SEC, IR)，IR 15 分钟不被 60 分钟拖住。"""
        monkeypatch.setattr(config.announcements, "enabled", "true")
        monkeypatch.setattr(config.announcements, "sec_interval_minutes", "60")
        monkeypatch.setattr(config.announcements, "ir_interval_minutes", "15")
        from core.scheduler import _announcements_job_minutes
        assert _announcements_job_minutes() == 15


class TestPrivateAddressRejection:
    def test_ip_literal_rejected_even_if_registered(self):
        """P2：allowed_domain 注册为 127.0.0.1 不能放行 IP 字面量。"""
        with pytest.raises(ir_source.IrFetchError):
            ir_source._validate_url("https://127.0.0.1/list", "127.0.0.1")

    def test_private_resolution_rejected(self, monkeypatch):
        """域名解析到内网地址同样拒绝（离线：monkeypatch 解析结果）。"""
        monkeypatch.setattr(ir_source, "_resolve_ips",
                            lambda host: {"10.0.0.1"})
        with pytest.raises(ir_source.IrFetchError) as exc:
            ir_source.http_get("https://nebius.com/newsroom",
                               allowed_domain="nebius.com")
        assert exc.value.error_class == "url_rejected"

