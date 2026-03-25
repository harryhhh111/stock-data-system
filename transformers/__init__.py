"""
transformers — 数据标准化层

将 fetcher 拉取的原始 DataFrame 转换为标准字段字典列表。
"""
from .base import BaseTransformer
from .eastmoney import EastmoneyTransformer
from .eastmoney_hk import EastmoneyHkTransformer
from .field_mappings import (
    EM_INCOME_FIELDS,
    EM_BALANCE_FIELDS,
    EM_CASHFLOW_FIELDS,
    HK_INCOME_FIELDS,
    HK_BALANCE_FIELDS,
    HK_CASHFLOW_FIELDS,
    REPORT_TYPE_MAP,
    HK_DATE_TYPE_MAP,
)

__all__ = [
    # 基类
    "BaseTransformer",
    # A 股转换器
    "EastmoneyTransformer",
    # 港股转换器
    "EastmoneyHkTransformer",
    # 字段映射常量
    "EM_INCOME_FIELDS",
    "EM_BALANCE_FIELDS",
    "EM_CASHFLOW_FIELDS",
    "HK_INCOME_FIELDS",
    "HK_BALANCE_FIELDS",
    "HK_CASHFLOW_FIELDS",
    "REPORT_TYPE_MAP",
    "HK_DATE_TYPE_MAP",
]
