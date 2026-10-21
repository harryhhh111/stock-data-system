"""公告看板 API 测试：pytest + 临时 PG 集群，覆盖 UI 方案 §5 验收清单。

集群执行 scripts/watchlist_announcements.sql 建表；watchlist_item/stock_info
为最小化复刻。不触发任何外网请求（服务层只查库）。
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
from web.services import announcements_service as svc

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
        pytest.skip("announcement API tests require initdb/pg_ctl")
    root = Path(tempfile.mkdtemp(prefix="anncapi-"))
    data = root / "data"
    started = False
    try:
        subprocess.run(["initdb", "-D", str(data), "-A", "trust", "--no-locale",
                        "-E", "UTF8"], check=True, capture_output=True)
        result = subprocess.run(
            ["pg_ctl", "-D", str(data), "-l", str(root / "server.log"),
             "-o", f"-k {root} -p 56436 -h '' -F", "-w", "start"], capture_output=True)
        if result.returncode:
            pytest.fail("Temporary PostgreSQL startup failed: " +
                        (root / "server.log").read_text())
        started = True
        dsn = (f"host={root} port=56436 dbname=postgres "
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
    monkeypatch.setattr(config.db, "port", 56436)
    monkeypatch.setattr(config.db, "dbname", "postgres")
    monkeypatch.setattr(config.db, "user", getpass.getuser())
    monkeypatch.setattr(config.db, "password", "")
    db.close_pool()
    db._pool = None
    monkeypatch.setattr(config.announcements, "enabled", "true")
    monkeypatch.setattr(config.announcements, "sec_interval_minutes", "60")
    monkeypatch.setattr(config.announcements, "ir_interval_minutes", "60")
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


# ── 种子辅助 ───────────────────────────────────────────────

def seed_company(dsn, code, cik=None, market="US"):
    if market == "US":
        sql(dsn, "INSERT INTO stock_info (stock_code, market, cik) "
                 "VALUES (%s, 'US', %s) ON CONFLICT (stock_code) DO NOTHING",
            (code, cik))
    sql(dsn, "INSERT INTO watchlist_item (market, stock_code, stock_name) "
             "VALUES (%s, %s, %s) RETURNING id", (market, code, code))


def seed_source(dsn, kind, issuer, *, platform=None, enabled=True,
                status="enabled", reason=None, interval=None):
    return sql(dsn, """
        INSERT INTO watchlist_announcement_source
            (market, source_kind, platform, issuer_identifier, allowed_domain,
             entry_url, interval_minutes, enabled, source_status, status_reason)
        VALUES ('US', %s, %s, %s, 'example.com', %s, %s, %s, %s, %s)
        RETURNING source_id
    """, (kind, platform, issuer, f"https://example.com/{kind}/{issuer}/{platform or ''}",
          interval, enabled, status, reason))[0][0]


_UNSET = object()


def seed_check(dsn, issuer, source_id, *, status="success_empty", error=None,
               interval=60, checked=_UNSET, success=_UNSET, attempt=None):
    now = datetime.now(UTC)
    sql(dsn, """
        INSERT INTO watchlist_announcement_check_state
            (market, issuer_identifier, source_id, effective_interval_minutes,
             last_attempt_at, last_attempt_status, last_error_class,
             last_success_at, next_due_at, checked_through)
        VALUES ('US', %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (issuer, source_id, interval, attempt or now, status, error,
          now if success is _UNSET else success,
          now + timedelta(minutes=interval),
          now if checked is _UNSET else checked))


def seed_gap(dsn, issuer, source_id, start, end):
    sql(dsn, """
        INSERT INTO watchlist_announcement_gap
            (market, issuer_identifier, source_id, gap_range, reason)
        VALUES ('US', %s, %s, tstzrange(%s, %s), 'history_unavailable')
    """, (issuer, source_id, start, end))


