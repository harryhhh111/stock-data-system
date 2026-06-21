"""Quality service — 数据质量检查（只看近 5 年 report_date，按市场汇总）。"""
from datetime import datetime

from db import Connection, execute


# 只关注近 5 年的财报数据
_LOOKBACK_YEARS = 5


def ensure_acknowledgments_table() -> None:
    """确保 validation_acknowledgments 表存在。"""
    ddl = """
    CREATE TABLE IF NOT EXISTS validation_acknowledgments (
        id BIGSERIAL PRIMARY KEY,
        validation_result_id BIGINT NOT NULL REFERENCES validation_results(id) ON DELETE CASCADE,
        stock_code TEXT NOT NULL,
        market TEXT NOT NULL,
        check_name TEXT NOT NULL,
        acknowledged_by TEXT,
        acknowledged_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        reason TEXT,
        UNIQUE (validation_result_id)
    );

    CREATE INDEX IF NOT EXISTS idx_validation_ack_result_id
        ON validation_acknowledgments(validation_result_id);
    CREATE INDEX IF NOT EXISTS idx_validation_ack_lookup
        ON validation_acknowledgments(stock_code, check_name, market);
    """
    execute(ddl, commit=True)


def _acknowledged_join(exclude: bool = True) -> tuple[str, str]:
    """返回用于排除已确认记录的 JOIN 和 WHERE 片段。"""
    join_sql = "LEFT JOIN validation_acknowledgments va ON vr.id = va.validation_result_id"
    where_sql = "va.validation_result_id IS NULL" if exclude else "va.validation_result_id IS NOT NULL"
    return join_sql, where_sql


def acknowledge_issue(validation_result_id: int, acknowledged_by: str | None, reason: str | None) -> bool:
    """确认某条校验记录无问题。"""
    # 先读取该记录信息
    with Connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT stock_code, market, check_name FROM validation_results WHERE id = %s",
            (validation_result_id,),
        )
        row = cur.fetchone()
        if not row:
            cur.close()
            return False
        stock_code, market, check_name = row

        cur.execute(
            """INSERT INTO validation_acknowledgments
               (validation_result_id, stock_code, market, check_name, acknowledged_by, reason)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (validation_result_id) DO UPDATE SET
                 acknowledged_by = EXCLUDED.acknowledged_by,
                 reason = EXCLUDED.reason,
                 acknowledged_at = now()""",
            (validation_result_id, stock_code, market, check_name, acknowledged_by, reason),
        )
        conn.commit()
        cur.close()
    return True


def unacknowledge_issue(validation_result_id: int) -> bool:
    """取消确认。"""
    execute(
        "DELETE FROM validation_acknowledgments WHERE validation_result_id = %s",
        (validation_result_id,),
        commit=True,
    )
    return True


def get_acknowledged_ids(validation_result_ids: list[int]) -> set[int]:
    """批量查询哪些校验记录已被确认。"""
    if not validation_result_ids:
        return set()
    with Connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT validation_result_id FROM validation_acknowledgments WHERE validation_result_id = ANY(%s)",
            (validation_result_ids,),
        )
        result = {r[0] for r in cur.fetchall()}
        cur.close()
    return result


