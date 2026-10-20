"""core/announcements/store.py — 公告存储写入（schema 见 scripts/watchlist_announcements.sql）。

所有函数接收调用方提供的 psycopg2 连接；事务边界由调用方控制
（方案 §3.1：最终数据、覆盖记录与新检查点必须在同一事务中提交；
业务写入使用持锁会话的短事务，不经过连接池）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable

import psycopg2.extras

logger = logging.getLogger(__name__)

SEC_SOURCE_KIND = "sec"
SEC_ALLOWED_DOMAIN = "sec.gov"  # 覆盖 data.sec.gov 与 www.sec.gov 子域
SEC_ENTRY_URL = "https://data.sec.gov/submissions/CIK{cik}.json"


# ── tstzrange[] 字面量 ─────────────────────────────────────

def to_tstzrange_array(ranges: Iterable[tuple[datetime, datetime]]) -> str:
    """把 [(start, end), ...] 转成 tstzrange[] 字面量（左闭右开）。"""
    parts = []
    for start, end in ranges:
        s = start.astimezone(timezone.utc).isoformat()
        e = end.astimezone(timezone.utc).isoformat()
        parts.append(f'"[{s},{e})"')
    return "{" + ",".join(parts) + "}"


def read_ranges(conn, table: str, column: str, key: dict) -> list[tuple[datetime, datetime]]:
    """读取某行的区间数组列为 [(lower, upper), ...]。"""
    where = " AND ".join(f"{k} = %s" for k in key)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT lower(r), upper(r) FROM {table} "
            f"CROSS JOIN LATERAL unnest({column}) r WHERE {where} ORDER BY lower(r)",
            tuple(key.values()),
        )
        return [(row[0], row[1]) for row in cur.fetchall()]


# ── 来源注册表 ─────────────────────────────────────────────

def ensure_sec_source(conn, cik: str, *, parser_version: int) -> int:
    """注册（或复用）某发行人的 SEC 来源行，返回 source_id。

    SEC 来源对具备有效 CIK 的关注公司自动启用；IR 来源由后续片逐家启用。
    """
    entry_url = SEC_ENTRY_URL.format(cik=cik)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO watchlist_announcement_source
                (market, source_kind, issuer_identifier, allowed_domain, entry_url,
                 source_timezone, parser_version, enabled, source_status)
            VALUES ('US', %s, %s, %s, %s, 'America/New_York', %s, true, 'enabled')
            ON CONFLICT (market, source_kind, issuer_identifier, entry_url)
            DO NOTHING
            """,
            (SEC_SOURCE_KIND, cik, SEC_ALLOWED_DOMAIN, entry_url, parser_version),
        )
        cur.execute(
            """
            SELECT source_id FROM watchlist_announcement_source
            WHERE market = 'US' AND source_kind = %s AND issuer_identifier = %s
              AND entry_url = %s
            """,
            (SEC_SOURCE_KIND, cik, entry_url),
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError(f"SEC 来源注册失败: CIK {cik}")
    return int(row[0])


# ── 来源检查状态 ───────────────────────────────────────────

def load_check_state(conn, issuer: str, source_id: int) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT effective_interval_minutes, last_attempt_at, last_attempt_status,
                   last_success_at, next_due_at, checked_through
            FROM watchlist_announcement_check_state
            WHERE market = 'US' AND issuer_identifier = %s AND source_id = %s
            """,
            (issuer, source_id),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "effective_interval_minutes": row[0],
        "last_attempt_at": row[1],
        "last_attempt_status": row[2],
        "last_success_at": row[3],
        "next_due_at": row[4],
        "checked_through": row[5],
    }


def upsert_check_state_attempt(
    conn, issuer: str, source_id: int, *, interval_minutes: int,
    attempt_at: datetime, status: str, next_due_at: datetime,
    error_class: str | None = None,
) -> None:
    """记录一次尝试（不推进 checked_through / last_success_at）。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO watchlist_announcement_check_state
                (market, issuer_identifier, source_id, effective_interval_minutes,
                 last_attempt_at, last_attempt_status, next_due_at, last_error_class)
            VALUES ('US', %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (market, issuer_identifier, source_id) DO UPDATE SET
                effective_interval_minutes = EXCLUDED.effective_interval_minutes,
                last_attempt_at = EXCLUDED.last_attempt_at,
                last_attempt_status = EXCLUDED.last_attempt_status,
                next_due_at = EXCLUDED.next_due_at,
                last_error_class = EXCLUDED.last_error_class,
                updated_at = NOW()
            """,
            (issuer, source_id, interval_minutes, attempt_at, status,
             next_due_at, error_class),
        )


def upsert_check_state_success(
    conn, issuer: str, source_id: int, *, interval_minutes: int,
    attempt_at: datetime, status: str, next_due_at: datetime,
    checked_through: datetime, covered_add: tuple[datetime, datetime],
) -> None:
    """完整成功：与最终数据在同一事务中推进 checked_through 与覆盖区间。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO watchlist_announcement_check_state
                (market, issuer_identifier, source_id, effective_interval_minutes,
                 last_attempt_at, last_attempt_status, last_success_at, next_due_at,
                 checked_through, covered_ranges, last_error_class)
            VALUES ('US', %s, %s, %s, %s, %s, %s, %s, %s, %s::tstzrange[], NULL)
            ON CONFLICT (market, issuer_identifier, source_id) DO UPDATE SET
                effective_interval_minutes = EXCLUDED.effective_interval_minutes,
                last_attempt_at = EXCLUDED.last_attempt_at,
                last_attempt_status = EXCLUDED.last_attempt_status,
                last_success_at = EXCLUDED.last_success_at,
                next_due_at = EXCLUDED.next_due_at,
                checked_through = EXCLUDED.checked_through,
                covered_ranges = watchlist_announcement_check_state.covered_ranges
                                 || EXCLUDED.covered_ranges,
                last_error_class = NULL,
                updated_at = NOW()
            """,
            (issuer, source_id, interval_minutes, attempt_at, status,
             attempt_at, next_due_at, checked_through,
             to_tstzrange_array([covered_add])),
        )


# ── 公告写入（去重 / 修订 / last_seen）─────────────────────

def upsert_announcement(conn, *, source_id: int, issuer: str,
                        stock_codes: list[str], filing, title: str,
                        is_backfill: bool, observed_at: datetime) -> str:
    """写入一条公告，返回 'inserted' | 'revised' | 'seen'。

    - 未见过的 (source_id, source_record_id)：插入 revision=1，
      first_observed_at=observed_at（系统实际首次发现时间，不倒填）。
    - 已见且 content_hash 相同：只推进 last_seen_at，不动 first_observed_at。
    - 已见但内容变更：追加 revision+1 不可变行，不覆盖旧版本。
    证券关联挂在该逻辑公告的首个版本行（revision 最小）；同一公告可关联
    多个证券（共享 CIK 的多个关注代码逐一写入，重复抓取不重复关联）。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, revision, content_hash FROM watchlist_announcement
            WHERE source_id = %s AND source_record_id = %s
            ORDER BY revision DESC LIMIT 1
            """,
            (source_id, filing.accession),
        )
        existing = cur.fetchone()

        if existing is not None and existing[2] == filing.content_hash:
            cur.execute(
                "UPDATE watchlist_announcement SET last_seen_at = %s WHERE id = %s",
                (observed_at, existing[0]),
            )
            outcome = "seen"
        else:
            revision = 1 if existing is None else int(existing[1]) + 1
            cur.execute(
                """
                INSERT INTO watchlist_announcement
                    (market, issuer_identifier, source_id, source_record_id, title,
                     form_type, original_url, published_at, published_at_precision,
                     source_published_raw, source_timezone, first_observed_at,
                     last_seen_at, content_hash, revision, is_backfill)
                VALUES ('US', %s, %s, %s, %s, %s, %s, %s, %s, %s, 'America/New_York',
                        %s, %s, %s, %s, %s)
                ON CONFLICT (source_id, source_record_id, revision) DO NOTHING
                RETURNING id
                """,
                (issuer, source_id, filing.accession, title, filing.form,
                 filing.url, filing.published_at, filing.published_precision,
                 filing.acceptance_raw or filing.filing_date.isoformat(),
                 observed_at, observed_at, filing.content_hash, revision,
                 is_backfill),
            )
            outcome = "inserted" if revision == 1 else "revised"

        # 证券关联锚定首个版本行；重复抓取 ON CONFLICT 不产生重复关联。
        # 共享发行人（同 CIK）的全部关注证券逐一关联。
        for stock_code in stock_codes:
            cur.execute(
                """
                INSERT INTO watchlist_announcement_security
                    (announcement_id, market, stock_code)
                SELECT id, 'US', %s FROM watchlist_announcement
                WHERE source_id = %s AND source_record_id = %s AND revision = 1
                ON CONFLICT (announcement_id, stock_code) DO NOTHING
                """,
                (stock_code, source_id, filing.accession),
            )
    return outcome


# ── 恢复进度 ───────────────────────────────────────────────

def load_recovery(conn, issuer: str, source_id: int) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT backfill_start, target_through, cursor, cursor_applicable
            FROM watchlist_announcement_recovery
            WHERE market = 'US' AND issuer_identifier = %s AND source_id = %s
            """,
            (issuer, source_id),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "backfill_start": row[0],
        "target_through": row[1],
        "completed_ranges": read_ranges(
            conn, "watchlist_announcement_recovery", "completed_ranges",
            {"market": "US", "issuer_identifier": issuer, "source_id": source_id}),
        "pending_ranges": read_ranges(
            conn, "watchlist_announcement_recovery", "pending_ranges",
            {"market": "US", "issuer_identifier": issuer, "source_id": source_id}),
        "cursor": row[2],
        "cursor_applicable": row[3],
    }


def upsert_recovery(conn, issuer: str, source_id: int, *, backfill_start: datetime,
                    target_through: datetime,
                    completed: list[tuple[datetime, datetime]],
                    pending: list[tuple[datetime, datetime]],
                    cursor: str | None = None,
                    cursor_applicable: bool = False) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO watchlist_announcement_recovery
                (market, issuer_identifier, source_id, backfill_start, target_through,
                 completed_ranges, pending_ranges, cursor, cursor_applicable)
            VALUES ('US', %s, %s, %s, %s, %s::tstzrange[], %s::tstzrange[], %s, %s)
            ON CONFLICT (market, issuer_identifier, source_id) DO UPDATE SET
                completed_ranges = EXCLUDED.completed_ranges,
                pending_ranges = EXCLUDED.pending_ranges,
                cursor = EXCLUDED.cursor,
                cursor_applicable = EXCLUDED.cursor_applicable,
                updated_at = NOW()
            """,
            (issuer, source_id, backfill_start, target_through,
             to_tstzrange_array(completed), to_tstzrange_array(pending),
             cursor, cursor_applicable),
        )


def delete_recovery(conn, issuer: str, source_id: int) -> None:
    """回补完成（目标区间全部覆盖）后删除恢复行；检查点由 check_state 承载。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM watchlist_announcement_recovery
            WHERE market = 'US' AND issuer_identifier = %s AND source_id = %s
            """,
            (issuer, source_id),
        )


# ── 缺口 ───────────────────────────────────────────────────

def record_gap(conn, issuer: str, source_id: int,
               gap_range: tuple[datetime, datetime], reason: str,
               detail: str = "") -> None:
    """持久记录无法补齐的区间；未解决缺口按部分唯一索引去重。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO watchlist_announcement_gap
                (market, issuer_identifier, source_id, gap_range, reason, detail)
            VALUES ('US', %s, %s, %s::tstzrange, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (issuer, source_id,
             f"[{gap_range[0].astimezone(timezone.utc).isoformat()},"
             f"{gap_range[1].astimezone(timezone.utc).isoformat()})",
             reason, detail),
        )


def has_open_gaps(conn, issuer: str, source_id: int) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM watchlist_announcement_gap
            WHERE market = 'US' AND issuer_identifier = %s AND source_id = %s
              AND resolved_at IS NULL LIMIT 1
            """,
            (issuer, source_id),
        )
        return cur.fetchone() is not None


def load_open_gap_ranges(conn, issuer: str, source_id: int,
                         ) -> list[tuple[datetime, datetime]]:
    """读取未解决缺口区间（左闭右开）；这些区间不再自动重试。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT lower(gap_range), upper(gap_range)
            FROM watchlist_announcement_gap
            WHERE market = 'US' AND issuer_identifier = %s AND source_id = %s
              AND resolved_at IS NULL ORDER BY lower(gap_range)
            """,
            (issuer, source_id),
        )
        return [(row[0], row[1]) for row in cur.fetchall()]


def append_covered_ranges(conn, issuer: str, source_id: int, *,
                          interval_minutes: int,
                          ranges: list[tuple[datetime, datetime]]) -> None:
    """追加已覆盖区间（不推进 checked_through / last_success_at）。

    用于存在未解决缺口时的近期区间独立检查：检查进度可记录，
    但完整检查点不能越过缺口。
    """
    if not ranges:
        return
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO watchlist_announcement_check_state
                (market, issuer_identifier, source_id, effective_interval_minutes,
                 covered_ranges)
            VALUES ('US', %s, %s, %s, %s::tstzrange[])
            ON CONFLICT (market, issuer_identifier, source_id) DO UPDATE SET
                covered_ranges = watchlist_announcement_check_state.covered_ranges
                                 || EXCLUDED.covered_ranges,
                updated_at = NOW()
            """,
            (issuer, source_id, interval_minutes, to_tstzrange_array(ranges)),
        )


# ── 采集运行记录 ───────────────────────────────────────────

def insert_run(conn, *, task_id, issuer: str = "", source_id=None,
               started_at: datetime, finished_at: datetime | None = None,
               status: str, fetched=None, inserted=None, revised=None,
               error_class=None, error_detail=None, parser_version=None,
               trigger_kind="scheduled") -> None:
    """按来源粒度记录一轮运行；失败/skipped 时数量保持 NULL（不伪造为 0）。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO watchlist_announcement_run
                (task_id, market, issuer_identifier, source_id, started_at,
                 finished_at, status, fetched_count, inserted_count, revised_count,
                 error_class, error_detail, parser_version, trigger_kind)
            VALUES (%s, 'US', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (str(task_id), issuer, source_id, started_at, finished_at, status,
             fetched, inserted, revised, error_class, error_detail,
             parser_version, trigger_kind),
        )
