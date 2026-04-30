"""Quality service — 数据质量检查。"""
from db import Connection


def get_summary(days: int = 7) -> dict:
    """质量问题汇总，返回 QualitySummary。"""
    with Connection() as conn:
        cur = conn.cursor()

        cur.execute(
            """
            SELECT severity, COUNT(*) FROM validation_results
            WHERE created_at >= now() - interval '%s days'
            GROUP BY severity
            """,
            (days,),
        )
        by_severity = [{"severity": r[0], "count": r[1]} for r in cur.fetchall()]

        cur.execute(
            """
            SELECT check_name, severity, COUNT(*)
            FROM validation_results
            WHERE created_at >= now() - interval '%s days'
            GROUP BY check_name, severity
            ORDER BY COUNT(*) DESC
            """,
            (days,),
        )
        by_check = [
            {"check_name": r[0], "label": r[0], "severity": r[1], "count": r[2]}
            for r in cur.fetchall()
        ]

        cur.execute("SELECT MAX(created_at) FROM validation_results")
        last = cur.fetchone()[0]

        cur.close()

    return {
        "by_severity": by_severity,
        "by_check": by_check,
        "last_check_at": last.isoformat() if last else None,
    }


def get_issues(
    severity: str | None,
    market: str | None,
    check: str | None,
    limit: int,
    offset: int,
) -> dict:
    """问题列表，返回 Paginated<QualityIssue>。"""
    with Connection() as conn:
        cur = conn.cursor()

        conditions = []
        params: list = []

        if severity:
            conditions.append("severity = %s")
            params.append(severity)
        if market:
            conditions.append("market = %s")
            params.append(market)
        if check:
            conditions.append("check_name = %s")
            params.append(check)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params.extend([limit, offset])

        cur.execute(
            f"""
            SELECT vr.id, vr.batch_id, vr.stock_code,
                   COALESCE(si.stock_name, vr.stock_code) AS stock_name,
                   vr.market,
                   to_char(vr.report_date, 'YYYY-MM-DD') AS report_date,
                   vr.check_name, vr.severity, vr.field_name, vr.actual_value, vr.expected_value,
                   vr.message, vr.suggestion,
                   to_char(vr.created_at, 'YYYY-MM-DD HH24:MI:SS') AS created_at
            FROM validation_results vr
            LEFT JOIN stock_info si ON vr.stock_code = si.stock_code
            {where}
            ORDER BY vr.created_at DESC
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
            })

        # 总数
        count_params = params[: len(params) - 2]
        cur.execute(
            f"SELECT COUNT(*) FROM validation_results vr {where}",
            count_params,
        )
        total = cur.fetchone()[0]
        cur.close()

    return {"items": items, "total": total, "limit": limit, "offset": offset}
