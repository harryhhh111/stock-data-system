"""关注股官方公告流第一片：配置校验与迁移幂等性测试。

合同：docs/WATCHLIST_ANNOUNCEMENTS_MVP_PLAN.md §3/§4。
迁移测试使用一次性临时 PostgreSQL 集群，不触碰生产数据库。
每个迁移测试自造自清数据（唯一 entry_url 命名空间 + finally 清理），
乱序或单独运行均可通过。
"""
import os
import shutil
import subprocess
import tempfile
import uuid
from contextlib import closing
from pathlib import Path

import psycopg2
import psycopg2.errors
import pytest

from config import AnnouncementsConfig

SQL_PATH = Path("scripts/watchlist_announcements.sql")


# ── 配置校验单测 ─────────────────────────────────────────

@pytest.fixture
def announcements_env(monkeypatch):
    """清除公告配置环境变量，返回便利构造函数。"""
    keys = (
        "STOCK_ANNOUNCEMENTS_ENABLED",
        "STOCK_ANNOUNCEMENTS_SEC_INTERVAL_MINUTES",
        "STOCK_ANNOUNCEMENTS_IR_INTERVAL_MINUTES",
    )
    for key in keys:
        monkeypatch.delenv(key, raising=False)

    def build(**overrides):
        for key, value in overrides.items():
            env_key = f"STOCK_ANNOUNCEMENTS_{key.upper()}"
            if value is None:
                monkeypatch.delenv(env_key, raising=False)
            else:
                monkeypatch.setenv(env_key, str(value))
        return AnnouncementsConfig()

    return build


class TestAnnouncementsConfigDefaults:
    def test_defaults(self, announcements_env):
        cfg = announcements_env()
        assert cfg.validated_enabled() is False
        assert cfg.validated_sec_interval() == 60
        assert cfg.validated_ir_interval() == 60
        cfg.validate()  # 默认值必须合法

    def test_valid_overrides(self, announcements_env):
        cfg = announcements_env(enabled="true", sec_interval_minutes="30",
                                ir_interval_minutes="120")
        assert cfg.validated_enabled() is True
        assert cfg.validated_sec_interval() == 30
        assert cfg.validated_ir_interval() == 120
        cfg.validate()

    def test_import_preserves_invalid_values(self, announcements_env):
        """与 SECConfig.rate_limit 同理：import/实例化不报错，注册前才校验。"""
        cfg = announcements_env(sec_interval_minutes="bogus")
        assert cfg.sec_interval_minutes == "bogus"


class TestAnnouncementsConfigInvalid:
    @pytest.mark.parametrize("value", ["0", "-5", "abc", "1.5", ""])
    def test_invalid_sec_interval_rejected(self, announcements_env, value):
        cfg = announcements_env(sec_interval_minutes=value)
        with pytest.raises(ValueError, match="SEC_INTERVAL"):
            cfg.validate()

    @pytest.mark.parametrize("value", ["0", "-1", "x"])
    def test_invalid_ir_interval_rejected(self, announcements_env, value):
        cfg = announcements_env(ir_interval_minutes=value)
        with pytest.raises(ValueError, match="IR_INTERVAL"):
            cfg.validate()

    @pytest.mark.parametrize("value", ["maybe", "2", "enabled", ""])
    def test_invalid_enabled_rejected(self, announcements_env, value):
        cfg = announcements_env(enabled=value)
        with pytest.raises(ValueError, match="ENABLED"):
            cfg.validate()

    @pytest.mark.parametrize("value,expected", [
        ("true", True), ("1", True), ("yes", True), ("TRUE", True),
        ("false", False), ("0", False), ("no", False),
    ])
    def test_enabled_accepted_values(self, announcements_env, value, expected):
        cfg = announcements_env(enabled=value)
        assert cfg.validated_enabled() is expected

    def test_no_silent_fallback(self, announcements_env):
        """非法值不得静默回退到默认 60（项目规则：永不静默失败）。"""
        cfg = announcements_env(sec_interval_minutes="0")
        with pytest.raises(ValueError):
            cfg.validated_sec_interval()


# ── 迁移测试（临时 PostgreSQL 集群）──────────────────────

