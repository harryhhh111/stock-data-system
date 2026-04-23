"""
选股筛选器 — 结果格式化输出
"""

import json
import pandas as pd

from quant.screener.presets import OUTPUT_COLUMNS, FACTOR_LABELS


try:
    from tabulate import tabulate
    HAS_TABULATE = True
except ImportError:
    HAS_TABULATE = False


def _format_value(val, fmt: str) -> str:
    """格式化单个值"""
    if pd.isna(val):
        return "-"

    if fmt == "str":
        return str(val)
    elif fmt == "currency_billion":
        return f"{val / 1e8:.1f}"  # 元 → 亿
    elif fmt == "float_1":
        return f"{val:.1f}"
    elif fmt == "float_2":
        return f"{val:.2f}"
    elif fmt == "pct_1":
        return f"{val * 100:.1f}%"
    elif fmt == "pct_2":
        return f"{val * 100:.2f}%"
    else:
        return str(val)


def format_results(df: pd.DataFrame, top_n: int = 30, fmt: str = "table") -> str:
    """
    格式化选股结果为指定格式

    Args:
        df: 已打分排序的 DataFrame
        top_n: 输出前 N 条
        fmt: 'table' | 'csv' | 'json'

    Returns:
        格式化后的字符串
    """
    if df.empty:
        return "无符合条件的股票"

    # 取 Top N
    display = df.nsmallest(top_n, "score_rank").copy()

    if fmt == "csv":
        return display.to_csv(index=False, encoding="utf-8")

    if fmt == "json":
        return display.to_json(orient="records", force_ascii=False, indent=2)

    # table 格式（默认）
    return _format_table(display)


def _format_table(df: pd.DataFrame) -> str:
    """格式化为终端表格"""
    # 准备显示用的 DataFrame
    rows = []
    for _, row in df.iterrows():
        r = {}
        for col, label, fmt in OUTPUT_COLUMNS:
            if col in df.columns:
                r[label] = _format_value(row.get(col), fmt)
            else:
                r[label] = "-"
        rows.append(r)

    display_df = pd.DataFrame(rows)

    if HAS_TABULATE:
        return tabulate(display_df, headers="keys", tablefmt="simple", showindex=False)
    else:
        return display_df.to_string(index=False)


def format_summary(n_before: int, n_after_filter: int, n_after_score: int, preset_name: str, market: str) -> str:
    """格式化运行摘要"""
    lines = [
        "=" * 60,
        f"  选股筛选器 — {preset_name}",
        f"  市场: {market} | 候选池: {n_before} → 硬过滤后: {n_after_filter} → Top {n_after_score}",
        f"  运行时间: {pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')}",
        "=" * 60,
        "",
    ]
    return "\n".join(lines)
