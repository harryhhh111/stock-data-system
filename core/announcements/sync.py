"""core/announcements/sync.py — 关注股官方公告采集整轮编排与 CLI。

合同：docs/WATCHLIST_ANNOUNCEMENTS_MVP_PLAN.md §3/§3.1/§3.2。

整轮互斥（§3.2）：
- 定时/手工/恢复入口共用 PostgreSQL 会话级 advisory lock，
  逻辑名 watchlist_announcements:US，数值 key 固定为 ROUND_LOCK_KEY。
- ROUND_LOCK_KEY = 0x414E4E4F554E4345（ASCII "ANNOUNCE"），已核对避让：
  core/sec_client.py LOCK_KEY=0x53454347415445（SEC HTTP gate）、
  scripts/backfill_historical_market_cap.py ADVISORY_LOCK_KEY=987654321、
  core/fetchers/us_financial.py pg_advisory_xact_lock(snapshot_id)（小整数序列）、
  core/us_financial_worker.py md5 动态 key（随机 64-bit，理论碰撞不可完全排除，
  概率可忽略；本 key 不与任何固定 key 冲突）。
- 未取得锁记 skipped_running 立即退出：不排队、不发请求、不更新来源到期
  时间与成功检查点。finally 释放锁并关闭专用会话；会话失效后中止本轮剩余
  提交，不自动重连沿用旧许可（恢复必须重新取得锁）。
- 先取整轮任务锁；SEC HTTP 许可由 sec_client 自行获取，两层不混。

检查点（§3.1）：
- 首次回补固定 [T-90d, T]；checked_through 仅在目标区间完整成功后，
  与最终数据在同一事务中推进；中间分页提交只更新恢复进度，不推进检查点。
- 增量窗口固定为 [checked_through - 7d, T]（7 天重叠容纳迟到条目）；
  连续失败时 checked_through 不推进，窗口自然仍从原检查点减 7 天起，
  不退化为"现在减 7 天"。
- 缺口语义：历史不可访问（filings.files 页 404 或目录不覆盖）写
  watchlist_announcement_gap 且持久保留，不再自动重试；存在未解决缺口时
  完整检查点不能越过缺口——checked_through / last_success_at 不推进，
  状态持续 partial，近期区间的检查进度另行记入 covered_ranges 与恢复行；
  缺口补齐后才能推进检查点并合并覆盖区间。抓取失败/预算耗尽属于可重试
  pending：不推进 checked_through，下轮优先恢复。

CLI（手工入口，§3.2 同一互斥锁）：
    python -m core.announcements.sync [--market US] [--stock-code MU]

IR 来源（第三片）：注册表驱动（platform 列显式标识平台），适配器见
core/announcements/ir_source.py；与 SEC 共用同一整轮锁与检查点表；
HTTP 独立实现，不占 SEC gate；受阻/待验证来源不抓取、不计入成功率
分母，状态与原因在整轮摘要 ir_inactive 中列出。首批种子 MU/WDC/NBIS
（core/announcements/ir_source.py IR_SEEDS），默认均不启用。

手工入口安全语义：要求 STOCK_ANNOUNCEMENTS_ENABLED=true 才运行，不提供
--force 绕过——"默认关闭=无生产行为"不允许例外路径；要手工验证就显式
改配置（留下 .env 痕迹），这同时覆盖调度与手工两个入口。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone

import psycopg2

import config
from core import sec_client
from core.announcements import ir_source, sec_source, store

logger = logging.getLogger(__name__)

ROUND_LOCK_NAME = "watchlist_announcements:US"
ROUND_LOCK_KEY = 0x414E4E4F554E4345  # ASCII "ANNOUNCE"；避让清单见模块 docstring

BACKFILL_DAYS = 90            # 首次回补最近 90 个自然日
OVERLAP_DAYS = 7              # 增量重叠窗口，容纳近期迟到条目与变更
ROUND_MAX_SECONDS = 900       # 整轮总预算；耗尽不提前推进剩余项状态
PER_SOURCE_MAX_PAGES = 10     # 单来源 filings.files 分页上限
PER_SOURCE_MAX_SECONDS = 240  # 单来源预算，避免队尾长期得不到检查


# ── 整轮互斥锁 ─────────────────────────────────────────────

class RoundLock:
    """整轮任务锁：专用数据库会话上的会话级 advisory lock。

    不使用连接池连接（会话锁不能随连接归还泄漏）；finally 释放并关闭。
    """

    def __init__(self) -> None:
        self.conn: psycopg2.extensions.connection | None = None
        self.acquired = False

    def __enter__(self) -> "RoundLock":
        self.conn = psycopg2.connect(
            host=config.db.host, port=config.db.port, dbname=config.db.dbname,
            user=config.db.user, password=config.db.password, connect_timeout=2,
            keepalives=1, keepalives_idle=5, keepalives_interval=5,
            keepalives_count=3, tcp_user_timeout=10000,
            application_name="watchlist_announcements_round",
            # 与 db.py 连接池一致显式固定客户端编码：生产库 server_encoding
            # 为 SQL_ASCII 时裸连接会让 psycopg2 退回 ascii codec，中文
            # status_reason/error_detail 在客户端编码阶段直接 UnicodeEncodeError
            options="-c client_encoding=utf8",
        )
        self.conn.autocommit = True
        with self.conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (ROUND_LOCK_KEY,))
            self.acquired = bool(cur.fetchone()[0])
        if self.acquired:
            # 业务写入使用该持锁会话的短事务
            self.conn.autocommit = False
        return self

    def check_alive(self) -> None:
        """提交前确认持锁会话仍有效；失效即抛错，本轮后续不得提交。

        会话级 advisory lock 同会话可重入：仍持锁时 pg_try_advisory_lock
        返回 true；连接失效抛 psycopg2.Error。
        """
        with self.conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (ROUND_LOCK_KEY,))
            if not cur.fetchone()[0]:
                raise psycopg2.Error("整轮任务锁已丢失")

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        # 进程退出时数据库会识别会话终止并释放锁；正常/异常路径均显式释放
        if self.conn is not None:
            try:
                if self.acquired:
                    self.conn.autocommit = True
                    with self.conn.cursor() as cur:
                        cur.execute("SELECT pg_advisory_unlock(%s)", (ROUND_LOCK_KEY,))
            except Exception:
                logger.warning("整轮锁释放异常（会话关闭后由数据库回收）", exc_info=True)
            finally:
                self.conn.close()
                self.conn = None
                self.acquired = False


# ── 日期区间工具 ───────────────────────────────────────────

def _subtract_ranges(base: tuple[date, date],
                     covered: list[tuple[date, date]]) -> list[tuple[date, date]]:
    """base 区间减去 covered 列表，返回未覆盖子区间（闭区间日期语义）。"""
    pending = [base]
    for c_start, c_end in covered:
        next_pending = []
        for p_start, p_end in pending:
            if c_end < p_start or c_start > p_end:
                next_pending.append((p_start, p_end))
                continue
            if p_start < c_start:
                next_pending.append((p_start, c_start - timedelta(days=1)))
            if p_end > c_end:
                next_pending.append((c_end + timedelta(days=1), p_end))
        pending = next_pending
    return pending


def _error_class(exc: BaseException) -> str:
    if isinstance(exc, sec_client.SECHTTPError):
        return f"http_{exc.response.status_code}"
    if isinstance(exc, sec_client.SECCoordinationError):
        return "coordination"
    if isinstance(exc, sec_client.SECWaitTimeout):
        return "wait_timeout"
    if isinstance(exc, sec_client.SECTransportError):
        return "transport_timeout" if "deadline" in str(exc) else "transport"
    if isinstance(exc, sec_client.SECError):
        return "sec_client"
    if isinstance(exc, (ValueError, KeyError)):
        return "parse_error"
    return "internal"


def _round_slot(now: datetime, interval_minutes: int,
                anchor: datetime | None) -> datetime:
    """本轮的计划槽位时刻。

    定时入口：按 IntervalTrigger 相位计算——锚点（任务注册时刻，与
    IntervalTrigger start_date 一致）+ floor((now-锚点)/interval)*interval，
    与调度器实际触发网格同相位；无论任务何时开始、耗时多久，
    next_due = 槽位 + interval 在下次触发时必然已到期。
    手工/恢复入口没有调度相位，以本轮开始时刻为槽位基准
    （anchor=None 时调用方传 now），基准差异见 run_round 注释。
    """
    if anchor is None:
        return now
    elapsed = (now - anchor).total_seconds()
    if elapsed < 0:
        return anchor
    period_seconds = interval_minutes * 60
    periods = int(elapsed // period_seconds)
    return anchor + timedelta(seconds=periods * period_seconds)


# ── 单来源同步 ─────────────────────────────────────────────

def _sync_sec_source(
    conn, *, source_id: int, cik: str, stock_codes: list[str],
    state: dict | None, target: datetime, slot: datetime,
    interval_minutes: int,
    task_id: uuid.UUID, trigger: str, deadline: float,
    request_timeout: float, request_total_timeout: float,
) -> dict:
    """同步一个发行人的 SEC 来源；返回结果摘要（不抛采集异常）。

    slot：本轮计划槽位时刻（定时入口为调度相位槽位，手工/恢复入口为
    本轮开始时刻）；next_due = slot + interval，见 run_round 注释。
    """
    started_at = datetime.now(timezone.utc)
    attempt_status = "failed"
    error_class = None
    error_detail = None
    fetched = inserted = revised = 0
    llm_stats: dict = {}
    filings: list[sec_source.SecFiling] = []
    backfill = state is None or state["checked_through"] is None
    recovery = store.load_recovery(conn, cik, source_id) if backfill else None
    fresh_backfill = backfill and recovery is None

    if backfill:
        if fresh_backfill:
            backfill_start = target - timedelta(days=BACKFILL_DAYS)
            target_through = target
            pending_dates: list[tuple[date, date]] = [
                (backfill_start.date(), target.date())]
        else:
            # 跨轮恢复：固定起点与目标，不随日期滚动
            backfill_start = recovery["backfill_start"]
            target_through = recovery["target_through"]
            pending_dates = [(lo.date(), hi.date() - timedelta(days=1))
                             for lo, hi in recovery["pending_ranges"]]
        if pending_dates:
            scan_from = min(p[0] for p in pending_dates)
            scan_to = target_through.date()
        else:
            # 回补除已知缺口外已完成：近期重叠窗口独立检查（缺口未解决前
            # 不推进 checked_through / last_success_at，状态持续 partial）
            scan_from = (target_through - timedelta(days=OVERLAP_DAYS)).date()
            scan_to = target.date()
    else:
        backfill_start = target_through = None
        scan_from = (state["checked_through"]
                     - timedelta(days=OVERLAP_DAYS)).date()
        scan_to = target.date()

    # 下次到期：本轮计划槽位 + interval。不用"旧到期 + interval"——
    # 停采恢复后旧到期仍在过去，会造成过期检查紧接着重复采集；
    # 也不用本轮实际开始/结束时间——任务开始延迟与耗时会叠加漂移，
    # 使下一次调度触发时来源未到期（漏轮）。槽位与调度触发同相位，
    # 下次触发时来源必然已到期；恢复检查后直接推进到当前轮之后的
    # 下一有效到期（§3：到期基于尝试记录推进，失败同样推进；
    # 过期触发只合并为一次检查，不补发）。
    next_due_at = slot + timedelta(minutes=interval_minutes)

    completed_dates: list[tuple[date, date]] = []
    new_pending: list[tuple[date, date]] = []
    new_gaps: list[tuple[date, date]] = []

    try:
        # 首次回补：在首次网络请求之前持久化固定起点与目标截止时间，
        # 并立即提交——请求开始时独立连接必须已能读到恢复行；此刻进程
        # 被杀也不丢失起点，下轮从持久起点恢复，不随日期滚动
        if fresh_backfill:
            store.upsert_recovery(
                conn, cik, source_id, backfill_start=backfill_start,
                target_through=target_through, completed=[],
                pending=[(backfill_start, target_through)])
            conn.commit()

        data = sec_source.fetch_submissions(
            cik, timeout=request_timeout, total_timeout=request_total_timeout)
        parsed = sec_source.parse_submissions(data)

        # recent 段覆盖 [recent_min, 今天]；分页覆盖其声明区间。
        # filings.files 为空时 recent 即公司全部申报历史（SEC 仅在 recent
        # 装不下时才给出分页目录），整段视为已覆盖。
        covered_dates: list[tuple[date, date]] = []
        if parsed.recent_min_date is not None:
            covered_dates.append((parsed.recent_min_date, scan_to))
        filings.extend(parsed.filings)

        if not parsed.files:
            uncovered = []
        else:
            uncovered = _subtract_ranges((scan_from, scan_to), covered_dates)
        pages_used = 0
        page_by_range: list[tuple[sec_source.SubmissionsPage, str]] = []
        # 只取与未覆盖区间重叠的历史分页，最旧优先
        for page in sorted(parsed.files, key=lambda p: p.filing_from):
            if not uncovered:
                break
            if not any(page.filing_from <= u[1] and page.filing_to >= u[0]
                       for u in uncovered):
                continue
            if pages_used >= PER_SOURCE_MAX_PAGES or time.monotonic() >= deadline:
                break
            pages_used += 1
            page_data = sec_source.fetch_page(
                page.name, timeout=request_timeout,
                total_timeout=request_total_timeout)
            if page_data is None:  # 404：历史不可访问，持久记缺口
                page_by_range.append((page, "missing"))
                continue
            filings.extend(sec_source.parse_page(page_data, cik))
            page_by_range.append((page, "ok"))
            covered_dates.append((page.filing_from, page.filing_to))
            uncovered = _subtract_ranges((scan_from, scan_to), covered_dates)

        # 已记录的未解决缺口不再自动重试：从未覆盖区间中剔除后再归因
        open_gap_dates = [
            (lo.date(), hi.date() - timedelta(days=1))
            for lo, hi in store.load_open_gap_ranges(conn, cik, source_id)
        ]
        for g in open_gap_dates:
            nxt: list[tuple[date, date]] = []
            for seg in uncovered:
                nxt.extend(_subtract_ranges(seg, [g]))
            uncovered = nxt

        # 未覆盖区间归因：有分页但取失败/预算耗尽 → pending（下轮恢复）；
        # 分页 404 或目录不含 → 持久缺口（不自动重试）
        missing_pages = [(p.filing_from, p.filing_to)
                         for p, s in page_by_range if s == "missing"]
        declared = [(p.filing_from, p.filing_to) for p in parsed.files]
        for u in uncovered:
            if any(m[0] <= u[1] and m[1] >= u[0] for m in missing_pages):
                new_gaps.append(u)
            elif any(d[0] <= u[1] and d[1] >= u[0] for d in declared):
                new_pending.append(u)  # 目录有页但未成功取回
            elif parsed.recent_min_date is None or u[1] < parsed.recent_min_date:
                new_gaps.append(u)     # 来源可枚举范围不含该历史
            else:
                new_pending.append(u)
        # completed = scan - pending - gaps（含既有缺口）
        remaining = [(scan_from, scan_to)]
        for block in new_pending + new_gaps + open_gap_dates:
            nxt = []
            for seg in remaining:
                nxt.extend(_subtract_ranges(seg, [block]))
            remaining = nxt
        completed_dates = remaining

        # 已覆盖区间的目标表单记录（幂等写入，不含未覆盖区间假装完整）
        day_from, day_to = scan_from, scan_to
        window_filings = [
            f for f in filings
            if day_from <= f.filing_date <= day_to
            and any(c[0] <= f.filing_date <= c[1] for c in completed_dates)
        ]
        # 同一 accession 多段重复（recent 与分页重叠）只写一次
        seen_accessions: set[str] = set()
        unique_filings = []
        for f in window_filings:
            if f.accession not in seen_accessions:
                seen_accessions.add(f.accession)
                unique_filings.append(f)
        fetched = len(unique_filings)

        observed_at = datetime.now(timezone.utc)
        new_rows: list[tuple] = []
        for f in unique_filings:
            # 修订（内容变更）同样重新增强：importance/中文字段不复用旧版本
            outcome, rev = store.upsert_announcement(
                conn, source_id=source_id, issuer=cik, stock_codes=stock_codes,
                record=store.AnnouncementRecord(
                    source_record_id=f.accession, form_type=f.form,
                    title=sec_source.neutral_title(f.form), url=f.url,
                    published_at=f.published_at,
                    published_precision=f.published_precision,
                    published_raw=f.acceptance_raw or f.filing_date.isoformat(),
                    source_timezone="America/New_York",
                    content_hash=f.content_hash),
                is_backfill=backfill, observed_at=observed_at)
            if outcome == "inserted":
                inserted += 1
                new_rows.append((f.accession, sec_source.neutral_title(f.form),
                                 f.form, rev))
            elif outcome == "revised":
                revised += 1
                new_rows.append((f.accession, sec_source.neutral_title(f.form),
                                 f.form, rev))
        # 官方披露只算 importance（用户 2026-10-23 确认），不出中文标题
        llm_stats: dict = {}
        _enhance_new_rows(conn, source_id=source_id, kind="official",
                          new_rows=new_rows, stock_code=stock_codes[0],
                          deadline=deadline, result=llm_stats,
                          label=f"sec/{cik}")

        has_open_gaps = bool(open_gap_dates or new_gaps)
        complete = not new_pending
        finished_at = datetime.now(timezone.utc)
        if complete and not has_open_gaps:
            if backfill:
                new_checked = target_through
                covered_add = (backfill_start, target_through)
                store.delete_recovery(conn, cik, source_id)
            else:
                new_checked = target
                covered_add = (datetime.combine(
                    scan_from, datetime.min.time(), tzinfo=timezone.utc), target)
            attempt_status = "success_new" if (inserted or revised) else "success_empty"
            store.upsert_check_state_success(
                conn, cik, source_id, interval_minutes=interval_minutes,
                attempt_at=started_at, status=attempt_status,
                next_due_at=next_due_at,
                checked_through=new_checked, covered_add=covered_add)
        elif complete and has_open_gaps:
            # 仅剩不可补齐缺口：完整检查点不能越过缺口——
            # 不推进 checked_through / last_success_at，状态持续 partial；
            # 近期区间的检查进度另行记入 covered_ranges 与恢复行，
            # 缺口持久保留（补齐后才能推进检查点并合并覆盖区间）
            attempt_status = "partial"
            for g in new_gaps:
                store.record_gap(
                    conn, cik, source_id,
                    (datetime.combine(g[0], datetime.min.time(),
                                      tzinfo=timezone.utc),
                     datetime.combine(g[1] + timedelta(days=1),
                                      datetime.min.time(),
                                      tzinfo=timezone.utc)),
                    "history_unavailable",
                    "filings.files 分页 404 或目录不覆盖该区间")
            covered_adds = [
                (datetime.combine(s, datetime.min.time(), tzinfo=timezone.utc),
                 datetime.combine(e + timedelta(days=1), datetime.min.time(),
                                  tzinfo=timezone.utc))
                for s, e in completed_dates
            ]
            if backfill:
                store.upsert_recovery(
                    conn, cik, source_id, backfill_start=backfill_start,
                    target_through=target_through,
                    completed=covered_adds, pending=[])
            store.append_covered_ranges(
                conn, cik, source_id, interval_minutes=interval_minutes,
                ranges=covered_adds)
            store.upsert_check_state_attempt(
                conn, cik, source_id, interval_minutes=interval_minutes,
                attempt_at=started_at, status=attempt_status,
                next_due_at=next_due_at, error_class=None)
        else:
            attempt_status = "partial" if (fetched or completed_dates) else "failed"
            if backfill:
                store.upsert_recovery(
                    conn, cik, source_id, backfill_start=backfill_start,
                    target_through=target_through,
                    completed=[(datetime.combine(s, datetime.min.time(),
                                                 tzinfo=timezone.utc),
                                datetime.combine(e + timedelta(days=1),
                                                 datetime.min.time(),
                                                 tzinfo=timezone.utc))
                               for s, e in completed_dates],
                    pending=[(datetime.combine(s, datetime.min.time(),
                                               tzinfo=timezone.utc),
                              datetime.combine(e + timedelta(days=1),
                                               datetime.min.time(),
                                               tzinfo=timezone.utc))
                             for s, e in new_pending],
                    cursor=None, cursor_applicable=False)
            store.upsert_check_state_attempt(
                conn, cik, source_id, interval_minutes=interval_minutes,
                attempt_at=started_at, status=attempt_status,
                next_due_at=next_due_at, error_class=None)
        store.insert_run(
            conn, task_id=task_id, issuer=cik, source_id=source_id,
            started_at=started_at, finished_at=finished_at, status=attempt_status,
            fetched=fetched, inserted=inserted, revised=revised,
            error_class=None, error_detail=None,
            parser_version=sec_source.PARSER_VERSION, trigger_kind=trigger)
    except Exception as exc:
        error_class = _error_class(exc)
        error_detail = f"{type(exc).__name__}: {exc}"[:500]
        logger.exception("SEC 公告采集失败 CIK=%s", cik)
        finished_at = datetime.now(timezone.utc)
        attempt_status = "failed"
        # 失败落库：尝试/运行记录写入，检查点不动；回补固定起点必须保住
        # （首次回补在请求前已写恢复行，回滚后这里重写）。若这些写入也失败
        # （如持锁会话失效），回滚并向上抛——不自动重连沿用旧许可。
        conn.rollback()
        try:
            if backfill:
                if pending_dates:
                    restore_pending = [
                        (datetime.combine(s, datetime.min.time(),
                                          tzinfo=timezone.utc),
                         datetime.combine(e + timedelta(days=1),
                                          datetime.min.time(),
                                          tzinfo=timezone.utc))
                        for s, e in pending_dates
                    ]
                else:
                    restore_pending = []
                store.upsert_recovery(
                    conn, cik, source_id, backfill_start=backfill_start,
                    target_through=target_through, completed=[],
                    pending=restore_pending)
            store.upsert_check_state_attempt(
                conn, cik, source_id, interval_minutes=interval_minutes,
                attempt_at=started_at, status="failed",
                next_due_at=next_due_at,
                error_class=error_class)
            store.insert_run(
                conn, task_id=task_id, issuer=cik, source_id=source_id,
                started_at=started_at, finished_at=finished_at, status="failed",
                fetched=None, inserted=None, revised=None,
                error_class=error_class, error_detail=error_detail,
                parser_version=sec_source.PARSER_VERSION, trigger_kind=trigger)
        except Exception:
            conn.rollback()
            raise

    return {
        "cik": cik, "stock_codes": stock_codes, "status": attempt_status,
        "fetched": fetched, "inserted": inserted, "revised": revised,
        "error_class": error_class, "error_detail": error_detail,
        "backfill": backfill, **llm_stats,
    }


# ── 单来源同步（IR）────────────────────────────────────────

def _consecutive_failures(conn, source_id: int, limit: int = 8) -> int:
    """最近连续 failed 的运行次数（用于持续失败退避）。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT status FROM watchlist_announcement_run
            WHERE source_id = %s ORDER BY id DESC LIMIT %s
            """,
            (source_id, limit),
        )
        n = 0
        for (status,) in cur.fetchall():
            if status != "failed":
                break
            n += 1
    return n


def _item_date(item: ir_source.IrItem) -> date | None:
    """条目日期（窗口归属判断）：精确时间取 UTC 日期，仅日期解析原始串。"""
    if item.published_at is not None:
        return item.published_at.astimezone(timezone.utc).date()
    try:
        return date.fromisoformat(item.published_raw[:10])
    except ValueError:
        return None


def _sync_ir_source(
    conn, *, source: dict, stock_codes: list[str],
    state: dict | None, target: datetime, slot: datetime,
    interval_minutes: int, task_id: uuid.UUID, trigger: str,
    deadline: float,
) -> dict:
    """同步一个 IR 来源；返回结果摘要（不抛采集异常）。

    覆盖语义：IR 列表没有 SEC filings.files 那样的声明区间，覆盖能力
    按列表实际深度诚实化——回补时列表最旧条目晚于回补起点，或列表
    为空，则缺口写 gap 并标 partial，检查点不越过缺口。
    增量窗口（checked_through - 7d 起）内列表新近条目即可覆盖：
    无新条目为 success_empty。
    """
    source_id = source["source_id"]
    issuer = source["issuer_identifier"]
    adapter = ir_source.IR_ADAPTERS.get(source["platform"] or "")
    started_at = datetime.now(timezone.utc)
    next_due_at = slot + timedelta(minutes=interval_minutes)
    fetched = inserted = revised = 0
    llm_stats: dict = {}

    if adapter is None:
        # 注册表指向未知平台：配置错误，落 failed，不抓取
        store.upsert_check_state_attempt(
            conn, issuer, source_id, interval_minutes=interval_minutes,
            attempt_at=started_at, status="failed",
            next_due_at=next_due_at, error_class="unknown_platform")
        store.insert_run(
            conn, task_id=task_id, issuer=issuer, source_id=source_id,
            started_at=started_at, finished_at=datetime.now(timezone.utc),
            status="failed", error_class="unknown_platform",
            error_detail=f"platform={source['platform']!r} 无适配器",
            parser_version=None, trigger_kind=trigger)
        return {"cik": issuer, "stock_codes": stock_codes, "status": "failed",
                "fetched": 0, "inserted": 0, "revised": 0,
                "error_class": "unknown_platform", "error_detail": None,
                "backfill": state is None or state["checked_through"] is None,
                "platform": source["platform"]}

    backfill = state is None or state["checked_through"] is None
    recovery = store.load_recovery(conn, issuer, source_id) if backfill else None
    fresh_backfill = backfill and recovery is None
    if backfill:
        if fresh_backfill:
            backfill_start = target - timedelta(days=BACKFILL_DAYS)
            target_through = target
        else:
            backfill_start = recovery["backfill_start"]
            target_through = recovery["target_through"]
        # 回补起点固定（不随日期滚动），但扫描上界始终到当前时间：
        # 历史缺口不能让新公告停止入库；覆盖完整性仍按回补起点判定
        scan_from = backfill_start.date()
        scan_to = target.date()
    else:
        backfill_start = target_through = None
        scan_from = (state["checked_through"]
                     - timedelta(days=OVERLAP_DAYS)).date()
        scan_to = target.date()

    try:
        # 首次回补：请求前持久化固定起点并立即提交（同 SEC 路径语义）
        if fresh_backfill:
            store.upsert_recovery(
                conn, issuer, source_id, backfill_start=backfill_start,
                target_through=target_through, completed=[],
                pending=[(backfill_start, target_through)])
            conn.commit()

        # 分页拉取：直到最旧条目早于窗口起点、分页耗尽、页数上限或
        # 时间预算耗尽。预算/页数耗尽 = budget_cut（下轮恢复，不记缺口）
        items: list[ir_source.IrItem] = []
        exhausted = not adapter.supports_pagination
        oldest_seen: date | None = None
        pages_used = 0
        budget_cut = False
        for page in range(PER_SOURCE_MAX_PAGES):
            if time.monotonic() >= deadline:
                budget_cut = True
                break
            url = adapter.page_url(source["entry_url"], page)
            if url is None:
                exhausted = True
                break
            body = ir_source.http_get(
                url, allowed_domain=source["allowed_domain"])
            page_items = adapter.parse_listing(body, source["entry_url"])
            pages_used += 1
            if not page_items:
                exhausted = True   # 空页 = 没有更多历史
                break
            items.extend(page_items)
            page_dates = [d for d in (_item_date(it) for it in page_items)
                          if d is not None]
            if page_dates:
                page_oldest = min(page_dates)
                oldest_seen = (page_oldest if oldest_seen is None
                               else min(oldest_seen, page_oldest))
                if page_oldest <= scan_from:
                    exhausted = True  # 列表深度已覆盖窗口起点
                    break
        else:
            # 页数上限用尽而分页未耗尽：按预算截断处理，下轮恢复
            budget_cut = adapter.supports_pagination

        # 去重（同一来源稳定 ID），按窗口过滤
        seen: set[str] = set()
        unique_items = []
        for it in items:
            if it.source_record_id not in seen:
                seen.add(it.source_record_id)
                unique_items.append(it)
        window_items = [
            it for it in unique_items
            if (d := _item_date(it)) is not None and scan_from <= d <= scan_to
        ]
        fetched = len(window_items)

        observed_at = datetime.now(timezone.utc)
        new_rows: list[tuple] = []
        for it in window_items:
            # 修订（内容变更）同样重新增强：importance 不复用旧版本
            outcome, rev = store.upsert_announcement(
                conn, source_id=source_id, issuer=issuer,
                stock_codes=stock_codes,
                record=store.AnnouncementRecord(
                    source_record_id=it.source_record_id,
                    form_type="press_release", title=it.title, url=it.url,
                    published_at=it.published_at,
                    published_precision=it.precision,
                    published_raw=it.published_raw,
                    source_timezone=source["source_timezone"] or "UTC",
                    content_hash=it.content_hash),
                is_backfill=backfill, observed_at=observed_at)
            if outcome == "inserted":
                inserted += 1
                new_rows.append((it.source_record_id, it.title,
                                 "press_release", rev))
            elif outcome == "revised":
                revised += 1
                new_rows.append((it.source_record_id, it.title,
                                 "press_release", rev))
        # IR 官方发布只算 importance（同 SEC 口径）
        _enhance_new_rows(conn, source_id=source_id, kind="official",
                          new_rows=new_rows, stock_code=stock_codes[0],
                          deadline=deadline, result=llm_stats,
                          label=f"ir/{issuer}")

        finished_at = datetime.now(timezone.utc)
        open_gaps = store.load_open_gap_ranges(conn, issuer, source_id)

        if budget_cut:
            # 时间/页数预算耗尽：可重试 pending，不记缺口，检查点不动
            attempt_status = "partial"
            if backfill:
                store.upsert_recovery(
                    conn, issuer, source_id, backfill_start=backfill_start,
                    target_through=target_through, completed=[],
                    pending=[(backfill_start, target_through)])
            store.upsert_check_state_attempt(
                conn, issuer, source_id, interval_minutes=interval_minutes,
                attempt_at=started_at, status=attempt_status,
                next_due_at=next_due_at, error_class="budget_exhausted")
        elif not backfill:
            # 增量：列表深度须达到窗口起点才算完整覆盖；停采期间落在列表
            # 深度之外的区间不能当作已覆盖（缺口落库，检查点不动）。
            # 存在未解决缺口时即使覆盖完整也只报 partial（缺口语义持续）。
            # 列表突空（历史上有公告）= 可疑，落 failed 不掩盖。
            shallow = bool(unique_items) and (
                oldest_seen is None or oldest_seen > scan_from)
            if not unique_items:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT 1 FROM watchlist_announcement "
                        "WHERE source_id = %s LIMIT 1", (source_id,))
                    had_items = cur.fetchone() is not None
                if had_items:
                    raise ir_source.IrFetchError(
                        "IR 列表突空（该来源历史上有公告），疑似结构变更或截断",
                        error_class="empty_listing")
            if shallow:
                attempt_status = "partial"
                gap_end = min(oldest_seen - timedelta(days=1), scan_to)
                store.record_gap(
                    conn, issuer, source_id,
                    (datetime.combine(scan_from, datetime.min.time(),
                                      tzinfo=timezone.utc),
                     datetime.combine(gap_end + timedelta(days=1),
                                      datetime.min.time(), tzinfo=timezone.utc)),
                    "history_unavailable",
                    "IR 列表深度不足，中断期间区间不可枚举")
                store.append_covered_ranges(
                    conn, issuer, source_id, interval_minutes=interval_minutes,
                    ranges=[(datetime.combine(oldest_seen, datetime.min.time(),
                                              tzinfo=timezone.utc), target)])
                store.upsert_check_state_attempt(
                    conn, issuer, source_id, interval_minutes=interval_minutes,
                    attempt_at=started_at, status=attempt_status,
                    next_due_at=next_due_at, error_class=None)
            elif open_gaps:
                attempt_status = "partial"
                store.append_covered_ranges(
                    conn, issuer, source_id, interval_minutes=interval_minutes,
                    ranges=[(datetime.combine(scan_from, datetime.min.time(),
                                              tzinfo=timezone.utc), target)])
                store.upsert_check_state_attempt(
                    conn, issuer, source_id, interval_minutes=interval_minutes,
                    attempt_at=started_at, status=attempt_status,
                    next_due_at=next_due_at, error_class=None)
            else:
                attempt_status = ("success_new" if (inserted or revised)
                                  else "success_empty")
                store.upsert_check_state_success(
                    conn, issuer, source_id, interval_minutes=interval_minutes,
                    attempt_at=started_at, status=attempt_status,
                    next_due_at=next_due_at, checked_through=target,
                    covered_add=(datetime.combine(scan_from, datetime.min.time(),
                                                  tzinfo=timezone.utc), target))
        else:
            # 回补覆盖判定：列表耗尽且深度达到起点 → 覆盖完整；否则记录
            # 实际覆盖范围与缺口，标 partial，检查点不越过缺口
            complete_coverage = (exhausted and oldest_seen is not None
                                 and oldest_seen <= scan_from)
            if complete_coverage and not open_gaps:
                attempt_status = ("success_new" if (inserted or revised)
                                  else "success_empty")
                store.delete_recovery(conn, issuer, source_id)
                store.upsert_check_state_success(
                    conn, issuer, source_id, interval_minutes=interval_minutes,
                    attempt_at=started_at, status=attempt_status,
                    next_due_at=next_due_at, checked_through=target,
                    covered_add=(backfill_start, target))
            elif complete_coverage and open_gaps:
                # 覆盖已完整但缺口未解决：不宣称完整成功，检查点不推进
                attempt_status = "partial"
                store.upsert_recovery(
                    conn, issuer, source_id, backfill_start=backfill_start,
                    target_through=target_through,
                    completed=[(backfill_start, target)], pending=[])
                store.append_covered_ranges(
                    conn, issuer, source_id, interval_minutes=interval_minutes,
                    ranges=[(backfill_start, target)])
                store.upsert_check_state_attempt(
                    conn, issuer, source_id, interval_minutes=interval_minutes,
                    attempt_at=started_at, status=attempt_status,
                    next_due_at=next_due_at, error_class=None)
            else:
                attempt_status = "partial"
                # 缺口 = [回补起点, 实际覆盖起点)；列表完全为空 = 整段无覆盖证据
                covered_from = (max(oldest_seen, scan_from)
                                if oldest_seen is not None else None)
                gap_end_date = (covered_from - timedelta(days=1)
                                if covered_from is not None else scan_to)
                store.record_gap(
                    conn, issuer, source_id,
                    (datetime.combine(scan_from, datetime.min.time(),
                                      tzinfo=timezone.utc),
                     datetime.combine(gap_end_date + timedelta(days=1),
                                      datetime.min.time(), tzinfo=timezone.utc)),
                    "history_unavailable",
                    "IR 列表历史深度不足回补窗口"
                    if covered_from is not None else
                    "IR 列表为空，无覆盖证据")
                if covered_from is not None:
                    store.append_covered_ranges(
                        conn, issuer, source_id,
                        interval_minutes=interval_minutes,
                        ranges=[(datetime.combine(
                            covered_from, datetime.min.time(),
                            tzinfo=timezone.utc), target)])
                # 恢复行保留固定边界，pending 置空（缺口不自动重试）
                store.upsert_recovery(
                    conn, issuer, source_id, backfill_start=backfill_start,
                    target_through=target_through,
                    completed=[(datetime.combine(covered_from,
                                                 datetime.min.time(),
                                                 tzinfo=timezone.utc),
                                target)]
                    if covered_from is not None else [],
                    pending=[])
                store.upsert_check_state_attempt(
                    conn, issuer, source_id, interval_minutes=interval_minutes,
                    attempt_at=started_at, status=attempt_status,
                    next_due_at=next_due_at, error_class=None)
        store.insert_run(
            conn, task_id=task_id, issuer=issuer, source_id=source_id,
            started_at=started_at, finished_at=finished_at,
            status=attempt_status, fetched=fetched, inserted=inserted,
            revised=revised, error_class=None, error_detail=None,
            parser_version=adapter.parser_version, trigger_kind=trigger)
    except Exception as exc:
        if isinstance(exc, ir_source.IrFetchError):
            error_class = exc.error_class
        else:
            error_class = _error_class(exc)
        error_detail = f"{type(exc).__name__}: {exc}"[:500]
        logger.exception("IR 公告采集失败 %s", issuer)
        finished_at = datetime.now(timezone.utc)
        attempt_status = "failed"
        conn.rollback()
        # 持续失败退避：按最近连续失败次数指数拉长下次到期
        # （不紧密重跑）；恢复条件 = 下次到期后自动重试，状态与原因可见
        failures = 0
        try:
            failures = _consecutive_failures(conn, source_id)
        except Exception:
            failures = 0
        backoff = min(2 ** failures, 8)
        try:
            if backfill:
                store.upsert_recovery(
                    conn, issuer, source_id, backfill_start=backfill_start,
                    target_through=target_through, completed=[],
                    pending=[(backfill_start, target_through)])
            store.upsert_check_state_attempt(
                conn, issuer, source_id, interval_minutes=interval_minutes,
                attempt_at=started_at, status="failed",
                next_due_at=slot + timedelta(minutes=interval_minutes * backoff),
                error_class=error_class)
            store.insert_run(
                conn, task_id=task_id, issuer=issuer, source_id=source_id,
                started_at=started_at, finished_at=finished_at, status="failed",
                fetched=None, inserted=None, revised=None,
                error_class=error_class, error_detail=error_detail,
                parser_version=adapter.parser_version, trigger_kind=trigger)
        except Exception:
            conn.rollback()
            raise

    return {
        "cik": issuer, "stock_codes": stock_codes, "status": attempt_status,
        "fetched": fetched, "inserted": inserted, "revised": revised,
        "error_class": None if attempt_status != "failed" else error_class,
        "error_detail": None,
        "backfill": backfill, "platform": source["platform"], **llm_stats,
    }


# ── LLM 增强（每日资讯 v1.1：新入库条目一次 mmx 调用）─────────

def _enhance_new_rows(conn, *, source_id: int, kind: str,
                      new_rows: list[tuple], stock_code: str,
                      deadline: float, result: dict,
                      label: str = "") -> None:
    """对本来源新插入/新修订的条目做 LLM 增强并回写对应版本行。

    new_rows：[(source_record_id, title, aux, revision)]；媒体 aux=摘要
    素材，官方 aux=表单类型。revised 条目同样重新增强（修订=内容已变，
    不复用旧版本字段），回写到该 revision 行（store.update_llm_fields）。
    失败/超时/JSON 非法 → 字段留 NULL，不阻塞采集；预算耗尽停止增强
    （剩余条目正常入库、无增强字段）。计数进整轮摘要并打 INFO 日志
    （出问题可按来源排查）。
    """
    from core.announcements import llm_enhance

    ok = failed = skipped = 0
    for record_id, title, aux, revision in new_rows:
        if time.monotonic() >= deadline:
            skipped += len(new_rows) - (ok + failed + skipped)
            break
        try:
            if kind == "media":
                fields = llm_enhance.enhance_media(title, aux, stock_code)
            else:
                fields = llm_enhance.enhance_official(title, aux, stock_code)
            store.update_llm_fields(
                conn, source_id=source_id, source_record_id=record_id,
                revision=revision, **fields)
            ok += 1
        except llm_enhance.EnhanceError as exc:
            logger.warning("LLM 增强失败（条目已正常入库）%s: %s",
                           record_id, exc)
            failed += 1
    if new_rows:
        logger.info("LLM 增强 %s: ok=%d failed=%d skipped=%d",
                    label or source_id, ok, failed, skipped)
    result["llm_ok"] = ok
    result["llm_failed"] = failed
    result["llm_skipped"] = skipped


# ── 单来源同步（媒体：Google News RSS / Finnhub）─────────────

def _sync_media_source(
    conn, *, source: dict, stock_codes: list[str],
    state: dict | None, target: datetime, slot: datetime,
    interval_minutes: int, task_id: uuid.UUID, trigger: str,
    deadline: float,
) -> dict:
    """同步一个媒体来源。返回结果摘要（不抛采集异常）。

    媒体语义无 90 天回补完整性（RSS 100 条上限/7~10 天回溯、Finnhub
    from=当日−1 天，来源本身不支持历史枚举）：每轮快照落库 + 幂等去重；
    上次完整检查点早于来源覆盖视野时，中断区间记缺口并标 partial，
    完整检查点不越过缺口（同 IR gap 语义）；失败退避。首接的历史条目
    （发布时间早于北京时间今日）标 is_backfill，不冒充新发生。
    """
    from zoneinfo import ZoneInfo

    from core.announcements import media_source

    source_id = source["source_id"]
    issuer = source["issuer_identifier"]
    started_at = datetime.now(timezone.utc)
    next_due_at = slot + timedelta(minutes=interval_minutes)
    fetched = inserted = revised = 0
    llm_stats: dict = {}
    dropped = None
    attempt_status = "failed"
    error_class = None
    error_detail = None
    try:
        match_terms = media_source.match_terms_for(source)
        items = media_source.fetch_and_parse(source, today=target.date())
        fetched = len(items)
        kept, dropped_items = [], 0
        new_rows: list[tuple] = []
        for it in items:
            if media_source.matches_company(it.title, it.summary, match_terms):
                kept.append(it)
            else:
                dropped_items += 1
        dropped = dropped_items

        first_run = state is None or state["last_success_at"] is None
        today_bj = target.astimezone(ZoneInfo("Asia/Shanghai")).date()
        observed_at = datetime.now(timezone.utc)
        for it in kept:
            item_date = (it.published_at.astimezone(
                ZoneInfo("Asia/Shanghai")).date()
                if it.published_at is not None else None)
            outcome, rev = store.upsert_announcement(
                conn, source_id=source_id, issuer=issuer,
                stock_codes=stock_codes,
                record=store.AnnouncementRecord(
                    source_record_id=it.source_record_id,
                    form_type="news", title=it.title, url=it.url,
                    published_at=it.published_at,
                    published_precision=it.precision,
                    published_raw=it.published_raw,
                    source_timezone=source["source_timezone"] or "UTC",
                    content_hash=it.content_hash,
                    excerpt=it.summary or None,
                    content_nature="unclassified",
                    relation_reason=media_source.relation_reason_for(source),
                    media_source=it.source_name or None),
                is_backfill=bool(first_run and item_date is not None
                                 and item_date < today_bj),
                observed_at=observed_at)
            if outcome == "inserted":
                inserted += 1
                new_rows.append((it.source_record_id, it.title, it.summary, rev))
            elif outcome == "revised":
                # 修订 = 内容已变：重新增强并回写到新版本行
                revised += 1
                new_rows.append((it.source_record_id, it.title, it.summary, rev))
        # 媒体条目一次调用产出 title_zh/summary_zh/importance 三字段
        _enhance_new_rows(conn, source_id=source_id, kind="media",
                          new_rows=new_rows, stock_code=stock_codes[0],
                          deadline=deadline, result=llm_stats,
                          label=f"media/{issuer}/{source['platform']}")

        finished_at = datetime.now(timezone.utc)
        # 停采缺口语义（参照 IR gap 语义）：上次完整检查点早于来源覆盖视野
        # （Finnhub 约 2 天、RSS 约 7~10 天）时，本轮快照无法证明中断区间
        # 的覆盖——记录缺口、标 partial，完整检查点不越过缺口推进
        horizon_days = media_source.COVERAGE_HORIZON_DAYS.get(
            source["platform"], 2)
        stale_gap = None
        if state is not None and state["checked_through"] is not None:
            ct = state["checked_through"]
            if ct < target - timedelta(days=horizon_days):
                stale_gap = (ct + timedelta(days=horizon_days), target)
        if stale_gap is not None:
            attempt_status = "partial"
            store.record_gap(
                conn, issuer, source_id, stale_gap, "history_unavailable",
                f"媒体来源覆盖视野约 {horizon_days} 天，停采区间无法补齐")
            store.append_covered_ranges(
                conn, issuer, source_id, interval_minutes=interval_minutes,
                ranges=[(target - timedelta(days=horizon_days), target)])
            store.upsert_check_state_attempt(
                conn, issuer, source_id, interval_minutes=interval_minutes,
                attempt_at=started_at, status=attempt_status,
                next_due_at=next_due_at, error_class=None)
        else:
            attempt_status = ("success_new" if (inserted or revised)
                              else "success_empty")
            store.upsert_check_state_success(
                conn, issuer, source_id, interval_minutes=interval_minutes,
                attempt_at=started_at, status=attempt_status,
                next_due_at=next_due_at, checked_through=target,
                covered_add=(datetime.combine(target.date(), datetime.min.time(),
                                              tzinfo=timezone.utc), target))
        store.insert_run(
            conn, task_id=task_id, issuer=issuer, source_id=source_id,
            started_at=started_at, finished_at=finished_at,
            status=attempt_status, fetched=fetched, inserted=inserted,
            revised=revised, dropped=dropped,
            parser_version=media_source.MEDIA_PARSER_VERSION,
            trigger_kind=trigger)
    except Exception as exc:
        if isinstance(exc, ir_source.IrFetchError):
            error_class = exc.error_class
        else:
            error_class = _error_class(exc)
        error_detail = f"{type(exc).__name__}: {exc}"[:500]
        logger.exception("媒体资讯采集失败 %s/%s", issuer, source["platform"])
        finished_at = datetime.now(timezone.utc)
        conn.rollback()
        try:
            failures = _consecutive_failures(conn, source_id)
        except Exception:
            failures = 0
        backoff = min(2 ** failures, 8)
        try:
            store.upsert_check_state_attempt(
                conn, issuer, source_id, interval_minutes=interval_minutes,
                attempt_at=started_at, status="failed",
                next_due_at=slot + timedelta(minutes=interval_minutes * backoff),
                error_class=error_class)
            store.insert_run(
                conn, task_id=task_id, issuer=issuer, source_id=source_id,
                started_at=started_at, finished_at=finished_at, status="failed",
                fetched=None, inserted=None, revised=None, dropped=None,
                error_class=error_class, error_detail=error_detail,
                parser_version=media_source.MEDIA_PARSER_VERSION,
                trigger_kind=trigger)
        except Exception:
            conn.rollback()
            raise

    return {
        "cik": issuer, "stock_codes": stock_codes, "status": attempt_status,
        "fetched": fetched, "inserted": inserted, "revised": revised,
        "dropped": dropped,
        "error_class": error_class, "error_detail": error_detail,
        "backfill": False, "platform": source["platform"], **llm_stats,
    }


# ── 整轮编排 ───────────────────────────────────────────────

def _load_watchlist_ciks(conn) -> tuple[list[tuple[str, str]], list[str]]:
    """读取当前 US 关注名单并解析 CIK。

    返回 ([(stock_code, cik)], [缺 CIK 的 stock_code])。相同 CIK 合并请求。
    CIK 解析复用 core/us_security_identity 的口径（stock_info.cik 本地优先）。
    """
    from core.us_security_identity import resolve_us_cik

    with conn.cursor() as cur:
        cur.execute(
            "SELECT stock_code FROM watchlist_item WHERE market = 'US' "
            "ORDER BY sort_order, id"
        )
        codes = [row[0] for row in cur.fetchall()]
    with_cik: dict[str, list[str]] = {}
    missing: list[str] = []
    for code in codes:
        cik = resolve_us_cik(code)
        if cik is None:
            missing.append(code)
        else:
            # 相同 CIK 合并网络请求，但保留完整证券集合：
            # 采集后要为全部证券写入关联，不能只关联第一个代码
            with_cik.setdefault(cik, []).append(code)
    return [(cik, sorted(codes)) for cik, codes in with_cik.items()], missing


def run_round(*, market: str = "US", trigger: str = "scheduled",
              stock_filter: str | None = None, due_only: bool | None = None,
              schedule_anchor: datetime | None = None,
              round_max_seconds: float = ROUND_MAX_SECONDS,
              request_timeout: float = 60,
              request_total_timeout: float = 180) -> dict:
    """执行一轮公告采集。返回整轮摘要。

    due_only：定时入口按各来源到期时间选取；手工/单来源入口默认检查全部
    关注来源（显式触发语义）。skipped_running 不更新任何来源状态。

    计划槽位（决定 next_due 的时间基准）：
    - 定时入口：调度器注册时把 IntervalTrigger 的 start_date 作为
      schedule_anchor 传入，槽位按触发相位对齐（见 _round_slot），
      与调度器实际触发网格一致；
    - 手工/恢复入口：无调度相位，anchor=None → 槽位 = 本轮开始时刻。
      两类入口基准不同是刻意的：手工轮只保证"本轮之后 +interval 到期"，
      不假装知道调度相位。
    """
    if market != "US":
        raise ValueError(f"本片只实施美股适配，收到 market={market!r}")
    # 注册/运行前配置校验：非法值明确报错，不静默回退
    config.announcements.validate()
    if not config.announcements.validated_enabled():
        raise RuntimeError(
            "STOCK_ANNOUNCEMENTS_ENABLED=false：公告采集未启用。"
            "手工入口同样要求显式开启配置（不提供绕过开关）。")
    interval_minutes = config.announcements.validated_sec_interval()
    if due_only is None:
        due_only = trigger == "scheduled"

    task_id = uuid.uuid4()
    started_at = datetime.now(timezone.utc)
    deadline = time.monotonic() + round_max_seconds

    with RoundLock() as lock:
        if not lock.acquired:
            # 未取得整轮锁：记 skipped_running 并立即退出，不排队、不发请求、
            # 不更新任何来源到期时间与检查点（由下一轮到期检查恢复）
            logger.warning("整轮锁 %s 被占用，本轮 skipped_running", ROUND_LOCK_NAME)
            with pool_conn() as conn:
                store.insert_run(
                    conn, task_id=task_id, started_at=started_at,
                    finished_at=datetime.now(timezone.utc),
                    status="skipped_running", trigger_kind=trigger)
                conn.commit()
            return {"task_id": str(task_id), "status": "skipped_running",
                    "sources": []}

        conn = lock.conn
        results: list[dict] = []
        now = datetime.now(timezone.utc)
        slot = _round_slot(now, interval_minutes, schedule_anchor)

        companies, missing_cik = _load_watchlist_ciks(conn)
        if stock_filter:
            code = stock_filter.strip().upper()
            companies = [(cik, codes) for cik, codes in companies
                         if code in codes]
            missing_cik = [c for c in missing_cik if c == code]

        # 缺 CIK 明确阻塞该公司 SEC 来源：落运行状态，不静默跳过
        for code in missing_cik:
            logger.error("关注股 %s 缺 CIK，SEC 来源阻塞", code)
            store.insert_run(
                conn, task_id=task_id, started_at=started_at,
                finished_at=datetime.now(timezone.utc), status="failed",
                error_class="missing_cik",
                error_detail=f"stock_code={code} 无 stock_info.cik，SEC 来源不可用",
                parser_version=sec_source.PARSER_VERSION, trigger_kind=trigger)
        if missing_cik:
            conn.commit()

        # 候选来源：注册 + 到期筛选 + 最久未检查优先
        candidates = []
        for cik, stock_codes in companies:
            source_id = store.ensure_sec_source(
                conn, cik, parser_version=sec_source.PARSER_VERSION)
            state = store.load_check_state(conn, cik, source_id)
            candidates.append({
                "stock_codes": stock_codes, "cik": cik, "source_id": source_id,
                "state": state,
                "last_attempt": (state or {}).get("last_attempt_at"),
            })
        conn.commit()
        if due_only:
            candidates = [
                c for c in candidates
                if c["state"] is None or c["state"]["next_due_at"] is None
                or c["state"]["next_due_at"] <= now
            ]
        # 最久未检查优先，避免总从同一家公司开始让队尾长期得不到检查
        candidates.sort(key=lambda c: (c["last_attempt"] is not None,
                                       c["last_attempt"]))

        # ── IR 来源：注册表驱动，受阻/待验证是一等状态 ──
        # 关注名单内且有 seed 的公司确保注册行存在（初始状态见 IR_SEEDS）；
        # 已存在的行不被 seed 覆盖（部署验证后逐家启用由运营显式调整）。
        ir_interval_default = config.announcements.validated_ir_interval()
        code_to_cik = {code: cik for cik, codes in companies for code in codes}
        for code in (c for c in code_to_cik if c in ir_source.IR_SEEDS):
            seed = ir_source.IR_SEEDS[code]
            issuer = code_to_cik[code]
            store.ensure_ir_source(
                conn, issuer=issuer, platform=seed["platform"],
                allowed_domain=seed["allowed_domain"],
                entry_url=seed["entry_url"],
                source_timezone=seed["source_timezone"],
                parser_version=ir_source.IR_ADAPTERS[seed["platform"]].parser_version,
                interval_minutes=None,  # None = 用配置默认；注册表可单源覆盖
                enabled=False, source_status=seed["initial_status"],
                status_reason=seed["initial_reason"])
        conn.commit()
        ir_all = store.load_ir_sources(conn)  # source_kind IN ('ir','media')
        watched_issuers = set(code_to_cik.values()) | set(code_to_cik)
        # 取消关注停止主动轮询；只有 enabled 且 source_status='enabled' 的
        # 来源才抓取——blocked/pending_verification 即使 enabled=true 也零
        # 请求，且列入 ir_inactive（状态与原因可见，不进成功率分母）
        def _ir_active(s) -> bool:
            return bool(s["enabled"]) and s["source_status"] == "enabled"

        ir_rows = [s for s in ir_all if s["source_kind"] == "ir"]
        ir_inactive = [
            {"source_id": s["source_id"], "issuer": s["issuer_identifier"],
             "platform": s["platform"], "status": s["source_status"],
             "reason": s["status_reason"]}
            for s in ir_rows
            if s["issuer_identifier"] in watched_issuers and not _ir_active(s)
        ]
        ir_candidates = []
        for s in ir_rows:
            if not _ir_active(s):
                continue
            codes = ([c for c, cik in code_to_cik.items()
                      if cik == s["issuer_identifier"]]
                     or ([s["issuer_identifier"]]
                         if s["issuer_identifier"] in code_to_cik else []))
            if not codes:
                continue
            if stock_filter and stock_filter.strip().upper() not in codes:
                continue
            state = store.load_check_state(
                conn, s["issuer_identifier"], s["source_id"])
            interval = s["interval_minutes"] or ir_interval_default
            if due_only and state is not None and state["next_due_at"] is not None \
                    and state["next_due_at"] > now:
                continue
            ir_candidates.append({
                "source": s, "stock_codes": sorted(codes), "state": state,
                "interval_minutes": interval,
                "last_attempt": (state or {}).get("last_attempt_at"),
            })
        ir_candidates.sort(key=lambda c: (c["last_attempt"] is not None,
                                          c["last_attempt"]))

        # ── 媒体来源（每日资讯公司新闻层）：每股 Google News RSS + Finnhub ──
        # 注册行默认 enabled=false/pending_verification（真实启用属部署片）；
        # 不占 SEC gate，每源每轮一次请求，无重试循环
        from core.announcements import media_source
        media_interval_default = config.announcements.validated_media_interval()
        for code in (c for c in code_to_cik if c in media_source.MEDIA_SEEDS):
            for spec in media_source.media_source_specs(code):
                store.ensure_ir_source(
                    conn, issuer=code_to_cik[code], platform=spec["platform"],
                    allowed_domain=spec["allowed_domain"],
                    entry_url=spec["entry_url"],
                    source_timezone=spec["source_timezone"],
                    parser_version=media_source.MEDIA_PARSER_VERSION,
                    interval_minutes=None, enabled=False,
                    source_status="pending_verification",
                    status_reason="媒体来源，部署片验证后逐源启用",
                    source_kind="media", query_config=spec["query_config"])
        conn.commit()
        media_rows = [s for s in store.load_ir_sources(conn)
                      if s["source_kind"] == "media"]
        media_inactive = [
            {"source_id": s["source_id"], "issuer": s["issuer_identifier"],
             "platform": s["platform"], "status": s["source_status"],
             "reason": s["status_reason"]}
            for s in media_rows
            if s["issuer_identifier"] in watched_issuers and not _ir_active(s)
        ]
        media_candidates = []
        for s in media_rows:
            if not _ir_active(s):
                continue
            codes = ([c for c, cik in code_to_cik.items()
                      if cik == s["issuer_identifier"]]
                     or ([s["issuer_identifier"]]
                         if s["issuer_identifier"] in code_to_cik else []))
            if not codes:
                continue
            if stock_filter and stock_filter.strip().upper() not in codes:
                continue
            state = store.load_check_state(
                conn, s["issuer_identifier"], s["source_id"])
            interval = s["interval_minutes"] or media_interval_default
            if due_only and state is not None and state["next_due_at"] is not None \
                    and state["next_due_at"] > now:
                continue
            media_candidates.append({
                "source": s, "stock_codes": sorted(codes), "state": state,
                "interval_minutes": interval,
                "last_attempt": (state or {}).get("last_attempt_at"),
            })
        media_candidates.sort(key=lambda c: (c["last_attempt"] is not None,
                                             c["last_attempt"]))

        budget_exhausted = False
        lock_lost = False
        for cand in candidates:
            if time.monotonic() >= deadline:
                budget_exhausted = True
                logger.warning("整轮预算耗尽，剩余 %d 个来源下轮处理",
                               len(candidates) - len(results))
                break
            try:
                lock.check_alive()
            except psycopg2.Error:
                logger.error("持锁会话失效，中止本轮剩余来源（不重连沿用旧许可）")
                lock_lost = True
                break
            source_deadline = min(deadline,
                                  time.monotonic() + PER_SOURCE_MAX_SECONDS)
            try:
                result = _sync_sec_source(
                    conn, source_id=cand["source_id"], cik=cand["cik"],
                    stock_codes=cand["stock_codes"], state=cand["state"],
                    target=now, slot=slot,
                    interval_minutes=interval_minutes,
                    task_id=task_id, trigger=trigger, deadline=source_deadline,
                    request_timeout=request_timeout,
                    request_total_timeout=request_total_timeout)
            except psycopg2.Error:
                # 持锁会话失效（含失败落库都写不进去）：中止本轮剩余来源，
                # 不自动重连沿用旧许可；恢复必须重新取得锁并读持久进度
                logger.error("持锁会话失效，中止本轮剩余来源", exc_info=True)
                lock_lost = True
                break
            try:
                conn.commit()  # 最终数据 + 覆盖记录 + 检查点同一事务
            except psycopg2.Error:
                logger.error("提交失败（持锁会话失效？），中止本轮", exc_info=True)
                lock_lost = True
                break
            results.append(result)

        # ── IR 来源处理（同一持锁会话，同一提交节奏）──
        for cand in ir_candidates:
            if time.monotonic() >= deadline:
                budget_exhausted = True
                logger.warning("整轮预算耗尽，剩余 IR 来源下轮处理")
                break
            try:
                lock.check_alive()
            except psycopg2.Error:
                logger.error("持锁会话失效，中止本轮剩余来源（不重连沿用旧许可）")
                lock_lost = True
                break
            try:
                result = _sync_ir_source(
                    conn, source=cand["source"],
                    stock_codes=cand["stock_codes"], state=cand["state"],
                    target=now, slot=slot,
                    interval_minutes=cand["interval_minutes"],
                    task_id=task_id, trigger=trigger, deadline=deadline)
            except psycopg2.Error:
                logger.error("持锁会话失效，中止本轮剩余来源", exc_info=True)
                lock_lost = True
                break
            try:
                conn.commit()
            except psycopg2.Error:
                logger.error("提交失败（持锁会话失效？），中止本轮", exc_info=True)
                lock_lost = True
                break
            results.append(result)

        # ── 媒体来源处理（同一持锁会话，同一提交节奏）──
        for cand in media_candidates:
            if time.monotonic() >= deadline:
                budget_exhausted = True
                logger.warning("整轮预算耗尽，剩余媒体来源下轮处理")
                break
            try:
                lock.check_alive()
            except psycopg2.Error:
                logger.error("持锁会话失效，中止本轮剩余来源（不重连沿用旧许可）")
                lock_lost = True
                break
            try:
                result = _sync_media_source(
                    conn, source=cand["source"],
                    stock_codes=cand["stock_codes"], state=cand["state"],
                    target=now, slot=slot,
                    interval_minutes=cand["interval_minutes"],
                    task_id=task_id, trigger=trigger, deadline=deadline)
            except psycopg2.Error:
                logger.error("持锁会话失效，中止本轮剩余来源", exc_info=True)
                lock_lost = True
                break
            try:
                conn.commit()
            except psycopg2.Error:
                logger.error("提交失败（持锁会话失效？），中止本轮", exc_info=True)
                lock_lost = True
                break
            results.append(result)

        # 整轮状态汇总：全部成功 / 部分成功 / 全部失败 / 预算耗尽 / 锁会话失效
        # （skipped_running 在上方提前返回；采集失败或协调失败不得汇总成成功）
        statuses = [r["status"] for r in results]
        if lock_lost:
            round_status = "lock_lost"
        elif budget_exhausted:
            round_status = "budget_exhausted"
        elif all(s in ("success_new", "success_empty") for s in statuses) \
                and not missing_cik:
            round_status = "completed"
        elif statuses and all(s == "failed" for s in statuses):
            round_status = "failed"
        else:
            round_status = "partial"

        summary = {
            "task_id": str(task_id),
            "status": round_status,
            "lock": ROUND_LOCK_NAME,
            "companies": len(companies),
            "missing_cik": missing_cik,
            "processed": len(results),
            # 受阻/待验证 IR 来源：一等状态，不进成功率分母，原因可见
            "ir_inactive": ir_inactive,
            # 媒体来源（每日资讯公司新闻层）同语义
            "media_inactive": media_inactive,
            # LLM 增强整轮汇总（可见性：不靠手工查库排查增强故障）
            "llm": {
                "ok": sum(r.get("llm_ok", 0) for r in results),
                "failed": sum(r.get("llm_failed", 0) for r in results),
                "skipped": sum(r.get("llm_skipped", 0) for r in results),
            },
            "sources": results,
        }
        logger.info("公告采集整轮完成: %s", json.dumps(
            {k: v for k, v in summary.items() if k != "sources"},
            ensure_ascii=False, default=str))
        return summary


class pool_conn:
    """skipped_running 记录走连接池的短事务（不持锁写入）。"""

    def __enter__(self):
        from db import get_connection, release_connection
        self._release = release_connection
        self.conn = get_connection()
        return self.conn

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self.conn.rollback()
        self._release(self.conn)


# ── CLI ────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m core.announcements.sync",
        description="关注股官方公告采集（手工入口；与定时入口共用整轮互斥锁）")
    parser.add_argument("--market", default="US", help="本片只支持 US")
    parser.add_argument("--stock-code", default=None,
                        help="只采集指定关注股（调试/单来源验证）")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    try:
        summary = run_round(market=args.market, trigger="manual",
                            stock_filter=args.stock_code)
    except (RuntimeError, ValueError) as exc:
        print(f"拒绝执行: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    # 退出码：0 全部成功；2 配置拒绝；3 整轮锁占用；4 采集未全部成功
    if summary["status"] == "skipped_running":
        return 3
    return 0 if summary["status"] == "completed" else 4


if __name__ == "__main__":
    sys.exit(main())
