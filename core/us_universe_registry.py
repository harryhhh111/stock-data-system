"""core/us_universe_registry.py — US universe 登记/分类自动化。

方案：docs/core/US_UNIVERSE_REGISTRY_AUTOMATION_PLAN.md（2026-10-22 审核通过）。

把「人工逐条起草 CSV 台账」变为「自动验证起草 + 规则放行 + 到期复核」：
- index-only ticker：身份表解析出有效 CIK 无冲突 + companyfacts 200
  （经 core/sec_client.py 公共客户端，占 SEC gate 预算）→ 自动登记；
  任一不满足保持阻塞，列入人工待办。XE/XP 类解析垃圾天然过不了这两条。
- 未分类 universe 股票：「在 active US universe 且不在指数 canonical 集合」
  这一事实由对账已确认（身份本就在库内，无需再查 SEC）→ 自动纳入补充清单。
- 自动条目 registered_by=auto，evidence 写明依据与日期，review_by = 登记日
  +90 天（与人工条目惯例一致）；到期复核对 auto/manual 一视同仁。
- SEC 验证请求每轮上限 50，防异常解析放大请求量；companyfacts 结果本轮
  有效，不做长期缓存（主体状态由复核机制兜底）。
- 只写 CSV 文件，不自动 git commit。
"""
from __future__ import annotations

import csv
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

import config
from core import sec_client
from db import execute

logger = logging.getLogger(__name__)

AUTO_VERIFY_MAX_REQUESTS = 50   # 每轮 SEC 验证请求上限
REVIEW_DAYS = 90                # 登记日起约 90 天复核（现有人工条目惯例）

INDEX_ONLY_HEADER = ["stock_code", "evidence_ref", "first_confirmed",
                     "review_by", "registered_by"]
SUPPLEMENT_HEADER = INDEX_ONLY_HEADER


# ── CSV 读写（向后兼容：无 registered_by 列的旧格式可读）────

def _read_rows(path: Path) -> tuple[list[str], list[dict]]:
    if not path.exists():
        return INDEX_ONLY_HEADER, []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        return list(reader.fieldnames or []), list(reader)


def _ensure_registered_by_column(path: Path) -> None:
    """升级表头并回填存量条目 registered_by=manual（幂等）。"""
    header, rows = _read_rows(path)
    if not header:
        return
    if "registered_by" in header:
        return
    new_header = header + ["registered_by"]
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=new_header, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            row["registered_by"] = "manual"
            writer.writerow(row)
    logger.info("%s 升级 registered_by 列，回填 %d 条存量条目为 manual",
                path, len(rows))


def _append_row(path: Path, row: dict) -> None:
    _ensure_registered_by_column(path)
    if not path.exists():
        with open(path, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=INDEX_ONLY_HEADER, lineterminator="\n").writeheader()
    existing = {r["stock_code"].strip().upper() for r in _read_rows(path)[1]}
    if row["stock_code"].strip().upper() in existing:
        return  # 幂等：已登记不重复追加
    with open(path, "a", newline="") as f:
        csv.DictWriter(f, fieldnames=INDEX_ONLY_HEADER, lineterminator="\n").writerow(row)


# ── 自动验证 ───────────────────────────────────────────────

def _load_identity():
    from core.us_security_identity import load_symbols
    symbols = load_symbols()
    stock_cik = {str(r[0]).upper(): (str(r[1]).zfill(10) if r[1] else None)
                 for r in execute(
                     "SELECT stock_code, cik FROM stock_info WHERE market='US'",
                     fetch=True) or []}
    return symbols, stock_cik


def _resolve_cik(ticker: str, symbols, stock_cik) -> tuple[str | None, str | None]:
    """身份表解析 CIK 且无冲突。返回 (cik, None) 或 (None, 原因)。"""
    matches = [s for s in symbols if s.ticker == ticker]
    if not matches:
        return None, "身份表无映射（疑似解析垃圾或未登记主体）"
    canonicals = {s.canonical_stock_code for s in matches}
    if len(canonicals) > 1:
        return None, f"身份冲突: ticker 映射多个 canonical {sorted(canonicals)}"
    canonical = canonicals.pop()
    currents = [s for s in matches if s.symbol_role == "current"]
    if len(currents) > 1:
        return None, f"身份冲突: canonical {canonical} 多个 current ticker"
    cik = (currents[0] if currents else matches[0]).cik
    info_cik = stock_cik.get(canonical)
    if info_cik and info_cik != cik:
        return None, f"身份行 CIK {cik} 与 stock_info.cik {info_cik} 不一致"
    return cik, None


