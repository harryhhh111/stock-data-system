"""每日资讯 API 测试：feed / pinned / 折叠 / 五态 / 降级（临时 PG 集群）。

复用公告片测试的 cluster 模式；媒体条目直接种子进库（采集路径已在
tests/test_announcements_media.py 覆盖）。
"""
import getpass
import shutil
import subprocess
import tempfile
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import psycopg2
import psycopg2.errors
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
import db
from web.routes import watchlist as watchlist_routes
from web.services import news_service
from web.services.news_service import fold_media, normalize_title

UTC = timezone.utc
BJ = timezone(timedelta(hours=8))


def sql(dsn, query, args=()):
    with closing(psycopg2.connect(dsn)) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(query, args)
            return cur.fetchall() if cur.description else None


@pytest.fixture(scope="module")
def cluster():
    if not shutil.which("initdb") or not shutil.which("pg_ctl"):
        pytest.skip("news API tests require initdb/pg_ctl")
    root = Path(tempfile.mkdtemp(prefix="anncnews-"))
    data = root / "data"
    started = False
    try:
        subprocess.run(["initdb", "-D", str(data), "-A", "trust", "--no-locale",
                        "-E", "UTF8"], check=True, capture_output=True)
        result = subprocess.run(
            ["pg_ctl", "-D", str(data), "-l", str(root / "server.log"),
             "-o", f"-k {root} -p 56439 -h '' -F", "-w", "start"], capture_output=True)
        if result.returncode:
            pytest.fail("Temporary PostgreSQL startup failed: " +
                        (root / "server.log").read_text())
        started = True
        dsn = (f"host={root} port=56439 dbname=postgres "
               f"user={getpass.getuser()} connect_timeout=2")
        sql(dsn, Path("scripts/watchlist_announcements.sql").read_text())
        sql(dsn, "CREATE TABLE stock_info (stock_code VARCHAR(20) PRIMARY KEY, "
                 "stock_name VARCHAR(100) NOT NULL DEFAULT '', "
                 "market VARCHAR(10) NOT NULL, cik VARCHAR(20))")
        sql(dsn, "CREATE TABLE watchlist_item (id BIGSERIAL PRIMARY KEY, "
                 "market VARCHAR(10) NOT NULL, stock_code VARCHAR(20) NOT NULL, "
                 "stock_name VARCHAR(200) NOT NULL DEFAULT '')")
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
    monkeypatch.setattr(config.db, "port", 56439)
    monkeypatch.setattr(config.db, "dbname", "postgres")
    monkeypatch.setattr(config.db, "user", getpass.getuser())
    monkeypatch.setattr(config.db, "password", "")
    db.close_pool()
    db._pool = None
    monkeypatch.setattr(config.announcements, "enabled", "true")
    monkeypatch.setattr(config.announcements, "sec_interval_minutes", "60")
    monkeypatch.setattr(config.announcements, "ir_interval_minutes", "60")
    monkeypatch.setattr(config.announcements, "media_interval_minutes", "360")
    yield dsn
    db.close_pool()
    db._pool = None
    for table in ("watchlist_announcement_security", "watchlist_announcement",
                  "watchlist_announcement_run", "watchlist_announcement_check_state",
                  "watchlist_announcement_recovery", "watchlist_announcement_gap",
                  "watchlist_announcement_source", "watchlist_item", "stock_info"):
        sql(cluster, f"DELETE FROM {table}")


@pytest.fixture
def client(env):
    app = FastAPI()
    app.include_router(watchlist_routes.router, prefix="/api/v1")
    return TestClient(app)


def seed_company(dsn, code, cik=None):
    sql(dsn, "INSERT INTO stock_info (stock_code, market, cik) VALUES (%s, 'US', %s) "
             "ON CONFLICT DO NOTHING", (code, cik))
    sql(dsn, "INSERT INTO watchlist_item (market, stock_code, stock_name) "
             "VALUES ('US', %s, %s)", (code, code))


def seed_source(dsn, kind, issuer, platform, *, enabled=True,
                status="enabled", reason=None):
    return sql(dsn, """
        INSERT INTO watchlist_announcement_source
            (market, source_kind, platform, issuer_identifier, allowed_domain,
             entry_url, enabled, source_status, status_reason)
        VALUES ('US', %s, %s, %s, 'example.com', %s, %s, %s, %s)
        RETURNING source_id
    """, (kind, platform, issuer, f"https://example.com/{kind}/{issuer}/{platform}",
          enabled, status, reason))[0][0]


