#!/usr/bin/env python3
"""
scheduler.py — 定时任务调度器

使用 APScheduler 定时触发 sync.py 的增量同步任务。
支持三个市场独立调度规则（A股/港股/美股），失败重试，通知预留。

任务分两套：
  - 行情同步：A 股 16:37、港股 17:12，同步 daily_quote + 刷 mv_fcf_yield
  - 财务同步：A 股 17:07、港股 17:37 刷物化视图；美股 06:12 走版本层编排

用法:
    python -m core.scheduler           # 启动调度器
    python -m core.scheduler --dry-run # 预览调度计划，不实际执行
    python -m core.scheduler --once    # 立即执行一次所有任务后退出
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

os.environ.setdefault("TQDM_DISABLE", "1")

import config
from db import health_check, close_pool, execute

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("scheduler")


# ── 交易日判断 ──────────────────────────────────────────────


def _is_china_trading_day(dt: datetime | None = None) -> bool:
    """简单交易日判断（仅排除周末）。

    TODO: 接入节假日日历（exchange_calendars 或 akshare）排除法定节假日。
    目前只排除周六周日，足以满足基本调度需求。
    """
    if dt is None:
        dt = datetime.now()
    # 北京时间 UTC+8
    return dt.weekday() < 5  # 0=Mon, 4=Fri


def _is_us_trading_day(dt: datetime | None = None) -> bool:
    """简单美股交易日判断（以美东时间为准）。

    美股周末不开市。cron 表达式已配置为周二~周六（北京时间早晨，
    对应美东周一~周五收盘后），这里做二次检查。

    注意必须用美东日期判断：北京时间周六早晨 = 美东周五收盘后，
    若按服务器本地时间判 weekday，周五的行情/财务同步会被误杀。
    """
    us_tz = ZoneInfo("America/New_York")
    if dt is None:
        us_dt = datetime.now(us_tz)
    elif dt.tzinfo is None:
        # scheduler 的 cron 明确固定 Asia/Shanghai，不能依赖测试机或容器的
        # 系统时区；无时区输入按调度器时间解释后再换算美东。
        us_dt = dt.replace(tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(us_tz)
    else:
        us_dt = dt.astimezone(us_tz)
    return us_dt.weekday() < 5


# ── 通知接口 ────────────────────────────────────────────────


def _notify(message: str, level: str = "info") -> None:
    """发送通知（预留接口）。

    目前仅写日志。未来可扩展为：
    - HTTP POST 到 webhook（钉钉/飞书/Slack）
    - 企业微信消息推送
    - 邮件
    """
    if level == "error":
        logger.error("[通知] %s", message)
    else:
        logger.info("[通知] %s", message)

    # 预留：如果配置了 notify_url，发送 HTTP 通知
    if config.scheduler.notify_url:
        try:
            import requests

            payload = {
                "message": message,
                "level": level,
                "timestamp": datetime.now().isoformat(),
            }
            requests.post(
                config.scheduler.notify_url,
                json=payload,
                timeout=10,
            )
        except Exception as exc:
            logger.warning("通知发送失败: %s", exc)


# ── 物化视图刷新 ────────────────────────────────────────────


def _refresh_materialized_views(job_type: str, market: str = "") -> None:
    """根据任务类型和市场刷新物化视图。

    行情同步后只刷新 mv_fcf_yield（因为只有市值变了）。
    财务同步后按依赖顺序刷新全部三层物化视图。
    Phase C1(2026-08-11):US 的三个旧物化视图与 mv_us_fcf_yield 停止刷新
    (待退役对象,估值由 snapshot 读取路径按 daily_quote 实时计算);CN 不变。

    刷新失败只记 warning，不影响同步结果。
    """
    views = []
    if job_type == "daily_quote":
        views = ["mv_fcf_yield"]
    elif job_type == "daily_quote_us":
        views = []  # Phase C1: mv_us_fcf_yield 停刷
    elif job_type == "financial":
        if market == "US":
            views = []  # Phase C1: 三个 US 旧物化视图停刷
        else:
            views = ["mv_financial_indicator", "mv_indicator_ttm", "mv_fcf_yield"]

    for view in views:
        try:
            execute(f"REFRESH MATERIALIZED VIEW CONCURRENTLY {view}")
            logger.info("物化视图刷新完成: %s", view)
        except Exception as exc:
            logger.warning("物化视图刷新失败（不影响同步结果）: %s → %s", view, exc)

    if views:
        logger.info("物化视图刷新完成: %s", " → ".join(views))


# ── 同步任务执行器（带重试）────────────────────────────────


def _run_sync_job(market: str, job_type: str = "financial") -> dict:
    """执行单市场同步任务，带重试机制。

    Args:
        market: "CN_A" | "CN_HK" | "US"
        job_type: "financial" | "daily_quote"

    Returns:
        {"success": bool, "attempt": int, "elapsed": float, "error": str|None}
    """
    max_retries = config.scheduler.max_retries
    base_delay = config.scheduler.retry_base_delay

    for attempt in range(1, max_retries + 1):
        t0 = time.time()
        try:
            logger.info(
                "[%s/%s] 同步开始（第 %d/%d 次尝试）",
                market,
                job_type,
                attempt,
                max_retries,
            )

            if job_type in ("daily_quote", "daily_quote_us"):
                result = _sync_daily_quote(market)
            elif market == "US":
                # Phase C1: 原子编排(sync 分类 → 护栏 → projection → validate)
                result = _run_us_financial_orchestration(t0)
            else:
                result = _sync_financial(market)

            elapsed = time.time() - t0
            logger.info(
                "[%s/%s] 同步完成: 成功=%d, 失败=%d, 耗时=%.1fs",
                market,
                job_type,
                result.get("success", 0),
                result.get("failed", 0),
                elapsed,
            )

            # 写入 sync_log（仪表板 7 天趋势）
            from core.sync._utils import log_sync_result

            log_sync_result(
                data_type=f"{job_type}_{market}",
                status="success",
                success_count=result.get("success", 0),
                fail_count=result.get("failed", 0),
                started_at=datetime.fromtimestamp(t0),
            )

            # 港股财务同步后：capex NULL 自动兜底（T1 腾讯即时补 + T2 披露易 PDF 夜间排队）。
            # 挂在视图刷新之前，T1 写入可顺带被本次刷新带进 mv_* 视图；兜底失败不影响同步主流程。
            if (
                job_type == "financial"
                and market == "CN_HK"
                and config.scheduler.hk_capex_fallback_auto
            ):
                try:
                    from core.sync.hk_fallback import run_hk_capex_fallback

                    fb = run_hk_capex_fallback(dry_run=False)
                    logger.info(
                        "[CN_HK] capex 自动兜底: 候选=%d 只, T1 写入=%d 条, T2 入队=%d 条",
                        fb["candidate_stocks"], fb["t1_written"], fb["queued"],
                    )
                except Exception as fb_err:
                    logger.error("[CN_HK] capex 自动兜底失败（不影响同步结果）: %s", fb_err)

            # 同步完成后刷新物化视图
            _refresh_materialized_views(job_type, market)

            _notify(
                f"{market}/{job_type} 同步完成: 成功={result.get('success', 0)}, "
                f"失败={result.get('failed', 0)}, 耗时={elapsed:.0f}s"
            )

            # 财务同步完成后自动触发数据校验
            # Phase C1:US 的 validate 已在原子编排内执行,不再重复;
            # CN 路径保持原样(其 validate import 问题不在本任务范围)
            if job_type == "financial" and market != "US":
                try:
                    from validate import run_after_sync

                    val_market = {"CN_A": "A", "CN_HK": "HK", "US": "US"}.get(
                        market, ""
                    )
                    val_result = run_after_sync(market=val_market)
                    if val_result.get("success"):
                        logger.info(
                            "[%s] 校验完成: errors=%d, warnings=%d",
                            market,
                            val_result.get("errors", 0),
                            val_result.get("warnings", 0),
                        )
                        _notify(
                            f"{market} 校验: errors={val_result.get('errors', 0)}, "
                            f"warnings={val_result.get('warnings', 0)}"
                        )
                    else:
                        logger.warning(
                            "[%s] 校验失败: %s", market, val_result.get("error")
                        )
                except Exception as val_exc:
                    logger.warning(
                        "[%s] 校验异常（不影响同步结果）: %s", market, val_exc
                    )

            return {
                "success": True,
                "attempt": attempt,
                "elapsed": elapsed,
                "error": None,
            }

        except Exception as exc:
            elapsed = time.time() - t0
            error_msg = f"{type(exc).__name__}: {exc}"
            logger.error(
                "[%s/%s] 第 %d 次尝试失败: %s (耗时=%.1fs)",
                market,
                job_type,
                attempt,
                error_msg,
                elapsed,
            )

            if attempt < max_retries:
                delay = base_delay * (2 ** (attempt - 1))
                logger.info("[%s/%s] 等待 %.0f 秒后重试...", market, job_type, delay)
                time.sleep(delay)
            else:
                # 写入 sync_log（失败记录）
                from core.sync._utils import log_sync_result

                log_sync_result(
                    data_type=f"{job_type}_{market}",
                    status="failed",
                    success_count=0,
                    fail_count=1,
                    started_at=datetime.fromtimestamp(t0),
                    error_detail=error_msg,
                )

                _notify(
                    f"{market}/{job_type} 同步最终失败（重试 {attempt} 次）: {error_msg}",
                    level="error",
                )
                return {
                    "success": False,
                    "attempt": attempt,
                    "elapsed": elapsed,
                    "error": error_msg,
                }


def _sync_daily_quote(market: str) -> dict:
    """执行行情同步。

    通过调用 sync.py 的 SyncManager.sync_daily_quote() 来完成。
    market 为规范名（CN_A / CN_HK / US）。
    """
    from core.sync import SyncManager

    manager = SyncManager(
        max_workers=config.scheduler.sync_workers,
        force=config.scheduler.force_sync,
    )
    return manager.sync_daily_quote(market)


def _sync_financial(market: str) -> dict:
    """执行 A 股/港股增量同步。

    通过调用 core.sync 的 SyncManager 来完成，不重写同步逻辑。
    market 为规范名（CN_A / CN_HK），与 MARKET_CONFIG 键一致。
    """
    from core.sync import SyncManager

    manager = SyncManager(
        max_workers=config.scheduler.sync_workers,
        force=config.scheduler.force_sync,
    )
    return manager.sync_financial(market)


def _run_hk_pdf_queue_job() -> None:
    """夜间排空港股 capex PDF 队列（T2 兜底慢链，与同步窗口解耦）。

    由调度器按 hk_capex_pdf_cron 触发；队列为空时快速返回。
    有写入时刷新港股 mv_* 视图，让 FCF 收益率次日可见。
    """
    from core.sync.hk_fallback import drain_pdf_queue

    try:
        result = drain_pdf_queue(
            limit=config.scheduler.hk_capex_pdf_nightly_limit, dry_run=False
        )
    except Exception as exc:
        logger.error("[HK_capex_pdf] 队列排空失败: %s", exc)
        _notify(f"港股 capex PDF 队列排空失败: {exc}", level="error")
        return

    if result["queue_before"] == 0:
        logger.info("[HK_capex_pdf] 队列为空，跳过")
        return

    t2 = result.get("t2_stats") or {}
    logger.info(
        "[HK_capex_pdf] 排空完成: 处理=%d, 写入=%d, 人工队列=%d, 升级耗尽=%d, 失败=%d, 剩余=%d, 耗时=%.0fs",
        result["processed"], result["written"],
        t2.get("review_queue", 0), t2.get("escalate_exhausted", 0), t2.get("failed", 0),
        result["remaining"], result["elapsed"],
    )
    _notify(
        f"HK_capex_pdf 排空: 处理={result['processed']}, 写入={result['written']}, "
        f"剩余={result['remaining']}"
    )
    if result["written"] > 0:
        _refresh_materialized_views("financial", "CN_HK")


def build_us_sync_scope() -> dict:
    """构造目录与财务共用的 US 计划范围，不启动财务或目录写入。"""
    from core.us_security_identity import resolve_us_symbols_batch

    indexes_str = os.environ.get("STOCK_US_INDEXES", "SP500")
    indexes = [idx.strip().upper() for idx in indexes_str.split(",") if idx.strip()]
    logger.info("美股同步范围: %s", ", ".join(indexes))
    scope_result = {
        "total": 0, "success": 0, "failed": 0, "skipped": 0, "elapsed": 0,
        "indexes_synced": [], "errors": [], "failures": [], "index_errors": [],
        "no_write": [], "index_sources": {}, "index_tickers": set(),
        "raw_index_tickers": set(),
    }
    from core.fetchers.us_financial import USFinancialFetcher
    raw_index: set[str] = set()
    for index in indexes:
        fetcher = USFinancialFetcher()
        try:
            tickers = set(fetcher.get_tickers_by_index(index))
            scope_result["index_sources"][index] = fetcher.get_index_source_status(index)
            if not tickers:
                scope_result["index_errors"].append(f"{index}: 指数成分解析为空")
                logger.error("指数 %s 成分解析为空", index)
                continue
            raw_index |= tickers
            scope_result["indexes_synced"].append(index)
        except Exception as exc:
            error_msg = f"{index}: {type(exc).__name__}: {exc}"
            logger.error("指数 %s 解析失败: %s", index, exc)
            scope_result["index_sources"][index] = fetcher.get_index_source_status(index)
            scope_result["index_errors"].append(error_msg)
    scope_result["raw_index_tickers"] = raw_index
    if scope_result["index_errors"]:
        scope_result["error"] = "index constituent resolution failed"
        return {"scope_result": scope_result, "scope": [],
                "raw_index": raw_index, "resolved_map": {}}

    resolved_map = resolve_us_symbols_batch(raw_index)
    canonical_index = set(resolved_map.values())
    scope_result["index_tickers"] = canonical_index
    supplement = _load_supplement_tickers()
    scope_result["supplement_tickers"] = sorted(supplement)
    scope_result["supplement_now_in_index"] = sorted(supplement & canonical_index)
    scope = sorted(canonical_index | supplement)
    if not scope:
        scope_result["index_errors"].append("empty scope: 无有效指数成分")
        scope_result["error"] = "empty scope"
    return {"scope_result": scope_result, "scope": scope,
            "raw_index": raw_index, "resolved_map": resolved_map}


def _sync_us() -> dict:
    """目录准入后，对同一计划范围内身份安全的证券执行财务同步。"""
    from core.sync import sync_us_market
    from core.sync.us_security_directory import sync_us_security_directory

    built = build_us_sync_scope()
    total_result = built["scope_result"]
    scope = built["scope"]
    if total_result.get("error"):
        total_result["directory"] = {
            "status": "not_run", "complete": False, "planned_scope": scope,
            "collectable_scope": [], "blocked_tickers": [], "items": [],
        }
        return total_result

    try:
        directory = sync_us_security_directory(
            planned_scope=scope, raw_index_tickers=built["raw_index"],
            resolved_map=built["resolved_map"],
            index_sources=total_result.get("index_sources"), apply=True)
    except Exception as exc:
        logger.exception("US 证券目录同步失败")
        directory = {
            "status": "failed", "complete": False,
            "planned_scope": scope, "collectable_scope": [],
            "blocked_tickers": [], "items": [],
            "fatal_error": f"{type(exc).__name__}: {exc}",
        }
        total_result["errors"].append(f"directory: {type(exc).__name__}: {exc}")
    total_result["directory"] = directory
    total_result["planned_scope"] = scope
    total_result["resolved_map"] = built["resolved_map"]
    collectable = sorted(directory.get("collectable_scope", []))
    total_result["collectable_scope"] = collectable

    class Args:
        us_index = None
        us_tickers = ",".join(collectable)
        force = config.scheduler.force_sync

    if not collectable:
        logger.error("US 目录未确认可采集证券，财务阶段不启动；计划范围=%d", len(scope))
        return total_result
    try:
        result = sync_us_market(Args())
        total_result["total"] += result.get("total", 0)
        total_result["success"] += result.get("success", 0)
        total_result["failed"] += result.get("failed", 0)
        total_result["skipped"] += result.get("skipped", 0)
        total_result["elapsed"] += result.get("elapsed", 0)
        total_result["no_write"].extend(result.get("no_write", []))
        total_result["errors"].extend(result.get("errors", []))
        total_result["failures"].extend(result.get("failures", []))
        if result.get("error"):
            total_result["index_errors"].append(f"sync: {result['error']}")
        logger.info(
            "美股同步完成: planned=%d collectable=%d success=%d failed=%d skipped=%d",
            len(scope), len(collectable), result.get("success", 0),
            result.get("failed", 0), result.get("skipped", 0),
        )
    except Exception as exc:
        error_msg = f"sync: {type(exc).__name__}: {exc}"
        logger.error("美股同步失败: %s", exc)
        total_result["errors"].append(error_msg)
        total_result["index_errors"].append(error_msg)
    return total_result


# ── Phase C1: US 原子编排(sync 分类 → 护栏 → projection → validate)────

_PHASE_C_SKIPS_CSV = Path("docs/core/US_PHASE_C_EXPECTED_SKIPS.csv")
_PHASE_C_INDEX_ONLY_CSV = Path("docs/core/US_PHASE_C_INDEX_ONLY.csv")
_PHASE_C_SUPPLEMENT_CSV = Path("docs/core/US_PHASE_C_UNIVERSE_SCOPE_SUPPLEMENT.csv")
_PHASE_C_SUMMARY_DIR = Path("build/financial_comparison/phaseC_sync")


def _load_expected_skips(today=None) -> dict[str, dict]:
    """加载受控 expected-skip 台账(仅未过期条目,§3.3)。"""
    import csv as _csv

    if today is None:
        today = datetime.now().date()
    skips: dict[str, dict] = {}
    if not _PHASE_C_SKIPS_CSV.exists():
        return skips
    with open(_PHASE_C_SKIPS_CSV) as f:
        for row in _csv.DictReader(f):
            if date.fromisoformat(row["review_by"].strip()) >= today:
                skips[row["stock_code"].strip().upper()] = row
    return skips


# 台账 reason_code → 允许匹配的失败 kind;kind 不匹配一律 blocking(§3.3,
# 防止"version writer 失败被 404 台账放行"类误豁免)
_SKIP_KIND_BY_REASON = {
    "COMPANYFACTS_PERMANENT_404": {"fetch_404"},
    "CIK_MAPPING_MISSING": {"cik_mapping"},
    "TICKER_MAPPING_DRIFT": {"cik_mapping"},
    "FOREIGN_IFRS_NO_USGAAP_FACTS": {"zero_facts"},
    "NO_MAPPABLE_USGAAP_FACTS": {"zero_facts"},
    "NO_USGAAP_FACTS": {"zero_facts"},
    "NO_COMPANYFACTS_DATA": {"no_data"},
}


def _classify_us_sync_outcome(sync_result: dict, skips: dict[str, dict]) -> dict:
    """把 sync 结果分类为 expected_skip / blocking_failure(§3.3)。

    规则:
    - 指数级失败(index_errors)一律 blocking;
    - ticker 级失败仅当 (ticker, 失败 kind) 与未过期台账条目的 reason_code
      匹配时才计 expected_skip;kind 不匹配或未登记一律 blocking;
    - no_write(抓取成功但版本层零事实)kind 为 zero_facts。
    """
    expected: set[str] = set()
    blocking: set[str] = set()

    for f in sync_result.get("failures", []):
        ticker = str(f.get("ticker", "")).upper()
        kind = str(f.get("kind", "other"))
        entry = skips.get(ticker)
        allowed = _SKIP_KIND_BY_REASON.get(str((entry or {}).get("reason_code", "")), set())
        if entry is not None and kind in allowed:
            expected.add(ticker)
        else:
            blocking.add(ticker)

    for ticker in sync_result.get("no_write", []):
        t = str(ticker).upper()
        entry = skips.get(t)
        allowed = _SKIP_KIND_BY_REASON.get(str((entry or {}).get("reason_code", "")), set())
        if entry is not None and "zero_facts" in allowed:
            expected.add(t)
        else:
            blocking.add(t)

    index_errors = list(sync_result.get("index_errors", []))
    return {
        "expected_skip": sorted(expected),
        "blocking_failure": sorted(blocking),
        "index_errors": index_errors,
    }


def _load_index_only_registry() -> set[str]:
    """加载已登记的 index-only ticker(指数可解析但不在 universe,§3.2 对账)。"""
    import csv as _csv

    if not _PHASE_C_INDEX_ONLY_CSV.exists():
        return set()
    with open(_PHASE_C_INDEX_ONLY_CSV) as f:
        return {r["stock_code"].strip().upper() for r in _csv.DictReader(f)}


def _load_supplement_tickers(today=None) -> set[str]:
    """加载 universe 补充清单(C2 §3.1),带完整校验。

    校验失败(空值/重复/非 US universe/格式非法)抛 ValueError → 调用方阻断。
    review_by 过期:明确告警并保留当前范围,不静默移出(人工决定前不清除)。
    """
    import csv as _csv
    import re as _re

    if today is None:
        today = datetime.now().date()
    if not _PHASE_C_SUPPLEMENT_CSV.exists():
        return set()
    tickers: list[str] = []
    with open(_PHASE_C_SUPPLEMENT_CSV) as f:
        for row in _csv.DictReader(f):
            code = (row.get("stock_code") or "").strip().upper()
            if not code or not _re.fullmatch(r"[A-Z0-9.\-]+", code):
                raise ValueError(f"补充清单非法 ticker: {row!r}")
            review_by = (row.get("review_by") or "").strip()
            if not review_by:
                raise ValueError(f"补充清单缺 review_by: {code}")
            if date.fromisoformat(review_by) < today:
                logger.error(
                    "补充清单 ticker %s 的 review_by=%s 已过期,需人工复核(范围保留)",
                    code, review_by,
                )
            tickers.append(code)
    dupes = sorted({t for t in tickers if tickers.count(t) > 1})
    if dupes:
        raise ValueError(f"补充清单重复 ticker: {dupes}")
    rows = execute(
        "SELECT stock_code FROM stock_info WHERE market = 'US' AND stock_code = ANY(%s)",
        (tickers,), fetch=True,
    ) or []
    unknown = sorted(set(tickers) - {r[0] for r in rows})
    if unknown:
        raise ValueError(f"补充清单含非 US universe ticker: {unknown}")
    return set(tickers)


def _reconcile_us_universe(
    scope_tickers: set[str],
    index_tickers: set[str],
    expected_skip: list[str],
    raw_index_tickers: set[str] | None = None,
    resolved_map: dict[str, str] | None = None,
) -> dict:
    """最终 sync scope(指数 canonical ∪ 补充清单)vs active US universe 对账。

    out_of_sync_scope = 不在 scope 的 universe 股票 + universe 内的 expected_skip。
    index_only 按原始指数 ticker 定义,但经身份表解析到 active canonical 的
    (BNY→BK 等)视为已覆盖,不再计入(Phase C-US-IDENTITY §3.2 规则 3)。
    """
    universe = {r[0] for r in execute(
        "SELECT stock_code FROM stock_info WHERE market = 'US' "
        "AND (delist_date IS NULL OR delist_date > CURRENT_DATE)", fetch=True) or []}
    not_in_scope = sorted(universe - scope_tickers)
    out_scope = sorted(set(not_in_scope) | (set(expected_skip) & universe))
    if raw_index_tickers is None:
        raw_index_tickers = index_tickers
    resolved_map = resolved_map or {}
    index_only = sorted(
        t for t in raw_index_tickers
        if resolved_map.get(t, t) not in universe
    )
    return {
        "universe_count": len(universe),
        "index_ticker_count": len(index_tickers),
        "raw_index_ticker_count": len(raw_index_tickers),
        "scope_ticker_count": len(scope_tickers),
        "out_of_sync_scope": out_scope,
        "universe_not_in_sync_scope": not_in_scope,
        "index_only_tickers": index_only,
        "expected_skip_in_universe": sorted(set(expected_skip) & universe),
    }


def _missing_current_ttm_scope(scope_tickers: set[str]) -> list[str]:
    """范围内已有版本事实但缺 current TTM 的证券，需补投影而非跳过。"""
    codes = sorted(scope_tickers)
    if not codes:
        return []
    rows = execute("""
        SELECT DISTINCT f.stock_code
        FROM us_financial_fact_version f
        LEFT JOIN us_financial_current_ttm t ON t.stock_code = f.stock_code
        WHERE f.stock_code = ANY(%s) AND t.stock_code IS NULL
        ORDER BY f.stock_code
    """, (codes,), fetch=True) or []
    return [row[0] for row in rows]


def _check_zero_write_baseline() -> list[dict]:
    """E-1(2026-08-21)后:六个旧对象已删除,本护栏改为退役断言(防复活)。

    E-1 前为 BXP 型零写入护栏:相对切换前基线的任何写入(§3.5.2)视为违规。
    现在任何对象仍然存在即违规。
    """
    import scripts.phase_c_baseline as baseline_mod

    violations: list[dict] = []
    for obj in baseline_mod.RETIRING_OBJECTS:
        rows = execute("SELECT to_regclass(%s)", (obj,), fetch=True)
        if rows and rows[0][0] is not None:
            violations.append(
                {"object": obj, "error": "已退役对象仍存在(E-1 后应已删除)"}
            )
    return violations


def _write_phase_c_summary(summary: dict) -> Path:
    run_dir = _PHASE_C_SUMMARY_DIR / summary["run_id"]
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    sync = summary.get("sync", {})
    classification = summary.get("classification", {})
    reconciliation = summary.get("reconciliation", {})
    directory = sync.get("directory") or {"status": "not_run", "items": []}
    rejected = directory.get("rejected", [
        item for item in directory.get("items", []) if item.get("decision") == "rejected"])
    lines = [
        "# Phase C1 US financial run 摘要",
        "",
        f"- run_id: {summary['run_id']}",
        f"- 时间: {summary.get('started_at', 'not_run')} → "
        f"{summary.get('finished_at', 'not_run')}",
        f"- 状态: {summary.get('status', 'unknown')}",
        f"- 失败阶段: {summary.get('failure_stage', summary.get('stage', 'not_run'))}",
        "",
        "## 证券目录",
        "",
        f"- directory: {directory.get('status', 'not_run')} | "
        f"计划范围: {len(directory.get('planned_scope', sync.get('planned_scope', [])))} | "
        f"可采集范围: {len(directory.get('collectable_scope', sync.get('collectable_scope', [])))} | "
        f"实际新增: {directory.get('inserted', [])}",
        f"- 拒绝/未完成: {rejected}",
        f"- 目录证据: {directory.get('evidence_ref', 'not_run')}",
        "",
        "## sync 分类",
        "",
        f"- synced(success): {sync.get('success', 'not_run')} | no_new_filing(skip): "
        f"{sync.get('skipped', 'not_run')} | expected_skip: "
        f"{classification.get('expected_skip', 'not_run')} | "
        f"blocking_failure: {classification.get('blocking_failure', 'not_run')}",
        f"- 零写入(no_write): {sync.get('no_write', 'not_run')}",
        "",
        "## 范围对账",
        "",
        f"- universe: {reconciliation.get('universe_count', 'not_run')} | "
        f"index tickers: {reconciliation.get('index_ticker_count', 'not_run')} | "
        f"最终计划 scope: {reconciliation.get('scope_ticker_count', 'not_run')} | "
        f"out_of_sync_scope: {len(reconciliation.get('out_of_sync_scope', []))} | "
        f"index-only: {reconciliation.get('index_only_tickers', 'not_run')}",
        f"- universe_not_in_sync_scope: "
        f"{reconciliation.get('universe_not_in_sync_scope', 'not_run')}",
        f"- expected_skip_in_universe: "
        f"{reconciliation.get('expected_skip_in_universe', 'not_run')}",
        f"- supplement: {sync.get('supplement_tickers', 'not_run')}",
        f"- supplement_now_in_index: {sync.get('supplement_now_in_index', 'not_run')}",
        f"- index sources: {sync.get('index_sources', 'not_run')}",
        "",
        "## projection / compare",
        "",
        f"- projection: {summary.get('projection', 'not_run')}",
        f"- compare UNEXPLAINED: {summary.get('compare', {}).get('UNEXPLAINED', 'n/a')}",
        f"- 新 filing 滚动队列: {summary.get('compare', {}).get('rolling_queue', 'n/a')}",
        f"- 零写入护栏: {summary.get('zero_write', 'not_run')}",
        f"- 全部阻断原因: {summary.get('blocking_reasons', [])}",
    ]
    if summary.get("auto_registry"):
        auto = summary["auto_registry"]
        lines += [
            "",
            "## 自动登记（US_UNIVERSE_REGISTRY_AUTOMATION_PLAN）",
            "",
            f"- index-only 自动登记 {len(auto.get('index_only_registered', []))} 条: "
            f"{auto.get('index_only_registered', [])}",
            f"- index-only 未通过（维持阻塞/人工待办）"
            f"{len(auto.get('index_only_failed', []))} 条: "
            f"{auto.get('index_only_failed', [])}",
            f"- 未通过原因分类: {auto.get('index_only_failed_by_reason', {})}",
            f"- supplement 自动纳入 {len(auto.get('supplement_registered', []))} 条: "
            f"{auto.get('supplement_registered', [])}",
            f"- 目录阻断跳过: {auto.get('skipped_by_directory', [])}",
        ]
    (run_dir / "summary.md").write_text("\n".join(lines) + "\n")
    return run_dir


def _persist_phase_c_summary(summary: dict) -> None:
    _write_phase_c_summary(summary)
    summary["_summary_written"] = True


def _run_us_financial_orchestration(t0: float) -> dict:
    """统一保存后续阶段异常，且保留已完成的目录与财务结果。"""
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + f"_{time.time_ns()}"
    summary: dict = {
        "run_id": run_id,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "status": "started", "stage": "preflight",
        "sync": {"directory": {"status": "not_run", "items": []}},
        "classification": {}, "reconciliation": {},
        "projection": "not_run", "validate": "not_run",
    }
    try:
        return _run_us_financial_orchestration_stages(t0, summary)
    except Exception as exc:
        if not summary.get("_summary_written"):
            summary["status"] = "failed"
            summary["failure_stage"] = summary.get("stage", "unknown")
            summary.setdefault("blocking_reasons", []).append(
                f"{summary['failure_stage']}: {type(exc).__name__}: {exc}")
            summary["finished_at"] = datetime.now().isoformat(timespec="seconds")
            _persist_phase_c_summary(summary)
        raise


def _run_us_financial_orchestration_stages(t0: float, summary: dict) -> dict:
    """目录、财务、R2、projection 与校验的统一发布编排。"""
    from scripts.project_us_financial_snapshots import run_projection
    from core.us_security_identity import (
        resolve_us_symbols_batch, validate_us_security_symbols,
    )
    try:
        identity_conflicts = validate_us_security_symbols()
        if identity_conflicts:
            raise RuntimeError(f"US 身份表冲突,禁止同步: {identity_conflicts}")
        skips = _load_expected_skips()
        summary["stage"] = "sync"
        sync_result = _sync_us()
    except Exception as exc:
        reason = f"US 前置或同步执行失败: {type(exc).__name__}: {exc}"
        summary.update({
            "status": "blocked", "blocking_reasons": [reason],
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "zero_write": "not_run",
        })
        _persist_phase_c_summary(summary)
        raise RuntimeError(reason) from exc

    directory = sync_result.get("directory")
    if not isinstance(directory, dict) or directory.get("status") not in {
            "success", "partial", "failed", "not_run"}:
        directory = {
            "status": "failed", "complete": False,
            "planned_scope": sync_result.get("planned_scope", []),
            "collectable_scope": [], "blocked_tickers": [], "items": [],
            "fatal_error": "目录结果缺失或非法",
        }
        sync_result["directory"] = directory
    directory_status = directory["status"]
    summary["stage"] = "reconciliation"
    summary["sync"] = {
        k: (sorted(v) if isinstance(v, set) else v)
        for k, v in sync_result.items() if k != "index_tickers"
    }
    classification = _classify_us_sync_outcome(sync_result, skips)
    summary["classification"] = classification
    index_tickers = sync_result.get("index_tickers", set())
    raw_index_tickers = sync_result.get("raw_index_tickers", set())
    supplement_tickers = set(sync_result.get("supplement_tickers", []))
    scope_tickers = index_tickers | supplement_tickers
    resolved_map = sync_result.get("resolved_map") or resolve_us_symbols_batch(raw_index_tickers)
    reconciliation = _reconcile_us_universe(
        scope_tickers, index_tickers, classification["expected_skip"],
        raw_index_tickers=raw_index_tickers, resolved_map=resolved_map)
    summary["reconciliation"] = reconciliation
    logger.info(
        "Phase C1 分类: directory=%s synced=%d skip=%d expected_skip=%s blocking=%s",
        directory_status, sync_result["success"], sync_result["skipped"],
        classification["expected_skip"], classification["blocking_failure"],
    )

    summary["stage"] = "r2"
    unregistered_index_only = sorted(
        set(reconciliation["index_only_tickers"]) - _load_index_only_registry())
    summary["unregistered_index_only"] = unregistered_index_only
    uncovered_universe = sorted(
        set(reconciliation["universe_not_in_sync_scope"])
        - set(classification["expected_skip"]))
    summary["uncovered_universe"] = uncovered_universe

    blocked_by_directory = set(directory.get("blocked_tickers", []))
    if directory_status in ("failed", "not_run") and not directory.get("complete", False):
        skipped_by_directory = set(unregistered_index_only)
    else:
        skipped_by_directory = set(reconciliation["index_only_tickers"]) & blocked_by_directory
    rejected_items = directory.get("rejected", [
        item for item in directory.get("items", []) if item.get("decision") == "rejected"])
    reason_by_ticker = {}
    for item in rejected_items:
        for ticker in [item.get("canonical_code"), *item.get("raw_tickers", [])]:
            if ticker:
                reason_by_ticker[ticker] = item.get("reasons", [])
    skipped_detail = [
        {"stock_code": ticker,
         "reasons": reason_by_ticker.get(ticker, ["目录未完成，禁止 R2 自动登记"])}
        for ticker in sorted(skipped_by_directory)
    ]
    summary["auto_registry"] = {
        "index_only_registered": [], "index_only_failed": [],
        "index_only_failed_by_reason": {}, "supplement_registered": [],
        "skipped_by_directory": skipped_detail,
    }
    r2_index_candidates = sorted(set(unregistered_index_only) - skipped_by_directory)
    if r2_index_candidates or uncovered_universe:
        from core.us_universe_registry import auto_verify_and_register
        auto = auto_verify_and_register(
            r2_index_candidates, uncovered_universe,
            index_only_csv=_PHASE_C_INDEX_ONLY_CSV,
            supplement_csv=_PHASE_C_SUPPLEMENT_CSV)
        summary["auto_registry"].update({
            "index_only_registered": auto["index_only_registered"],
            "index_only_failed": auto["index_only_failed"],
            "index_only_failed_by_reason": auto.get("index_only_failed_by_reason", {}),
            "supplement_registered": auto["supplement_registered"],
        })
        unregistered_index_only = sorted(
            set(unregistered_index_only) - set(auto["index_only_registered"]))
        uncovered_universe = sorted(
            set(uncovered_universe) - set(auto["supplement_registered"]))
        summary["unregistered_index_only"] = unregistered_index_only
        summary["uncovered_universe"] = uncovered_universe

    summary["stage"] = "blocking"
    blocking_reasons = []
    if directory_status != "success":
        blocking_reasons.append(
            f"证券目录 {directory_status}: 拒绝/未完成 "
            f"{[item.get('canonical_code') for item in rejected_items] or directory.get('fatal_error')}")
    if classification["blocking_failure"]:
        blocking_reasons.append(f"ticker 级未登记失败: {classification['blocking_failure']}")
    if classification["index_errors"]:
        blocking_reasons.append(f"指数级失败: {classification['index_errors']}")
    if unregistered_index_only:
        blocking_reasons.append(f"未登记 index-only ticker: {unregistered_index_only}")
    if uncovered_universe:
        blocking_reasons.append(f"未分类 universe 股票: {uncovered_universe}")
    if blocking_reasons:
        summary["status"] = "blocked"
        summary["blocking_reasons"] = blocking_reasons
        summary["finished_at"] = datetime.now().isoformat(timespec="seconds")
        summary["zero_write"] = "not_run"
        summary["projection"] = "not_run"
        summary["validate"] = "not_run"
        _persist_phase_c_summary(summary)
        raise RuntimeError("US sync blocking: " + "; ".join(blocking_reasons))

    summary["stage"] = "zero_write"
    violations = _check_zero_write_baseline()
    summary["zero_write"] = "pass" if not violations else violations
    if violations:
        summary["status"] = "blocked_zero_write"
        summary["finished_at"] = datetime.now().isoformat(timespec="seconds")
        _persist_phase_c_summary(summary)
        raise RuntimeError(f"已退役对象仍存在: {[v['object'] for v in violations]}")

    summary["stage"] = "projection"
    projection_trigger = {
        "financial_writes": sync_result["success"] > 0 or bool(sync_result["no_write"]),
        "directory_inserted": list(directory.get("inserted", [])),
        "missing_current_ttm": [],
    }
    if sync_result["success"] == 0 and not sync_result["no_write"]:
        projection_trigger["missing_current_ttm"] = _missing_current_ttm_scope(scope_tickers)
    summary["projection_trigger"] = projection_trigger
    needs_projection = any(projection_trigger.values())
    if not needs_projection:
        summary["status"] = "no_new_filings"
        summary["projection"] = None
        summary["validate"] = "not_run"
        logger.info("全部 ticker 已同步跳过且快照完整: 不运行 projection(no_new_filings)")
    else:
        proj = run_projection(
            out_of_sync_scope=set(reconciliation["out_of_sync_scope"]))
        summary["projection"] = proj
        summary["status"] = "projected"

    if summary["projection"]:
        summary["stage"] = "validate"
        from core.validate import run_after_sync
        val = run_after_sync(market="US")
        summary["validate"] = val
        if not val.get("success"):
            summary["status"] = "validate_failed"
            summary["finished_at"] = datetime.now().isoformat(timespec="seconds")
            _persist_phase_c_summary(summary)
            raise RuntimeError(f"US validate 失败: {val.get('error')}")

    summary["stage"] = "finalizing"
    summary["finished_at"] = datetime.now().isoformat(timespec="seconds")
    if summary["status"] == "projected":
        summary["status"] = "success"
    _persist_phase_c_summary(summary)
    return {
        "success": sync_result["success"],
        "failed": sync_result["failed"],
        "skipped": sync_result["skipped"],
        "classification": classification,
        "projection": summary.get("projection"),
    }


# ── 关注股官方公告采集（默认关闭；方案 §3/§7.6）─────────────


def _announcements_job_minutes() -> int | None:
    """公告采集任务注册前的配置门。

    任何非法配置（间隔非正整数、enabled 非布尔）在此明确报错——
    任务注册前失败，不静默回退默认值。enabled=false 返回 None（不注册）。
    唤醒间隔取 SEC/IR/媒体配置间隔的最小值：到期筛选按各来源 next_due_at
    （core/announcements/sync.py），调度器必须按最短来源间隔唤醒，
    否则媒体 360 分钟间隔会被 SEC 60 分钟以外的更长值拖住。
    注意：注册表 interval_minutes 单源覆盖若短于配置默认，也只在
    唤醒粒度上生效（已知限制，不为此引入 DB 读取进注册路径）。
    """
    config.announcements.validate()
    if not config.announcements.validated_enabled():
        return None
    return min(config.announcements.validated_sec_interval(),
               config.announcements.validated_ir_interval(),
               config.announcements.validated_media_interval())


# ── 调度任务定义 ────────────────────────────────────────────

JOB_DEFS: dict[str, dict] = {
    # ── 行情同步 ──
    "CN_A_daily_quote": {
        "cron_key": "cn_a_daily_quote_cron",
        "market": "CN_A",
        "job_type": "daily_quote",
        "check_trading_day": _is_china_trading_day,
        "description": "A股行情同步",
    },
    "CN_HK_daily_quote": {
        "cron_key": "hk_daily_quote_cron",
        "market": "CN_HK",
        "job_type": "daily_quote",
        "check_trading_day": _is_china_trading_day,
        "description": "港股行情同步",
    },
    "US_daily_quote": {
        "cron_key": "us_daily_quote_cron",
        "market": "US",
        "job_type": "daily_quote_us",
        "check_trading_day": _is_us_trading_day,
        "description": "美股行情同步",
    },
    # ── 财务同步 ──
    "CN_A_financial": {
        "cron_key": "cn_a_cron",
        "market": "CN_A",
        "job_type": "financial",
        "check_trading_day": _is_china_trading_day,
        "description": "A股财务同步",
    },
    "CN_HK_financial": {
        "cron_key": "hk_cron",
        "market": "CN_HK",
        "job_type": "financial",
        "check_trading_day": _is_china_trading_day,
        "description": "港股财务同步",
    },
    "US_financial": {
        "cron_key": "us_cron",
        "market": "US",
        "job_type": "financial",
        "check_trading_day": _is_us_trading_day,
        "description": "美股财务同步",
    },
}


def _get_cron_parts(cron_expr: str) -> dict:
    """解析 cron 表达式为 APScheduler CronTrigger 参数。

    Args:
        cron_expr: 标准 5 段 cron（分 时 日 月 周）

    Returns:
        CronTrigger 关键字参数
    """
    parts = cron_expr.strip().split()
    if len(parts) != 5:
        raise ValueError(f"无效的 cron 表达式（需要 5 段）: {cron_expr!r}")

    return {
        "minute": parts[0],
        "hour": parts[1],
        "day": parts[2],
        "month": parts[3],
        # 配置使用标准 cron 星期编号（0/7=周日, 1=周一），而
        # APScheduler 使用 0=周一。转成星期名称可避免整体错后一天。
        "day_of_week": _standard_cron_dow_to_apscheduler(parts[4]),
    }


def _standard_cron_dow_to_apscheduler(expr: str) -> str:
    """把标准 cron 的星期字段转换为 APScheduler 可识别的表达式。"""
    names = {
        "0": "sun",
        "1": "mon",
        "2": "tue",
        "3": "wed",
        "4": "thu",
        "5": "fri",
        "6": "sat",
        "7": "sun",
    }

    def convert_atom(atom: str) -> str:
        if atom == "*":
            return atom
        base, separator, step = atom.partition("/")
        if "-" in base:
            start, end = base.split("-", 1)
            converted = f"{names.get(start, start)}-{names.get(end, end)}"
        else:
            converted = names.get(base, base)
        return f"{converted}/{step}" if separator else converted

    return ",".join(convert_atom(atom) for atom in expr.split(","))


def _make_job_wrapper(job_id: str):
    """创建带交易日检查的任务包装器。"""
    job_def = JOB_DEFS[job_id]
    market = job_def["market"]
    job_type = job_def["job_type"]

    def wrapper():
        # 检查是否为交易日
        if not job_def["check_trading_day"]():
            logger.info("[%s/%s] 今日非交易日，跳过同步", market, job_type)
            return
        _run_sync_job(market, job_type=job_type)

    wrapper.__name__ = f"sync_{job_id.lower()}"
    return wrapper


# ── Dry Run 预览 ────────────────────────────────────────────


def _filter_job_defs() -> dict[str, dict]:
    """根据 config.scheduler.markets 过滤 JOB_DEFS，只保留允许的市场任务。

    如果 markets 为空，输出警告并返回空字典。
    """
    markets = config.scheduler.markets
    if not markets:
        return {}
    return {
        job_id: job_def
        for job_id, job_def in JOB_DEFS.items()
        if job_def["market"] in markets
    }


def dry_run() -> None:
    """预览调度计划，不实际执行。"""
    active_jobs = _filter_job_defs()

    print("=" * 70)
    print("  Stock Data Scheduler — 调度计划预览（--dry-run）")
    print("=" * 70)
    print(
        f"\n  STOCK_MARKETS = {','.join(config.scheduler.markets) if config.scheduler.markets else '（未配置）'}"
    )

    # ── 行情同步 ──
    print(f"\n  {'─' * 66}")
    print(f"  行情同步（daily_quote）")
    print(f"  {'─' * 66}")
    print(f"  {'任务 ID':<24} {'cron 表达式':<25} {'说明':<16}")
    print(f"  {'─' * 24} {'─' * 25} {'─' * 16}")

    if config.scheduler.daily_quote_enabled:
        for job_id, job_def in active_jobs.items():
            if job_def["job_type"] in ("daily_quote", "daily_quote_us"):
                cron_expr = getattr(config.scheduler, job_def["cron_key"])
                print(f"  {job_id:<24} {cron_expr:<25} {job_def['description']}")
        if not any(
            jd["job_type"] in ("daily_quote", "daily_quote_us")
            for jd in active_jobs.values()
        ):
            print(f"  （无匹配的行情同步任务）")
    else:
        print(f"  （行情同步已禁用: daily_quote_enabled=false）")

    # ── 财务同步 ──
    print(f"\n  {'─' * 66}")
    print(f"  财务同步（financial）")
    print(f"  {'─' * 66}")
    print(f"  {'任务 ID':<24} {'cron 表达式':<25} {'说明':<16}")
    print(f"  {'─' * 24} {'─' * 25} {'─' * 16}")

    for job_id, job_def in active_jobs.items():
        if job_def["job_type"] == "financial":
            cron_expr = getattr(config.scheduler, job_def["cron_key"])
            print(f"  {job_id:<24} {cron_expr:<25} {job_def['description']}")
    if not any(jd["job_type"] == "financial" for jd in active_jobs.values()):
        print(f"  （无匹配的财务同步任务）")

    # ── 港股 capex NULL 自动兜底 ──
    print(f"\n  {'─' * 66}")
    print(f"  港股 capex NULL 自动兜底")
    print(f"  {'─' * 66}")
    if "CN_HK" in config.scheduler.markets and config.scheduler.hk_capex_fallback_auto:
        print(f"  同步后 T1 腾讯即时兜底 : 开启")
        print(
            f"  T2 披露易 PDF 夜间排空 : cron={config.scheduler.hk_capex_pdf_cron}"
            f"（单次上限 {config.scheduler.hk_capex_pdf_nightly_limit} 只）"
        )
    else:
        print(f"  （未开启：市场不含 CN_HK 或 hk_capex_fallback_auto=false）")

    # ── 关注股官方公告采集 ──
    print(f"\n  {'─' * 66}")
    print(f"  关注股官方公告采集（watchlist_announcements）")
    print(f"  {'─' * 66}")
    if "US" not in config.scheduler.markets:
        print(f"  （未开启：市场不含 US）")
    else:
        try:
            minutes = _announcements_job_minutes()
        except ValueError as exc:
            print(f"  （配置非法，注册前报错: {exc}）")
        else:
            if minutes is None:
                print(f"  （未开启：STOCK_ANNOUNCEMENTS_ENABLED=false）")
            else:
                print(f"  SEC 检查间隔: 每 {minutes} 分钟（IR 来源后续片接入）")

    # ── 配置概要 ──
    print(f"\n  {'─' * 66}")
    print(f"  配置概要")
    print(f"  {'─' * 66}")
    print(
        f"  活跃市场     : {', '.join(config.scheduler.markets) if config.scheduler.markets else '（未配置）'}"
    )
    print(
        f"  行情同步开关 : {'开启' if config.scheduler.daily_quote_enabled else '关闭'}"
    )
    print(
        f"  重试次数     : {config.scheduler.max_retries}（间隔递增，基数 {config.scheduler.retry_base_delay}s）"
    )
    print(f"  并发线程     : {config.scheduler.sync_workers}")
    print(f"  强制全量     : {'是' if config.scheduler.force_sync else '否'}")
    print(f"  通知 URL     : {config.scheduler.notify_url or '（未配置，仅日志）'}")
    print()
    active_markets = set(config.scheduler.markets)
    print("  物化视图刷新策略:")
    if active_markets - {"US"}:
        print("    CN_A/CN_HK 行情后: mv_fcf_yield")
        print("    CN_A/CN_HK 财务后: mv_financial_indicator → mv_indicator_ttm → mv_fcf_yield")
    if "US" in active_markets:
        print("    US: 不刷新已退役的旧 MV；版本层 sync → projection → compare → validate")
    print()
    print("  注: cron 触发时还会二次检查是否为交易日，非交易日自动跳过")
    print("=" * 70)


# ── 主调度器 ────────────────────────────────────────────────


def run_scheduler(once: bool = False) -> None:
    """启动 APScheduler 调度器。

    Args:
        once: 如果为 True，立即执行一次所有任务后退出。
    """
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    # ── 检查 STOCK_MARKETS 配置 ──
    markets = config.scheduler.markets
    if not markets:
        logger.warning(
            "STOCK_MARKETS 环境变量未配置，没有任务可注册。"
            "请在 .env 中设置，例如: STOCK_MARKETS=CN_A,CN_HK 或 STOCK_MARKETS=US"
        )
        sys.exit(1)

    if not health_check():
        logger.error("数据库连接失败，调度器无法启动")
        sys.exit(1)

    active_jobs = _filter_job_defs()
    logger.info("活跃市场: %s → %d 个任务", ", ".join(markets), len(active_jobs))

    sched = BlockingScheduler(timezone="Asia/Shanghai")
    logger.info("调度器启动，时区: Asia/Shanghai")

    # ── 注册任务 ──
    for job_id, job_def in active_jobs.items():
        # 跳过禁用的行情同步
        if (
            job_def["job_type"] == "daily_quote"
            and not config.scheduler.daily_quote_enabled
        ):
            logger.info("行情同步已禁用，跳过注册: %s", job_id)
            continue

        cron_expr = getattr(config.scheduler, job_def["cron_key"])
        wrapper = _make_job_wrapper(job_id)
        cron_kwargs = _get_cron_parts(cron_expr)
        trigger = CronTrigger(**cron_kwargs, timezone="Asia/Shanghai")

        sched.add_job(
            wrapper,
            trigger=trigger,
            id=f"sync_{job_id.lower()}",
            name=job_def["description"],
            replace_existing=True,
        )
        logger.info(
            "注册任务: %s → cron=%s (%s, %s)",
            job_id,
            cron_expr,
            job_def["description"],
            job_def["job_type"],
        )

    # ── 港股 capex PDF 队列夜间排空（独立于同步任务，非交易日也跑）──
    if "CN_HK" in markets and config.scheduler.hk_capex_fallback_auto:
        cron_expr = config.scheduler.hk_capex_pdf_cron
        trigger = CronTrigger(
            **_get_cron_parts(cron_expr), timezone="Asia/Shanghai"
        )
        sched.add_job(
            _run_hk_pdf_queue_job,
            trigger=trigger,
            id="hk_capex_pdf_drain",
            name="港股capex PDF队列排空",
            replace_existing=True,
        )
        logger.info("注册任务: HK_capex_pdf → cron=%s", cron_expr)

    # ── 关注股官方公告采集（默认关闭；US 市场）──
    # coalesce=True：服务重启/积压只合并为一次到期检查，不补发周期触发；
    # max_instances=1 只是进程内额外保护，跨进程互斥由
    # core/announcements/sync.py 的 advisory lock 承担。
    # schedule_anchor 与 IntervalTrigger.start_date 用同一时刻：整轮按此
    # 相位计算计划槽位与 next_due，保证下次触发时来源必然已到期。
    if "US" in markets:
        minutes = _announcements_job_minutes()
        if minutes is not None:
            from apscheduler.triggers.interval import IntervalTrigger
            from core.announcements.sync import run_round as _announcements_round

            anchor = datetime.now(timezone.utc)
            trigger = IntervalTrigger(minutes=minutes, start_date=anchor)
            sched.add_job(
                _announcements_round,
                trigger=trigger,
                kwargs={"trigger": "scheduled",
                        "schedule_anchor": trigger.start_date},
                id="watchlist_announcements",
                name="关注股官方公告采集",
                replace_existing=True,
                coalesce=True,
                max_instances=1,
                misfire_grace_time=300,
            )
            logger.info("注册任务: watchlist_announcements → 每 %d 分钟（锚点 %s）",
                        minutes, anchor.isoformat(timespec="seconds"))
        else:
            logger.info("公告采集已禁用（STOCK_ANNOUNCEMENTS_ENABLED=false），不注册任务")

    if once:
        # 立即执行一次，按正确顺序：
        # 先执行行情同步，再执行财务同步
        logger.info("--once 模式：立即执行所有任务...")
        for job_id, job_def in active_jobs.items():
            if (
                job_def["job_type"] == "daily_quote"
                and not config.scheduler.daily_quote_enabled
            ):
                logger.info("行情同步已禁用，跳过: %s", job_id)
                continue

            market = job_def["market"]
            job_type = job_def["job_type"]

            if not job_def["check_trading_day"]():
                logger.info("[%s/%s] 非交易日，跳过", market, job_type)
                continue

            logger.info("执行 %s (%s)...", job_id, job_def["description"])
            result = _run_sync_job(market, job_type=job_type)
            if result["success"]:
                logger.info("%s 同步成功", job_id)
            else:
                logger.error("%s 同步失败: %s", job_id, result.get("error"))

        logger.info("一次性执行完成")
        close_pool()
        return

    # ── 打印下次执行时间 ──
    print("\n调度器已启动，等待下次触发...")
    for job in sched.get_jobs():
        try:
            next_run = job.next_run_time
            if next_run:
                print(
                    f"  {job.name:20s} 下次执行: {next_run.strftime('%Y-%m-%d %H:%M:%S %Z')}"
                )
        except (AttributeError, TypeError):
            print(f"  {job.name:20s} 下次执行: 计算中...")
    print("按 Ctrl+C 退出\n")

    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("调度器收到退出信号，正在关闭...")
        sched.shutdown(wait=False)
        close_pool()
        logger.info("调度器已停止")


# ── CLI ──────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="股票数据定时同步调度器")
    parser.add_argument(
        "--dry-run", action="store_true", help="预览调度计划，不实际执行"
    )
    parser.add_argument(
        "--once", action="store_true", help="立即执行一次所有市场同步后退出"
    )

    args = parser.parse_args()

    if args.dry_run:
        dry_run()
        return

    run_scheduler(once=args.once)


if __name__ == "__main__":
    main()
