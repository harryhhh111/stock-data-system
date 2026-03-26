"""
transformers/dividend.py — 分红数据标准化
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Optional

import numpy as np
import pandas as pd

from .base import parse_report_date

logger = logging.getLogger(__name__)


def _clean_value(val: Any) -> Any:
    """清理单个值：NaN/NaT → None。"""
    if val is None:
        return None
    if isinstance(val, float) and np.isnan(val):
        return None
    try:
        if pd.isna(val):
            return None
    except (ValueError, TypeError):
        pass
    return val


def _to_date(val: Any) -> Optional[date]:
    """尝试将值转为 date。"""
    val = _clean_value(val)
    if val is None:
        return None
    return parse_report_date(val)


def transform_a_dividend(raw_df: pd.DataFrame, stock_code: str) -> list[dict[str, Any]]:
    """将 A 股分红 DataFrame 转换为 dividend_split 记录列表。

    akshare stock_history_dividend_detail 返回的列名可能包含中文，
    这里做通用映射。
    """
    if raw_df is None or raw_df.empty:
        return []

    records = []
    for _, row in raw_df.iterrows():
        # 尝试识别常见列名
        def get(cols):
            for c in cols:
                if c in row.index:
                    return row[c]
            return None

        record: dict[str, Any] = {"stock_code": stock_code}

        # 公告日期
        announce = get(["股权登记日", "公告日期", "announce_date", "A股权登记日"])
        # 除权除息日
        ex = get(["除权除息日", "ex_date", "除息日"])
        # 每股派息
        dps = get(["派息(每10股)", "分红金额", "dividend_per_share", "每股派息(元)"])
        # 送股
        bonus = get(["送股(每10股)", "送股比例", "bonus_share"])
        # 转增
        convert = get(["转增(每10股)", "转增比例", "convert_share"])
        # 进度
        progress = get(["方案进度", "progress", "实施状态"])

        # 转换日期
        record["announce_date"] = _to_date(announce)
        record["ex_date"] = _to_date(ex)
        record["record_date"] = None
        record["payable_date"] = None

        # 转换数值（akshare 可能返回字符串如 "10派10.5"）
        def parse_dividend(val: Any) -> Optional[float]:
            if val is None:
                return None
            val = _clean_value(val)
            if val is None:
                return None
            if isinstance(val, (int, float)):
                return float(val)
            s = str(val).strip()
            if not s or s == "-":
                return None
            # 处理 "10派10.5" 格式 → 1.05
            import re
            m = re.search(r'派([\d.]+)', s)
            if m:
                return float(m.group(1)) / 10.0
            try:
                return float(s)
            except ValueError:
                return None

        def parse_ratio(val: Any) -> Optional[float]:
            if val is None:
                return None
            val = _clean_value(val)
            if val is None:
                return None
            if isinstance(val, (int, float)):
                return float(val)
            s = str(val).strip()
            if not s or s == "-":
                return None
            import re
            m = re.search(r'([\d.]+)', s)
            if m:
                return float(m.group(1)) / 10.0
            try:
                return float(s)
            except ValueError:
                return None

        record["dividend_per_share"] = parse_dividend(dps)
        record["bonus_share"] = parse_ratio(bonus)
        record["convert_share"] = parse_ratio(convert)
        record["rights_share"] = None
        record["rights_price"] = None
        record["progress"] = str(progress).strip() if progress else None
        record["currency"] = "CNY"
        record["source"] = "eastmoney"

        # 至少有日期或金额才保存
        if record["announce_date"] or record["dividend_per_share"] or record["ex_date"]:
            records.append(record)

    return records


def transform_hk_dividend(raw_df: pd.DataFrame, stock_code: str) -> list[dict[str, Any]]:
    """将港股分红 DataFrame 转换为 dividend_split 记录列表。"""
    if raw_df is None or raw_df.empty:
        return []

    records = []
    for _, row in raw_df.iterrows():
        record: dict[str, Any] = {"stock_code": stock_code}

        # 港股分红的列名通常是中文
        def get(cols):
            for c in cols:
                if c in row.index:
                    return row[c]
            return None

        announce = get(["公告日期", " announce_date"])
        ex = get(["除权日", "除净日", "ex_date"])
        record_date = get(["股权登记日", "record_date"])
        payable = get(["派息日", "payable_date"])
        dps = get(["每股派息", "dividend_per_share", "派息金额"])

        record["announce_date"] = _to_date(announce)
        record["ex_date"] = _to_date(ex)
        record["record_date"] = _to_date(record_date)
        record["payable_date"] = _to_date(payable)
        record["dividend_per_share"] = _clean_value(dps)
        record["bonus_share"] = None
        record["convert_share"] = None
        record["rights_share"] = None
        record["rights_price"] = None
        record["progress"] = None
        record["currency"] = "HKD"
        record["source"] = "eastmoney_hk"

        if record["announce_date"] or record["dividend_per_share"] or record["ex_date"]:
            records.append(record)

    return records