def seed_item(dsn, source_id, record_id, code, *, title, kind_form="news",
              published_at=None, precision="second", raw="", media_source=None,
              content_nature=None, relation_reason=None, first_observed=None,
              importance=None, title_zh=None, summary_zh=None):
    observed = first_observed or datetime.now(UTC)
    sql(dsn, """
        INSERT INTO watchlist_announcement
            (market, issuer_identifier, source_id, source_record_id, title,
             form_type, original_url, published_at, published_at_precision,
             source_published_raw, source_timezone, first_observed_at,
             last_seen_at, content_hash, content_nature, relation_reason,
             media_source, importance, title_zh, summary_zh, revision)
        VALUES ('US', '', %s, %s, %s, %s, %s, %s, %s, %s, 'UTC', %s, %s, %s,
                %s, %s, %s, %s, %s, %s, 1)
    """, (source_id, record_id, title, kind_form,
          f"https://example.com/{record_id}", published_at, precision, raw,
          observed, observed, f"h-{record_id}", content_nature,
          relation_reason, media_source, importance, title_zh, summary_zh))
    sql(dsn, """
        INSERT INTO watchlist_announcement_security (announcement_id, market, stock_code)
        SELECT id, 'US', %s FROM watchlist_announcement
        WHERE source_id = %s AND source_record_id = %s AND revision = 1
        ON CONFLICT DO NOTHING
    """, (code, source_id, record_id))


# ── 折叠（纯函数）─────────────────────────────────────────

class TestFolding:
    def row(self, title, hours_ago, source="Reuters", rid=None):
        return {"title": title, "published_at": datetime.now(UTC) - timedelta(hours=hours_ago),
                "first_observed_at": datetime.now(UTC), "id": rid or title,
                "original_url": f"https://example.com/{rid or title}",
                "media_source": source, "platform": "google_news_rss",
                "published_at_precision": "second"}

    def test_normalize_strips_source_suffix_and_case(self):
        assert normalize_title("Micron Rises - Reuters") == \
            normalize_title("micron rises")
        assert normalize_title("Fed holds rates - CNBC") == \
            normalize_title("Fed holds rates")

    def test_same_title_within_48h_folds(self):
        rows = [self.row("Micron Rises - Reuters", 5, "Reuters", "a"),
                self.row("Micron Rises - Yahoo Finance", 10, "Yahoo", "b"),
                self.row("micron rises", 20, "Benzinga", "c")]
        clusters = fold_media(rows)
        assert len(clusters) == 1 and len(clusters[0]) == 3

    def test_beyond_48h_does_not_fold(self):
        rows = [self.row("Micron Rises - Reuters", 5, "Reuters", "a"),
                self.row("Micron Rises - Yahoo", 60, "Yahoo", "b")]
        assert len(fold_media(rows)) == 2

    def test_different_titles_never_fold(self):
        """标题不同不折叠：后续更新/不同观点不得被误删。"""
        rows = [self.row("Micron Rises On Earnings", 5, "Reuters", "a"),
                self.row("Micron Falls After Guidance Cut", 6, "Reuters", "b")]
        assert len(fold_media(rows)) == 2

    def test_unknown_time_not_folded(self):
        rows = [self.row("Same Title", 5, "Reuters", "a"),
                self.row("Same Title", 6, "Yahoo", "b")]
        rows[0]["published_at"] = None
        rows[0]["published_at_precision"] = "unknown"
        assert len(fold_media(rows)) == 2


# ── feed / pinned ─────────────────────────────────────────