def _companyfacts_status(cik: str, ticker: str) -> tuple[int | None, str | None]:
    """经公共 SEC 客户端探测 companyfacts。返回 (status, None) 或
    (None, 错误类别)——协调/传输失败不等于主体不存在，按未通过处理。"""
    url = config.sec.base_url.format(cik=cik)
    try:
        response = sec_client.get(
            url, task="us_universe_registry", identity=ticker,
            allowed_statuses=(404,))
        return response.status_code, None
    except sec_client.SECError as exc:
        return None, f"{type(exc).__name__}: {exc}"


def auto_register_index_only(tickers: list[str], *, csv_path: Path,
                             today: date | None = None,
                             max_requests: int = AUTO_VERIFY_MAX_REQUESTS,
                             ) -> dict:
    """对未登记 index-only ticker 跑自动验证并写入台账。

    返回 {"registered": [...], "failed": [(ticker, 原因), ...]}。
    验证请求每轮上限 max_requests，超限项保持阻塞到下轮。
    """
    today = today or datetime.now().date()
    tickers = sorted({t.strip().upper() for t in tickers})
    if not tickers:
        return {"registered": [], "failed": []}
    symbols, stock_cik = _load_identity()
    registered: list[str] = []
    failed: list[tuple[str, str]] = []
    requests_used = 0
    for ticker in tickers:
        if requests_used >= max_requests:
            failed.append((ticker, f"本轮验证请求上限 {max_requests} 已耗尽，下轮继续"))
            continue
        cik, reason = _resolve_cik(ticker, symbols, stock_cik)
        if cik is None:
            failed.append((ticker, reason))
            continue
        requests_used += 1
        status, error = _companyfacts_status(cik, ticker)
        if error is not None:
            failed.append((ticker, f"companyfacts 探测失败（{error}）"))
            continue
        if status != 200:
            failed.append((ticker, f"companyfacts HTTP {status}（非真实申报主体）"))
            continue
        _append_row(csv_path, {
            "stock_code": ticker,
            "evidence_ref": (
                f"自动登记({today}): 身份表解析 CIK{cik} 无冲突; "
                f"companyfacts HTTP 200 确认真实申报主体; "
                f"指数成分但不在 stock_info US universe, 纳入 universe 或"
                f"移出指数范围前不阻断"),
            "first_confirmed": today.isoformat(),
            "review_by": (today + timedelta(days=REVIEW_DAYS)).isoformat(),
            "registered_by": "auto",
        })
        registered.append(ticker)
        logger.info("index-only 自动登记: %s (CIK%s)", ticker, cik)
    return {"registered": registered, "failed": failed}


def auto_register_supplement(tickers: list[str], *, csv_path: Path,
                             today: date | None = None) -> list[str]:
    """未分类 universe 股票自动纳入补充清单。

    只需确认「在 active US universe 且不在指数 canonical 集合」——该事实
    由调用方对账得出（universe_not_in_sync_scope），身份本就在库内，
    无需再查 SEC。
    """
    today = today or datetime.now().date()
    registered = []
    for ticker in sorted({t.strip().upper() for t in tickers}):
        _append_row(csv_path, {
            "stock_code": ticker,
            "evidence_ref": (
                f"自动登记({today}): 对账确认在 active US universe "
                f"(stock_info market=US) 且不在指数 canonical 集合; "
                f"纳入自动 SEC 同步"),
            "first_confirmed": today.isoformat(),
            "review_by": (today + timedelta(days=REVIEW_DAYS)).isoformat(),
            "registered_by": "auto",
        })
        registered.append(ticker)
        logger.info("supplement 自动登记: %s", ticker)
    return registered


def auto_verify_and_register(index_only: list[str], uncovered: list[str], *,
                             index_only_csv: Path, supplement_csv: Path,
                             today: date | None = None) -> dict:
    """对账之后、阻断判定之前的自动验证入口（scheduler 调用）。"""
    today = today or datetime.now().date()
    io_result = auto_register_index_only(
        index_only, csv_path=index_only_csv, today=today)
    supplement_registered = auto_register_supplement(
        uncovered, csv_path=supplement_csv, today=today)
    return {
        "index_only_registered": io_result["registered"],
        "index_only_failed": io_result["failed"],
        "supplement_registered": supplement_registered,
    }