EXPECTED_TABLES = {
    "watchlist_announcement_source",
    "watchlist_announcement",
    "watchlist_announcement_security",
    "watchlist_announcement_check_state",
    "watchlist_announcement_gap",
    "watchlist_announcement_run",
    "watchlist_announcement_recovery",
}


@pytest.fixture(scope="module")
def cluster():
    if not shutil.which("initdb") or not shutil.which("pg_ctl"):
        pytest.skip("migration idempotency tests require initdb/pg_ctl")
    root = Path(tempfile.mkdtemp(prefix="anncpg-"))
    data = root / "data"
    started = False
    try:
        subprocess.run(["initdb", "-D", str(data), "-A", "trust", "--no-locale",
                        "-E", "UTF8"], check=True, capture_output=True)
        result = subprocess.run(
            ["pg_ctl", "-D", str(data), "-l", str(root / "server.log"),
             "-o", f"-k {root} -p 56433 -h '' -F", "-w", "start"], capture_output=True)
        if result.returncode:
            pytest.fail("Temporary PostgreSQL startup failed: " +
                        (root / "server.log").read_text())
        started = True
        yield f"host={root} port=56433 dbname=postgres connect_timeout=2"
    finally:
        if started:
            subprocess.run(["pg_ctl", "-D", str(data), "-m", "immediate", "-w", "stop"],
                           check=True, capture_output=True)
        shutil.rmtree(root)


@pytest.fixture
def db(cluster):
    """每个迁移测试独立应用一次迁移（幂等），保证单独运行也可通过。"""
    sql(cluster, SQL_PATH.read_text())
    return cluster


def sql(dsn, query, args=()):
    with closing(psycopg2.connect(dsn)) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(query, args)
            return cur.fetchall() if cur.description else None


def make_source(dsn, tag, issuer=""):
    """插入本测试专属来源（entry_url 含唯一 tag），返回 source_id。"""
    entry_url = f"https://test.example.com/{tag}/{uuid.uuid4().hex}"
    rows = sql(dsn, """
        INSERT INTO watchlist_announcement_source
            (market, source_kind, issuer_identifier, allowed_domain, entry_url)
        VALUES ('US', 'test', %s, 'test.example.com', %s)
        RETURNING source_id
    """, (issuer, entry_url))
    return rows[0][0]


def cleanup_source(dsn, source_id):
    """按外键依赖顺序清理本测试制造的全部数据。"""
    sql(dsn, """
        DELETE FROM watchlist_announcement_security WHERE announcement_id IN
            (SELECT id FROM watchlist_announcement WHERE source_id = %s)
    """, (source_id,))
    for table in ("watchlist_announcement", "watchlist_announcement_gap",
                  "watchlist_announcement_check_state", "watchlist_announcement_run",
                  "watchlist_announcement_recovery"):
        sql(dsn, f"DELETE FROM {table} WHERE source_id = %s", (source_id,))
    sql(dsn, "DELETE FROM watchlist_announcement_source WHERE source_id = %s",
        (source_id,))


class TestMigrationIdempotency:
    def test_apply_twice_and_objects_complete(self, db):
        sql(db, SQL_PATH.read_text())  # db fixture 已执行一次，再执行不得报错

        rows = sql(db, "SELECT tablename FROM pg_tables WHERE schemaname='public'")
        tables = {r[0] for r in rows}
        missing = EXPECTED_TABLES - tables
        assert not missing, f"迁移后缺少表: {missing}"

        # 公告去重唯一键 (source_id, source_record_id, revision)
        rows = sql(db, """
            SELECT COUNT(*) FROM pg_constraint
            WHERE conrelid = 'watchlist_announcement'::regclass
              AND contype = 'u'
        """)
        assert rows[0][0] >= 1

        # 公告主表不再携带单一 stock_code（证券关联在独立表）
        rows = sql(db, """
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_name = 'watchlist_announcement' AND column_name = 'stock_code'
        """)
        assert rows[0][0] == 0

        # 证券关联表主键 (announcement_id, stock_code)
        rows = sql(db, """
            SELECT COUNT(*) FROM pg_constraint
            WHERE conrelid = 'watchlist_announcement_security'::regclass
              AND contype = 'p'
        """)
        assert rows[0][0] == 1

        # 缺口表部分唯一索引（未解决缺口去重）
        rows = sql(db, """
            SELECT indexname FROM pg_indexes
            WHERE tablename = 'watchlist_announcement_gap'
              AND indexdef LIKE '%%WHERE%%'
        """)
        assert any("gap_open" in r[0] for r in rows)