class TestFeed:
    def test_feed_folds_and_labels(self, env, client):
        dsn = env
        seed_company(dsn, "MU", "0000723125")
        g = seed_source(dsn, "media", "0000723125", "google_news_rss")
        sec = seed_source(dsn, "sec", "0000723125", "sec")
        now = datetime.now(UTC)
        # 同题两条媒体（不同来源后缀）→ 折叠
        seed_item(dsn, g, "m1", "MU", title="Micron Rises - Reuters",
                  published_at=now - timedelta(hours=3),
                  raw="2026-10-22T04:00:00 GMT", media_source="Reuters",
                  content_nature="unclassified",
                  relation_reason='ticker 直连：Google 查询词 "Micron Technology"')
        seed_item(dsn, g, "m2", "MU", title="Micron Rises - Yahoo Finance",
                  published_at=now - timedelta(hours=5),
                  raw="2026-10-22T02:00:00 GMT", media_source="Yahoo Finance",
                  content_nature="unclassified")
        # 标题不同 → 不折叠
        seed_item(dsn, g, "m3", "MU", title="Micron Falls After Open - Reuters",
                  published_at=now - timedelta(hours=2),
                  raw="2026-10-22T05:00:00 GMT", media_source="Reuters")
        # 官方披露同页
        seed_item(dsn, sec, "s1", "MU", title="8-K 当前报告", kind_form="8-K",
                  published_at=now - timedelta(hours=1),
                  raw="2026-10-22T06:00:00.000Z")
        data = client.get("/api/v1/watchlist/news/feed").json()["data"]
        assert data["initialized"] is True
        media = [e for e in data["entries"] if e["source_kind"] == "media"]
        official = [e for e in data["entries"] if e["source_kind"] == "sec"]
        assert len(official) == 1
        assert len(media) == 2                      # 两条同题折成一组
        group = next(e for e in media if e["title"].startswith("Micron Rises"))
        assert group["fold_count"] == 2
        assert {s["name"] for s in group["fold_sources"]} == {"Reuters", "Yahoo Finance"}
        assert group["content_nature"] == "unclassified"   # 不伪造为「报道」
        assert group["relation_reason"]
        # kind 筛选
        only_media = client.get("/api/v1/watchlist/news/feed?kind=media").json()["data"]
        assert all(e["source_kind"] == "media" for e in only_media["entries"])
        only_official = client.get(
            "/api/v1/watchlist/news/feed?kind=official").json()["data"]
        assert len(only_official["entries"]) == 1
        # stock 筛选
        none_mu = client.get("/api/v1/watchlist/news/feed?stock=NBIS").json()["data"]
        assert none_mu["entries"] == []
        # 非法参数
        assert client.get("/api/v1/watchlist/news/feed?kind=x").status_code == 409
        assert client.get("/api/v1/watchlist/news/feed?date=bad").status_code == 409

    def test_feed_unknown_time_labeled(self, env, client):
        dsn = env
        seed_company(dsn, "MU", "0000723125")
        g = seed_source(dsn, "media", "0000723125", "google_news_rss")
        seed_item(dsn, g, "u1", "MU", title="Unknown Time News - Reuters",
                  published_at=None, precision="unknown", raw="")
        data = client.get("/api/v1/watchlist/news/feed").json()["data"]
        assert len(data["entries"]) == 1
        assert data["entries"][0]["time_unknown"] is True
        # 指定其他日期：未知时间条目按发现时间归入，今日发现 → 昨日不在
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        other = client.get(f"/api/v1/watchlist/news/feed?date={yesterday}").json()["data"]
        assert other["entries"] == []

    def test_feed_media_source_states(self, env, client):
        """媒体来源状态并入五态；窗口内无条目区分「暂无资讯」与「不可用」。"""
        dsn = env
        seed_company(dsn, "MU", "0000723125")
        g = seed_source(dsn, "media", "0000723125", "google_news_rss")
        f = seed_source(dsn, "media", "0000723125", "finnhub",
                        enabled=False, status="pending_verification",
                        reason="部署片启用")
        sql(dsn, """INSERT INTO watchlist_announcement_check_state
            (market, issuer_identifier, source_id, effective_interval_minutes,
             last_attempt_at, last_attempt_status, last_success_at, checked_through)
            VALUES ('US', '0000723125', %s, 360, NOW(), 'success_empty', NOW(), NOW())
        """, (g,))
        data = client.get("/api/v1/watchlist/news/feed").json()["data"]
        states = {s["platform"]: s["state"] for s in data["sources"]}
        assert states["google_news_rss"] == "ok"
        assert states["finnhub"] == "pending_verification"

    def test_feed_degrades_only_on_missing_announcement_schema(self, env, client,
                                                               monkeypatch):
        real = news_service.ann._read_rows

        def fake(sql_text, params=()):
            if "watchlist_announcement" in sql_text:
                raise psycopg2.errors.UndefinedTable("no table")
            return real(sql_text, params)
        monkeypatch.setattr(news_service.ann, "_read_rows", fake)
        seed_company(env, "MU", "0000723125")
        data = client.get("/api/v1/watchlist/news/feed").json()["data"]
        assert data["initialized"] is False
        assert "尚未初始化" in data["message"]

    def test_pinned_rules(self, env, client):
        """v1.1 置顶：近 48h 候选；importance='high' 或折叠来源数 ≥3 入选；
        官方媒体一视同仁。"""
        dsn = env
        seed_company(dsn, "MU", "0000723125")
        g = seed_source(dsn, "media", "0000723125", "google_news_rss")
        sec = seed_source(dsn, "sec", "0000723125", "sec")
        now = datetime.now(UTC)
        # 近 48h 官方 + high → 置顶
        seed_item(dsn, sec, "s1", "MU", title="8-K 当前报告", kind_form="8-K",
                  published_at=now - timedelta(hours=3), raw="x",
                  importance="high")
        # 近 48h 官方但 normal → 不置顶（保证 AI 可用判定有样本）
        seed_item(dsn, sec, "s0", "MU", title="10-Q 季报", kind_form="10-Q",
                  published_at=now - timedelta(hours=30), raw="x",
                  importance="normal")
        # 50h 前 high → 超出候选窗口
        seed_item(dsn, sec, "s9", "MU", title="旧 8-K", kind_form="8-K",
                  published_at=now - timedelta(hours=50), raw="x",
                  importance="high")
        # 同题三源媒体（normal）→ 折叠 ≥3 入选
        for i, src in enumerate(("Reuters", "Yahoo", "CNBC")):
            seed_item(dsn, g, f"m{i}", "MU", title=f"Micron Rises - {src}",
                      published_at=now - timedelta(hours=2 + i), raw="x",
                      media_source=src, importance="normal")
        # 同题双源（normal）→ 不入选
        seed_item(dsn, g, "d1", "MU", title="Micron Pair - Reuters",
                  published_at=now - timedelta(hours=2), raw="x",
                  media_source="Reuters", importance="normal")
        seed_item(dsn, g, "d2", "MU", title="Micron Pair - Yahoo",
                  published_at=now - timedelta(hours=3), raw="x",
                  media_source="Yahoo", importance="normal")
        # 单源媒体但 high → 入选
        seed_item(dsn, g, "h1", "MU", title="Micron Solo High - Reuters",
                  published_at=now - timedelta(hours=1), raw="x",
                  media_source="Reuters", importance="high")
        data = client.get("/api/v1/watchlist/news/pinned").json()["data"]
        assert data["ai_available"] is True
        titles = [e["title"] for e in data["entries"]]
        assert "8-K 当前报告" in titles               # 官方 high 入选
        assert "10-Q 季报" not in titles              # 官方 normal 不入选
        assert "旧 8-K" not in titles                 # 超 48h 候选窗
        assert any(t.startswith("Micron Rises") for t in titles)   # ≥3 源
        assert not any(t.startswith("Micron Pair") for t in titles)  # 2 源不够
        assert "Micron Solo High - Reuters" in titles  # 单源 high 入选
        # 排序：high 优先于 normal（8-K 与 Solo High 在最前）
        group_idx = next(i for i, t in enumerate(titles)
                         if t.startswith("Micron Rises"))
        assert titles.index("8-K 当前报告") < group_idx

    def test_pinned_day_precision_and_unknown(self, env, client):
        """置顶时间口径：仅日期按北京日期（今天/昨天可进候选，不回退发现
        时间）；旧回补条目不冒充新发生；unknown 永不进置顶。"""
        dsn = env
        seed_company(dsn, "NBIS", "0001713445")
        ir = seed_source(dsn, "ir", "0001713445", "nebius_newsroom")
        now = datetime.now(UTC)
        today_bj = now.astimezone(BJ).date()
        yesterday_bj = today_bj - timedelta(days=1)
        # 生产故障复刻：8 月的仅日期 IR 公告、昨天回补首次发现 → 不进候选
        seed_item(dsn, ir, "old-backfill", "NBIS", title="August IR item",
                  kind_form="press_release", published_at=None,
                  precision="day", raw="2026-08-15",
                  first_observed=now - timedelta(hours=3), importance="high")
        # 仅日期今天/昨天（北京）+ high → 进置顶，保持仅日期标注
        seed_item(dsn, ir, "today-day", "NBIS", title="Today IR item",
                  kind_form="press_release", published_at=None,
                  precision="day", raw=today_bj.isoformat(), importance="high")
        seed_item(dsn, ir, "yday-day", "NBIS", title="Yesterday IR item",
                  kind_form="press_release", published_at=None,
                  precision="day", raw=yesterday_bj.isoformat(),
                  importance="high")
        # 仅日期前天 → 不进候选
        seed_item(dsn, ir, "dbd-day", "NBIS", title="Day before yesterday",
                  kind_form="press_release", published_at=None,
                  precision="day",
                  raw=(today_bj - timedelta(days=2)).isoformat(),
                  importance="high")
        # 未知时间、刚发现、high → 永不进置顶
        seed_item(dsn, ir, "unk", "NBIS", title="Unknown time item",
                  kind_form="press_release", published_at=None,
                  precision="unknown", raw="",
                  first_observed=now - timedelta(minutes=30),
                  importance="high")
        data = client.get("/api/v1/watchlist/news/pinned").json()["data"]
        titles = [e["title"] for e in data["entries"]]
        assert "Today IR item" in titles
        assert "Yesterday IR item" in titles
        assert "August IR item" not in titles        # 回补不冒充新发生
        assert "Day before yesterday" not in titles
        assert "Unknown time item" not in titles
        today_entry = next(e for e in data["entries"]
                           if e["title"] == "Today IR item")
        assert today_entry["date_only"] is True      # 保持「仅日期」标注

    def test_pinned_cap_and_sort(self, env, client):
        """封顶 10；排序 high 优先 → 来源数 → 发布时间。"""
        dsn = env
        seed_company(dsn, "MU", "0000723125")
        sec = seed_source(dsn, "sec", "0000723125", "sec")
        g = seed_source(dsn, "media", "0000723125", "google_news_rss")
        now = datetime.now(UTC)
        # 12 条 high 官方（时间递增）
        for i in range(12):
            seed_item(dsn, sec, f"cap-{i}", "MU", title=f"Cap {i:02d}",
                      kind_form="8-K",
                      published_at=now - timedelta(hours=i), raw="x",
                      importance="high")
        # 一个 4 源媒体组（normal）
        for i, srcn in enumerate(("A", "B", "C", "D")):
            seed_item(dsn, g, f"mg-{i}", "MU", title=f"Grouped - {srcn}",
                      published_at=now - timedelta(hours=1), raw="x",
                      media_source=srcn, importance="normal")
        data = client.get("/api/v1/watchlist/news/pinned").json()["data"]
        assert len(data["entries"]) == 10             # 封顶 10
        # high 全部排在 normal 4 源组之前；组内按时间倒序
        titles = [e["title"] for e in data["entries"]]
        assert titles[0] == "Cap 00"                  # 最新 high 在前
        assert "Grouped - A" not in titles            # normal 组被挤出前 10
        assert all(t.startswith("Cap") for t in titles)

    def test_pinned_ai_unavailable_fallback(self, env, client):
        """AI 持续不可用（近 24h 新条目全部无 importance）→ 机械回退：
        ≥3 源媒体组 + 近 24h 官方披露。"""
        dsn = env
        seed_company(dsn, "MU", "0000723125")
        sec = seed_source(dsn, "sec", "0000723125", "sec")
        g = seed_source(dsn, "media", "0000723125", "google_news_rss")
        now = datetime.now(UTC)
        # 全部无 importance（AI 持续失败形态）
        seed_item(dsn, sec, "s1", "MU", title="8-K 当前报告", kind_form="8-K",
                  published_at=now - timedelta(hours=3), raw="x")
        seed_item(dsn, sec, "s2", "MU", title="旧官方", kind_form="10-Q",
                  published_at=now - timedelta(hours=30), raw="x")
        for i, srcn in enumerate(("A", "B", "C")):
            seed_item(dsn, g, f"g{i}", "MU", title=f"Hot - {srcn}",
                      published_at=now - timedelta(hours=2), raw="x",
                      media_source=srcn)
        seed_item(dsn, g, "g9", "MU", title="Cool - A",
                  published_at=now - timedelta(hours=1), raw="x",
                  media_source="A")
        data = client.get("/api/v1/watchlist/news/pinned").json()["data"]
        assert data["ai_available"] is False
        titles = [e["title"] for e in data["entries"]]
        assert "8-K 当前报告" in titles               # 机械规则：24h 官方
        assert "旧官方" not in titles                 # 超 24h
        assert any(t.startswith("Hot") for t in titles)   # ≥3 源组
        assert "Cool - A" not in titles               # 单源不入选
