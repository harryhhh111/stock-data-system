"""
transformers/us_gaap.py — 美股 US-GAAP 数据转换器

将 SEC Company Facts 提取的宽表 DataFrame 转换为标准数据库记录。
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import Any, Optional

import numpy as np
import pandas as pd

from fetchers.us_financial import USFinancialFetcher

# 数据库列名常量（用于 all_keys 补全）
_INCOME_DB_COLS = {
    "stock_code", "cik", "report_date", "report_type", "filed_date",
    "accession_no", "currency", "revenues", "cost_of_goods_sold", "gross_profit",
    "operating_expenses", "selling_general_admin", "research_and_development",
    "depreciation_amortization", "operating_income", "interest_expense",
    "interest_income", "other_income_expense", "income_before_tax",
    "income_tax_expense", "net_income", "net_income_common", "preferred_dividends",
    "eps_basic", "eps_diluted", "weighted_avg_shares_basic",
    "weighted_avg_shares_diluted", "other_comprehensive_income",
    "comprehensive_income", "edgar_tags", "extra_items", "updated_at",
}
_BALANCE_DB_COLS = {
    "stock_code", "cik", "report_date", "report_type", "filed_date",
    "accession_no", "currency", "cash_and_equivalents", "short_term_investments",
    "total_current_assets", "total_assets", "total_current_liabilities",
    "total_liabilities", "total_equity", "retained_earnings",
    "total_debt", "long_term_debt", "short_term_debt",
    "goodwill", "intangible_assets", "edgar_tags", "extra_items", "updated_at",
}
_CASHFLOW_DB_COLS = {
    "stock_code", "cik", "report_date", "report_type", "filed_date",
    "accession_no", "currency", "operating_cashflow", "depreciation_amortization",
    "capital_expenditure", "free_cashflow", "acquisitions",
    "investing_cashflow", "dividends_paid", "share_buyback",
    "debt_issued", "debt_repaid", "financing_cashflow",
    "net_change_cash", "stock_based_compensation",
    "equity_issued", "edgar_tags", "extra_items", "updated_at",
}

from .base import BaseTransformer, parse_report_date

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════
# SEC fp → 标准 report_type 映射
# ═══════════════════════════════════════════════════════════
SEC_FP_MAP: dict[str, str] = {
    "FY": "annual",
    "Q4": "quarterly",  # Q4 通常和 FY 相同，后面去重
    "Q3": "quarterly",
    "Q2": "quarterly",
    "Q1": "quarterly",
    "H1": "semi",
}

# ═══════════════════════════════════════════════════════════
# 标签优先级映射（TAG_PRIORITY）
# 同一字段可能有多个 US-GAAP 标签名，按优先级依次尝试
# ═══════════════════════════════════════════════════════════

# 利润表
INCOME_TAG_PRIORITY: dict[str, list[str]] = {
    "revenues": ["Revenues", "SalesRevenueNet",
                 "RevenueFromContractWithCustomerExcludingAssessedTax"],
    "cost_of_goods_sold": ["CostOfGoodsAndServicesSold", "CostOfRevenue", "CostOfGoodsSold"],
    "gross_profit": ["GrossProfit"],
    "operating_expenses": ["OperatingExpenses"],
    "selling_general_admin": ["SellingGeneralAndAdministrativeExpenses"],
    "research_and_development": ["ResearchAndDevelopmentExpense"],
    "depreciation_amortization": ["DepreciationAndAmortization"],
    "operating_income": ["OperatingIncomeLoss"],
    "interest_expense": ["InterestExpense", "InterestExpenseDebt", "InterestExpenseOnDebt"],
    "interest_income": ["InterestIncome", "InvestmentIncomeInterest"],
    "other_income_expense": ["OtherIncomeExpense", "OtherNonOperatingIncomeExpense"],
    "income_before_tax": ["IncomeBeforeTax"],
    "income_tax_expense": ["IncomeTaxExpenseBenefit"],
    "net_income": ["NetIncomeLoss"],
    "net_income_common": ["NetIncomeAvailableToCommonStockholdersBasic"],
    "preferred_dividends": ["PreferredStockDividendsAndOtherAdjustments"],
    "eps_basic": ["EarningsPerShareBasic"],
    "eps_diluted": ["EarningsPerShareDiluted"],
    "weighted_avg_shares_basic": ["WeightedAverageNumberOfSharesOutstandingBasic"],
    "weighted_avg_shares_diluted": ["WeightedAverageNumberOfDilutedSharesOutstanding"],
    "other_comprehensive_income": ["OtherComprehensiveIncomeLossNetOfTax"],
    "comprehensive_income": ["ComprehensiveIncomeNetOfTax"],
}

# 资产负债表
BALANCE_TAG_PRIORITY: dict[str, list[str]] = {
    "cash_and_equivalents": [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsAndShortTermInvestments",
        "CashAndCashEquivalents",
    ],
    "short_term_investments": ["ShortTermInvestments"],
    "accounts_receivable_net": ["AccountsReceivableNetCurrent", "ReceivablesNetCurrent"],
    "inventory_net": ["InventoryNet"],
    "prepaid_assets": ["PrepaidAssetsCurrent"],
    "other_current_assets": ["OtherAssetsCurrent"],
    "total_current_assets": ["AssetsCurrent"],
    "long_term_investments": ["Investments", "LongTermInvestments"],
    "property_plant_equipment": ["PropertyPlantAndEquipmentNet"],
    "goodwill": ["Goodwill"],
    "intangible_assets_net": ["IntangibleAssetsNet"],
    "operating_right_of_use": ["OperatingLeaseRightOfUseAsset"],
    "deferred_tax_assets": ["DeferredTaxAssetsNet"],
    "other_non_current_assets": ["OtherNonCurrentAssets"],
    "total_non_current_assets": ["AssetsNoncurrent"],
    "total_assets": ["Assets"],
    "accounts_payable": ["AccountsPayableCurrent"],
    "accrued_liabilities": ["AccruedLiabilitiesCurrent"],
    "short_term_debt": ["ShortTermBorrowings"],
    "current_operating_lease": ["CurrentOperatingLeaseLiability"],
    "other_current_liabilities": ["OtherLiabilitiesCurrent"],
    "total_current_liabilities": ["LiabilitiesCurrent"],
    "long_term_debt": ["LongTermDebt", "LongTermDebtNoncurrent", "DebtNoncurrent"],
    "non_current_operating_lease": ["NoncurrentOperatingLeaseLiability"],
    "deferred_tax_liabilities": ["DeferredTaxLiabilitiesNet"],
    "other_non_current_liabilities": ["OtherLiabilitiesNoncurrent"],
    "total_non_current_liabilities": ["LiabilitiesNoncurrent"],
    "total_liabilities": ["Liabilities"],
    "preferred_stock": ["PreferredStockValue"],
    "common_stock": ["CommonStockValue", "CommonStocksIncludingAdditionalPaidInCapital"],
    "additional_paid_in_capital": ["AdditionalPaidInCapital"],
    "retained_earnings": ["RetainedEarningsAccumulatedDeficit"],
    "accumulated_other_ci": ["AccumulatedOtherComprehensiveIncomeLossNetOfTax"],
    "treasury_stock": ["TreasuryStockValue"],
    "noncontrolling_interest": ["NoncontrollingInterest"],
    "total_equity": ["StockholdersEquity"],
    "total_equity_including_nci": [
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ],
}

# 现金流量表
CASHFLOW_TAG_PRIORITY: dict[str, list[str]] = {
    "net_income_cf": ["NetIncomeLoss"],
    "depreciation_amortization": ["DepreciationAndAmortization"],
    "stock_based_compensation": ["ShareBasedCompensation"],
    "deferred_income_tax": ["DeferredIncomeTaxExpenseBenefit"],
    "changes_in_working_capital": ["ChangesInWorkingCapital"],
    # Operating cash flow - multiple aliases (in priority order)
    "net_cash_from_operations": [
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
        "CashFlowFromContinuingOperatingActivities",
        "OperatingCashFlow",
    ],
    # Capital expenditures - multiple aliases (SEC most commonly uses PaymentsToAcquirePropertyPlantAndEquipment)
    "capital_expenditures": [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquirePropertyPlantAndEquipmentNetOfAccumulatedDepreciationAndAmortization",
        "CapitalExpenditures",
        "CapitalExpendituresIncurredButNotYetPaid",
        "PaymentsToAcquireProductiveAssets",
    ],
    "acquisitions": ["PaymentsToAcquireBusinessesNetOfCashAcquired"],
    "investment_purchases": [
        "PurchaseOfInvestments",
        "PaymentsToAcquireAvailableForSaleSecurities",
        "PaymentsToAcquireOtherInvestments",
    ],
    "investment_maturities": [
        "ProceedsFromMaturitiesOfInvestments",
        "ProceedsFromSaleAndMaturityOfOtherInvestments",
        "ProceedsFromMaturitiesPrepaymentsAndCallsOfAvailableForSaleSecurities",
    ],
    "other_investing_activities": [
        "OtherCashPaymentsFromInvestingActivities",
        "PaymentsForProceedsFromOtherInvestingActivities",
    ],
    # Investing cash flow - multiple aliases
    "net_cash_from_investing": [
        "NetCashProvidedByUsedInInvestingActivities",
        "NetCashProvidedByUsedInInvestingActivitiesContinuingOperations",
        "NetCashUsedInInvestingActivities",
    ],
    "debt_issued": ["ProceedsFromIssuanceOfDebt"],
    # Debt repayments - multiple aliases
    "debt_repaid": [
        "RepaymentsOfDebt",
        "RepaymentsOfLongTermDebt",
        "ProceedsFromRepaymentsOfLongTermDebtAndCapitalSecurities",
        "RepaymentsOfLongTermDebtAndCapitalSecurities",
    ],
    "equity_issued": ["PaymentsForRepurchaseOfCommonStock"],
    # Share buyback - multiple aliases
    "share_buyback": [
        "PaymentsForRepurchaseOfCommonStock",
        "PaymentsForRepurchaseOfCommonStockNetOfTreasurySharesAcquired",
    ],
    # Dividends paid - multiple aliases
    "dividends_paid": [
        "PaymentsOfDividends",
        "PaymentsOfDividendsCommonStock",
        "DividendsPaid",
        "DividendsDeclaredCash",
    ],
    "other_financing_activities": [
        "OtherCashPaymentsFromFinancingActivities",
        "ProceedsFromPaymentsForOtherFinancingActivities",
    ],
    # Financing cash flow - multiple aliases
    "net_cash_from_financing": [
        "NetCashProvidedByUsedInFinancingActivities",
        "NetCashProvidedByUsedInFinancingActivitiesContinuingOperations",
    ],
    # Exchange rate effects
    "effect_of_exchange_rate": [
        "EffectOfExchangeRateOnCashAndCashEquivalents",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseIncludingExchangeRateEffect",
    ],
    # Net change in cash
    "net_change_in_cash": [
        "IncreaseDecreaseInCashAndCashEquivalents",
        "CashAndCashEquivalentsPeriodIncreaseDecrease",
    ],
    # Beginning cash
    "cash_beginning": [
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsBeginningOfPeriod",
    ],
    # Ending cash
    "cash_ending": [
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        "CashAndCashEquivalentsAtCarryingValue",
    ],
    # Free cash flow (rarely reported by SEC, usually calculated)
    "free_cash_flow": ["FreeCashFlow"],
}


# ═══════════════════════════════════════════════════════════
# USGAAPTransformer
# ═══════════════════════════════════════════════════════════
class USGAAPTransformer(BaseTransformer):
    """美股 US-GAAP 数据转换器。

    将 fetcher 层提取的宽表 DataFrame 转换为标准化的数据库记录列表。
    """

    def __init__(self) -> None:
        # 构建 SEC 标签名 → 标准字段名的反向索引
        # 用于收集 edgar_tags
        self._sec_to_standard_income: dict[str, str] = {}
        for std_field, tags in INCOME_TAG_PRIORITY.items():
            for tag in tags:
                self._sec_to_standard_income[tag] = std_field

        self._sec_to_standard_balance: dict[str, str] = {}
        for std_field, tags in BALANCE_TAG_PRIORITY.items():
            for tag in tags:
                self._sec_to_standard_balance[tag] = std_field

        self._sec_to_standard_cashflow: dict[str, str] = {}
        for std_field, tags in CASHFLOW_TAG_PRIORITY.items():
            for tag in tags:
                self._sec_to_standard_cashflow[tag] = std_field

    def transform(self, raw_df: pd.DataFrame, market: str = "US") -> list[dict[str, Any]]:
        """通用转换入口（由 BaseTransformer 要求）。"""
        return []

    def transform_income(self, raw_df: pd.DataFrame, stock_code: str = "",
                         cik: str = "") -> list[dict[str, Any]]:
        """转换利润表宽表为数据库记录。"""
        if raw_df.empty:
            return []

        records = []
        for _, row in raw_df.iterrows():
            record = self._build_record(row, stock_code, cik)
            if record is None:
                continue
            records.append(record)

        # 确保所有记录有相同的 key（补 None）
        # 加入 tag_map 所有 key，避免某些公司缺少 tag 导致 upsert KeyError
        if records:
            all_keys = set()
            for r in records:
                all_keys.update(r.keys())
            all_keys.update(_INCOME_DB_COLS)
            records = [{k: r.get(k) for k in all_keys} for r in records]

        # net_income_common fallback: 优先用原始 tag，取不到则用 net_income - preferred_dividends
        for r in records:
            if r.get("net_income_common") is None and r.get("net_income") is not None:
                pref = r.get("preferred_dividends")
                r["net_income_common"] = r["net_income"] - pref if pref else r["net_income"]

        logger.debug("利润表转换: %s, %d 条记录", stock_code, len(records))
        return records

    def transform_balance(self, raw_df: pd.DataFrame, stock_code: str = "",
                         cik: str = "") -> list[dict[str, Any]]:
        """转换资产负债表宽表为数据库记录。"""
        if raw_df.empty:
            return []

        records = []
        for _, row in raw_df.iterrows():
            record = self._build_record(row, stock_code, cik)
            if record is None:
                continue
            records.append(record)

        if records:
            all_keys = set()
            for r in records:
                all_keys.update(r.keys())
            all_keys.update(_BALANCE_DB_COLS)
            records = [{k: r.get(k) for k in all_keys} for r in records]

        logger.debug("资产负债表转换: %s, %d 条记录", stock_code, len(records))
        return records

    def transform_cashflow(self, raw_df: pd.DataFrame, stock_code: str = "",
                          cik: str = "") -> list[dict[str, Any]]:
        """转换现金流量表宽表为数据库记录。"""
        if raw_df.empty:
            return []

        records = []
        for _, row in raw_df.iterrows():
            record = self._build_record(row, stock_code, cik)
            if record is None:
                continue
            records.append(record)

        if records:
            all_keys = set()
            for r in records:
                all_keys.update(r.keys())
            all_keys.update(_CASHFLOW_DB_COLS)
            records = [{k: r.get(k) for k in all_keys} for r in records]

        logger.debug("现金流量表转换: %s, %d 条记录", stock_code, len(records))
        return records

    # ── 内部方法 ──────────────────────────────────────────

    def _build_record(self, row: pd.Series, stock_code: str,
                      cik: str) -> Optional[dict[str, Any]]:
        """从宽表的一行构建一条标准记录。

        Args:
            row: 宽表的一行数据
            stock_code: 股票代码
            cik: SEC CIK 号

        Returns:
            标准化字典，或 None（跳过无效行）
        """
        # 解析报告日期
        report_date = parse_report_date(row.get("end"))
        if report_date is None:
            return None

        # 解析 report_type
        fp = str(row.get("fp", "")).strip()
        report_type = SEC_FP_MAP.get(fp)
        if report_type is None:
            return None

        # 解析 filed_date
        filed_date = parse_report_date(row.get("filed"))

        # 解析 accession_no
        accn = row.get("accn", "")
        if accn:
            accn = str(accn).strip()

        # 构建记录
        record: dict[str, Any] = {
            "stock_code": stock_code,
            "cik": cik,
            "report_date": report_date,
            "report_type": report_type,
            "filed_date": filed_date,
            "accession_no": accn,
            "currency": "USD",
            "updated_at": datetime.now(),
        }

        # 收集数值字段和 edgar_tags
        edgar_tags: dict[str, str] = {}
        extra_items: dict[str, Any] = {}

        # 标准字段列表（排除元数据列）
        meta_cols = {"end", "fp", "filed", "accn", "_date", "_fp_order"}
        for col in row.index:
            if col in meta_cols:
                continue
            val = row.get(col)
            if val is None or (isinstance(val, float) and np.isnan(val)):
                continue

            # 如果列名是标准字段名，直接映射
            col_str = str(col)
            if col_str in record:
                # 跳过已在 record 中的字段名
                continue

            # 尝试转为 float
            try:
                val = float(val)
            except (ValueError, TypeError):
                extra_items[col_str] = val
                continue

            record[col_str] = val
            # 收集 edgar_tags
            edgar_tags[col_str] = col_str

        record["edgar_tags"] = json.dumps(edgar_tags) if edgar_tags else None
        if extra_items:
            record["extra_items"] = json.dumps(extra_items)
        else:
            record["extra_items"] = None

        return record


def json_safe(obj: Any) -> Any:
    """确保对象可 JSON 序列化。"""
    if isinstance(obj, dict):
        return {str(k): json_safe(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    elif isinstance(obj, (int, float, str, bool, type(None))):
        return obj
    elif isinstance(obj, (date, datetime)):
        return obj.isoformat()
    return str(obj)
