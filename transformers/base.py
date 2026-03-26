"""
transformers/base.py — 数据标准化基类 + 通用工具函数
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import date, datetime
from typing import Any, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
# 报告类型映射
# ═══════════════════════════════════════════════════════════

REPORT_TYPE_MAP: dict[str, str] = {
    "一季报": "quarterly",   # Q1 (截至 03-31)
    "中报": "semi",          # H1 (截至 06-30)
    "三季报": "quarterly",   # Q3 (截至 09-30)
    "年报": "annual",        # FY (截至 12-31)
}


def transform_report_type(report_type_str: str) -> Optional[str]:
    """将东方财富报告类型转为标准值。

    Args:
        report_type_str: 东方财富的 REPORT_TYPE，如 '三季报'、'年报'。

    Returns:
        标准报告类型：'annual' | 'semi' | 'quarterly'，未知值返回 None。
    """
    return REPORT_TYPE_MAP.get(report_type_str)


# ═══════════════════════════════════════════════════════════
# 日期解析
# ═══════════════════════════════════════════════════════════

def parse_report_date(date_val: Any) -> Optional[date]:
    """将多种日期格式转为 Python date 对象。

    支持的输入类型：
    - pandas Timestamp
    - numpy datetime64
    - Python datetime / date
    - 字符串（'YYYY-MM-DD'、'YYYY-MM-DD HH:MM:SS' 等）
    - None / NaT / NaN → 返回 None

    Args:
        date_val: 日期值。

    Returns:
        date 对象，或 None。
    """
    if date_val is None:
        return None

    # pd.NaT（需要在 isinstance 检查之前，因为 NaT 是 Timestamp 子类）
    try:
        if pd.isna(date_val):
            return None
    except (ValueError, TypeError):
        pass

    # pandas Timestamp
    if isinstance(date_val, pd.Timestamp):
        return date_val.date()

    # numpy datetime64
    if isinstance(date_val, np.datetime64):
        ts = pd.Timestamp(date_val)
        return ts.date()

    # Python date
    if isinstance(date_val, date) and not isinstance(date_val, datetime):
        return date_val

    # Python datetime
    if isinstance(date_val, datetime):
        return date_val.date()

    # 字符串
    if isinstance(date_val, str):
        date_val = date_val.strip()
        if not date_val or date_val.lower() in ("nan", "nat", "none", ""):
            return None
        # 尝试多种格式
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y%m%d"):
            try:
                return datetime.strptime(date_val, fmt).date()
            except ValueError:
                continue
        logger.warning("无法解析日期字符串: %r", date_val)
        return None

    # float NaN / int NaN
    try:
        if isinstance(date_val, (int, float)) and np.isnan(float(date_val)):
            return None
    except (TypeError, ValueError):
        pass

    logger.warning("parse_report_date: 不支持的类型 %s: %r", type(date_val), date_val)
    return None


# ═══════════════════════════════════════════════════════════
# 基类
# ═══════════════════════════════════════════════════════════

class BaseTransformer(ABC):
    """数据标准化转换器基类。

    子类需实现 ``transform`` 方法，将原始 DataFrame 转换为标准字段列表。
    """

    @abstractmethod
    def transform(self, raw_df: pd.DataFrame, market: str = "CN_A") -> list[dict[str, Any]]:
        """将原始 DataFrame 转换为标准化记录列表。

        Args:
            raw_df: 原始 DataFrame（来自 fetcher 层）
            market: 市场标识，'CN_A' 或 'HK'

        Returns:
            标准化字典列表，每个字典对应一行报表记录
        """
        ...