def get_summary() -> dict:
    """质量问题汇总（近 5 年所有校验结果，排除已确认），返回 QualitySummary。"""
    with Connection() as conn:
        cur = conn.cursor()

        join_sql, ack_where = _acknowledged_join(exclude=True)

        cur.execute(
            f"""
            SELECT vr.severity, COUNT(*)
            FROM validation_results vr
            {join_sql}
            WHERE vr.report_date >= CURRENT_DATE - interval %s
              AND {ack_where}
            GROUP BY vr.severity
            """,
            (f"{_LOOKBACK_YEARS} years",),
        )
        by_severity = [{"severity": r[0], "count": r[1]} for r in cur.fetchall()]

        cur.execute(
            f"""
            SELECT vr.market, vr.severity, COUNT(*)
            FROM validation_results vr
            {join_sql}
            WHERE vr.report_date >= CURRENT_DATE - interval %s
              AND {ack_where}
            GROUP BY vr.market, vr.severity
            ORDER BY vr.market, vr.severity
            """,
            (f"{_LOOKBACK_YEARS} years",),
        )
        by_market: dict[str, dict] = {}
        for r in cur.fetchall():
            mkt, sev, cnt = r[0], r[1], r[2]
            if mkt not in by_market:
                by_market[mkt] = {"market": mkt, "error": 0, "warning": 0, "info": 0}
            by_market[mkt][sev] = cnt

        cur.execute(
            f"""
            SELECT vr.check_name, vr.severity, COUNT(*)
            FROM validation_results vr
            {join_sql}
            WHERE vr.report_date >= CURRENT_DATE - interval %s
              AND {ack_where}
            GROUP BY vr.check_name, vr.severity
            ORDER BY COUNT(*) DESC
            """,
            (f"{_LOOKBACK_YEARS} years",),
        )
        by_check = [
            {"check_name": r[0], "label": r[0], "severity": r[1], "count": r[2]}
            for r in cur.fetchall()
        ]

        cur.execute(
            f"""
            SELECT MAX(vr.created_at)
            FROM validation_results vr
            {join_sql}
            WHERE vr.report_date >= CURRENT_DATE - interval %s
              AND {ack_where}
            """,
            (f"{_LOOKBACK_YEARS} years",)
        )
        last = cur.fetchone()[0]

        cur.close()

    return {
        "by_severity": by_severity,
        "by_market": list(by_market.values()),
        "by_check": by_check,
        "last_check_at": last.isoformat() if last else None,
    }


def get_issues(
    severity: str | None,
    market: str | None,
    check: str | None,
    limit: int,
    offset: int,
    include_acknowledged: bool = False,
) -> dict:
    """问题列表（近 5 年所有校验结果），默认排除已确认，返回 Paginated<QualityIssue>。"""
    with Connection() as conn:
        cur = conn.cursor()

        join_sql, ack_where = _acknowledged_join(exclude=not include_acknowledged)
        conditions = ["vr.report_date >= CURRENT_DATE - interval %s"]
        params: list = [f"{_LOOKBACK_YEARS} years"]

        if severity:
            conditions.append("vr.severity = %s")
            params.append(severity)
        if market:
            conditions.append("vr.market = %s")
            params.append(market)
        if check:
            conditions.append("vr.check_name = %s")
            params.append(check)

        conditions.append(ack_where)
        where = "WHERE " + " AND ".join(conditions)
        params.extend([limit, offset])

        cur.execute(
            f"""
            SELECT vr.id, vr.batch_id, vr.stock_code,
                   COALESCE(si.stock_name, vr.stock_code) AS stock_name,
                   vr.market,
                   to_char(vr.report_date, 'YYYY-MM-DD') AS report_date,
                   vr.check_name, vr.severity, vr.field_name, vr.actual_value, vr.expected_value,
                   vr.message, vr.suggestion,
                   to_char(vr.created_at, 'YYYY-MM-DD HH24:MI:SS') AS created_at,
                   va.validation_result_id IS NOT NULL AS acknowledged,
                   va.acknowledged_by,
                   va.reason,
                   to_char(va.acknowledged_at, 'YYYY-MM-DD HH24:MI:SS') AS acknowledged_at
            FROM validation_results vr
            LEFT JOIN stock_info si ON vr.stock_code = si.stock_code
            {join_sql}
            {where}
            ORDER BY vr.severity, vr.check_name, vr.stock_code
            LIMIT %s OFFSET %s
            """,
            params,
        )
        items = []
        for row in cur.fetchall():
            items.append({
                "id": row[0],
                "batch_id": row[1],
                "stock_code": row[2],
                "stock_name": row[3],
                "market": row[4],
                "report_date": row[5],
                "check_name": row[6],
                "severity": row[7],
                "field_name": row[8],
                "actual_value": str(row[9]) if row[9] is not None else None,
                "expected_value": str(row[10]) if row[10] is not None else None,
                "message": row[11],
                "suggestion": row[12],
                "created_at": row[13],
                "acknowledged": row[14],
                "acknowledged_by": row[15],
                "acknowledged_reason": row[16],
                "acknowledged_at": row[17],
            })

        # 总数
        count_params = params[: len(params) - 2]
        cur.execute(
            f"""
            SELECT COUNT(*)
            FROM validation_results vr
            {join_sql}
            {where}
            """,
            count_params,
        )
        total = cur.fetchone()[0]
        cur.close()

    return {"items": items, "total": total, "limit": limit, "offset": offset}
