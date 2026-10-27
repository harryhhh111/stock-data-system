"""US 证券目录：离线 SEC 映射 + 隔离 PostgreSQL 真实事务测试。"""
from __future__ import annotations

import getpass
import json
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import date
from pathlib import Path

import pandas as pd
import psycopg2
import pytest

import config
import db
from core.sync import us_security_directory as directory
from core.us_security_identity import resolve_us_cik
from web.services import watchlist_service
from web.wrappers.analyzer_wrapper import search_stocks

UTC = "UTC"


def sql(dsn, query, args=()):
    with closing(psycopg2.connect(dsn)) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(query, args)
            return cur.fetchall() if cur.description else None


@pytest.fixture(scope="module")
def cluster():
    if not shutil.which("initdb") or not shutil.which("pg_ctl"):
        pytest.skip("US directory tests require initdb/pg_ctl")
    root = Path(tempfile.mkdtemp(prefix="us-directory-"))
    data = root / "data"
    started = False
    try:
        subprocess.run(["initdb", "-D", str(data), "-A", "trust", "--no-locale",
                        "-E", "UTF8"], check=True, capture_output=True)
        result = subprocess.run(
            ["pg_ctl", "-D", str(data), "-l", str(root / "server.log"),
             "-o", f"-k {root} -p 56440 -h '' -F", "-w", "start"],
            capture_output=True)
        if result.returncode:
            pytest.fail("Temporary PostgreSQL startup failed: " +
                        (root / "server.log").read_text())
        started = True
        dsn = f"host={root} port=56440 dbname=postgres user={getpass.getuser()} connect_timeout=2"
        sql(dsn, "SET TIME ZONE 'UTC'")
        sql(dsn, """
            CREATE TABLE stock_info (
                stock_code VARCHAR(20) PRIMARY KEY,
                stock_name VARCHAR(100) NOT NULL,
                market VARCHAR(10) NOT NULL,
                cik VARCHAR(20), currency VARCHAR(10) DEFAULT 'CNY',
                list_date DATE, delist_date DATE, industry VARCHAR(100),
                exchange VARCHAR(20), updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        sql(dsn, """
            CREATE TABLE us_security_ticker_symbol (
                market VARCHAR(10) NOT NULL DEFAULT 'US',
                ticker VARCHAR(20) PRIMARY KEY,
                canonical_stock_code VARCHAR(20) NOT NULL,
                cik VARCHAR(10) NOT NULL,
                symbol_role VARCHAR(10) NOT NULL
            )
        """)
        sql(dsn, "CREATE TABLE us_filing (stock_code VARCHAR(20), cik VARCHAR(20))")
        sql(dsn, "CREATE TABLE us_financial_fact_version (stock_code VARCHAR(20), cik VARCHAR(20))")
        sql(dsn, """
            CREATE TABLE watchlist_item (
                id BIGSERIAL PRIMARY KEY, market VARCHAR(10) NOT NULL,
                stock_code VARCHAR(20) NOT NULL, stock_name VARCHAR(200) NOT NULL,
                group_name VARCHAR(80) NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
                research_url TEXT NOT NULL DEFAULT '', sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE(market, stock_code)
            )
        """)
        yield dsn
    finally:
        if started:
            subprocess.run(["pg_ctl", "-D", str(data), "-m", "immediate", "-w", "stop"],
                           check=True, capture_output=True)
        shutil.rmtree(root)


@pytest.fixture
def env(cluster, monkeypatch, tmp_path):
    host = cluster.split()[0].split("=")[1]
    monkeypatch.setattr(config.db, "host", host)
    monkeypatch.setattr(config.db, "port", 56440)
    monkeypatch.setattr(config.db, "dbname", "postgres")
    monkeypatch.setattr(config.db, "user", getpass.getuser())
    monkeypatch.setattr(config.db, "password", "")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "sec_cache")
    config.DATA_DIR.mkdir()
    (config.DATA_DIR / "company_tickers.json").write_text('{"source": "fixture"}')
    db.close_pool()
    db._pool = None

    def mapping(entries):
        grouped = {}
        for ticker, title, cik in entries:
            grouped.setdefault(ticker, []).append({
                "raw_ticker": ticker, "ticker": ticker,
                "title": title, "cik": cik,
            })
        return grouped

    def load(entries):
        grouped = mapping(entries)
        monkeypatch.setattr(directory, "_load_company_mapping", lambda: (
            grouped, {"path": "fixture", "sha256": "abc", "row_count": len(entries)}))
        return grouped

    yield cluster, load, tmp_path
    db.close_pool()
    db._pool = None
    for table in ("watchlist_item", "us_financial_fact_version", "us_filing",
                  "us_security_ticker_symbol", "stock_info"):
        sql(cluster, f"DELETE FROM {table}")


def run(env, codes, *, apply=False, raw=None, resolved=None):
    dsn, _load, tmp_path = env
    raw = set(codes) if raw is None else set(raw)
    resolved = {code: code for code in raw} if resolved is None else resolved
    return directory.sync_us_security_directory(
        planned_scope=codes, raw_index_tickers=raw, resolved_map=resolved,
        index_sources={"TEST": {"mode": "fixture"}}, apply=apply,
        evidence_root=tmp_path / "evidence")


def test_crwv_preview_apply_search_watchlist_and_history_unchanged(env):
    dsn, load, _tmp = env
    load([("CRWV", "CoreWeave, Inc.", "0001769628")])
    sql(dsn, "INSERT INTO us_filing VALUES ('CRWV', '0001769628')")
    sql(dsn, "INSERT INTO us_financial_fact_version VALUES ('CRWV', '0001769628')")
    before = sql(dsn, "SELECT COUNT(*) FROM us_financial_fact_version")[0][0]

    preview = run(env, ["CRWV"])
    assert preview["status"] == "success"
    assert preview["inserted"] == []
    assert preview["items"][0]["insert_result"] == "preview_not_applied"
    assert sql(dsn, "SELECT COUNT(*) FROM stock_info")[0][0] == 0

    applied = run(env, ["CRWV"], apply=True)
    assert applied["status"] == "success" and applied["inserted"] == ["CRWV"]
    row = sql(dsn, "SELECT stock_name, market, cik, currency, list_date, industry "
                   "FROM stock_info WHERE stock_code='CRWV'")
    assert row == [("CoreWeave, Inc.", "US", "0001769628", None, None, None)]
    assert search_stocks("CoreWeave", "US")[0]["stock_code"] == "CRWV"
    assert search_stocks("CRWV", "US")[0]["stock_name"] == "CoreWeave, Inc."
    saved = watchlist_service.save_item({
        "market": "US", "stock_code": "CRWV", "stock_name": "CoreWeave, Inc.",
        "group_name": "", "note": "", "research_url": "",
    })
    assert saved["id"] > 0
    assert resolve_us_cik("CRWV") == "0001769628"
    assert sql(dsn, "SELECT COUNT(*) FROM us_financial_fact_version")[0][0] == before

    again = run(env, ["CRWV"], apply=True)
    assert again["status"] == "success" and again["inserted"] == []
    assert again["existing"] == ["CRWV"]
    assert sql(dsn, "SELECT COUNT(*) FROM stock_info")[0][0] == 1


def test_index_removal_does_not_delete_or_delist(env):
    dsn, load, _tmp = env
    load([("CRWV", "CoreWeave, Inc.", "0001769628"),
          ("NEW", "New Company", "0000000002")])
    run(env, ["CRWV"], apply=True)
    run(env, ["NEW"], apply=True)
    rows = sql(dsn, "SELECT stock_code, delist_date FROM stock_info ORDER BY stock_code")
    assert rows == [("CRWV", None), ("NEW", None)]


def test_cross_market_code_and_batch_cik_conflicts(env):
    dsn, load, _tmp = env
    load([("CRWV", "CoreWeave, Inc.", "0001769628")])
    sql(dsn, "INSERT INTO stock_info (stock_code, stock_name, market) "
             "VALUES ('CRWV', 'A share', 'CN_A')")
    result = run(env, ["CRWV"], apply=True)
    assert result["status"] == "partial"
    assert "CN_A" in result["items"][0]["reasons"][0]
    assert sql(dsn, "SELECT market FROM stock_info")[0][0] == "CN_A"

    sql(dsn, "DELETE FROM stock_info")
    load([("AAA", "A", "0000000001"), ("BBB", "B", "0000000001")])
    result = run(env, ["AAA", "BBB"], apply=True)
    assert result["status"] == "partial"
    assert set(result["blocked_tickers"]) == {"AAA", "BBB"}
    assert sql(dsn, "SELECT COUNT(*) FROM stock_info")[0][0] == 0


def test_normalized_collision_history_conflict_and_delisted(env):
    dsn, load, _tmp = env
    grouped = load([("CRWV", "CoreWeave, Inc.", "0001769628")])
    grouped["CRWV"].append({"raw_ticker": "CRW.V", "ticker": "CRWV",
                            "title": "Other", "cik": "0000000002"})
    result = run(env, ["CRWV"], apply=True)
    assert result["status"] == "partial"
    assert "碰撞" in result["items"][0]["reasons"][0]

    grouped["CRWV"] = grouped["CRWV"][:1]
    sql(dsn, "INSERT INTO us_filing VALUES ('CRWV', '0000000009')")
    result = run(env, ["CRWV"], apply=True)
    assert result["status"] == "partial"
    assert any("历史" in reason for reason in result["items"][0]["reasons"])

    sql(dsn, "DELETE FROM us_filing")
    sql(dsn, "INSERT INTO stock_info (stock_code, stock_name, market, cik, delist_date) "
             "VALUES ('CRWV', 'Old', 'US', '0001769628', '2026-01-01')")
    result = run(env, ["CRWV"], apply=True)
    assert result["status"] == "partial"
    assert sql(dsn, "SELECT stock_name, delist_date FROM stock_info")[0][0] == "Old"


def test_legacy_alias_does_not_create_second_security(env):
    dsn, load, _tmp = env
    load([("EQR", "Equity Residential", "0000906107")])
    sql(dsn, "INSERT INTO stock_info (stock_code, stock_name, market, cik) "
             "VALUES ('EQR', 'Equity Residential', 'US', '0000906107')")
    sql(dsn, "INSERT INTO us_security_ticker_symbol "
             "(ticker, canonical_stock_code, cik, symbol_role) "
             "VALUES ('VMRK', 'EQR', '0000906107', 'legacy')")
    result = run(env, ["EQR"], apply=True, raw={"VMRK"}, resolved={"VMRK": "EQR"})
    assert result["status"] == "success"
    assert sql(dsn, "SELECT COUNT(*) FROM stock_info")[0][0] == 1


def test_source_failure_keeps_only_independently_safe_existing(env):
    dsn, load, _tmp = env
    load([])
    sql(dsn, "INSERT INTO stock_info (stock_code, stock_name, market, cik) "
             "VALUES ('OLD', 'Existing', 'US', '0000000001')")
    monkeypatch_error = directory.DirectorySyncError("SEC unavailable")
    import unittest.mock as mock
    with mock.patch.object(directory, "_load_company_mapping", side_effect=monkeypatch_error):
        result = run(env, ["OLD", "NEW"], apply=True)
    assert result["status"] == "failed" and result["complete"] is False
    assert result["collectable_scope"] == ["OLD"]
    assert set(result["blocked_tickers"]) == {"NEW"}


def test_identity_change_after_precheck_is_rejected(env, monkeypatch):
    dsn, load, _tmp = env
    load([("NEW", "New Company", "0000000002")])
    real = directory._decide
    calls = 0

    def wrapped(*args, **kwargs):
        nonlocal calls
        calls += 1
        items, warnings = real(*args, **kwargs)
        if calls == 1:
            sql(dsn, "INSERT INTO stock_info (stock_code, stock_name, market, cik) "
                     "VALUES ('OTHER', 'Other', 'US', '0000000002')")
        return items, warnings

    monkeypatch.setattr(directory, "_decide", wrapped)
    result = run(env, ["NEW"], apply=True)
    assert result["status"] == "partial"
    assert calls == 2
    assert sql(dsn, "SELECT COUNT(*) FROM stock_info WHERE stock_code='NEW'")[0][0] == 0


def test_failed_identity_recheck_invalidates_precheck_collectable(env, monkeypatch):
    dsn, load, _tmp = env
    load([("NEW", "New Company", "0000000002")])
    real = directory._decide
    calls = 0

    def wrapped(*args, **kwargs):
        nonlocal calls
        calls += 1
        items, warnings = real(*args, **kwargs)
        if calls == 1:
            sql(dsn, "INSERT INTO us_security_ticker_symbol "
                     "(ticker, canonical_stock_code, cik, symbol_role) "
                     "VALUES ('NEW', 'MISSING', '0000000002', 'current')")
        return items, warnings

    monkeypatch.setattr(directory, "_decide", wrapped)
    result = run(env, ["NEW"], apply=True)
    assert result["status"] == "failed"
    assert "身份表冲突" in result["transaction_error"]
    assert result["collectable_scope"] == []
    assert result["complete"] is False


def test_concurrent_apply_inserts_once(env):
    dsn, load, _tmp = env
    load([("CRWV", "CoreWeave, Inc.", "0001769628")])
    assert db.health_check()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _i: run(env, ["CRWV"], apply=True), range(2)))
    assert [r["status"] for r in results] == ["success", "success"]
    assert sum(len(r["inserted"]) for r in results) == 1
    assert sql(dsn, "SELECT COUNT(*) FROM stock_info")[0][0] == 1


def test_transaction_error_rolls_back_whole_batch(env):
    dsn, load, _tmp = env
    load([("AAA", "A", "0000000001"), ("BBB", "B", "0000000002")])
    sql(dsn, """
        CREATE FUNCTION fail_b_directory() RETURNS trigger AS $$
        BEGIN
            IF NEW.stock_code = 'BBB' THEN RAISE EXCEPTION 'forced directory failure'; END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    sql(dsn, "CREATE TRIGGER fail_b BEFORE INSERT ON stock_info "
             "FOR EACH ROW EXECUTE FUNCTION fail_b_directory()")
    result = run(env, ["AAA", "BBB"], apply=True)
    assert result["status"] == "failed" and result["inserted"] == []
    assert sql(dsn, "SELECT COUNT(*) FROM stock_info")[0][0] == 0
    sql(dsn, "DROP TRIGGER fail_b ON stock_info")
    sql(dsn, "DROP FUNCTION fail_b_directory()")


def test_downstream_consumers_preserve_missing_data_boundaries(env, monkeypatch):
    dsn, load, _tmp = env
    load([("CRWV", "CoreWeave, Inc.", "0001769628")])
    run(env, ["CRWV"], apply=True)
    sql(dsn, """
        ALTER TABLE us_financial_fact_version
            ADD fact_version_id INTEGER,
            ADD statement VARCHAR(20), ADD standard_field VARCHAR(100),
            ADD period_kind VARCHAR(10), ADD period_start DATE,
            ADD report_date DATE, ADD unit VARCHAR(50),
            ADD value_hash TEXT, ADD value_numeric NUMERIC, ADD value_text TEXT,
            ADD accession_no TEXT, ADD form VARCHAR(20), ADD filed_date DATE,
            ADD dimensions JSONB, ADD sec_tag TEXT, ADD context_hash TEXT,
            ADD fiscal_period_raw TEXT
    """)
    sql(dsn, """
        CREATE TABLE us_financial_fact_exclusion (
            fact_version_id INTEGER, reason_code TEXT,
            effective_from TIMESTAMPTZ, status TEXT
        )
    """)
    sql(dsn, """
        INSERT INTO us_financial_fact_version (
            fact_version_id, stock_code, cik, statement, standard_field,
            period_kind, period_start, report_date, unit, value_hash,
            value_numeric, accession_no, form, filed_date, dimensions,
            sec_tag, context_hash, fiscal_period_raw
        ) VALUES (
            1, 'CRWV', '0001769628', 'income', 'revenues',
            'duration', '2024-01-01', '2024-12-31', 'USD', 'h1',
            100, 'accn-1', '10-K', '2025-02-20', '{}'::jsonb,
            'Revenues', 'ctx1', 'FY'
        )
    """)
    from core.selectors.us_financial import USFactSelector
    from quant.backtest import us_pit_source as pit
    from quant.backtest.common import build_universe as merge_quotes
    from quant.screener.filters import apply_hard_filters
    from scripts import project_us_financial_snapshots as projection
    monkeypatch.setattr(USFactSelector, "_load_restatement_reviews",
                        lambda self, ids: {})

    full = pit.load_fact_rows(min_filed_date="2020-01-01",
                              min_report_date="2020-01-01", chunk_size=1)
    assert [row["stock_code"] for row in full] == ["CRWV"]
    selected = pit.select_as_of_from_db(date(2025, 3, 1), chunk_size=1)
    assert [row.stock_code for row in selected] == ["CRWV"]
    assert all(row.filed_date <= date(2025, 3, 1) for row in selected)
    assert pit.select_as_of_from_db(date(2020, 1, 1), chunk_size=1) == []

    monkeypatch.setattr(projection, "_fetch_facts", lambda codes, fields: [])
    projected = projection.run_projection(dry_run=True)
    assert projected["stocks"] == 1
    assert projected["annual_rows"] == 0 and projected["ttm_rows"] == 0
    assert projected["dry_run"] is True

    info = pd.DataFrame([{
        "stock_code": "CRWV", "stock_name": "CoreWeave, Inc.", "market": "US",
        "industry": None, "list_date": None,
    }])
    shares = pd.DataFrame(columns=["stock_code", "trade_date", "total_shares"])
    base = pit.build_universe(selected, date(2025, 3, 1), info, shares)
    assert pd.isna(base.iloc[0]["days_since_list"])
    quote = pd.DataFrame(columns=[
        "stock_code", "close", "market_cap", "pe_ttm", "pb", "currency"])
    universe = merge_quotes(base, quote)
    row = universe.iloc[0]
    assert pd.isna(row["close"]) and pd.isna(row["market_cap"])
    filtered, _, _ = apply_hard_filters(
        universe, {"market_cap_min": 1, "min_days_since_list": 1})
    assert filtered.empty

    sql(dsn, "CREATE TABLE daily_quote (stock_code VARCHAR(20), trade_date DATE, "
             "market VARCHAR(10), close NUMERIC)")
    quotes = watchlist_service.quote_batch("US", ["CRWV"])["quotes"][0]
    assert quotes["status"] == "missing"
    assert quotes["close"] is None and quotes["trend"] == []

    sql(dsn, Path("scripts/us_financial_snapshots.sql").read_text())
    sql(dsn, "ALTER TABLE daily_quote ADD market_cap NUMERIC, ADD currency VARCHAR(10)")
    from quant.analyzer import query_us
    from quant.screener import query as screener_query
    monkeypatch.setenv("US_FINANCIAL_VERSION_CURRENT", "1")
    monkeypatch.setenv("US_SCREENER_SNAPSHOT_CURRENT", "1")
    analyzer_row = query_us.get_stock_info("CRWV", "US").iloc[0]
    assert analyzer_row["financial_data_status"] == query_us.STATUS_SNAPSHOT_UNAVAILABLE
    for column in ("close", "market_cap", "pe_ttm", "pb", "fcf_yield"):
        assert pd.isna(analyzer_row[column])
    screener_row = screener_query.get_us_universe_snapshot().iloc[0]
    assert screener_row["stock_code"] == "CRWV"
    assert screener_row["financial_data_status"] == query_us.STATUS_SNAPSHOT_UNAVAILABLE
    for column in ("close", "market_cap", "days_since_list", "quote_currency"):
        assert pd.isna(screener_row[column])
