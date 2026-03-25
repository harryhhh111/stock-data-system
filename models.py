"""
字段映射定义 — 东方财富 API → 数据库标准字段

负责：
1. REPORT_TYPE 映射（'三季报'/'中报'/'一季报'/'年报' → 标准值）
2. 三大报表（利润表、资产负债表、现金流量表）的列名映射
3. 通用转换函数：parse_report_date、map_row
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
# 报告类型映射
# ═══════════════════════════════════════════════════════════

#: 东方财富 REPORT_TYPE → 标准报告类型
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
# 利润表映射（东方财富 → income_statement）
# ═══════════════════════════════════════════════════════════

EM_INCOME_FIELDS: dict[str, str] = {
    # ── 元数据 ──
    "SECURITY_CODE":           "stock_code",
    "REPORT_DATE":             "report_date",
    "REPORT_TYPE":             "report_type",
    "NOTICE_DATE":             "notice_date",
    "UPDATE_DATE":             "update_date",
    "CURRENCY":                "currency",
    # ── 收入 ──
    "TOTAL_OPERATE_INCOME":    "total_revenue",         # 营业总收入
    "OPERATE_INCOME":          "operating_revenue",     # 营业收入
    "OPERATE_COST":            "operating_cost",        # 营业成本
    # ── 费用 ──
    "SALE_EXPENSE":            "selling_expense",       # 销售费用
    "MANAGE_EXPENSE":          "admin_expense",         # 管理费用
    "ME_RESEARCH_EXPENSE":     "rd_expense",            # 研发费用（管理费用中的研发）
    "FINANCE_EXPENSE":         "finance_expense",       # 财务费用
    # ── 利润 ──
    "OPERATE_PROFIT":          "operating_profit",      # 营业利润
    "TOTAL_PROFIT":            "total_profit",          # 利润总额
    "INCOME_TAX":              "income_tax",            # 所得税费用
    "NETPROFIT":               "net_profit",            # 净利润
    "DEDUCT_PARENT_NETPROFIT": "net_profit_excl",       # 扣非净利润
    "PARENT_NETPROFIT":        "parent_net_profit",     # 归母净利润
    "MINORITY_INTEREST":       "minority_interest",     # 少数股东损益
    # ── 综合收益 ──
    "OTHER_COMPRE_INCOME":     "other_comprehensive",   # 其他综合收益
    "TOTAL_COMPRE_INCOME":     "total_comprehensive",   # 综合收益总额
    # ── 每股 ──
    "BASIC_EPS":               "eps_basic",             # 基本每股收益
    "DILUTED_EPS":             "eps_diluted",           # 稀释每股收益
}

# 不映射的字段（同比 *_YOY 等衍生列）
EM_INCOME_SKIP = {c for c in []}  # reserved for future use


# ═══════════════════════════════════════════════════════════
# 资产负债表映射（东方财富 → balance_sheet）
# ═══════════════════════════════════════════════════════════

EM_BALANCE_FIELDS: dict[str, str] = {
    # ── 元数据 ──
    "SECURITY_CODE":            "stock_code",
    "REPORT_DATE":              "report_date",
    "REPORT_TYPE":              "report_type",
    "NOTICE_DATE":              "notice_date",
    "UPDATE_DATE":              "update_date",
    "CURRENCY":                 "currency",
    # ── 流动资产 ──
    "MONETARYFUNDS":            "cash_equivalents",       # 货币资金
    "TRADE_FINASSET":           "trading_assets",         # 交易性金融资产
    "ACCOUNTS_RECE":            "accounts_receivable",    # 应收账款
    "PREPAYMENT":               "prepayments",            # 预付款项
    "OTHER_RECE":               "other_receivables",      # 其他应收款
    "INVENTORY":                "inventory",              # 存货
    "CONTRACT_ASSET":           "contract_assets",        # 合同资产
    "TOTAL_CURRENT_ASSETS":     "current_assets",         # 流动资产合计
    # ── 非流动资产 ──
    "LONG_EQUITY_INVEST":       "long_equity_invest",     # 长期股权投资
    "FIXED_ASSET":              "fixed_assets",           # 固定资产
    "CIP":                      "construction_in_prog",   # 在建工程
    "INTANGIBLE_ASSET":         "intangible_assets",      # 无形资产
    "GOODWILL":                 "goodwill",               # 商誉
    "DEFER_TAX_ASSET":          "long_deferred_tax",      # 递延所得税资产
    "TOTAL_NONCURRENT_ASSETS":  "non_current_assets",     # 非流动资产合计
    "TOTAL_ASSETS":             "total_assets",           # 资产总计
    # ── 流动负债 ──
    "SHORT_LOAN":               "short_term_borrow",      # 短期借款
    "ACCOUNTS_PAYABLE":         "accounts_payable",       # 应付账款
    "CONTRACT_LIAB":            "contract_liab",          # 合同负债
    "STAFF_SALARY_PAYABLE":     "employee_payable",       # 应付职工薪酬
    "TAX_PAYABLE":              "tax_payable",            # 应交税费
    "TOTAL_CURRENT_LIAB":       "current_liab",           # 流动负债合计
    # ── 非流动负债 ──
    "LONG_LOAN":                "long_term_borrow",       # 长期借款
    "BOND_PAYABLE":             "bonds_payable",          # 应付债券
    "DEFER_TAX_LIAB":           "long_deferred_liab",     # 递延所得税负债
    "TOTAL_NONCURRENT_LIAB":    "non_current_liab",      # 非流动负债合计
    "TOTAL_LIABILITIES":        "total_liab",             # 负债合计
    # ── 所有者权益 ──
    "SHARE_CAPITAL":            "paid_in_capital",        # 实收资本（股本）
    "CAPITAL_RESERVE":          "capital_reserve",        # 资本公积
    "SURPLUS_RESERVE":          "surplus_reserve",        # 盈余公积
    "UNASSIGN_RPOFIT":          "retained_earnings",      # 未分配利润
    "MINORITY_EQUITY":          "minority_equity",        # 少数股东权益
    "TOTAL_EQUITY":             "total_equity",           # 所有者权益（净资产）
    "TOTAL_PARENT_EQUITY":      "parent_equity",          # 归母净资产
}


# ═══════════════════════════════════════════════════════════
# 现金流量表映射（东方财富 → cash_flow_statement）
# ═══════════════════════════════════════════════════════════

EM_CASHFLOW_FIELDS: dict[str, str] = {
    # ── 元数据 ──
    "SECURITY_CODE":             "stock_code",
    "REPORT_DATE":               "report_date",
    "REPORT_TYPE":               "report_type",
    "NOTICE_DATE":               "notice_date",
    "UPDATE_DATE":               "update_date",
    "CURRENCY":                  "currency",
    # ── 经营活动 ──
    "NETCASH_OPERATE":           "cfo_net",               # 经营活动现金流净额
    "SALES_SERVICES":            "cfo_sales",             # 销售商品收到现金
    "RECEIVE_TAX_REFUND":        "cfo_tax_refund",        # 收到税费返还
    "RECEIVE_OTHER_OPERATE":     "cfo_operating_receive", # 收到其他经营活动现金
    # ── 投资活动 ──
    "NETCASH_INVEST":            "cfi_net",               # 投资活动现金流净额
    "WITHDRAW_INVEST":           "cfi_disposal",          # 收回投资收到现金
    "CONSTRUCT_LONG_ASSET":      "capex",                 # 购建固定资产/无形资产支付的现金
    "INVEST_PAY_CASH":           "cfi_invest_paid",       # 投资支付的现金
    # ── 筹资活动 ──
    "NETCASH_FINANCE":           "cff_net",               # 筹资活动现金流净额
    "ACCEPT_INVEST_CASH":        "cff_borrow_received",   # 取得借款收到现金
    "PAY_DEBT_CASH":             "cff_borrow_repaid",     # 偿还债务支付现金
    "ASSIGN_DIVIDEND_PORFIT":    "cff_dividend_paid",     # 分配股利/利润/偿付利息支付现金
    # ── 汇率及现金 ──
    "RATE_CHANGE_EFFECT":        "fx_effect",             # 汇率变动影响
    "CCE_ADD":                   "cash_increase",         # 现金及等价物净增加额
    "BEGIN_CCE":                 "cash_begin",            # 期初现金及等价物余额
    "END_CCE":                   "cash_end",              # 期末现金及等价物余额
}


# ═══════════════════════════════════════════════════════════
# 通用映射函数
# ═══════════════════════════════════════════════════════════

def _clean_value(val: Any) -> Any:
    """清理单个值：NaN/NaT → None，保持其他类型不变。"""
    if val is None:
        return None
    if isinstance(val, float) and np.isnan(val):
        return None
    if isinstance(val, (pd.Timestamp, np.datetime64)) and pd.isna(val):
        return None
    return val


def map_row(row: dict[str, Any], field_mapping: dict[str, str]) -> dict[str, Any]:
    """将一行原始数据按映射表转换为目标格式。

    转换规则：
    1. 按 field_mapping 重命名键
    2. 过滤掉 None 值（NaN / NaT / None）
    3. 保留原始值类型（float / str / int / date 等）

    Args:
        row: 原始数据行（键 = 东方财富列名）。
        field_mapping: {东方财富列名: 标准列名} 映射表。

    Returns:
        映射后的字典。
    """
    result: dict[str, Any] = {}
    for src_key, dst_key in field_mapping.items():
        if src_key not in row:
            continue
        val = _clean_value(row[src_key])
        if val is None:
            continue
        result[dst_key] = val
    return result


def map_income_row(row: dict[str, Any]) -> dict[str, Any]:
    """映射利润表行 + 特殊处理报告类型和日期。"""
    mapped = map_row(row, EM_INCOME_FIELDS)
    if "report_type" in mapped:
        mapped["report_type"] = transform_report_type(str(mapped["report_type"]))
    if "report_date" in mapped:
        mapped["report_date"] = parse_report_date(mapped["report_date"])
    if "notice_date" in mapped:
        mapped["notice_date"] = parse_report_date(mapped["notice_date"])
    if "update_date" in mapped:
        mapped["update_date"] = parse_report_date(mapped["update_date"])
    # 计算毛利润（如果未直接提供）
    if "operating_revenue" in mapped and "operating_cost" in mapped:
        rev = mapped["operating_revenue"]
        cost = mapped["operating_cost"]
        if rev is not None and cost is not None:
            mapped["gross_profit"] = Decimal(str(rev)) - Decimal(str(cost))
    return mapped


def map_balance_row(row: dict[str, Any]) -> dict[str, Any]:
    """映射资产负债表行 + 特殊处理报告类型和日期。"""
    mapped = map_row(row, EM_BALANCE_FIELDS)
    if "report_type" in mapped:
        mapped["report_type"] = transform_report_type(str(mapped["report_type"]))
    if "report_date" in mapped:
        mapped["report_date"] = parse_report_date(mapped["report_date"])
    if "notice_date" in mapped:
        mapped["notice_date"] = parse_report_date(mapped["notice_date"])
    if "update_date" in mapped:
        mapped["update_date"] = parse_report_date(mapped["update_date"])
    return mapped


def map_cashflow_row(row: dict[str, Any]) -> dict[str, Any]:
    """映射现金流量表行 + 特殊处理报告类型和日期。"""
    mapped = map_row(row, EM_CASHFLOW_FIELDS)
    if "report_type" in mapped:
        mapped["report_type"] = transform_report_type(str(mapped["report_type"]))
    if "report_date" in mapped:
        mapped["report_date"] = parse_report_date(mapped["report_date"])
    if "notice_date" in mapped:
        mapped["notice_date"] = parse_report_date(mapped["notice_date"])
    if "update_date" in mapped:
        mapped["update_date"] = parse_report_date(mapped["update_date"])
    return mapped


if __name__ == "__main__":
    print("=== models.py 自检 ===\n")

    # 1. 报告类型映射
    print("[1] 报告类型映射:")
    for rt in ["一季报", "中报", "三季报", "年报", "未知类型"]:
        print(f"    {rt} → {transform_report_type(rt)}")

    # 2. 日期解析
    print("\n[2] 日期解析:")
    test_dates = [
        pd.Timestamp("2024-09-30"),
        np.datetime64("2024-06-30"),
        "2024-12-31",
        "2024-03-31 00:00:00",
        None,
        pd.NaT,
    ]
    for d in test_dates:
        result = parse_report_date(d)
        print(f"    {repr(d):40s} → {result}")

    # 3. 字段映射数量
    print("\n[3] 字段映射:")
    print(f"    利润表 EM_INCOME_FIELDS:    {len(EM_INCOME_FIELDS)} 个字段")
    print(f"    资产负债表 EM_BALANCE_FIELDS: {len(EM_BALANCE_FIELDS)} 个字段")
    print(f"    现金流量表 EM_CASHFLOW_FIELDS: {len(EM_CASHFLOW_FIELDS)} 个字段")

    # 4. 模拟 map_row
    print("\n[4] 模拟利润表行映射:")
    mock_income = {
        "SECURITY_CODE": "600519",
        "REPORT_DATE": "2024-09-30",
        "REPORT_TYPE": "三季报",
        "NOTICE_DATE": "2024-10-30 00:00:00",
        "OPERATE_INCOME": 1200.0,
        "OPERATE_COST": 300.0,
        "NETPROFIT": 500.0,
        "PARENT_NETPROFIT": 480.0,
        "BASIC_EPS": 3.82,
        "TOTAL_OPERATE_INCOME": 1250.0,
        "SALE_EXPENSE": 50.0,
        "MANAGE_EXPENSE": 80.0,
        "ME_RESEARCH_EXPENSE": 20.0,
        "FINANCE_EXPENSE": -5.0,
        "OPERATE_PROFIT": 550.0,
        "TOTAL_PROFIT": 540.0,
        "INCOME_TAX": 40.0,
        "DEDUCT_PARENT_NETPROFIT": 475.0,
        "MINORITY_INTEREST": 20.0,
        "OTHER_COMPRE_INCOME": 10.0,
        "TOTAL_COMPRE_INCOME": 510.0,
        "DILUTED_EPS": 3.80,
    }
    mapped = map_income_row(mock_income)
    for k, v in sorted(mapped.items()):
        print(f"    {k:30s} = {v}")

    print("\n✅ 全部测试完成。")
