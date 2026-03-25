"""
transformers/eastmoney.py — 东方财富 A 股报表标准化转换器

将东方财富 API 返回的宽格式 DataFrame 转换为标准字段字典列表。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import pandas as pd

from .base import BaseTransformer
from .field_mappings import (
    EM_INCOME_FIELDS,
    EM_BALANCE_FIELDS,
    EM_CASHFLOW_FIELDS,
    REPORT_TYPE_MAP,
)

logger = logging.getLogger(__name__)


def _parse_report_type(raw_type: str) -> str | None:
    """将中文 report_type 映射为标准值。"""
    return REPORT_TYPE_MAP.get(str(raw_type).strip())


def _parse_date(val: Any) -> str | None:
    """解析日期为 YYYY-MM-DD 字符串。"""
    if pd.isna(val) or val is None:
        return None
    if isinstance(val, str):
        val = val.strip()
        if len(val) >= 10:
            return val[:10]
        return val
    if isinstance(val, (pd.Timestamp, datetime)):
        return val.strftime("%Y-%m-%d")
    return str(val)[:10]


def _safe_float(val: Any) -> float | None:
    """安全转换为 float，无效值返回 None。"""
    if val is None or pd.isna(val):
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


class EastmoneyTransformer(BaseTransformer):
    """东方财富 A 股财务报表标准化转换器。"""

    def transform(self, raw_df: pd.DataFrame, market: str = "CN_A") -> list[dict[str, Any]]:
        """通用 transform 入口（通常按报表类型调用具体方法）。

        此方法不区分报表类型，仅做基础字段提取。
        如需按报表类型标准化，请使用 transform_income / transform_balance / transform_cashflow。
        """
        results: list[dict[str, Any]] = []
        for _, row in raw_df.iterrows():
            record: dict[str, Any] = {
                "stock_code": str(row.get("SECURITY_CODE", "")).strip(),
                "report_date": _parse_date(row.get("REPORT_DATE")),
                "report_type": _parse_report_type(row.get("REPORT_TYPE", "")),
                "notice_date": _parse_date(row.get("NOTICE_DATE")),
                "update_date": _parse_date(row.get("UPDATE_DATE")),
                "currency": str(row.get("CURRENCY", "CNY")).strip(),
            }
            if record["report_date"] and record["report_type"]:
                results.append(record)
        return results

    def transform_income(self, raw_df: pd.DataFrame, market: str = "CN_A") -> list[dict[str, Any]]:
        """标准化利润表。

        Args:
            raw_df: 东方财富利润表 DataFrame（宽格式）
            market: 市场标识

        Returns:
            标准化记录列表
        """
        results: list[dict[str, Any]] = []
        mapped_cols = {k: v for k, v in EM_INCOME_FIELDS.items() if k in raw_df.columns}

        for _, row in raw_df.iterrows():
            report_type = _parse_report_type(row.get("REPORT_TYPE", ""))
            report_date = _parse_date(row.get("REPORT_DATE"))
            if not report_date or not report_type:
                continue

            record: dict[str, Any] = {}
            # 元数据
            record["stock_code"] = str(row.get("SECURITY_CODE", "")).strip()
            record["report_date"] = report_date
            record["report_type"] = report_type
            record["notice_date"] = _parse_date(row.get("NOTICE_DATE"))
            record["update_date"] = _parse_date(row.get("UPDATE_DATE"))
            record["currency"] = str(row.get("CURRENCY", "CNY")).strip()

            # 字段映射
            for em_col, std_field in mapped_cols.items():
                if std_field in ("stock_code", "report_date", "report_type",
                                 "notice_date", "update_date", "currency"):
                    continue  # 已处理
                record[std_field] = _safe_float(row.get(em_col))

            # 计算毛利润
            op_rev = record.get("operating_revenue")
            op_cost = record.get("operating_cost")
            if op_rev is not None and op_cost is not None:
                record["gross_profit"] = op_rev - op_cost

            results.append(record)
        return results

    def transform_balance(self, raw_df: pd.DataFrame, market: str = "CN_A") -> list[dict[str, Any]]:
        """标准化资产负债表。"""
        results: list[dict[str, Any]] = []
        mapped_cols = {k: v for k, v in EM_BALANCE_FIELDS.items() if k in raw_df.columns}

        for _, row in raw_df.iterrows():
            report_type = _parse_report_type(row.get("REPORT_TYPE", ""))
            report_date = _parse_date(row.get("REPORT_DATE"))
            if not report_date or not report_type:
                continue

            record: dict[str, Any] = {
                "stock_code": str(row.get("SECURITY_CODE", "")).strip(),
                "report_date": report_date,
                "report_type": report_type,
                "notice_date": _parse_date(row.get("NOTICE_DATE")),
                "update_date": _parse_date(row.get("UPDATE_DATE")),
                "currency": str(row.get("CURRENCY", "CNY")).strip(),
            }

            for em_col, std_field in mapped_cols.items():
                if std_field in ("stock_code", "report_date", "report_type",
                                 "notice_date", "update_date", "currency"):
                    continue
                record[std_field] = _safe_float(row.get(em_col))

            results.append(record)
        return results

    def transform_cashflow(self, raw_df: pd.DataFrame, market: str = "CN_A") -> list[dict[str, Any]]:
        """标准化现金流量表。"""
        results: list[dict[str, Any]] = []
        mapped_cols = {k: v for k, v in EM_CASHFLOW_FIELDS.items() if k in raw_df.columns}

        for _, row in raw_df.iterrows():
            report_type = _parse_report_type(row.get("REPORT_TYPE", ""))
            report_date = _parse_date(row.get("REPORT_DATE"))
            if not report_date or not report_type:
                continue

            record: dict[str, Any] = {
                "stock_code": str(row.get("SECURITY_CODE", "")).strip(),
                "report_date": report_date,
                "report_type": report_type,
                "notice_date": _parse_date(row.get("NOTICE_DATE")),
                "update_date": _parse_date(row.get("UPDATE_DATE")),
                "currency": str(row.get("CURRENCY", "CNY")).strip(),
            }

            for em_col, std_field in mapped_cols.items():
                if std_field in ("stock_code", "report_date", "report_type",
                                 "notice_date", "update_date", "currency"):
                    continue
                record[std_field] = _safe_float(row.get(em_col))

            results.append(record)
        return results


if __name__ == "__main__":
    import os
    os.environ["TQDM_DISABLE"] = "1"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    import akshare as ak

    print("=== 测试利润表转换 ===")
    df = ak.stock_profit_sheet_by_report_em(symbol="SH600519")
    transformer = EastmoneyTransformer()
    records = transformer.transform_income(df)
    print(f"转换结果: {len(records)} 条")
    if records:
        r = records[0]
        print(f"示例: stock={r['stock_code']} date={r['report_date']} type={r['report_type']}")
        print(f"  营收={r.get('total_revenue')} 净利润={r.get('net_profit')} 归母={r.get('parent_net_profit')}")

    print("\n=== 测试资产负债表转换 ===")
    df2 = ak.stock_balance_sheet_by_report_em(symbol="SH600519")
    records2 = transformer.transform_balance(df2)
    print(f"转换结果: {len(records2)} 条")
    if records2:
        r = records2[0]
        print(f"示例: 总资产={r.get('total_assets')} 总负债={r.get('total_liab')} 净资产={r.get('total_equity')}")

    print("\n=== 测试现金流量表转换 ===")
    df3 = ak.stock_cash_flow_sheet_by_report_em(symbol="SH600519")
    records3 = transformer.transform_cashflow(df3)
    print(f"转换结果: {len(records3)} 条")
    if records3:
        r = records3[0]
        print(f"示例: 经营={r.get('cfo_net')} 投资={r.get('cfi_net')} 筹资={r.get('cff_net')}")
