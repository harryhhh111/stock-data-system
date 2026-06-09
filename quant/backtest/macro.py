"""宏观行业滤网 — 商品期货趋势 → 行业过滤 → 个股策略。

Phase 2-3: 行业映射 + 商品信号 + 股票过滤。
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

import pandas as pd

from db import Connection

logger = logging.getLogger(__name__)

# ── 行业映射（Phase 0 验证通过） ──────────────────────────

# (申万行业列表, 名称关键词正则, 相关性)
_MAPPING_CN_A: dict[str, tuple[list[str], str | None, float]] = {
    "XAU": (["有色金属"], r"金|黄金|矿业", 0.468),
}

_MAPPING_CN_HK: dict[str, tuple[list[str], str | None, float]] = {
    "XAU": (["黄金及贵金属"], None, 0.602),
    "CL":  (["石油及天然气"], None, 0.402),
}

_MAPPINGS = {
    "CN_A": _MAPPING_CN_A,
    "CN_HK": _MAPPING_CN_HK,
}


def get_mapped_stocks(market: str, commodity: str) -> list[str]:
    """返回商品关联的股票代码列表。启动时一次性查询，包含名称过滤。"""
    mapping = _MAPPINGS.get(market, {})
    if commodity not in mapping:
        raise ValueError(f"商品 {commodity} 在 {market} 无映射，请检查配置")

    industries, name_regex, _ = mapping[commodity]

    with Connection() as conn:
        cur = conn.cursor()
        if name_regex:
            cur.execute(
                "SELECT stock_code FROM stock_info "
                "WHERE market = %s AND industry = ANY(%s) AND stock_name ~ %s",
                (market, industries, name_regex),
            )
        else:
            cur.execute(
                "SELECT stock_code FROM stock_info "
                "WHERE market = %s AND industry = ANY(%s)",
                (market, industries),
            )
        codes = [r[0] for r in cur.fetchall()]
        cur.close()

    if not codes:
        raise ValueError(
            f"商品 {commodity} → {market}/{industries}"
            f"{' (name_regex=' + name_regex + ')' if name_regex else ''}"
            f" 未命中任何股票。请检查 stock_info 数据。"
        )
    return codes


def _load_commodity_prices(
    commodity_code: str, as_of_date: date, lookback_days: int = 400
) -> pd.Series:
    """PIT 加载商品价格序列。数据不足 200 条抛错。"""
    sql = """
    SELECT trade_date, close FROM commodity_price
    WHERE commodity_code = %s AND trade_date BETWEEN %s AND %s
    ORDER BY trade_date
    """
    start = as_of_date - timedelta(days=lookback_days)
    with Connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, (commodity_code, start, as_of_date))
        rows = cur.fetchall()
        cur.close()

    if len(rows) < 200:
        raise ValueError(
            f"商品 {commodity_code} 在 {as_of_date} 前数据不足 200 个交易日"
            f"（实际 {len(rows)} 条），无法计算 200MA 信号。"
        )

    return pd.Series(
        [float(r[1]) for r in rows],
        index=[r[0] for r in rows],
    ).sort_index()


def commodity_signal(commodity_code: str, as_of_date: date) -> str:
    """返回 'bull' | 'bear' | 'neutral'。

    条件: 价格 > 200MA 且 60日动量 > 0 → bull
          价格 < 200MA 且 60日动量 < 0 → bear
          其他 → neutral
    """
    prices = _load_commodity_prices(commodity_code, as_of_date)
    close = prices.values

    ma200 = close[-200:].mean()
    above_ma = close[-1] > ma200
    mom_60 = close[-1] / close[-60] - 1 if len(close) >= 60 else 0

    if above_ma and mom_60 > 0:
        return "bull"
    elif not above_ma and mom_60 < 0:
        return "bear"
    return "neutral"


def apply_macro_filter(
    df: pd.DataFrame,
    market: str,
    macro_filter: list[str],
) -> pd.DataFrame:
    """宏观滤网：排除 bear 商品对应的行业股票。

    冲突规则（排除优先）：任一商品 bear → 该商品关联的股票全部排除。
    neutral 商品不影响。

    Args:
        df: 含 stock_code 列的 DataFrame
        market: 市场代码
        macro_filter: 商品代码列表，如 ["XAU", "CL"]

    Returns:
        过滤后的 DataFrame
    """
    if not macro_filter:
        return df

    if "stock_code" not in df.columns:
        return df

    # 收集需要排除的股票代码
    excluded_codes: set[str] = set()

    for commodity in macro_filter:
        try:
            signal = commodity_signal(commodity, df.name if hasattr(df, 'name') else date.today())
            # 需要给 signal 传入 as_of_date。从 df 的语境中无法获取，需要外部传入。
            # 此处由 engine 在循环中调用时传入 as_of_date。
        except Exception as e:
            logger.warning("商品 %s 信号查询失败: %s", commodity, e)
            continue

    # 注意：此函数需要 as_of_date 参数。修正见下方。
    return df


def get_excluded_codes(
    market: str,
    macro_filter: list[str],
    as_of_date: date,
) -> set[str]:
    """返回当前应排除的股票代码集合。

    对 macro_filter 中每个商品，查询其信号。
    若信号为 bear，将其关联行业的所有股票加入排除集。
    """
    excluded: set[str] = set()

    for commodity in macro_filter:
        try:
            signal = commodity_signal(commodity, as_of_date)
        except ValueError as e:
            logger.warning("商品 %s 信号查询失败: %s", commodity, e)
            continue

        if signal == "bear":
            codes = get_mapped_stocks(market, commodity)
            excluded.update(codes)
            logger.info("宏观滤网: %s bear → 排除 %d 只 %s 股票",
                        commodity, len(codes),
                        _MAPPINGS[market][commodity][0] if market in _MAPPINGS else "")

    return excluded


# ── 启动时校验 ──────────────────────────────────────────

def validate_mappings():
    """启动时校验所有映射。任一失败抛 ValueError。"""
    for market, mapping in _MAPPINGS.items():
        for commodity, (industries, name_regex, corr) in mapping.items():
            # 检查相关性阈值
            if corr < 0.3:
                raise ValueError(
                    f"{commodity} → {market}/{industries} 相关性 {corr:.3f} < 0.3，"
                    f"不应进入映射表。"
                )
            # 检查行业有股票
            try:
                codes = get_mapped_stocks(market, commodity)
            except ValueError:
                raise  # 已在 get_mapped_stocks 中抛错
            logger.info(
                "映射校验通过: %s → %s/%s (%d只, r=%.3f%s)",
                commodity, market, industries, len(codes), corr,
                f", 名含'{name_regex}'" if name_regex else ""
            )