class TestSourceRegistryDedup:
    def test_blank_issuer_dedup(self, db):
        """空发行人标识（媒体/行业源占位 ''）的同入口重复注册被拒绝。

        issuer_identifier 为 NOT NULL DEFAULT ''：NULL 在 PostgreSQL 唯一约束中
        互不相等会导致去重失效，空串占位使唯一约束真正生效。
        """
        tag = f"dedup-{uuid.uuid4().hex}"
        entry_url = f"https://media.example.com/{tag}"
        source_id = None
        try:
            rows = sql(db, """
                INSERT INTO watchlist_announcement_source
                    (market, source_kind, allowed_domain, entry_url)
                VALUES ('US', 'media', 'media.example.com', %s)
                RETURNING source_id, issuer_identifier
            """, (entry_url,))
            source_id, issuer = rows[0]
            assert issuer == ""  # 缺省落空串占位

            # 幂等写法：同入口重复注册被忽略
            sql(db, """
                INSERT INTO watchlist_announcement_source
                    (market, source_kind, issuer_identifier, allowed_domain, entry_url)
                VALUES ('US', 'media', '', 'media.example.com', %s)
                ON CONFLICT DO NOTHING
            """, (entry_url,))
            rows = sql(db, """
                SELECT COUNT(*) FROM watchlist_announcement_source
                WHERE entry_url = %s
            """, (entry_url,))
            assert rows[0][0] == 1

            # 非幂等写法：重复注册明确报唯一冲突
            with pytest.raises(psycopg2.errors.UniqueViolation):
                sql(db, """
                    INSERT INTO watchlist_announcement_source
                        (market, source_kind, issuer_identifier, allowed_domain,
                         entry_url)
                    VALUES ('US', 'media', '', 'media.example.com', %s)
                """, (entry_url,))
        finally:
            if source_id is not None:
                cleanup_source(db, source_id)


class TestAnnouncementDedup:
    def test_dedup_and_revision(self, db):
        """幂等写入：同 (source_id, source_record_id, revision) 冲突忽略，
        修订版本追加新行，去重键行为不变。"""
        source_id = make_source(db, "announcement-dedup", issuer="TST000001")
        try:
            insert = """
                INSERT INTO watchlist_announcement
                    (market, issuer_identifier, source_id, source_record_id, title,
                     form_type, original_url, published_at, published_at_precision,
                     first_observed_at, last_seen_at, revision)
                VALUES ('US', 'TST000001', %s, 'TST000001-26-000001', '8-K 当前报告',
                        '8-K', 'https://www.sec.gov/Archives/x.htm', NOW(), 'second',
                        NOW(), NOW(), %s)
                ON CONFLICT DO NOTHING
            """
            sql(db, insert, (source_id, 1))
            sql(db, insert, (source_id, 1))  # 重复写入不产生重复行
            sql(db, insert, (source_id, 2))  # 修订版本追加新行
            rows = sql(db, """
                SELECT revision FROM watchlist_announcement
                WHERE source_id = %s ORDER BY revision
            """, (source_id,))
            assert [r[0] for r in rows] == [1, 2]

            # 去重键仍强制唯一：非幂等写法明确报冲突
            with pytest.raises(psycopg2.errors.UniqueViolation):
                sql(db, """
                    INSERT INTO watchlist_announcement
                        (market, issuer_identifier, source_id, source_record_id,
                         title, original_url, first_observed_at, last_seen_at,
                         revision)
                    VALUES ('US', 'TST000001', %s, 'TST000001-26-000001', 'dup',
                            'https://www.sec.gov/Archives/x.htm', NOW(), NOW(), 1)
                """, (source_id,))
        finally:
            cleanup_source(db, source_id)