def seed_announcement(dsn, source_id, record_id, code, *, title, form,
                      published_at=None, precision="second", raw="",
                      revision=1, first_observed=None, content_hash=None):
    observed = first_observed or datetime.now(UTC)
    sql(dsn, """
        INSERT INTO watchlist_announcement
            (market, issuer_identifier, source_id, source_record_id, title,
             form_type, original_url, published_at, published_at_precision,
             source_published_raw, source_timezone, first_observed_at,
             last_seen_at, content_hash, revision)
        VALUES ('US', '', %s, %s, %s, %s, %s, %s, %s, %s, 'UTC', %s, %s, %s, %s)
    """, (source_id, record_id, title, form,
          f"https://example.com/{record_id}", published_at, precision, raw,
          observed, observed, content_hash or f"h-{record_id}-r{revision}",
          revision))
    if revision == 1:
        sql(dsn, """
            INSERT INTO watchlist_announcement_security (announcement_id, market, stock_code)
            SELECT id, 'US', %s FROM watchlist_announcement
            WHERE source_id = %s AND source_record_id = %s AND revision = 1
            ON CONFLICT DO NOTHING
        """, (code, source_id, record_id))


def get_summary(client, days=7):
    resp = client.get(f"/api/v1/watchlist/announcements?days={days}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    return body["data"]


def by_code(data, code):
    return next(i for i in data["items"] if i["stock_code"] == code)


# ── 状态五态与窗口口径 ─────────────────────────────────────

class TestSummaryStates:
    def test_all_states(self, env, client):
        dsn = env
        today_bj = datetime.now(UTC).astimezone(BJ).date()
        now = datetime.now(UTC)

        seed_company(dsn, "OKCO", "0002000001")
        sec_ok = seed_source(dsn, "sec", "0002000001")
        seed_check(dsn, "0002000001", sec_ok, status="success_empty")
        ir_pending_first = seed_source(dsn, "ir", "0002000001",
                                       platform="nebius_newsroom")

        seed_company(dsn, "FAILCO", "0002000002")
        sec_fail = seed_source(dsn, "sec", "0002000002")
        seed_check(dsn, "0002000002", sec_fail, status="failed",
                   error="http_429")

        seed_company(dsn, "GAPCO", "0002000003")
        sec_gap = seed_source(dsn, "sec", "0002000003")
        seed_check(dsn, "0002000003", sec_gap, status="partial", checked=None,
                   success=None)
        gap_start = now - timedelta(days=80)
        seed_gap(dsn, "0002000003", sec_gap,
                 gap_start, gap_start + timedelta(days=30))

        seed_company(dsn, "BLOCKCO", "0002000004")
        seed_source(dsn, "sec", "0002000004")
        seed_source(dsn, "ir", "0002000004", platform="q4_press_release",
                    enabled=True, status="blocked", reason="列表接口 403")

        seed_company(dsn, "PENDCO", "0002000005")
        seed_source(dsn, "ir", "0002000005", platform="nebius_newsroom",
                    enabled=False, status="pending_verification",
                    reason="待真实采集验证")

        seed_company(dsn, "NOCIK", None)          # 缺 CIK + IR 未配置
        seed_company(dsn, "0700", market="CN_HK")  # 港股市场未接入

        data = get_summary(client)
        assert data["announcements_enabled"] is True
        assert data["window"]["timezone"] == "Asia/Shanghai"

        ok = by_code(data, "OKCO")
        ok_sec = next(s for s in ok["sources"] if s["kind"] == "sec")
        ok_ir = next(s for s in ok["sources"] if s["kind"] == "ir")
        assert ok_sec["state"] == "ok"
        assert ok_sec["last_attempt_at"] and ok_sec["last_success_at"]
        assert ok_sec["next_due_at"] and ok_sec["checked_through"]
        assert ok_sec["effective_interval_minutes"] == 60
        assert ok_ir["state"] == "pending_first_check"

        fail = by_code(data, "FAILCO")
        fail_sec = next(s for s in fail["sources"] if s["kind"] == "sec")
        assert fail_sec["state"] == "failed"
        assert "http_429" in fail_sec["message"]

        gap = by_code(data, "GAPCO")
        gap_sec = next(s for s in gap["sources"] if s["kind"] == "sec")
        assert gap_sec["state"] == "partial"
        assert gap_sec["open_gaps"] == 1
        assert gap_sec["earliest_gap_start"] is not None
        assert gap_sec["last_success_at"] is None
        assert gap_sec["checked_through"] is None

        blocked = by_code(data, "BLOCKCO")
        blocked_ir = next(s for s in blocked["sources"] if s["kind"] == "ir")
        assert blocked_ir["state"] == "blocked"
        assert "403" in blocked_ir["message"]
        blocked_sec = next(s for s in blocked["sources"] if s["kind"] == "sec")
        assert blocked_sec["state"] == "pending_first_check"

        pend = by_code(data, "PENDCO")
        pend_ir = next(s for s in pend["sources"] if s["kind"] == "ir")
        assert pend_ir["state"] == "pending_verification"
        assert "待真实采集验证" in pend_ir["message"]

        nocik = by_code(data, "NOCIK")
        nocik_sec = next(s for s in nocik["sources"] if s["kind"] == "sec")
        nocik_ir = next(s for s in nocik["sources"] if s["kind"] == "ir")
        assert nocik_sec["state"] == "not_configured"
        assert "CIK" in nocik_sec["message"]
        assert nocik_ir["state"] == "not_configured"

        hk = by_code(data, "0700")
        assert hk["sources"][0]["state"] == "unsupported_market"
        assert "未接入" in hk["sources"][0]["message"]
        assert hk["count"] == 0

        # degraded：存在已启用来源失败/partial
        assert data["degraded"] is True

    def test_global_disabled(self, env, client, monkeypatch):
        dsn = env
        seed_company(dsn, "OFFCO", "0002000010")
        sec = seed_source(dsn, "sec", "0002000010")
        seed_check(dsn, "0002000010", sec, status="success_new")
        monkeypatch.setattr(config.announcements, "enabled", "false")
        data = get_summary(client)
        assert data["announcements_enabled"] is False
        off = by_code(data, "OFFCO")
        assert all(s["state"] == "disabled_global" for s in off["sources"])
        assert "未启用" in off["sources"][0]["message"]
        assert data["degraded"] is False

    def test_degraded_false_when_all_ok(self, env, client):
        dsn = env
        seed_company(dsn, "FINE", "0002000011")
        sec = seed_source(dsn, "sec", "0002000011")
        seed_check(dsn, "0002000011", sec, status="success_empty")
        data = get_summary(client)
        assert data["degraded"] is False


class TestCountingAndWindow:
    def test_logical_count_and_revision_display(self, env, client):
        """修订不重复计数；latest 展示最高 revision 标题。"""
        dsn = env
        seed_company(dsn, "REVC", "0002000020")
        sec = seed_source(dsn, "sec", "0002000020")
        now = datetime.now(UTC)
        seed_announcement(dsn, sec, "acc-1", "REVC", title="旧标题",
                          form="8-K", published_at=now - timedelta(days=1),
                          raw="2026-10-20T10:00:00.000Z", revision=1)
        seed_announcement(dsn, sec, "acc-1", "REVC", title="新标题（更新）",
                          form="8-K", published_at=now - timedelta(hours=20),
                          raw="2026-10-20T15:00:00.000Z", revision=2)
        data = get_summary(client)
        item = by_code(data, "REVC")
        assert item["count"] == 1                      # 逻辑公告计一条
        assert item["latest"]["title"] == "新标题（更新）"  # 展示最高版本
        assert item["latest"]["revision"] == 2

    def test_window_rules_day_only_and_unknown(self, env, client):
        """仅日期按原始日期计入（不补午夜）；unknown 不计数。"""
        dsn = env
        seed_company(dsn, "DAYCO", "0002000021")
        sec = seed_source(dsn, "sec", "0002000021")
        today_bj = datetime.now(UTC).astimezone(BJ).date()
        seed_announcement(dsn, sec, "day-1", "DAYCO", title="仅日期",
                          form="press_release", published_at=None,
                          precision="day", raw=today_bj.isoformat())
        seed_announcement(dsn, sec, "day-old", "DAYCO", title="窗口外仅日期",
                          form="press_release", published_at=None,
                          precision="day",
                          raw=(today_bj - timedelta(days=30)).isoformat())
        seed_announcement(dsn, sec, "unk-1", "DAYCO", title="时间未知",
                          form="press_release", published_at=None,
                          precision="unknown", raw="")
        data = get_summary(client, days=7)
        item = by_code(data, "DAYCO")
        assert item["count"] == 1                      # 只有窗口内仅日期记录
        # latest：仅日期参与（按原始日期）；unknown 只在全部未知时才兜底
        assert item["latest"]["title"] == "仅日期"
        assert item["latest"]["date_only"] is True

    def test_latest_unbounded_and_unknown_fallback(self, env, client):
        """latest 不限窗口；全部未知时间时取最近发现并标注。"""
        dsn = env
        seed_company(dsn, "LATEC", "0002000022")
        sec = seed_source(dsn, "sec", "0002000022")
        now = datetime.now(UTC)
        seed_announcement(dsn, sec, "old-1", "LATEC", title="60 天前",
                          form="8-K", published_at=now - timedelta(days=60),
                          raw="2026-08-22T10:00:00.000Z")
        data = get_summary(client, days=7)
        item = by_code(data, "LATEC")
        assert item["count"] == 0                      # 窗口内无
        assert item["latest"]["title"] == "60 天前"     # latest 不限窗口
        assert item["latest"]["latest_by"] == "published"

        seed_company(dsn, "UNKC", "0002000023")
        sec2 = seed_source(dsn, "sec", "0002000023")
        seed_announcement(dsn, sec2, "u1", "UNKC", title="未知时间A",
                          form="8-K", precision="unknown", raw="",
                          first_observed=now - timedelta(days=2))
        seed_announcement(dsn, sec2, "u2", "UNKC", title="未知时间B",
                          form="8-K", precision="unknown", raw="",
                          first_observed=now - timedelta(hours=3))
        item2 = by_code(get_summary(client), "UNKC")
        assert item2["count"] == 0                     # unknown 不计数
        assert item2["latest"]["title"] == "未知时间B"  # 最近发现兜底
        assert item2["latest"]["latest_by"] == "first_observed"
        assert item2["latest"]["time_unknown"] is True

    def test_beijing_window_dst_boundary(self, env, client):
        """UTC 存储转北京时间判定窗口：美东晚间发布（UTC 深夜）归入
        北京时间次日。夏令时边界：UTC 固定偏移不受 DST 影响。"""
        dsn = env
        seed_company(dsn, "DSTCO", "0002000024")
        sec = seed_source(dsn, "sec", "0002000024")
        today_bj = datetime.now(UTC).astimezone(BJ).date()
        # 北京时间今天 06:59 = UTC 昨天 22:59（美东夏令时 18:59）
        bj_morning = datetime.combine(
            today_bj, datetime.min.time(), tzinfo=BJ) + timedelta(hours=7)
        seed_announcement(dsn, sec, "bj-1", "DSTCO", title="北京今晨",
                          form="8-K",
                          published_at=bj_morning.astimezone(UTC),
                          raw=bj_morning.astimezone(UTC).isoformat())
        data = get_summary(client, days=1)
        assert by_code(data, "DSTCO")["count"] == 1

    def test_latest_independent_of_db_session_timezone(self):
        """latest 的日期比较固定北京时间口径：同一瞬间无论会话返回 UTC
        还是 +08:00 表示，结果一致（先比日期新旧，同日精确时间优先）。"""
        from zoneinfo import ZoneInfo
        instant = datetime(2026, 10, 21, 20, tzinfo=UTC)  # 北京 10-22 04:00

        def record(title, published_at, precision, raw):
            return dict(id=1, title=title, published_at=published_at,
                        published_at_precision=precision,
                        source_published_raw=raw, form_type="press_release",
                        original_url="https://example.com/", source_kind="ir",
                        source_timezone="UTC",
                        first_observed_at=datetime(2026, 10, 22, tzinfo=UTC),
                        revision=1, is_backfill=False)

        day = record("date-only", None, "day", "2026-10-22")
        winners = [
            svc._latest_for([record("precise", instant.astimezone(tz),
                                    "second", ""), day])["title"]
            for tz in (UTC, ZoneInfo("Asia/Shanghai"))
        ]
        assert winners == ["precise", "precise"]
        # 同日不同时刻：精确时间晚者胜
        earlier = record("morning", datetime(2026, 10, 22, 1, tzinfo=UTC),
                         "second", "")
        later = record("evening", datetime(2026, 10, 22, 12, tzinfo=UTC),
                       "second", "")
        assert svc._latest_for([earlier, later])["title"] == "evening"
        # 跨日：仅日期的较新日期压过较旧的精确时间
        old_precise = record("old", datetime(2026, 10, 20, 20, tzinfo=UTC),
                             "second", "")
        assert svc._latest_for([old_precise, day])["title"] == "date-only"


# ── 详情分页 ───────────────────────────────────────────────

class TestDetail:
    def test_pagination_filter_and_ordering(self, env, client):
        dsn = env
        seed_company(dsn, "PGCO", "0002000030")
        sec = seed_source(dsn, "sec", "0002000030")
        ir = seed_source(dsn, "ir", "0002000030", platform="nebius_newsroom")
        now = datetime.now(UTC)
        for i in range(5):
            seed_announcement(dsn, sec, f"sec-{i}", "PGCO", title=f"SEC {i}",
                              form="8-K",
                              published_at=now - timedelta(days=i),
                              raw=(now - timedelta(days=i)).isoformat())
        seed_announcement(dsn, ir, "ir-1", "PGCO", title="IR 仅日期",
                          form="press_release", published_at=None,
                          precision="day", raw=date.today().isoformat())
        seed_announcement(dsn, ir, "ir-2", "PGCO", title="IR 未知时间",
                          form="press_release", published_at=None,
                          precision="unknown", raw="")

        item_id = sql(dsn, "SELECT id FROM watchlist_item WHERE stock_code='PGCO'")[0][0]
        resp = client.get(f"/api/v1/watchlist/{item_id}/announcements"
                          f"?days=90&offset=0&limit=3")
        body = resp.json()["data"]
        assert body["total"] == 7
        assert body["has_more"] is True
        assert len(body["items"]) == 3
        # 排序：发布时间倒序；NULL（仅日期/未知）在后；unknown 标注
        assert body["items"][0]["title"] == "SEC 0"
        page2 = client.get(f"/api/v1/watchlist/{item_id}/announcements"
                           f"?days=90&offset=5&limit=3").json()["data"]
        assert page2["has_more"] is False
        titles = [i["title"] for i in page2["items"]]
        assert titles == ["IR 未知时间", "IR 仅日期"]  # NULL 发布时间在后
        unk = page2["items"][0]
        assert unk["time_unknown"] is True
        # 来源筛选
        ir_only = client.get(f"/api/v1/watchlist/{item_id}/announcements"
                             f"?source=ir").json()["data"]
        assert ir_only["total"] == 2
        assert all(i["source_kind"] == "ir" for i in ir_only["items"])
        sec_only = client.get(f"/api/v1/watchlist/{item_id}/announcements"
                              f"?source=sec").json()["data"]
        assert sec_only["total"] == 5
        # limit 上限 50、days 上限 90（超限取上限，不报错）
        capped = client.get(f"/api/v1/watchlist/{item_id}/announcements"
                            f"?days=365&limit=500").json()["data"]
        assert capped["days"] == 90 and capped["limit"] == 50
        # 非法来源筛选
        bad = client.get(f"/api/v1/watchlist/{item_id}/announcements?source=x")
        assert bad.status_code == 409
        # 来源状态区随详情返回
        assert body["sources"]

    def test_revision_display_and_rewatch(self, env, client):
        """删除重关注仍可见历史（按 stock_code 查询）；详情展示最高版本
        且携带 revision=1 的首次发现时间。"""
        dsn = env
        seed_company(dsn, "REW", "0002000031")
        sec = seed_source(dsn, "sec", "0002000031")
        old_observed = datetime.now(UTC) - timedelta(days=40)
        seed_announcement(dsn, sec, "rw-1", "REW", title="v1",
                          form="10-Q", published_at=old_observed,
                          raw="2026-09-11T09:00:00.000Z", revision=1,
                          first_observed=old_observed)
        seed_announcement(dsn, sec, "rw-1", "REW", title="v2 更新",
                          form="10-Q", published_at=old_observed,
                          raw="2026-09-11T09:00:00.000Z", revision=2,
                          first_observed=datetime.now(UTC) - timedelta(days=1))
        old_id = sql(dsn, "SELECT id FROM watchlist_item WHERE stock_code='REW'")[0][0]
        sql(dsn, "DELETE FROM watchlist_item WHERE id = %s", (old_id,))
        sql(dsn, "INSERT INTO watchlist_item (market, stock_code, stock_name) "
                 "VALUES ('US', 'REW', 'REW')")
        new_id = sql(dsn, "SELECT id FROM watchlist_item WHERE stock_code='REW'")[0][0]
        assert new_id != old_id
        body = client.get(f"/api/v1/watchlist/{new_id}/announcements").json()["data"]
        assert body["total"] == 1                       # 逻辑公告一条
        assert body["items"][0]["title"] == "v2 更新"
        assert body["items"][0]["revision"] == 2
        first_seen = datetime.fromisoformat(body["items"][0]["first_observed_at"])
        assert first_seen == old_observed  # 同一时刻（响应带 +08:00 偏移）

    def test_cn_market_item_unsupported(self, env, client):
        dsn = env
        seed_company(dsn, "00700", market="CN_HK")
        item_id = sql(dsn, "SELECT id FROM watchlist_item "
                           "WHERE stock_code='00700'")[0][0]
        body = client.get(f"/api/v1/watchlist/{item_id}/announcements").json()["data"]
        assert body["supported"] is False
        assert "未接入" in body["message"]
        assert body["items"] == []

    def test_missing_item_404(self, env, client):
        resp = client.get("/api/v1/watchlist/99999/announcements")
        assert resp.status_code == 404


# ── 降级 ───────────────────────────────────────────────────

class TestDegradation:
    def test_undefined_table_returns_not_initialized(self, env, client,
                                                     monkeypatch):
        """仅 undefined_table（公告 schema 未安装）降级；count=None → 「—」。"""
        real = svc._read_rows

        def fake(sql_text, params=()):
            if "watchlist_announcement" in sql_text:
                raise psycopg2.errors.UndefinedTable(
                    'relation "watchlist_announcement" does not exist')
            return real(sql_text, params)

        seed_company(env, "DEG", "0002000099")
        monkeypatch.setattr(svc, "_read_rows", fake)
        data = get_summary(client)
        assert data["initialized"] is False
        assert "尚未初始化" in data["message"]
        item = by_code(data, "DEG")
        assert item["count"] is None                 # 前端显示「—」而非「0 条」

    def test_other_db_errors_still_raise(self, env, client, monkeypatch):
        """其他数据库错误不包装成空数据，仍是服务异常 503。"""
        monkeypatch.setattr(
            svc, "_read_rows",
            lambda *a, **kw: (_ for _ in ()).throw(
                psycopg2.OperationalError("connection refused")))
        resp = client.get("/api/v1/watchlist/announcements")
        assert resp.status_code == 503
