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

    akshare stock_history_dividend_detail 返回列：派息/送股/转增/公告日期/除权除息日/股权登记日/进度
    """
    if raw_df is None or raw_df.empty:
        return []

    import re as _re

    records = []
    for _, row in raw_df.iterrows():
        def get(cols):
            for c in cols:
                if c in row.index:
                    return row[c]
            return None

        record: dict[str, Any] = {"stock_code": stock_code}

        # 列名映射（东方财富 A 股）
        announce = get(["公告日期"])
        ex = get(["除权除息日"])
        record_date = get(["股权登记日"])
        dps_raw = get(["派息"])          # 每 10 股派息金额（元）
        bonus_raw = get(["送股"])         # 每 10 股送股数
        convert_raw = get(["转增"])        # 每 10 股转增数
        progress = get(["进度"])

        # 解析数值：原始值为每 10 股，需除以 10
        def _per_share(val):
            v = _clean_value(val)
            if v is None:
                return None
            if isinstance(v, (int, float)):
                return float(v) / 10.0
            s = str(v).strip()
            if not s or s == "-":
                return None
            m = _re.search(r'([\d.]+)', s)
            if m:
                return float(m.group(1)) / 10.0
            return None

        record["announce_date"] = _to_date(announce)
        record["ex_date"] = _to_date(ex)
        record["record_date"] = _to_date(record_date)
        record["payable_date"] = None
        record["dividend_per_share"] = _per_share(dps_raw)
        record["bonus_share"] = _per_share(bonus_raw)
        record["convert_share"] = _per_share(convert_raw)
        record["rights_share"] = None
        record["rights_price"] = None
        record["progress"] = str(progress).strip() if progress else None
        record["currency"] = "CNY"
        record["source"] = "eastmoney"

        if record["announce_date"] or record["dividend_per_share"] or record["ex_date"]:
            records.append(record)

    return records


def transform_hk_dividend(raw_df: pd.DataFrame, stock_code: str) -> list[dict[str, Any]]:
    """将港股分红 DataFrame 转换为 dividend_split 记录列表。"""
    if raw_df is None or raw_df.empty:
        return []

    import re as _re

    records = []
    for _, row in raw_df.iterrows():
        record: dict[str, Any] = {"stock_code": stock_code}

        # 港股分红东方财富列名：发放日/除净日/分红方案/最新公告日期 等
        def get(cols):
            for c in cols:
                if c in row.index:
                    return row[c]
            return None

        announce = get(["最新公告日期", "公告日期"])
        ex = get(["除净日", "除权日", "ex_date"])
        payable = get(["发放日", "派息日", "payable_date"])
        record_date = get(["截至过户日", "股权登记日", "record_date"])
        dps_raw = get(["分红方案", "每股派息", "dividend_per_share"])

        # 解析分红方案文本，如 "每股派港币5.3元" → 5.3
        dps = None
        currency = "HKD"
        if dps_raw is not None:
            val = _clean_value(dps_raw)
            if isinstance(val, str):
                # 提取数字部分
                m = _re.search(r'([\d.]+)', val)
                if m:
                    dps = float(m.group(1))
                # 检测币种
                if "人民币" in val or "RMB" in val.upper():
                    currency = "CNY"
                elif "美元" in val or "USD" in val.upper():
                    currency = "USD"
            elif isinstance(val, (int, float)):
                dps = float(val)

        record["announce_date"] = _to_date(announce)
        record["ex_date"] = _to_date(ex)
        record["record_date"] = _to_date(record_date)
        record["payable_date"] = _to_date(payable)
        record["dividend_per_share"] = dps
        record["bonus_share"] = None
        record["convert_share"] = None
        record["rights_share"] = None
        record["rights_price"] = None
        record["progress"] = None
        record["currency"] = currency
        record["source"] = "eastmoney_hk"

        if record["announce_date"] or record["dividend_per_share"] or record["ex_date"]:
            records.append(record)

    return records
