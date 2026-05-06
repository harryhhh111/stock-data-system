"""Dashboard service — 仪表板聚合数据。"""
import os
from datetime import date, timedelta

from db import Connection


def get_stats() -> dict:
    """聚合仪表板数据，返回 DashboardStats 结构。"""
    markets = os.getenv("STOCK_MARKETS", "CN_A,CN_HK").split(",")
    with Connection() as conn:
        cur = conn.cursor()

        # 1. 各市场股票总数
        cur.execute(
            "SELECT market, COUNT(*) FROM stock_info WHERE market = ANY(%s) GROUP BY market",
            (markets,)
        )
        total_stocks = {r[0]: r[1] for r in cur.fetchall()}

        # 2. 各市场同步状态
        cur.execute(
            "SELECT market, status, COUNT(*) FROM sync_progress WHERE market = ANY(%s) GROUP BY market, status",
            (markets,)
        )
        sync_status: dict[str, dict] = {}
        for market, status, cnt in cur.fetchall():
            if market not in sync_status:
                sync_status[market] = {"success": 0, "failed": 0, "in_progress": 0, "partial": 0}
            sync_status[market][status] = cnt

        # 3. 近 7 天同步趋势（从 sync_log 聚合）
        seven_days_ago = date.today() - timedelta(days=6)
        cur.execute(
            """
            SELECT started_at::date AS d,
                   COALESCE(SUM(success_count), 0) AS success_total,
                   COALESCE(SUM(fail_count), 0) AS fail_total
            FROM sync_log
            WHERE started_at::date >= %s
            GROUP BY d
            ORDER BY d
            """,
            (seven_days_ago,),
        )
        trend_raw: dict[str, dict] = {}
        for d, success_cnt, fail_cnt in cur.fetchall():
            ds = d.isoformat()
            trend_raw[ds] = {"date": ds, "success": int(success_cnt), "failed": int(fail_cnt)}
        sync_trend: dict[str, list] = {}
        for market in total_stocks:
            sync_trend[market] = []  # 按 market 分组，同步日志无 market 列则放入第一个 market
        # sync_log 不区分 market，归入第一个可用 market 或 "all"
        if total_stocks:
            primary = list(total_stocks.keys())[0]
            sync_trend[primary] = sorted(trend_raw.values(), key=lambda x: x["date"])

        # 4. 数据新鲜度
        cur.execute(
            """
            SELECT si.market, MAX(inc.report_date)
            FROM income_statement inc
            JOIN stock_info si ON inc.stock_code = si.stock_code
            WHERE si.market = ANY(%s)
            GROUP BY si.market
            """,
            (markets,)
        )
        fin_latest = {r[0]: r[1] for r in cur.fetchall()}

        cur.execute(
            "SELECT market, MAX(trade_date) FROM daily_quote WHERE market = ANY(%s) GROUP BY market",
            (markets,)
        )
        quote_latest = {r[0]: r[1] for r in cur.fetchall()}

        # US financial last report date from us_income_statement
        if "US" in markets:
            cur.execute(
                """
                SELECT MAX(inc.report_date)
                FROM us_income_statement inc
                JOIN stock_info si ON inc.stock_code = si.stock_code
                WHERE si.market = 'US'
                """
            )
            us_fin = cur.fetchone()[0]
            if us_fin:
                fin_latest["US"] = us_fin

        # US quote from daily_quote (if synced on this server)
        # already covered by daily_quote query above

        today = date.today()
        finan_stale_days = 90
        quote_stale_days = 5

        freshness = []
        for market in total_stocks:
            f_date = fin_latest.get(market)
            q_date = quote_latest.get(market)
            freshness.append({
                "market": market,
                "financial_date": f_date.isoformat() if f_date else None,
                "quote_date": q_date.isoformat() if q_date else None,
                "financial_stale": (
                    f_date is None or (today - f_date).days > finan_stale_days
                ),
                "quote_stale": (
                    q_date is None or (today - q_date).days > quote_stale_days
                ),
            })

        # 5. 校验问题统计（只看近 5 年 report_date，忽略旧财报数据）
        cur.execute(
            "SELECT COUNT(*) FROM validation_results WHERE market = ANY(%s) AND report_date >= CURRENT_DATE - interval '5 years' AND created_at >= now() - interval '24 hours'",
            (markets,)
        )
        errors_24h = cur.fetchone()[0]

        cur.execute(
            "SELECT COUNT(*) FROM validation_results WHERE market = ANY(%s) AND report_date >= CURRENT_DATE - interval '5 years' AND severity = 'warning' AND created_at >= now() - interval '7 days'",
            (markets,)
        )
        warnings_7d = cur.fetchone()[0]

        # 近 5 年校验问题按严重程度分组
        cur.execute(
            "SELECT severity, COUNT(*) FROM validation_results WHERE market = ANY(%s) AND report_date >= CURRENT_DATE - interval '5 years' GROUP BY severity",
            (markets,)
        )
        severity_counts = {r[0]: r[1] for r in cur.fetchall()}
        validation_breakdown = {
            "errors": severity_counts.get("error", 0),
            "warnings": severity_counts.get("warning", 0),
            "info": severity_counts.get("info", 0),
        }

        # 最近一次校验时间
        cur.execute(
            "SELECT MAX(created_at) FROM validation_results WHERE market = ANY(%s)",
            (markets,)
        )
        last_val = cur.fetchone()[0]

        validation_issues = {
            "errors_24h": errors_24h,
            "warnings_7d": warnings_7d,
            "total_open": sum(severity_counts.values()),
            "breakdown": validation_breakdown,
            "last_check_at": last_val.isoformat() if last_val else None,
        }

        # 6. 今日新增问题数（只看近 5 年 report_date）
        cur.execute(
            "SELECT COUNT(*) FROM validation_results WHERE market = ANY(%s) AND report_date >= CURRENT_DATE - interval '5 years' AND created_at::date = CURRENT_DATE",
            (markets,)
        )
        anomalies_today = cur.fetchone()[0]

        # 7. 最近 10 条问题（只看近 5 年 report_date）
        cur.execute(
            """
            SELECT vr.id, vr.stock_code, COALESCE(si.stock_name, vr.stock_code) AS stock_name,
                   vr.market, vr.severity, vr.check_name, vr.message,
                   to_char(vr.created_at, 'YYYY-MM-DD HH24:MI:SS') AS created_at
            FROM validation_results vr
            LEFT JOIN stock_info si ON vr.stock_code = si.stock_code
            WHERE vr.market = ANY(%s)
              AND vr.report_date >= CURRENT_DATE - interval '5 years'
            ORDER BY vr.created_at DESC
            LIMIT 10
            """,
            (markets,)
        )
        recent_issues = []
        for row in cur.fetchall():
            recent_issues.append({
                "id": row[0],
                "stock_code": row[1],
                "stock_name": row[2],
                "market": row[3],
                "severity": row[4],
                "check_name": row[5],
                "message": row[6],
                "created_at": row[7],
            })

        cur.close()

    return {
        "total_stocks": total_stocks,
        "sync_status": sync_status,
        "sync_trend": sync_trend,
        "validation_issues": validation_issues,
        "anomalies_today": anomalies_today,
        "freshness": freshness,
        "recent_issues": recent_issues,
    }