class TestMultiSecurityAssociation:
    def test_one_announcement_multiple_securities(self, db):
        """同一公告（同一 source_record_id）可关联多个证券代码；
        重复抓取不产生重复公告行或重复关联。"""
        source_id = make_source(db, "multi-security", issuer="TST000002")
        try:
            insert_announcement = """
                INSERT INTO watchlist_announcement
                    (market, issuer_identifier, source_id, source_record_id, title,
                     form_type, original_url, published_at, published_at_precision,
                     first_observed_at, last_seen_at, revision)
                VALUES ('US', 'TST000002', %s, 'TST000002-26-000001',
                        '8-K 当前报告', '8-K',
                        'https://www.sec.gov/Archives/y.htm', NOW(), 'second',
                        NOW(), NOW(), %s)
                ON CONFLICT DO NOTHING
            """
            sql(db, insert_announcement, (source_id, 1))
            announcement_id = sql(db, """
                SELECT id FROM watchlist_announcement
                WHERE source_id = %s AND source_record_id = 'TST000002-26-000001'
                  AND revision = 1
            """, (source_id,))[0][0]

            link = """
                INSERT INTO watchlist_announcement_security
                    (announcement_id, market, stock_code)
                VALUES (%s, 'US', %s)
                ON CONFLICT DO NOTHING
            """
            for code in ("MU", "WDC"):
                sql(db, link, (announcement_id, code))
            # 重复抓取：公告行与关联行均不重复
            sql(db, insert_announcement, (source_id, 1))
            for code in ("MU", "WDC"):
                sql(db, link, (announcement_id, code))

            rows = sql(db, """
                SELECT COUNT(*) FROM watchlist_announcement
                WHERE source_id = %s AND source_record_id = 'TST000002-26-000001'
            """, (source_id,))
            assert rows[0][0] == 1
            rows = sql(db, """
                SELECT stock_code FROM watchlist_announcement_security
                WHERE announcement_id = %s ORDER BY stock_code
            """, (announcement_id,))
            assert [r[0] for r in rows] == ["MU", "WDC"]

            # 修订版本经 (source_id, source_record_id) 继承同一组关联
            sql(db, insert_announcement, (source_id, 2))
            rows = sql(db, """
                SELECT s.stock_code
                FROM watchlist_announcement a
                JOIN watchlist_announcement first_rev
                  ON first_rev.source_id = a.source_id
                 AND first_rev.source_record_id = a.source_record_id
                 AND first_rev.revision = 1
                JOIN watchlist_announcement_security s
                  ON s.announcement_id = first_rev.id
                WHERE a.source_id = %s AND a.revision = 2
                ORDER BY s.stock_code
            """, (source_id,))
            assert [r[0] for r in rows] == ["MU", "WDC"]
        finally:
            cleanup_source(db, source_id)


class TestGapPersistence:
    def test_gap_persistence_and_resolution(self, db):
        """缺口持久表达：未解决缺口去重，补齐后写 resolved_at 不删行。"""
        source_id = make_source(db, "gap", issuer="TST000003")
        try:
            insert = """
                INSERT INTO watchlist_announcement_gap
                    (market, issuer_identifier, source_id, gap_range, reason)
                VALUES ('US', 'TST000003', %s,
                        tstzrange('2026-07-01', '2026-08-01'), 'history_unavailable')
                ON CONFLICT DO NOTHING
            """
            sql(db, insert, (source_id,))
            sql(db, insert, (source_id,))  # 未解决缺口重复记录被忽略
            rows = sql(db, """
                SELECT COUNT(*) FROM watchlist_announcement_gap
                WHERE source_id = %s AND resolved_at IS NULL
            """, (source_id,))
            assert rows[0][0] == 1
            sql(db, """
                UPDATE watchlist_announcement_gap SET resolved_at = NOW()
                WHERE source_id = %s
            """, (source_id,))
            sql(db, insert, (source_id,))  # 补齐后同区间可记录新的未解决缺口
            rows = sql(db, """
                SELECT COUNT(*) FROM watchlist_announcement_gap
                WHERE source_id = %s
            """, (source_id,))
            assert rows[0][0] == 2
        finally:
            cleanup_source(db, source_id)
