"""TTM NULL 回退口径回归测试（docs/quant/TTM_NULL_FALLBACK_PLAN.md §6）。

事务内造 synthetic 夹具 + REFRESH 两个物化视图 + 断言 + ROLLBACK，
不污染生产数据。迁移未执行（无 *_ttm_estimated 列）的环境整模块跳过。
"""
import pytest

import db

S_FALLBACK = "99990"  # 最新中报 capex NULL → 回退年报值，established=True
S_REALTTM = "99991"   # 三期齐全 → 真 TTM，estimated=False
S_ANNUAL = "99992"    # 年报最新 → 不受影响控制组

_ALL = (S_FALLBACK, S_REALTTM, S_ANNUAL)


def _migration_applied() -> bool:
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid "
            "WHERE c.relname='mv_indicator_ttm' AND a.attname='capex_ttm_estimated'"
        )
        return cur.fetchone() is not None
    finally:
        db.release_connection(conn)


pytestmark = pytest.mark.skipif(
    not _migration_applied(),
    reason="mv_ttm_null_fallback 迁移未执行（缺 capex_ttm_estimated 列）",
)


def _seed(conn) -> None:
    cur = conn.cursor()
    for code in _ALL:
        cur.execute(
            "INSERT INTO stock_info (stock_code, stock_name, market, currency) "
            "VALUES (%s, %s, 'CN_HK', 'HKD')",
            (code, f"TTMTEST{code}"),
        )
    # S_FALLBACK：2024中报 / 2024年报 / 2025中报(capex NULL)
    # S_REALTTM： 三期齐全
    # S_ANNUAL：  仅两期年报
    rows_is = [
        (S_FALLBACK, "2024-06-30", "semi", 100, 10, 10),
        (S_FALLBACK, "2024-12-31", "annual", 300, 40, 40),
        (S_FALLBACK, "2025-06-30", "semi", 200, 20, 20),
        (S_REALTTM, "2024-06-30", "semi", 100, 10, 10),
        (S_REALTTM, "2024-12-31", "annual", 300, 40, 40),
        (S_REALTTM, "2025-06-30", "semi", 200, 20, 20),
        (S_ANNUAL, "2024-12-31", "annual", 300, 40, 40),
        (S_ANNUAL, "2025-12-31", "annual", 350, 45, 45),
    ]
    for code, dt, rt, rev, pnp, npe in rows_is:
        cur.execute(
            "INSERT INTO income_statement (stock_code, report_date, report_type,"
            " operating_revenue, parent_net_profit, net_profit_excl)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (code, dt, rt, rev, pnp, npe),
        )
    rows_cf = [
        (S_FALLBACK, "2024-06-30", "semi", 50, 5),
        (S_FALLBACK, "2024-12-31", "annual", 150, 20),
        (S_FALLBACK, "2025-06-30", "semi", 80, None),
        (S_REALTTM, "2024-06-30", "semi", 50, 5),
        (S_REALTTM, "2024-12-31", "annual", 150, 20),
        (S_REALTTM, "2025-06-30", "semi", 80, 8),
        (S_ANNUAL, "2024-12-31", "annual", 150, 20),
        (S_ANNUAL, "2025-12-31", "annual", 160, 25),
    ]
    for code, dt, rt, cfo, capex in rows_cf:
        cur.execute(
            "INSERT INTO cash_flow_statement (stock_code, report_date, report_type,"
            " cfo_net, capex) VALUES (%s, %s, %s, %s, %s)",
            (code, dt, rt, cfo, capex),
        )
    for code in _ALL:
        cur.execute(
            "INSERT INTO daily_quote (stock_code, trade_date, market, close,"
            " market_cap) VALUES (%s, '2025-10-01', 'CN_HK', 1.0, 1000000000)",
            (code,),
        )


def _query(conn, sql, code):
    cur = conn.cursor()
    cur.execute(sql, (code,))
    return cur.fetchone()


def test_ttm_null_fallback_semantics():
    conn = db.get_connection()
    try:
        _seed(conn)
        cur = conn.cursor()
        cur.execute("REFRESH MATERIALIZED VIEW mv_indicator_ttm")
        cur.execute("REFRESH MATERIALIZED VIEW mv_fcf_yield")

        # S_FALLBACK：capex 回退年报 20；cfo 三分量齐全 → 真 TTM 180；revenue 真 TTM 400
        r = _query(
            conn,
            "SELECT capex_ttm, capex_ttm_estimated, cfo_ttm, cfo_ttm_estimated,"
            " revenue_ttm, revenue_ttm_estimated FROM mv_indicator_ttm"
            " WHERE stock_code=%s",
            S_FALLBACK,
        )
        assert float(r[0]) == 20.0 and r[1] is True, f"capex 回退失败: {r[:2]}"
        assert float(r[2]) == 180.0 and r[3] is False, f"cfo 应走真 TTM: {r[2:4]}"
        assert float(r[4]) == 400.0 and r[5] is False, f"revenue 应走真 TTM: {r[4:]}"

        # S_REALTTM：capex_ttm = 8 + 20 − 5 = 23，分量标志查 mv_indicator_ttm
        r = _query(
            conn,
            "SELECT capex_ttm, capex_ttm_estimated, cfo_ttm, cfo_ttm_estimated"
            " FROM mv_indicator_ttm WHERE stock_code=%s",
            S_REALTTM,
        )
        assert float(r[0]) == 23.0 and r[1] is False, f"真 TTM capex: {r[:2]}"
        assert float(r[2]) == 180.0 and r[3] is False
        # fcf 综合标志查 mv_fcf_yield
        r = _query(
            conn,
            "SELECT fcf_ttm, fcf_ttm_estimated FROM mv_fcf_yield WHERE stock_code=%s",
            S_REALTTM,
        )
        assert float(r[0]) == 157.0 and r[1] is False

        # S_FALLBACK 在 fcf 视图：fcf = cfo − 回退capex = 160，综合标志 True
        r = _query(
            conn,
            "SELECT fcf_ttm, fcf_ttm_estimated FROM mv_fcf_yield WHERE stock_code=%s",
            S_FALLBACK,
        )
        assert float(r[0]) == 160.0 and r[1] is True, f"fcf 综合标志: {r}"

        # S_ANNUAL 控制组：年报最新，分量标志查 mv_indicator_ttm
        r = _query(
            conn,
            "SELECT capex_ttm, capex_ttm_estimated FROM mv_indicator_ttm"
            " WHERE stock_code=%s",
            S_ANNUAL,
        )
        assert float(r[0]) == 25.0 and r[1] is False
        r = _query(
            conn,
            "SELECT fcf_ttm_estimated FROM mv_fcf_yield WHERE stock_code=%s",
            S_ANNUAL,
        )
        assert r[0] is False
    finally:
        conn.rollback()
        db.release_connection(conn)
