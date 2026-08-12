"""core/us_security_identity.py — US 交易代码 ↔ canonical security 身份解析。

规格:docs/core/US_UNIVERSE_SECURITY_IDENTITY_TASK.md §3.2

三个概念严格分开:
- 交易 ticker(会更名:BK→BNY);
- 财务 CIK(发行人身份,SEC CompanyFacts 按它抓取);
- 项目 canonical stock_code(历史主键,不批量 rename)。

本模块只做交易代码与 canonical 的映射解析与冲突校验;SEC 同步按 CIK,
不经过本表(见 §3.1 CIK 优先 fetch)。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from db import execute

logger = logging.getLogger(__name__)

# 活跃 US universe 谓词(§3.4):当前证券范围的唯一口径,退市股不出现在
# scheduler scope/行情/dashboard/筛选器/中位数/校验/projection;
# PIT 回测另有 as-of 过滤(delist_date > as_of_date),不用本谓词。
ACTIVE_US_CONDITION = "(delist_date IS NULL OR delist_date > CURRENT_DATE)"


@dataclass(frozen=True)
class SymbolRow:
    ticker: str
    canonical_stock_code: str
    cik: str
    symbol_role: str  # 'current' | 'legacy'


def load_symbols() -> list[SymbolRow]:
    """读取全部 US 身份映射行。"""
    rows = execute(
        "SELECT ticker, canonical_stock_code, cik, symbol_role "
        "FROM us_security_ticker_symbol WHERE market = 'US'",
        fetch=True,
    ) or []
    return [
        SymbolRow(str(t).upper(), str(c).upper(), str(cik).zfill(10), role)
        for t, c, cik, role in rows
    ]


def validate_us_security_symbols(symbols: list[SymbolRow] | None = None) -> list[str]:
    """启动期身份冲突校验。返回冲突列表(空 = 通过)。

    规则(§3.2 启动期校验 + §3.3 CWEN 冲突门):
    1. canonical 必须是存在的 US stock_info 行;
    2. 同一 ticker 不得映射两个 canonical;
    3. 同一 canonical 不得有两个 current ticker;
    4. current ticker 不得同时是另一个 canonical 的 active stock_info.stock_code
       (CWEN-A/CWEN 型冲突在此被拦下);
    5. 身份行 CIK 必须与 canonical 在 stock_info 中的 CIK 一致;
    6. legacy ticker 可指向自身(更名)或经证据门批准的另一 canonical(换码合并,
       如 CWEN-A→CWEN);指向自身是最常见形态。
    """
    if symbols is None:
        symbols = load_symbols()
    conflicts: list[str] = []

    stock_rows = execute(
        "SELECT stock_code, cik FROM stock_info WHERE market = 'US'", fetch=True,
    ) or []
    stock_cik = {str(c).upper(): (str(k).zfill(10) if k else None) for c, k in stock_rows}

    by_ticker: dict[str, set[str]] = {}
    current_by_canonical: dict[str, list[str]] = {}
    for s in symbols:
        by_ticker.setdefault(s.ticker, set()).add(s.canonical_stock_code)
        if s.symbol_role == "current":
            current_by_canonical.setdefault(s.canonical_stock_code, []).append(s.ticker)

    for s in symbols:
        if s.canonical_stock_code not in stock_cik:
            conflicts.append(f"{s.ticker}: canonical {s.canonical_stock_code} 不在 US stock_info")
            continue
        info_cik = stock_cik[s.canonical_stock_code]
        if info_cik and info_cik != s.cik:
            conflicts.append(
                f"{s.ticker}: 身份行 CIK {s.cik} 与 stock_info.cik {info_cik} 不一致")
        if s.symbol_role == "current" and s.ticker in stock_cik and s.ticker != s.canonical_stock_code:
            conflicts.append(
                f"{s.ticker}: current ticker 同时是另一 active stock_info code"
                f"(canonical {s.canonical_stock_code})")

    for ticker, canonicals in by_ticker.items():
        if len(canonicals) > 1:
            conflicts.append(f"{ticker}: 映射到多个 canonical {sorted(canonicals)}")
    for canonical, currents in current_by_canonical.items():
        if len(currents) > 1:
            conflicts.append(f"{canonical}: 存在多个 current ticker {sorted(currents)}")

    return conflicts


def resolve_us_symbol(ticker: str, symbols: list[SymbolRow] | None = None) -> str:
    """交易代码 → canonical stock_code。

    旧 canonical code 直接命中 stock_info;新 ticker(BNY/ECHO/PPLI/P)经身份表
    解析回 canonical;两者都不命中时原样返回(由调用方决定如何处理)。
    """
    code = ticker.strip().upper()
    rows = execute(
        "SELECT 1 FROM stock_info WHERE stock_code = %s AND market = 'US' LIMIT 1",
        (code,), fetch=True,
    )
    if rows:
        return code
    if symbols is None:
        symbols = load_symbols()
    for s in symbols:
        if s.ticker == code and s.symbol_role == "current":
            return s.canonical_stock_code
    return code


def current_ticker_for(canonical: str, symbols: list[SymbolRow] | None = None) -> str:
    """canonical → 当前交易 ticker(行情出站请求用);无映射时返回自身。"""
    code = canonical.strip().upper()
    if symbols is None:
        symbols = load_symbols()
    for s in symbols:
        if s.canonical_stock_code == code and s.symbol_role == "current":
            return s.ticker
    return code


def resolve_us_cik(stock_code: str) -> Optional[str]:
    """canonical → stock_info.cik(十位)。本地 CIK 优先于 SEC ticker 映射(§3.1)。"""
    rows = execute(
        "SELECT cik FROM stock_info WHERE stock_code = %s AND market = 'US' LIMIT 1",
        (stock_code.strip().upper(),), fetch=True,
    )
    if rows and rows[0][0]:
        return str(rows[0][0]).strip().zfill(10)
    return None


def resolve_us_symbols_batch(
    tickers: set[str] | list[str],
    symbols: list[SymbolRow] | None = None,
) -> dict[str, str]:
    """批量 ticker → canonical(stock_info 命中优先,否则身份表 current 映射)。"""
    codes = sorted({str(t).strip().upper() for t in tickers})
    if not codes:
        return {}
    rows = execute(
        "SELECT stock_code FROM stock_info WHERE market = 'US' AND stock_code = ANY(%s)",
        (codes,), fetch=True,
    ) or []
    existing = {str(r[0]).upper() for r in rows}
    if symbols is None:
        symbols = load_symbols()
    current_map = {s.ticker: s.canonical_stock_code for s in symbols if s.symbol_role == "current"}
    return {t: (t if t in existing else current_map.get(t, t)) for t in codes}
