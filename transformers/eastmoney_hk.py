"""
transformers/eastmoney_hk.py — 东方财富港股报表标准化转换器

港股 API 返回长格式（行 = 字段名 × 报告期），需要先 pivot 再映射。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import pandas as pd

from .base import BaseTransformer
from .field_mappings import (
    HK_INCOME_FIELDS,
    HK_BALANCE_FIELDS,
    HK_CASHFLOW_FIELDS,
    HK_DATE_TYPE_MAP,
)

logger = logging.getLogger(__name__)


def _parse_date(val: Any) -> str | None:
    """解析日期为 YYYY-MM-DD 字符串。"""
    if val is None or pd.isna(val):
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


def _pivot_long_to_wide(
    raw_df: pd.DataFrame,
    item_col: str = "STD_ITEM_NAME",
    date_col: str = "REPORT_DATE",
    amount_col: str = "AMOUNT",
) -> pd.DataFrame:
    """将港股长格式 pivot 为宽格式。

    行 = 报告期，列 = 字段名（中文字段名）。

    Args:
        raw_df: 长格式 DataFrame
        item_col: 字段名列名
        date_col: 报告期列名
        amount_col: 金额列名

    Returns:
        宽格式 DataFrame，index = 报告期, columns = 中文字段名
    """
    # 过滤有效行
    df = raw_df[[date_col, item_col, amount_col]].copy()
    df = df.dropna(subset=[date_col, item_col])

    # pivot
    wide = df.pivot(index=date_col, columns=item_col, values=amount_col).reset_index()

    # 从原始数据提取元数据（每行都有，取第一条）
    meta_cols = ["SECURITY_CODE", "SECUCODE", "SECURITY_NAME_ABBR",
                 "ORG_CODE", "DATE_TYPE_CODE", "FISCAL_YEAR", "START_DATE"]
    available_meta = [c for c in meta_cols if c in raw_df.columns]
    if available_meta:
        meta = raw_df.groupby(date_col).first()[available_meta].reset_index()
        wide = wide.merge(meta, on=date_col, how="left")

    return wide


class EastmoneyHkTransformer(BaseTransformer):
    """东方财富港股财务报表标准化转换器。"""

    def transform(self, raw_df: pd.DataFrame, market: str = "HK") -> list[dict[str, Any]]:
        """通用 transform（建议使用具体方法）。

        对长格式数据做 pivot 后返回带元数据的基础记录。
        """
        wide = _pivot_long_to_wide(raw_df)
        results: list[dict[str, Any]] = []

        for _, row in wide.iterrows():
            report_date = _parse_date(row.get("REPORT_DATE"))
            date_type = str(row.get("DATE_TYPE_CODE", "")).strip()
            report_type = HK_DATE_TYPE_MAP.get(date_type)
            if not report_date or not report_type:
                continue

            record: dict[str, Any] = {
                "stock_code": str(row.get("SECURITY_CODE", "")).strip(),
                "report_date": report_date,
                "report_type": report_type,
                "currency": "HKD",
            }
            results.append(record)

        return results

    def transform_income(self, raw_df: pd.DataFrame, market: str = "HK") -> list[dict[str, Any]]:
        """标准化港股利润表。

        流程：长格式 pivot → 字段映射 → 日期解析
        """
        wide = _pivot_long_to_wide(raw_df)
        logger.info("港股利润表 pivot: %d 行 × %d 列", len(wide), len(wide.columns))

        # 检查映射覆盖
        available_fields = set(wide.columns)
        mapped = {k: v for k, v in HK_INCOME_FIELDS.items() if k in available_fields}
        unmapped = available_fields - set(HK_INCOME_FIELDS.keys()) - {
            "REPORT_DATE", "SECURITY_CODE", "SECUCODE", "SECURITY_NAME_ABBR",
            "ORG_CODE", "DATE_TYPE_CODE", "FISCAL_YEAR", "START_DATE",
        }
        if unmapped:
            logger.debug("港股利润表未映射字段: %s", unmapped)

        results: list[dict[str, Any]] = []
        for _, row in wide.iterrows():
            report_date = _parse_date(row.get("REPORT_DATE"))
            date_type = str(row.get("DATE_TYPE_CODE", "")).strip()
            report_type = HK_DATE_TYPE_MAP.get(date_type)
            if not report_date or not report_type:
                continue

            record: dict[str, Any] = {
                "stock_code": str(row.get("SECURITY_CODE", "")).strip(),
                "report_date": report_date,
                "report_type": report_type,
                "currency": "HKD",
            }

            for cn_name, std_field in mapped.items():
                record[std_field] = _safe_float(row.get(cn_name))

            # 计算毛利润
            rev = record.get("operating_revenue")
            cost = record.get("operating_cost")
            if rev is not None and cost is not None:
                record["gross_profit"] = rev - cost

            results.append(record)
        return results

    def transform_balance(self, raw_df: pd.DataFrame, market: str = "HK") -> list[dict[str, Any]]:
        """标准化港股资产负债表。"""
        wide = _pivot_long_to_wide(raw_df)
        logger.info("港股资产负债表 pivot: %d 行 × %d 列", len(wide), len(wide.columns))

        available_fields = set(wide.columns)
        mapped = {k: v for k, v in HK_BALANCE_FIELDS.items() if k in available_fields}

        results: list[dict[str, Any]] = []
        for _, row in wide.iterrows():
            report_date = _parse_date(row.get("REPORT_DATE"))
            date_type = str(row.get("DATE_TYPE_CODE", "")).strip()
            report_type = HK_DATE_TYPE_MAP.get(date_type)
            if not report_date or not report_type:
                continue

            record: dict[str, Any] = {
                "stock_code": str(row.get("SECURITY_CODE", "")).strip(),
                "report_date": report_date,
                "report_type": report_type,
                "currency": "HKD",
            }

            for cn_name, std_field in mapped.items():
                record[std_field] = _safe_float(row.get(cn_name))

            results.append(record)
        return results

    def transform_cashflow(self, raw_df: pd.DataFrame, market: str = "HK") -> list[dict[str, Any]]:
        """标准化港股现金流量表。"""
        wide = _pivot_long_to_wide(raw_df)
        logger.info("港股现金流量表 pivot: %d 行 × %d 列", len(wide), len(wide.columns))

        available_fields = set(wide.columns)
        mapped = {k: v for k, v in HK_CASHFLOW_FIELDS.items() if k in available_fields}

        results: list[dict[str, Any]] = []
        for _, row in wide.iterrows():
            report_date = _parse_date(row.get("REPORT_DATE"))
            date_type = str(row.get("DATE_TYPE_CODE", "")).strip()
            report_type = HK_DATE_TYPE_MAP.get(date_type)
            if not report_date or not report_type:
                continue

            record: dict[str, Any] = {
                "stock_code": str(row.get("SECURITY_CODE", "")).strip(),
                "report_date": report_date,
                "report_type": report_type,
                "currency": "HKD",
            }

            for cn_name, std_field in mapped.items():
                record[std_field] = _safe_float(row.get(cn_name))

            # CAPEX = 购建固定资产 + 购建无形资产及其他资产
            capex_fa = record.get("capex")
            capex_int = record.pop("capex_intangible", None)
            if capex_fa is not None and capex_int is not None:
                record["capex"] = capex_fa + capex_int
            elif capex_fa is None and capex_int is not None:
                record["capex"] = capex_int

            results.append(record)
        return results


if __name__ == "__main__":
    import os
    os.environ["TQDM_DISABLE"] = "1"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    import akshare as ak

    print("=== 测试港股利润表转换 (00700 腾讯) ===")
    df = ak.stock_financial_hk_report_em(stock="00700", symbol="利润表", indicator="按报告期")
    transformer = EastmoneyHkTransformer()
    records = transformer.transform_income(df)
    print(f"转换结果: {len(records)} 条")
    if records:
        r = records[0]
        print(f"示例: stock={r['stock_code']} date={r['report_date']} type={r['report_type']}")
        print(f"  营收={r.get('total_revenue')} 毛利={r.get('gross_profit')} 净利润={r.get('net_profit')}")
        print(f"  归母={r.get('parent_net_profit')} EPS={r.get('eps_basic')}")

    print("\n=== 测试港股资产负债表转换 ===")
    df2 = ak.stock_financial_hk_report_em(stock="00700", symbol="资产负债表", indicator="按报告期")
    records2 = transformer.transform_balance(df2)
    print(f"转换结果: {len(records2)} 条")
    if records2:
        r = records2[0]
        print(f"  总资产={r.get('total_assets')} 总负债={r.get('total_liab')} 净资产={r.get('total_equity')}")
        print(f"  现金={r.get('cash_equivalents')}")

    print("\n=== 测试港股现金流量表转换 ===")
    df3 = ak.stock_financial_hk_report_em(stock="00700", symbol="现金流量表", indicator="按报告期")
    records3 = transformer.transform_cashflow(df3)
    print(f"转换结果: {len(records3)} 条")
    if records3:
        r = records3[0]
        print(f"  经营={r.get('cfo_net')} 投资={r.get('cfi_net')} 筹资={r.get('cff_net')}")
