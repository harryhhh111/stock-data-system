"""媒体来源（Google News RSS / Finnhub）采集测试：离线 fixture + 临时 PG。

RSS XML 与 Finnhub JSON 用线 A 归档证据快照（docs/evidence/
watchlist_daily_news/）；http_get 全部 monkeypatch，不访问真实网络。
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
from core.announcements import ir_source, media_source, sec_source, store, sync

UTC = timezone.utc
EVIDENCE = Path("docs/evidence/watchlist_daily_news")


def sql(dsn, query, args=()):
    with closing(psycopg2.connect(dsn)) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(query, args)
            return cur.fetchall() if cur.description else None


# ── 解析与过滤（无数据库）─────────────────────────────────

class TestRssParsing:
    def test_parse_real_fixture(self):
        items = media_source.parse_google_rss(
            (EVIDENCE / "gn_2.xml").read_bytes())
        assert len(items) == 100
        item = items[0]
        assert item.source_record_id            # guid 身份
        assert item.published_at is not None
        assert item.published_at.tzinfo is not None
        assert item.precision == "second"
        assert item.source_name                 # 媒体名（<source> 标签）

    def test_guid_fallback_to_normalized_link(self):
        xml = b"""<?xml version="1.0"?><rss><channel><item>
        <title>Test News - Reuters</title>
        <link>HTTPS://Example.COM/a/?utm_source=x&amp;b=1#f</link>
        <pubDate>Wed, 21 Oct 2026 10:00:00 GMT</pubDate>
        <source>Reuters</source></item></channel></rss>"""
        items = media_source.parse_google_rss(xml)
        assert len(items) == 1
        assert items[0].source_record_id == "https://example.com/a?b=1"
        assert items[0].published_at == datetime(2026, 10, 21, 10, 0, tzinfo=UTC)

    def test_broken_xml_is_parse_error(self):
        with pytest.raises(ir_source.IrFetchError) as exc:
            media_source.parse_google_rss(b"<rss><channel><item>")
        assert exc.value.error_class == "parse_error"

    def test_google_url_query_shape(self):
        url = media_source.google_rss_url("Micron Technology")
        assert url.startswith(media_source.GOOGLE_NEWS_RSS_ENTRY)
        assert "when%3A14d" in url and "Micron" in url


class TestFinnhubParsing:
    def test_parse_real_fixture(self):
        items = media_source.parse_finnhub(
            (EVIDENCE / "fh_NBIS.json").read_bytes(), "NBIS")
        assert len(items) == 129
        item = items[0]
        assert item.source_record_id.isdigit()  # 稳定 id
        assert item.published_at is not None and item.precision == "second"
        assert item.summary                     # 摘要存入 excerpt

    def test_error_response_is_not_empty_list(self):
        """401 {"error":...} 明确失败，不当空列表（fh_bad.json 对照件）。"""
        with pytest.raises(ir_source.IrFetchError) as exc:
            media_source.parse_finnhub(
                (EVIDENCE / "fh_bad.json").read_bytes(), "MU")
        assert exc.value.error_class == "parse_error"

    def test_missing_api_key_blocks(self, monkeypatch):
        monkeypatch.setattr(config.finnhub, "api_key", "")
        with pytest.raises(ir_source.IrFetchError) as exc:
            media_source.finnhub_headers()
        assert exc.value.error_class == "missing_api_key"

    def test_from_date_is_yesterday(self):
        url = media_source.finnhub_url("MU", date(2026, 10, 22))
        assert "from=2026-10-21" in url and "to=2026-10-22" in url
        assert "token" not in url              # 凭据不进 URL


class TestCompanyFilter:
    terms = ["Micron Technology", "Micron", "MU"]

    def test_word_boundary(self):
        assert media_source.matches_company("Micron raises buyback", "", self.terms)
        assert media_source.matches_company("MU stock rises", "", self.terms)
        # 词边界：IMMUNE/COMMUNITY 里的 "mu" 不算命中
        assert not media_source.matches_company("Immune therapy news", "", self.terms)
        assert not media_source.matches_company("Community event", "", self.terms)


# ── 采集集成（临时 PG 集群）───────────────────────────────

@pytest.fixture(scope="module")
def cluster():
    if not shutil.which("initdb") or not shutil.which("pg_ctl"):
        pytest.skip("media tests require initdb/pg_ctl")
    root = Path(tempfile.mkdtemp(prefix="anncmedia-"))
    data = root / "data"
    started = False
    try:
        subprocess.run(["initdb", "-D", str(data), "-A", "trust", "--no-locale",
                        "-E", "UTF8"], check=True, capture_output=True)
        result = subprocess.run(
            ["pg_ctl", "-D", str(data), "-l", str(root / "server.log"),
             "-o", f"-k {root} -p 56438 -h '' -F", "-w", "start"], capture_output=True)
        if result.returncode:
            pytest.fail("Temporary PostgreSQL startup failed: " +
                        (root / "server.log").read_text())
        started = True
        dsn = (f"host={root} port=56438 dbname=postgres "
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
    monkeypatch.setattr(config.db, "port", 56438)
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
    monkeypatch.setattr(config.announcements, "media_interval_minutes", "360")
    monkeypatch.setattr(config.finnhub, "api_key", "test-key-not-real")

    calls = []

    def fake_http_get(url, *, allowed_domain, extra_headers=None, **_kw):
        calls.append(url)
        if "news.google.com" in url:
            return (EVIDENCE / "gn_0.xml").read_bytes()
        if "finnhub.io" in url:
            assert extra_headers and extra_headers.get("X-Finnhub-Token")
            assert "token" not in url
            return (EVIDENCE / "fh_MU.json").read_bytes()
        raise ir_source.IrFetchError(f"未注册路由: {url}", error_class="http_404")

    monkeypatch.setattr(ir_source, "http_get", fake_http_get)
    # SEC 路径打桩为空（本集群无 SEC gate）
    monkeypatch.setattr(
        sec_source, "fetch_submissions",
        lambda cik, **_kw: {"cik": str(int(cik)), "filings": {"recent": {
            "accessionNumber": [], "form": [], "filingDate": [],
            "acceptanceDateTime": [], "primaryDocument": []}, "files": []}})
    yield dsn, calls
    db.close_pool()
    db._pool = None
    for table in ("watchlist_announcement_security", "watchlist_announcement",
                  "watchlist_announcement_run", "watchlist_announcement_check_state",
                  "watchlist_announcement_recovery", "watchlist_announcement_gap",
                  "watchlist_announcement_source", "watchlist_item", "stock_info"):
        sql(cluster, f"DELETE FROM {table}")


def seed_mu(dsn):
    sql(dsn, "INSERT INTO stock_info (stock_code, market, cik) "
             "VALUES ('MU', 'US', '0000723125') ON CONFLICT DO NOTHING")
    sql(dsn, "INSERT INTO watchlist_item (market, stock_code, stock_name) "
             "VALUES ('US', 'MU', 'Micron')")


def enable_media(dsn):
    sql(dsn, "UPDATE watchlist_announcement_source SET enabled = true, "
             "source_status = 'enabled' WHERE source_kind = 'media'")


def run(**kw):
    params = dict(trigger="manual", due_only=False)
    params.update(kw)
    return sync.run_round(**params)


class TestMediaRound:
    def test_media_sources_registered_disabled_by_default(self, env):
        dsn, calls = env
        seed_mu(dsn)
        summary = run()
        media = sql(dsn, "SELECT platform, enabled, source_status FROM "
                         "watchlist_announcement_source WHERE source_kind='media' "
                         "ORDER BY platform")
        assert [(r[0], r[1], r[2]) for r in media] == [
            ("finnhub", False, "pending_verification"),
            ("google_news_rss", False, "pending_verification")]
        assert not calls  # 默认不启用 → 零请求
        assert len(summary["media_inactive"]) == 2
        rows = sql(dsn, "SELECT query_config FROM watchlist_announcement_source "
                        "WHERE source_kind='media' AND platform='google_news_rss'")
        cfg = json.loads(rows[0][0])
        assert cfg["google_query"] == "Micron Technology"
        assert "Micron" in cfg["match_terms"]

    def test_collect_filter_dedup_and_fields(self, env):
        dsn, calls = env
        seed_mu(dsn)
        run()                # 注册媒体源（pending）
        enable_media(dsn)
        summary = run()
        media_results = [s for s in summary["sources"]
                         if s.get("platform") in media_source.MEDIA_PLATFORMS]
        assert len(media_results) == 2
        assert all(s["status"] == "success_new" for s in media_results)
        # 每股两个来源，每源一轮一次请求
        assert len(calls) == 2

        rows = sql(dsn, """
            SELECT source_kind, content_nature, relation_reason, media_source,
                   is_backfill FROM watchlist_announcement a
            JOIN watchlist_announcement_source s USING (source_id)
            LIMIT 5
        """)
        assert rows
        for kind, nature, reason, msrc, _bf in rows:
            assert kind == "media"
            assert nature == "unclassified"
            assert reason                       # ticker 直连理由
        # 过滤丢弃计数落运行记录（可见、不静默）
        run_rows = sql(dsn, "SELECT dropped_count, fetched_count, inserted_count "
                            "FROM watchlist_announcement_run WHERE source_id IS NOT NULL "
                            "AND dropped_count IS NOT NULL ORDER BY id")
        assert run_rows
        for dropped, fetched, inserted in run_rows:
            assert dropped + inserted <= fetched
        # 相关率过滤确实在工作（Finnhub 线 A 实测相关率 60~70%，必有丢弃）
        assert any(dropped > 0 for dropped, _, _ in run_rows)

        # 重跑幂等：不新增、首次发现时间不变
        first = sql(dsn, "SELECT COUNT(*), MIN(first_observed_at) FROM "
                         "watchlist_announcement")
        summary2 = run()
        media2 = [s for s in summary2["sources"]
                  if s.get("platform") in media_source.MEDIA_PLATFORMS]
        assert all(s["inserted"] == 0 and s["status"] == "success_empty"
                   for s in media2)
        second = sql(dsn, "SELECT COUNT(*), MIN(first_observed_at) FROM "
                          "watchlist_announcement")
        assert second[0][0] == first[0][0] and second[0][1] == first[0][1]

    def test_media_failure_backoff_and_missing_key(self, env, monkeypatch):
        dsn, calls = env
        seed_mu(dsn)
        run()
        enable_media(dsn)
        monkeypatch.setattr(config.finnhub, "api_key", "")  # key 缺失
        summary = run()
        finnhub = next(s for s in summary["sources"]
                       if s.get("platform") == "finnhub")
        assert finnhub["status"] == "failed"
        assert finnhub["error_class"] == "missing_api_key"
        google = next(s for s in summary["sources"]
                      if s.get("platform") == "google_news_rss")
        assert google["status"] == "success_new"   # 单源失败不影响其他源


class TestLlmEnhanceIntegration:
    def test_media_rows_get_zh_fields(self, env, monkeypatch):
        """增强成功：媒体条目入库带中文标题/摘要/importance。"""
        from core.announcements import llm_enhance
        dsn, _calls = env
        seed_mu(dsn)
        monkeypatch.setattr(llm_enhance, "_run_mmx", lambda *a, **kw:
                            '{"title_zh": "中文标题", "summary_zh": "中文摘要",'
                            ' "importance": "high"}')
        run()
        enable_media(dsn)
        summary = run()
        media = [s for s in summary["sources"]
                 if s.get("platform") in media_source.MEDIA_PLATFORMS]
        assert all(s.get("llm_ok", 0) > 0 for s in media)
        row = sql(dsn, "SELECT title_zh, summary_zh, importance FROM "
                       "watchlist_announcement WHERE title_zh IS NOT NULL "
                       "LIMIT 1")
        assert row and row[0] == ("中文标题", "中文摘要", "high")

    def test_enhance_failure_keeps_row_with_null(self, env, monkeypatch):
        """增强失败：条目正常入库、字段 NULL、计数可见（不静默）。"""
        from core.announcements import llm_enhance
        dsn, _calls = env
        seed_mu(dsn)

        def boom(*_a, **_kw):
            raise llm_enhance.EnhanceError("test: mmx down")
        monkeypatch.setattr(llm_enhance, "_run_mmx", boom)
        run()
        enable_media(dsn)
        summary = run()
        media = [s for s in summary["sources"]
                 if s.get("platform") in media_source.MEDIA_PLATFORMS]
        assert all(s["status"] == "success_new" for s in media)  # 入库不受影响
        assert all(s.get("llm_failed", 0) > 0 for s in media)
        assert sql(dsn, "SELECT COUNT(*) FROM watchlist_announcement "
                        "WHERE title_zh IS NOT NULL")[0][0] == 0


class TestRevisedRowsReenhanced:
    def test_revised_media_row_reenhanced_on_latest_revision(self, env, monkeypatch):
        """revised（内容变更）条目重新增强并回写到最新 revision 行；
        旧版本行保持原值。"""
        import json as _json
        from core.announcements import llm_enhance
        dsn, _ = env
        seed_mu(dsn)
        now_ts = int(datetime.now(UTC).timestamp())
        payload = [dict(category="company", datetime=now_ts,
                        headline="Micron rises on AI demand", id=1,
                        related="MU", source="Yahoo", summary="v1",
                        url="https://example.com/1")]
        empty_rss = b'<?xml version="1.0"?><rss><channel></channel></rss>'
        monkeypatch.setattr(
            ir_source, "http_get",
            lambda url, **kw: (_json.dumps(payload).encode()
                               if "finnhub.io" in url else empty_rss))
        monkeypatch.setattr(llm_enhance, "_run_mmx", lambda *a, **kw:
                            '{"title_zh": "旧中文", "summary_zh": "旧摘要",'
                            ' "importance": "normal"}')
        run()
        enable_media(dsn)
        run()
        rows = sql(dsn, "SELECT revision, title_zh, importance FROM "
                        "watchlist_announcement ORDER BY revision")
        assert rows == [(1, "旧中文", "normal")]

        # 内容变更 → revision 2，重新增强并回写新版本行
        payload[0]["headline"] = "Micron surges on AI demand"
        monkeypatch.setattr(llm_enhance, "_run_mmx", lambda *a, **kw:
                            '{"title_zh": "新中文", "summary_zh": "新摘要",'
                            ' "importance": "high"}')
        summary = run()
        finnhub = next(s for s in summary["sources"]
                       if s.get("platform") == "finnhub")
        assert finnhub["revised"] == 1
        rows = sql(dsn, "SELECT revision, title_zh, importance FROM "
                        "watchlist_announcement ORDER BY revision")
        assert rows == [(1, "旧中文", "normal"), (2, "新中文", "high")]
        # 整轮摘要汇总 llm 计数（不靠手工查库排查）
        assert summary["llm"]["ok"] >= 1
        assert summary["llm"]["failed"] == 0

    def test_llm_counts_visible_in_summary_on_failure(self, env, monkeypatch):
        """增强失败：条目正常入库，llm_failed 计数进整轮摘要。"""
        import json as _json
        from core.announcements import llm_enhance
        dsn, _ = env
        seed_mu(dsn)
        now_ts = int(datetime.now(UTC).timestamp())
        payload = [dict(category="company", datetime=now_ts,
                        headline="Micron rises", id=1, related="MU",
                        source="Yahoo", summary="s", url="https://example.com/1")]
        monkeypatch.setattr(
            ir_source, "http_get",
            lambda url, **kw: (_json.dumps(payload).encode()
                               if "finnhub.io" in url
                               else b'<?xml version="1.0"?><rss><channel></channel></rss>'))
        def boom(*_a, **_kw):
            raise llm_enhance.EnhanceError("test: mmx down")
        monkeypatch.setattr(llm_enhance, "_run_mmx", boom)
        run()
        enable_media(dsn)
        summary = run()
        finnhub = next(s for s in summary["sources"]
                       if s.get("platform") == "finnhub")
        assert finnhub["status"] == "success_new"     # 入库不受增强失败影响
        assert finnhub["llm_failed"] == 1
        assert summary["llm"]["failed"] == 1
        row = sql(dsn, "SELECT title_zh, importance FROM watchlist_announcement")
        assert row == [(None, None)]                  # 字段留 NULL
