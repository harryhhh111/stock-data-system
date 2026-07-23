"""core/us_financial_exclusion.py — 美股财报 fact exclusion 管理。

提供错误 parser / 业务否决事实的显式排除机制，并保证四种 selector 统一 anti-join。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from db import Connection, execute


EXCLUSION_POLICY_VERSION = "us_fact_exclusion_v1"


def create_exclusion(
    fact_version_id: int,
    reason_code: str,
    reason: str,
    reviewed_by: str,
    batch_id: str | None = None,
    effective_from: datetime | str | None = None,
) -> int:
    """创建一条 active exclusion，返回 exclusion_id。

    同一 fact_version_id + reason_code 只能有一条 active exclusion；
    若已存在，则先撤销旧记录再新增。
    """
    if effective_from is None:
        effective_from = datetime.now()

    with Connection() as conn:
        with conn.cursor() as cur:
            # 撤销同一 fact+reason 的 active 旧记录
            cur.execute(
                """
                UPDATE us_financial_fact_exclusion
                SET status = 'revoked',
                    effective_to = NOW()
                WHERE fact_version_id = %s
                  AND reason_code = %s
                  AND status = 'active'
                """,
                (fact_version_id, reason_code),
            )

            cur.execute(
                """
                INSERT INTO us_financial_fact_exclusion (
                    fact_version_id, batch_id, reason_code, reason, status,
                    effective_from, reviewed_by, reviewed_at
                ) VALUES (%s, %s, %s, %s, 'active', %s, %s, NOW())
                RETURNING exclusion_id
                """,
                (fact_version_id, batch_id, reason_code, reason, effective_from, reviewed_by),
            )
            exclusion_id = cur.fetchone()[0]
        conn.commit()
    return exclusion_id


def get_active_exclusions(stock_codes: list[str] | None = None) -> list[dict[str, Any]]:
    """查询 active exclusion 列表，可选按股票过滤。"""
    sql = """
        SELECT e.exclusion_id, e.fact_version_id, e.batch_id, e.reason_code,
               e.reason, e.effective_from, e.reviewed_by, f.stock_code
        FROM us_financial_fact_exclusion e
        JOIN us_financial_fact_version f ON f.fact_version_id = e.fact_version_id
        WHERE e.status = 'active'
    """
    params: list[Any] = []
    if stock_codes:
        placeholders = ", ".join(["%s"] * len(stock_codes))
        sql += f" AND f.stock_code IN ({placeholders})"
        params.extend(stock_codes)

    rows = execute(sql, tuple(params), fetch=True)
    if not rows:
        return []

    cols = [
        "exclusion_id", "fact_version_id", "batch_id", "reason_code",
        "reason", "effective_from", "reviewed_by", "stock_code",
    ]
    return [dict(zip(cols, row)) for row in rows]


def revoke_exclusion(exclusion_id: int, revoked_by: str) -> bool:
    """撤销一条 active exclusion。"""
    with Connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE us_financial_fact_exclusion
                SET status = 'revoked',
                    effective_to = NOW()
                WHERE exclusion_id = %s AND status = 'active'
                """,
                (exclusion_id,),
            )
            updated = cur.rowcount
        conn.commit()
    return updated > 0


def supersede_exclusion(
    exclusion_id: int,
    superseded_by_fact_id: int,
    reviewed_by: str,
) -> bool:
    """将旧 exclusion 标记为被新 fact supersede。"""
    with Connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE us_financial_fact_exclusion
                SET status = 'superseded',
                    superseded_by_fact_id = %s,
                    effective_to = NOW()
                WHERE exclusion_id = %s AND status = 'active'
                """,
                (superseded_by_fact_id, exclusion_id),
            )
            updated = cur.rowcount
        conn.commit()
    return updated > 0
