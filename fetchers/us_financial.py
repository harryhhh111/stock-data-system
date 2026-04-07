"""
fetchers/us_financial.py — 美股 SEC EDGAR 数据拉取

提供：
- SECRateLimiter: 滑动窗口限流器（10次/秒）
- USFinancialFetcher: 继承 BaseFetcher，获取 SEC Company Facts 数据
- fetch_company_list(): 获取 CIK ↔ ticker 映射
- fetch_sp500_constituents(): 获取 S&P 500 成分股列表
- fetch_nasdaq100_constituents(): 获取 NASDAQ 100 成分股列表
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import requests

import config

from .base import BaseFetcher, retry_with_backoff

logger = logging.getLogger(__name__)

# 缓存目录
CACHE_DIR = config.DATA_DIR / "sec_cache"
CACHE_DIR.mkdir(exist_ok=True)

# User-Agent 必须设置，否则 SEC 拒绝请求
HEADERS = {
    "User-Agent": config.sec.user_agent,
    "Accept": "application/json",
}


# ═══════════════════════════════════════════════════════════
# SEC 专用滑动窗口限流器（10次/秒）
# ═══════════════════════════════════════════════════════════
class SECRateLimiter:
    """滑动窗口限流器：严格保证 1 秒内不超过 RATE 次请求。

    SEC 规则：10 requests/second（所有 endpoint 合计）。
    """

    def __init__(self, rate: int = 10, window: float = 1.0) -> None:
        self._rate = rate
        self._window = window
        self._timestamps: deque[float] = deque()
        self._lock = threading.Lock()

    def wait(self) -> None:
        """在发起请求前调用，必要时 sleep。"""
        with self._lock:
            now = time.time()
            # 清除窗口外的旧记录
            while self._timestamps and self._timestamps[0] < now - self._window:
                self._timestamps.popleft()
            # 如果已达上限，等待直到最早的请求离开窗口
            if len(self._timestamps) >= self._rate:
                sleep_time = self._timestamps[0] + self._window - now + 0.05
                if sleep_time > 0:
                    time.sleep(sleep_time)
                # 清理窗口外的记录（sleep 后可能有过期的）
                now = time.time()
                while self._timestamps and self._timestamps[0] < now - self._window:
                    self._timestamps.popleft()
            self._timestamps.append(time.time())


# ═══════════════════════════════════════════════════════════
# US Financial Fetcher
# ═══════════════════════════════════════════════════════════
class USFinancialFetcher(BaseFetcher):
    """美股 SEC EDGAR 数据拉取器。

    主要接口：
    - fetch_company_list() → CIK ↔ ticker 映射
    - fetch_sp500_constituents() → S&P 500 ticker 列表
    - fetch_company_facts(ticker) → 完整 Company Facts JSON
    - fetch_income(ticker) / fetch_balance(ticker) / fetch_cashflow(ticker) → 宽表 DataFrame
    """

    source_name = "sec_edgar"

    def __init__(self) -> None:
        super().__init__()
        self._rate_limiter = SECRateLimiter(
            rate=config.sec.rate_limit,
        )
        self._ticker_to_cik: dict[str, str] = {}
        self._cik_to_ticker: dict[str, str] = {}
        self._company_list_loaded = False

    # ── 公司列表 ──────────────────────────────────────────

    def fetch_company_list(self) -> pd.DataFrame:
        """获取所有 SEC 申报公司的 CIK ↔ ticker 映射。

        数据源：https://www.sec.gov/files/company_tickers.json
        本地缓存 7 天过期。

        Returns:
            DataFrame with columns: [cik, ticker]
        """
        cache_file = CACHE_DIR / "company_tickers.json"
        if self._load_cache(cache_file):
            data = json.loads(cache_file.read_text())
        else:
            logger.info("从 SEC 下载公司列表...")
            self._rate_limiter.wait()
            resp = self._request_sec(config.sec.ticker_url)
            self._save_cache(cache_file, json.dumps(resp))
            data = resp

        # 解析：data 是 { "0": {"cik": "0000320193", "ticker": "AAPL", ...}, ... }
        records = []
        for _idx, item in data.items():
            cik = str(item.get("cik", item.get("cik_str", ""))).strip().zfill(10)
            ticker = str(item["ticker"]).strip()
            title = str(item.get("title", "")).strip()
            records.append({"cik": cik, "ticker": ticker, "title": title})
            self._ticker_to_cik[ticker] = cik
            self._cik_to_ticker[cik] = ticker

        self._company_list_loaded = True
        logger.info("公司列表加载完成: %d 家", len(records))
        return pd.DataFrame(records)

    def fetch_sp500_constituents(self) -> list[str]:
        """获取 S&P 500 成分股 ticker 列表。

        数据源优先级：
        1. Wikipedia S&P 500 页面
        2. GitHub datasets/s-and-p-500-companies
        本地缓存 7 天过期。

        Returns:
            ticker 字符串列表，如 ["AAPL", "MSFT", ...]
        """
        cache_file = CACHE_DIR / "sp500_tickers.json"
        if self._load_cache(cache_file):
            tickers = json.loads(cache_file.read_text())
            logger.info("S&P 500 从缓存加载: %d 只", len(tickers))
            return tickers

        logger.info("获取 S&P 500 成分股...")

        # 方法1: Wikipedia
        try:
            resp = requests.get(config.sec.sp500_url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            tables = pd.read_html(resp.text)
            df = tables[0]
            ticker_col = None
            for col in df.columns:
                if "symbol" in str(col).lower() or "ticker" in str(col).lower():
                    ticker_col = col
                    break
            if ticker_col is None:
                ticker_col = df.columns[0]
            tickers = df[ticker_col].dropna().astype(str).str.strip().str.replace(
                r"\.", "-", regex=True
            ).tolist()
            tickers = list(dict.fromkeys(tickers))
            self._save_cache(cache_file, json.dumps(tickers))
            logger.info("S&P 500 成分股获取完成 (Wikipedia): %d 只", len(tickers))
            return tickers
        except Exception as e:
            logger.warning("Wikipedia 获取失败: %s，尝试 GitHub fallback", e)

        # 方法2: GitHub datasets/s-and-p-500-companies
        try:
            csv_url = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/master/data/constituents.csv"
            resp = requests.get(csv_url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            from io import StringIO
            df = pd.read_csv(StringIO(resp.text))
            # 列名通常是 "Symbol"
            ticker_col = None
            for col in df.columns:
                if "symbol" in str(col).lower() or "ticker" in str(col).lower():
                    ticker_col = col
                    break
            if ticker_col is None:
                ticker_col = df.columns[0]
            tickers = df[ticker_col].dropna().astype(str).str.strip().str.replace(
                r"\.", "-", regex=True
            ).tolist()
            tickers = list(dict.fromkeys(tickers))
            self._save_cache(cache_file, json.dumps(tickers))
            logger.info("S&P 500 成分股获取完成 (GitHub): %d 只", len(tickers))
            return tickers
        except Exception as e:
            logger.error("所有 S&P 500 数据源均失败: %s", e)
            raise

    def fetch_nasdaq100_constituents(self) -> list[str]:
        """获取 NASDAQ 100 成分股 ticker 列表。

        数据源优先级：
        1. Wikipedia NASDAQ-100 页面
        2. 内置 fallback 列表（data/nasdaq100_tickers.json）
        本地缓存 7 天过期。

        Returns:
            ticker 字符串列表，如 ["AAPL", "MSFT", ...]
        """
        cache_file = CACHE_DIR / "nasdaq100_tickers.json"
        if self._load_cache(cache_file):
            tickers = json.loads(cache_file.read_text())
            logger.info("NASDAQ 100 从缓存加载: %d 只", len(tickers))
            return tickers

        # 方法1: Wikipedia
        try:
            resp = requests.get(
                config.sec.nasdaq100_url, headers=HEADERS, timeout=30
            )
            resp.raise_for_status()
            tables = pd.read_html(resp.text)
            # NASDAQ-100 Wikipedia 有多张表，找包含 "Symbol" 或 "Ticker" 列的
            for df in tables:
                ticker_col = None
                for col in df.columns:
                    if "symbol" in str(col).lower() or "ticker" in str(col).lower():
                        ticker_col = col
                        break
                if ticker_col is None:
                    continue
                tickers = (
                    df[ticker_col]
                    .dropna()
                    .astype(str)
                    .str.strip()
                    # SEC company_tickers.json 使用 "-" 代替 "." (如 BRK-B 而非 BRK.B)，
                    # 因此 Wikipedia 中的 "." 需统一替换为 "-" 以匹配 SEC 的 ticker 格式
                    .str.replace(r"\.", "-", regex=True)
                    .tolist()
                )
                if len(tickers) >= 80:  # 合理的 NASDAQ-100 数量
                    tickers = list(dict.fromkeys(tickers))
                    self._save_cache(cache_file, json.dumps(tickers))
                    logger.info(
                        "NASDAQ 100 成分股获取完成 (Wikipedia): %d 只", len(tickers)
                    )
                    return tickers
            logger.warning("Wikipedia 表格解析未找到有效数据")
        except Exception as e:
            logger.warning("Wikipedia 获取 NASDAQ 100 失败: %s，尝试内置 fallback", e)

        # 方法2: 内置 JSON fallback
        fallback_path = Path(__file__).resolve().parent.parent / "data" / "nasdaq100_tickers.json"
        if fallback_path.exists():
            tickers = json.loads(fallback_path.read_text())
            self._save_cache(cache_file, json.dumps(tickers))
            logger.info("NASDAQ 100 从内置列表加载: %d 只", len(tickers))
            return tickers

        raise RuntimeError("NASDAQ 100 所有数据源均失败，请检查网络或内置 JSON 文件")

    def ticker_to_cik(self, ticker: str) -> str:
        """将 ticker 转为 10 位 CIK。"""
        if not self._company_list_loaded:
            self.fetch_company_list()
        cik = self._ticker_to_cik.get(ticker.upper())
        if not cik:
            raise ValueError(f"找不到 ticker {ticker} 对应的 CIK")
        return cik

    def cik_to_ticker(self, cik: str) -> str:
        """将 CIK 转为 ticker。"""
        if not self._company_list_loaded:
            self.fetch_company_list()
        ticker = self._cik_to_ticker.get(str(cik).strip().zfill(10))
        if not ticker:
            raise ValueError(f"找不到 CIK {cik} 对应的 ticker")
        return ticker

    # ── Company Facts ─────────────────────────────────────

    def fetch_company_facts(self, ticker: str) -> dict:
        """获取一家公司的完整 Company Facts 数据。

        本地缓存 7 天过期。

        Args:
            ticker: 股票代码，如 "AAPL"

        Returns:
            SEC Company Facts JSON dict
        """
        cik = self.ticker_to_cik(ticker)
        cache_file = CACHE_DIR / f"{ticker}.json"

        if self._load_cache(cache_file):
            data = json.loads(cache_file.read_text())
            logger.debug("Company Facts 缓存命中: %s", ticker)
            return data

        url = config.sec.base_url.format(cik=cik)
        logger.info("拉取 Company Facts: %s (CIK=%s)...", ticker, cik)
        self._rate_limiter.wait()
        data = self._request_sec(url)

        self._save_cache(cache_file, json.dumps(data))
        logger.info("Company Facts 拉取完成: %s", ticker)
        return data

    # ── 三大报表提取（从 Company Facts 中提取宽表）───────

    # 利润表标签
    INCOME_TAGS: dict[str, str] = {
        "Revenues": "revenues",
        "SalesRevenueNet": "revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax": "revenues",
        "CostOfGoodsAndServicesSold": "cost_of_goods_sold",
        "CostOfRevenue": "cost_of_goods_sold",
        "CostOfGoodsSold": "cost_of_goods_sold",
        "GrossProfit": "gross_profit",
        "OperatingExpenses": "operating_expenses",
        "SellingGeneralAndAdministrativeExpenses": "selling_general_admin",
        "ResearchAndDevelopmentExpense": "research_and_development",
        "DepreciationAndAmortization": "depreciation_amortization",
        "OperatingIncomeLoss": "operating_income",
        "InterestExpense": "interest_expense",
        "InterestExpenseDebt": "interest_expense",
        "InterestExpenseOnDebt": "interest_expense",
        "InterestIncome": "interest_income",
        "InvestmentIncomeInterest": "interest_income",
        "OtherIncomeExpense": "other_income_expense",
        "OtherNonOperatingIncomeExpense": "other_income_expense",
        "IncomeBeforeTax": "income_before_tax",
        "IncomeTaxExpenseBenefit": "income_tax_expense",
        "NetIncomeLoss": "net_income",
        "NetIncomeAvailableToCommonStockholdersBasic": "net_income_common",
        "PreferredStockDividendsAndOtherAdjustments": "preferred_dividends",
        "EarningsPerShareBasic": "eps_basic",
        "EarningsPerShareDiluted": "eps_diluted",
        "WeightedAverageNumberOfSharesOutstandingBasic": "weighted_avg_shares_basic",
        "WeightedAverageNumberOfDilutedSharesOutstanding": "weighted_avg_shares_diluted",
        "OtherComprehensiveIncomeLossNetOfTax": "other_comprehensive_income",
        "ComprehensiveIncomeNetOfTax": "comprehensive_income",
    }

    # 资产负债表标签
    BALANCE_TAGS: dict[str, str] = {
        "CashAndCashEquivalentsAtCarryingValue": "cash_and_equivalents",
        "CashCashEquivalentsAndShortTermInvestments": "cash_and_equivalents",
        "CashAndCashEquivalents": "cash_and_equivalents",
        "ShortTermInvestments": "short_term_investments",
        "AccountsReceivableNetCurrent": "accounts_receivable_net",
        "ReceivablesNetCurrent": "accounts_receivable_net",
        "InventoryNet": "inventory_net",
        "PrepaidAssetsCurrent": "prepaid_assets",
        "OtherAssetsCurrent": "other_current_assets",
        "AssetsCurrent": "total_current_assets",
        "Investments": "long_term_investments",
        "LongTermInvestments": "long_term_investments",
        "PropertyPlantAndEquipmentNet": "property_plant_equipment",
        "Goodwill": "goodwill",
        "IntangibleAssetsNet": "intangible_assets_net",
        "OperatingLeaseRightOfUseAsset": "operating_right_of_use",
        "DeferredTaxAssetsNet": "deferred_tax_assets",
        "OtherNonCurrentAssets": "other_non_current_assets",
        "AssetsNoncurrent": "total_non_current_assets",
        "Assets": "total_assets",
        "AccountsPayableCurrent": "accounts_payable",
        "AccruedLiabilitiesCurrent": "accrued_liabilities",
        "ShortTermBorrowings": "short_term_debt",
        "CurrentOperatingLeaseLiability": "current_operating_lease",
        "OtherLiabilitiesCurrent": "other_current_liabilities",
        "LiabilitiesCurrent": "total_current_liabilities",
        "LongTermDebt": "long_term_debt",
        "LongTermDebtNoncurrent": "long_term_debt",
        "DebtNoncurrent": "long_term_debt",
        "NoncurrentOperatingLeaseLiability": "non_current_operating_lease",
        "DeferredTaxLiabilitiesNet": "deferred_tax_liabilities",
        "OtherLiabilitiesNoncurrent": "other_non_current_liabilities",
        "LiabilitiesNoncurrent": "total_non_current_liabilities",
        "Liabilities": "total_liabilities",
        "PreferredStockValue": "preferred_stock",
        "CommonStockValue": "common_stock",
        "CommonStocksIncludingAdditionalPaidInCapital": "common_stock",
        "AdditionalPaidInCapital": "additional_paid_in_capital",
        "RetainedEarningsAccumulatedDeficit": "retained_earnings",
        "AccumulatedOtherComprehensiveIncomeLossNetOfTax": "accumulated_other_ci",
        "TreasuryStockValue": "treasury_stock",
        "NoncontrollingInterest": "noncontrolling_interest",
        "StockholdersEquity": "total_equity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest": "total_equity_including_nci",
    }

    # 现金流量表标签
    CASHFLOW_TAGS: dict[str, str] = {
        "NetIncomeLoss": "net_income_cf",
        "DepreciationAndAmortization": "depreciation_amortization",
        "ShareBasedCompensation": "stock_based_compensation",
        "DeferredIncomeTaxExpenseBenefit": "deferred_income_tax",
        "ChangesInWorkingCapital": "changes_in_working_capital",
        # Operating cash flow - multiple aliases
        "CashFlowFromContinuingOperatingActivities": "net_cash_from_operations",
        "NetCashProvidedByUsedInOperatingActivities": "net_cash_from_operations",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations": "net_cash_from_operations",
        "OperatingCashFlow": "net_cash_from_operations",
        # Capital expenditures - multiple aliases (SEC uses PaymentsToAcquirePropertyPlantAndEquipment most commonly)
        "CapitalExpenditures": "capital_expenditures",
        "PaymentsToAcquirePropertyPlantAndEquipment": "capital_expenditures",
        "PaymentsToAcquirePropertyPlantAndEquipmentNetOfAccumulatedDepreciationAndAmortization": "capital_expenditures",
        "CapitalExpendituresIncurredButNotYetPaid": "capital_expenditures",
        "PaymentsToAcquireProductiveAssets": "capital_expenditures",
        # Acquisitions
        "PaymentsToAcquireBusinessesNetOfCashAcquired": "acquisitions",
        # Investments
        "PurchaseOfInvestments": "investment_purchases",
        "PaymentsToAcquireAvailableForSaleSecurities": "investment_purchases",
        "PaymentsToAcquireOtherInvestments": "investment_purchases",
        "ProceedsFromMaturitiesOfInvestments": "investment_maturities",
        "ProceedsFromSaleAndMaturityOfOtherInvestments": "investment_maturities",
        "ProceedsFromMaturitiesPrepaymentsAndCallsOfAvailableForSaleSecurities": "investment_maturities",
        "OtherCashPaymentsFromInvestingActivities": "other_investing_activities",
        "PaymentsForProceedsFromOtherInvestingActivities": "other_investing_activities",
        # Investing cash flow - multiple aliases
        "NetCashProvidedByUsedInInvestingActivities": "net_cash_from_investing",
        "NetCashProvidedByUsedInInvestingActivitiesContinuingOperations": "net_cash_from_investing",
        "NetCashUsedInInvestingActivities": "net_cash_from_investing",
        # Financing - debt
        "ProceedsFromIssuanceOfDebt": "debt_issued",
        "RepaymentsOfDebt": "debt_repaid",
        "RepaymentsOfLongTermDebt": "debt_repaid",
        "ProceedsFromRepaymentsOfLongTermDebtAndCapitalSecurities": "debt_repaid",
        "RepaymentsOfLongTermDebtAndCapitalSecurities": "debt_repaid",
        # Share buyback - multiple aliases
        "PaymentsForRepurchaseOfCommonStock": "share_buyback",
        "PaymentsForRepurchaseOfCommonStockNetOfTreasurySharesAcquired": "share_buyback",
        # Dividends - multiple aliases
        "PaymentsOfDividends": "dividends_paid",
        "PaymentsOfDividendsCommonStock": "dividends_paid",
        "DividendsPaid": "dividends_paid",
        "DividendsDeclaredCash": "dividends_paid",
        # Other financing
        "OtherCashPaymentsFromFinancingActivities": "other_financing_activities",
        "ProceedsFromPaymentsForOtherFinancingActivities": "other_financing_activities",
        # Financing cash flow
        "NetCashProvidedByUsedInFinancingActivities": "net_cash_from_financing",
        "NetCashProvidedByUsedInFinancingActivitiesContinuingOperations": "net_cash_from_financing",
        # Exchange rate effects
        "EffectOfExchangeRateOnCashAndCashEquivalents": "effect_of_exchange_rate",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseIncludingExchangeRateEffect": "effect_of_exchange_rate",
        # Net change in cash
        "IncreaseDecreaseInCashAndCashEquivalents": "net_change_in_cash",
        "CashAndCashEquivalentsPeriodIncreaseDecrease": "net_change_in_cash",
        # Ending cash
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents": "cash_ending",
        "CashAndCashEquivalentsAtCarryingValue": "cash_ending",
        # Beginning cash
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsBeginningOfPeriod": "cash_beginning",
        # Free cash flow (rarely reported, usually calculated)
        "FreeCashFlow": "free_cash_flow",
    }

    def extract_table(self, facts: dict, tag_mapping: dict[str, str]) -> pd.DataFrame:
        """从 Company Facts 中提取某张报表的数据，返回宽表 DataFrame。

        每家公司只调用一次 fetch_company_facts，然后用此方法分别提取三大表。

        Args:
            facts: Company Facts JSON dict
            tag_mapping: {SEC标签: 标准字段名} 映射

        Returns:
            宽表 DataFrame，index 为 (end, fp)，列为各标准字段
        """
        import re as _re

        usgaap = facts.get("facts", {}).get("us-gaap", {})
        if not usgaap:
            return pd.DataFrame()

        # 收集所有标签的数据
        records = []
        for tag, field_name in tag_mapping.items():
            if tag in usgaap:
                for entry in usgaap[tag].get("units", {}).get("USD", []):
                    fp = entry.get("fp", "")
                    frame = str(entry.get("frame", ""))

                    # 优先使用 frame 修正 fp
                    if frame:
                        frame_match = _re.search(r"Q(\d)$", frame)
                        if frame_match:
                            fp = f"Q{frame_match.group(1)}"
                        elif "CY" in frame and "Q" not in frame:
                            fp = "FY"
                    # 记录是否 frame 有明确的季度指示
                    records.append({
                        "tag": tag,
                        "field": field_name,
                        "val": entry.get("val"),
                        "fy": entry.get("fy"),
                        "fp": fp,
                        "end": entry.get("end"),
                        "filed": entry.get("filed"),
                        "accn": entry.get("accn"),
                        "frame": frame,
                        "_frame_has_q": "Q" in frame,
                    })

        if not records:
            return pd.DataFrame()

        df = pd.DataFrame(records)

        # ── 关键去重：修正 fp=FY 但实际是季度数据的情况 ──
        # 策略：对同一 (tag, end)，如果存在 _frame_has_q=True 的条目，
        # 则将 fp=FY 且 _frame_has_q=False 的条目的 fp 修正为对应季度
        for (tag_key, end_key), grp in df.groupby(["tag", "end"]):
            # 找到有明确 frame 季度的条目
            frame_q_rows = grp[grp["_frame_has_q"]]
            if not frame_q_rows.empty:
                # 获取这些条目的 fp 值
                correct_fps = set(frame_q_rows["fp"])
                if correct_fps:
                    # 将同 (tag, end) 下 fp=FY 且无 frame 季度指示的条目标记
                    mask = (df["tag"] == tag_key) & (df["end"] == end_key) & \
                           (df["fp"] == "FY") & (~df["_frame_has_q"])
                    # 用 frame_q 的 fp 值修正（取第一个）
                    correct_fp = list(correct_fps)[0]
                    df.loc[mask, "fp"] = correct_fp

        # 当同一 (tag, end, fp) 有多个条目时，取最新 filed 的
        df = df.sort_values("filed").drop_duplicates(
            subset=["tag", "end", "fp"], keep="last"
        )

        df = df.dropna(subset=["val"])

        wide = df.pivot_table(
            index=["end", "fp", "filed", "accn"],
            columns="field",
            values="val",
            aggfunc="first",
        ).reset_index()

        # FY/Q4 去重：同一 end 日期，保留 FY（而非 Q4）
        wide["_date"] = pd.to_datetime(wide["end"])
        wide["_fp_order"] = wide["fp"].map({"FY": 0, "Q4": 1, "Q3": 2, "Q2": 3, "Q1": 4})
        wide = wide.sort_values(["_date", "_fp_order", "filed"])
        wide = wide.drop_duplicates(subset=["end", "fp"], keep="last")

        # ── 自动计算 free_cash_flow（如果 tag_mapping 是 CASHFLOW_TAGS）──
        # 如果 free_cash_flow 为空，但有 net_cash_from_operations 和 capital_expenditures，
        # 则计算 FCF = CFO - CapEx
        # 注意：CapEx 通常是负数（现金流出），但计算 FCF 时应使用绝对值
        if "free_cash_flow" in tag_mapping.values():
            # 确保 free_cash_flow 列存在
            if "free_cash_flow" not in wide.columns:
                wide["free_cash_flow"] = pd.Series(dtype=float)
            
            # 只在 free_cash_flow 为空的行计算
            mask = wide["free_cash_flow"].isna()
            if mask.any():
                cfo = wide.get("net_cash_from_operations")
                capex = wide.get("capital_expenditures")
                
                if cfo is not None and capex is not None:
                    # 计算逻辑：FCF = CFO - CapEx（CapEx 通常是负数，所以实际上是加）
                    # 如果 CapEx 是正数，表示现金流入（出售资产），此时应该用负值
                    # 但根据 SEC 标准，CapEx 通常是负数（现金流出）
                    calculated_fcf = cfo - capex
                    
                    # 只更新之前为空的值
                    wide.loc[mask, "free_cash_flow"] = calculated_fcf[mask]

        return wide

    def fetch_income(self, ticker: str) -> pd.DataFrame:
        """获取利润表宽表。"""
        facts = self.fetch_company_facts(ticker)
        return self.extract_table(facts, self.INCOME_TAGS)

    def fetch_balance(self, ticker: str) -> pd.DataFrame:
        """获取资产负债表宽表。"""
        facts = self.fetch_company_facts(ticker)
        return self.extract_table(facts, self.BALANCE_TAGS)

    def fetch_cashflow(self, ticker: str) -> pd.DataFrame:
        """获取现金流量表宽表。"""
        facts = self.fetch_company_facts(ticker)
        return self.extract_table(facts, self.CASHFLOW_TAGS)

    # ── 内部工具方法 ──────────────────────────────────────

    def _request_sec(self, url: str) -> dict:
        """向 SEC API 发送请求，带限流和重试。

        Returns:
            JSON dict
        """
        resp = requests.get(url, headers=HEADERS, timeout=30)
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", "10"))
            logger.warning("SEC 429 限流，等待 %ds...", retry_after)
            time.sleep(retry_after)
            self._rate_limiter.wait()
            resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _load_cache(self, cache_file: Path) -> bool:
        """检查缓存文件是否存在且未过期。"""
        if not cache_file.exists():
            return False
        age = time.time() - cache_file.stat().st_mtime
        if age > config.sec.cache_ttl_days * 86400:
            return False
        return True

    def _save_cache(self, cache_file: Path, content: str) -> None:
        """保存内容到缓存文件。"""
        try:
            cache_file.write_text(content, encoding="utf-8")
        except Exception as exc:
            logger.warning("缓存写入失败: %s", exc)


# ═══════════════════════════════════════════════════════════
# 自测
# ═══════════════════════════════════════════════════════════
if __name__ == "__main__":
    import os
    os.environ["TQDM_DISABLE"] = "1"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    fetcher = USFinancialFetcher()

    # 测试限流器
    print("\n=== 测试限流器 ===")
    rl = SECRateLimiter(rate=10)
    t0 = time.time()
    for i in range(12):
        rl.wait()
        print(f"  请求 {i+1}: {time.time()-t0:.3f}s")
    print(f"12 次请求耗时: {time.time()-t0:.2f}s（应 >1s）")

    # 测试公司列表
    print("\n=== 测试公司列表 ===")
    company_df = fetcher.fetch_company_list()
    print(f"总计: {len(company_df)} 家")
    print(f"AAPL CIK: {fetcher.ticker_to_cik('AAPL')}")
    print(f"MSFT CIK: {fetcher.ticker_to_cik('MSFT')}")

    # 测试 S&P 500
    print("\n=== 测试 S&P 500 ===")
    sp500 = fetcher.fetch_sp500_constituents()
    print(f"S&P 500 成分股: {len(sp500)} 只")
    print(f"前10只: {sp500[:10]}")

    # 测试 AAPL Company Facts
    print("\n=== 测试 AAPL Company Facts ===")
    facts = fetcher.fetch_company_facts("AAPL")
    usgaap_tags = list(facts.get("facts", {}).get("us-gaap", {}).keys())
    print(f"US-GAAP 标签数: {len(usgaap_tags)}")
    print(f"前10个标签: {usgaap_tags[:10]}")
