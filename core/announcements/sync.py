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
from core.announcements import sec_source, store

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
        for f in unique_filings:
            outcome = store.upsert_announcement(
                conn, source_id=source_id, issuer=cik, stock_codes=stock_codes,
                filing=f, title=sec_source.neutral_title(f.form),
                is_backfill=backfill, observed_at=observed_at)
            if outcome == "inserted":
                inserted += 1
            elif outcome == "revised":
                revised += 1

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
        "backfill": backfill,
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
