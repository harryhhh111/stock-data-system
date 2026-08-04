"""
fetchers/us_financial.py — 美股 SEC EDGAR 数据拉取

提供：
- SECRateLimiter: 滑动窗口限流器（10次/秒）
- USFinancialFetcher: 继承 BaseFetcher，获取 SEC Company Facts 数据
- fetch_company_list(): 获取 CIK ↔ ticker 映射
- fetch_sp500_constituents(): 获取 S&P 500 成分股列表
- fetch_nasdaq100_constituents(): 获取 NASDAQ 100 成分股列表
- fetch_russell1000_constituents(): 获取 Russell 1000 成分股列表
- get_tickers_by_index(): 根据指数名称获取成分股（支持 SP500/NASDAQ100/RUSSELL1000/ALL）
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import psycopg2.extras
import requests

import config
from core.us_financial_versioning import (
    USFactVersionWriter,
    classify_record,
    compute_context_hash,
    compute_value_hash,
    derive_filing_meta,
    reject_reason,
    split_value,
)
from db import Connection, get_or_create_raw_snapshot_version, save_raw_snapshot_observation

from .base import BaseFetcher, retry_with_backoff

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FetchContext:
    """一次 SEC 抓取的不可变上下文，随 facts 显式传递，不依赖 fetcher 状态。"""

    stock_code: str
    cik: str
    snapshot_id: int
    content_hash: str


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
# Period classification（start/end 第一判据，frame 仅佐证）
# ═══════════════════════════════════════════════════════════

def _classify_period(start: str | None, end: str | None, frame: str) -> tuple[str, str | None]:
    """以 SEC fact 的 start/end 判定 period kind，frame 仅作佐证。

    规则:
        start 缺失 + end 存在  → instant（资产负债表时点）
        start 存在 + end 存在  → duration（利润表/现金流期间）
        start 缺失 + end 缺失  → invalid（缺少期间信息）
        start 存在 + end 缺失  → invalid（期间不完整）

    frame 佐证:
        instant + frame=~Q#I$  → 一致
        duration + frame=~Q#$  → 一致
        冲突                   → FRAME_PERIOD_CONFLICT
        无 frame               → 允许

    Returns:
        (period_kind, quality_flag or None)
        period_kind ∈ {"instant", "duration", "invalid"}
    """
    has_start = bool(start)
    has_end = bool(end)

    if not has_start and not has_end:
        period_kind = "invalid"
    elif has_start and not has_end:
        period_kind = "invalid"
    elif not has_start and has_end:
        period_kind = "instant"
    else:
        period_kind = "duration"

    # frame 佐证检查（仅对有效 period_kind 进行）
    quality_flag = None
    if period_kind != "invalid" and frame:
        frame_is_instant = bool(frame) and bool(re.search(r"Q\d+I$", frame))
        frame_is_duration = bool(frame) and bool(re.search(r"Q\d+$", frame)) and not frame.endswith("I")
        if period_kind == "instant" and frame_is_duration:
            quality_flag = "FRAME_PERIOD_CONFLICT"
        elif period_kind == "duration" and frame_is_instant:
            quality_flag = "FRAME_PERIOD_CONFLICT"

    return period_kind, quality_flag


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

    # Fields that exist in both cumulative and standalone (single-quarter) versions.
    # When SEC returns multiple entries for the same (tag, end, fp) with different
    # start dates, the earliest start is cumulative; later starts are standalone.
    STANDALONE_FIELDS: set[str] = {
        "revenues", "cost_of_goods_sold", "gross_profit", "operating_expenses",
        "selling_general_admin", "research_and_development", "depreciation_amortization",
        "operating_income", "interest_expense", "interest_income", "other_income_expense",
        "income_before_tax", "income_tax_expense", "net_income", "net_income_common",
        "preferred_dividends", "eps_basic", "eps_diluted", "weighted_avg_shares_basic",
        "weighted_avg_shares_diluted", "other_comprehensive_income", "comprehensive_income",
        "net_income_cf", "stock_based_compensation", "deferred_income_tax",
        "changes_in_working_capital", "net_cash_from_operations", "capital_expenditures",
        "acquisitions", "investment_purchases", "investment_maturities",
        "other_investing_activities", "net_cash_from_investing", "debt_issued",
        "debt_repaid", "equity_issued", "share_buyback", "dividends_paid",
        "other_financing_activities", "net_cash_from_financing", "effect_of_exchange_rate",
    }

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
            data = resp.json()
            self._save_cache(cache_file, json.dumps(data))

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
            tables = pd.read_html(io.StringIO(resp.text))
            df = tables[0]
            ticker_col = None
            for col in df.columns:
                if "symbol" in str(col).lower() or "ticker" in str(col).lower():
                    ticker_col = col
                    break
            if ticker_col is None:
                ticker_col = df.columns[0]
            tickers = (
                df[ticker_col]
                .dropna()
                .astype(str)
                .str.strip()
                .str.replace(r"\.", "-", regex=True)
                .tolist()
            )
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
            tickers = (
                df[ticker_col]
                .dropna()
                .astype(str)
                .str.strip()
                .str.replace(r"\.", "-", regex=True)
                .tolist()
            )
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
            resp = requests.get(config.sec.nasdaq100_url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            tables = pd.read_html(io.StringIO(resp.text))
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
        fallback_path = (
            Path(__file__).resolve().parent.parent.parent / "data" / "nasdaq100_tickers.json"
        )
        if fallback_path.exists():
            tickers = json.loads(fallback_path.read_text())
            self._save_cache(cache_file, json.dumps(tickers))
            logger.info("NASDAQ 100 从内置列表加载: %d 只", len(tickers))
            return tickers

        raise RuntimeError("NASDAQ 100 所有数据源均失败，请检查网络或内置 JSON 文件")

    def fetch_russell1000_constituents(self) -> list[str]:
        """获取 Russell 1000 成分股 ticker 列表。

        数据源：Wikipedia Russell 1000 页面。
        本地缓存 7 天过期。

        Returns:
            ticker 字符串列表，如 ["AAPL", "MSFT", ...]
        """
        cache_file = CACHE_DIR / "russell1000_tickers.json"
        if self._load_cache(cache_file):
            tickers = json.loads(cache_file.read_text())
            logger.info("Russell 1000 从缓存加载: %d 只", len(tickers))
            return tickers

        logger.info("获取 Russell 1000 成分股...")

        try:
            resp = requests.get(config.sec.russell1000_url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            tables = pd.read_html(io.StringIO(resp.text))
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
                    .str.replace(r"\.", "-", regex=True)
                    .tolist()
                )
                if len(tickers) >= 800:  # 合理的 Russell 1000 数量
                    tickers = list(dict.fromkeys(tickers))
                    self._save_cache(cache_file, json.dumps(tickers))
                    logger.info("Russell 1000 成分股获取完成 (Wikipedia): %d 只", len(tickers))
                    return tickers
            logger.warning("Wikipedia 表格解析未找到有效数据")
        except Exception as e:
            logger.error("获取 Russell 1000 失败: %s", e)

        raise RuntimeError("Russell 1000 所有数据源均失败，请检查网络")

    def get_tickers_by_index(self, index_name: str) -> list[str]:
        """根据指数名称获取成分股 ticker 列表。

        Args:
            index_name: 指数名称，支持 SP500 / NASDAQ100 / RUSSELL1000 / ALL

        Returns:
            ticker 字符串列表（去重）
        """
        name = index_name.upper()
        if name == "SP500":
            return self.fetch_sp500_constituents()
        elif name == "NASDAQ100":
            return self.fetch_nasdaq100_constituents()
        elif name == "RUSSELL1000":
            return self.fetch_russell1000_constituents()
        elif name == "ALL":
            sp500 = set(self.fetch_sp500_constituents())
            nasdaq100 = set(self.fetch_nasdaq100_constituents())
            russell1000 = set(self.fetch_russell1000_constituents())
            all_tickers = sorted(sp500 | nasdaq100 | russell1000)
            logger.info("ALL 模式合并: SP500(%d) + NASDAQ100(%d) + Russell1000(%d) = %d 去重",
                        len(sp500), len(nasdaq100), len(russell1000), len(all_tickers))
            return all_tickers
        else:
            raise ValueError(f"不支持的指数: {index_name}，可选: SP500, NASDAQ100, RUSSELL1000, ALL")

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

    def fetch_company_facts_with_context(
        self,
        ticker: str,
        *,
        allow_cache: bool = True,
    ) -> tuple[dict, FetchContext]:
        """获取 Company Facts，并返回不可变的 fetch context。

        context 包含 snapshot_id、content_hash、stock_code、cik，
        必须显式传给 extract_table() 以建立版本追溯，避免依赖 fetcher 可变状态。
        """
        cik = self.ticker_to_cik(ticker)
        cache_file = CACHE_DIR / f"{ticker}.json"

        fetched_at = datetime.now()
        http_status: int | None = None
        source_last_modified: str | None = None
        fetch_source = "cache"

        if allow_cache and self._load_cache(cache_file):
            data = json.loads(cache_file.read_text())
            logger.debug("Company Facts 缓存命中: %s", ticker)
        else:
            url = config.sec.base_url.format(cik=cik)
            logger.info("拉取 Company Facts: %s (CIK=%s)...", ticker, cik)
            self._rate_limiter.wait()
            resp = self._request_sec(url)
            data = resp.json()
            http_status = resp.status_code
            source_last_modified = resp.headers.get("Last-Modified")
            fetch_source = "network"
            self._save_cache(cache_file, json.dumps(data))
            logger.info("Company Facts 拉取完成: %s", ticker)

        content_hash = self._compute_content_hash(data)
        try:
            snapshot_id = get_or_create_raw_snapshot_version(
                stock_code=ticker,
                data_type="company_facts",
                source="sec_edgar",
                api_params={},
                content_hash=content_hash,
                raw_data=data,
                fetched_at=fetched_at,
                source_last_modified=source_last_modified,
                parser_status="pending",
            )
            save_raw_snapshot_observation(
                snapshot_id=snapshot_id,
                fetched_at=fetched_at,
                http_status=http_status,
                source_last_modified=source_last_modified,
                fetch_source=fetch_source,
            )
        except Exception as exc:
            logger.error(
                "%s: raw_snapshot_version/observation 写入失败 (content_hash=%s): %s",
                ticker, content_hash, exc,
            )
            raise

        context = FetchContext(
            stock_code=ticker.upper(),
            cik=cik.zfill(10),
            snapshot_id=snapshot_id,
            content_hash=content_hash,
        )
        return data, context

    def fetch_company_facts(self, ticker: str) -> dict:
        """兼容旧接口：只返回 Company Facts dict（不带 context）。"""
        data, _ctx = self.fetch_company_facts_with_context(ticker)
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
        "SellingGeneralAndAdministrativeExpense": "selling_general_admin",
        "ResearchAndDevelopmentExpense": "research_and_development",
        "DepreciationAndAmortization": "depreciation_amortization",
        "DepreciationDepletionAndAmortization": "depreciation_amortization",
        "Depreciation": "depreciation_amortization",
        "AmortizationOfIntangibleAssets": "amortization_of_intangibles",
        "OperatingIncomeLoss": "operating_income",
        "ProfitLoss": "operating_income",
        "InterestExpense": "interest_expense",
        "InterestExpenseDebt": "interest_expense",
        "InterestExpenseOnDebt": "interest_expense",
        "InterestIncome": "interest_income",
        "InvestmentIncomeInterest": "interest_income",
        "OtherIncomeExpense": "other_income_expense",
        "OtherNonOperatingIncomeExpense": "other_income_expense",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments": "income_before_tax",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest": "income_before_tax",
        "IncomeBeforeTax": "income_before_tax",
        "IncomeTaxExpenseBenefit": "income_tax_expense",
        "NetIncomeLoss": "net_income",
        "NetIncomeLossAvailableToCommonStockholdersBasic": "net_income_common",
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
        "CommercialPaper": "short_term_debt",
        "CurrentOperatingLeaseLiability": "current_operating_lease",
        "OtherLiabilitiesCurrent": "other_current_liabilities",
        "LiabilitiesCurrent": "total_current_liabilities",
        "LongTermDebtNoncurrent": "long_term_debt",
        "LongTermDebt": "long_term_debt",
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
        "DepreciationDepletionAndAmortization": "depreciation_amortization",
        "Depreciation": "depreciation_amortization",
        "AmortizationOfIntangibleAssets": "amortization_of_intangibles",
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
        "PaymentsToAcquireOtherProductiveAssets": "capital_expenditures",
        "PaymentsToAcquireProductiveAssets": "capital_expenditures",
        # Cash CapEx tags disclosed by industry-specific issuers (e.g. REITs, utilities, oil & gas)
        "PaymentsForCapitalImprovements": "capital_expenditures",
        "PaymentsToAcquireBuildings": "capital_expenditures",
        "PaymentsToAcquireWasteWaterSystems": "capital_expenditures",
        "PaymentsToAcquireOilAndGasPropertyAndEquipment": "capital_expenditures",
        "PaymentsToAcquireOilAndGasProperty": "capital_expenditures",
        "PaymentsToAcquireOtherPropertyPlantAndEquipment": "capital_expenditures",
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
        "PaymentsOfOrdinaryDividends": "dividends_paid",
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

    def _extract_facts(
        self,
        facts: dict,
        tag_mapping: dict[str, str],
        statement: str | None = None,
    ) -> tuple[list[dict], list[dict], list[dict]]:
        """从 Company Facts 中提取原始 fact 记录。

        Returns:
            (records, invalid_records, fact_records)
            records: 用于构建宽表 DataFrame
            invalid_records: period invalid 被隔离的记录
            fact_records: 用于写入 us_financial_fact_version 的原始事实
        """
        import re as _re

        usgaap = facts.get("facts", {}).get("us-gaap", {})
        if not usgaap:
            return [], [], []

        if statement is None:
            statement = self._infer_statement(tag_mapping)

        _KEEP_UNITS = {"USD", "USD/shares", "shares"}
        records = []
        fact_records: list[dict] = []
        invalid_records: list[dict] = []

        for tag, field_name in tag_mapping.items():
            if tag not in usgaap:
                continue
            for unit_name, entries in usgaap[tag].get("units", {}).items():
                if unit_name not in _KEEP_UNITS:
                    continue
                for entry in entries:
                    fp_raw = entry.get("fp", "")
                    fp = fp_raw
                    frame = str(entry.get("frame", ""))
                    start = entry.get("start")
                    form = entry.get("form", "")

                    end_val = entry.get("end")
                    _period_kind, _quality_flag = _classify_period(start, end_val, frame)

                    if _period_kind == "invalid":
                        invalid_records.append({
                            "tag": tag,
                            "field": field_name,
                            "fp": fp,
                            "start": start,
                            "end": end_val,
                            "frame": frame,
                            "form": form,
                            "filed": entry.get("filed"),
                            "accn": entry.get("accn"),
                        })
                        logger.warning(
                            "INVALID_PERIOD 隔离: tag=%s field=%s start=%s end=%s fp=%s frame=%s form=%s accn=%s",
                            tag, field_name, start, end_val, fp, frame, form,
                            entry.get("accn", ""),
                        )
                        continue

                    _frame_has_q = "Q" in frame
                    _frame_is_instant = _period_kind == "instant"

                    if frame and _period_kind == "duration":
                        frame_match = _re.search(r"Q(\d+)$", frame)
                        if frame_match and fp in ("FY", "", None):
                            fp = f"Q{frame_match.group(1)}"
                        elif not frame_match and "CY" in frame and not _frame_has_q:
                            fp = "FY"

                    records.append({
                        "tag": tag,
                        "field": field_name,
                        "val": entry.get("val"),
                        "fy": entry.get("fy"),
                        "fp": fp,
                        "end": entry.get("end"),
                        "start": start,
                        "filed": entry.get("filed"),
                        "accn": entry.get("accn"),
                        "frame": frame,
                        "form": form,
                        "_frame_has_q": _frame_has_q,
                        "_frame_is_instant": _frame_is_instant,
                        "_period_kind": _period_kind,
                        "_quality_flag": _quality_flag,
                    })
                    fact_records.append({
                        "tag": tag,
                        "field": field_name,
                        "unit": unit_name,
                        "val": entry.get("val"),
                        "fy": entry.get("fy"),
                        "fp": fp_raw,
                        "start": start,
                        "end": end_val,
                        "filed": entry.get("filed"),
                        "accn": entry.get("accn"),
                        "frame": frame,
                        "form": form,
                        "_period_kind": _period_kind,
                        "_quality_flag": _quality_flag,
                        "dimensions": entry.get("dimensions", {}),
                    })

        return records, invalid_records, fact_records

    def extract_table(
        self,
        facts: dict,
        tag_mapping: dict[str, str],
        context: FetchContext | None = None,
        statement: str | None = None,
    ) -> pd.DataFrame:
        """从 Company Facts 中提取某张报表的数据，返回宽表 DataFrame。

        同时把原始 fact 写入不可变版本层（us_filing + us_financial_fact_version）。
        """
        records, invalid_records, fact_records = self._extract_facts(
            facts, tag_mapping, statement=statement
        )

        if statement is None:
            statement = self._infer_statement(tag_mapping)

        # ── Filing XBRL fallback：补充 Company Facts 中缺失的字段 ──
        if statement == "balance" and context is not None:
            records, fact_records = self._supplement_total_liabilities_records(
                records, fact_records, context
            )

        usgaap = facts.get("facts", {}).get("us-gaap", {})

        if invalid_records:
            logger.warning(
                "extract_table: %d 条 invalid period 记录已隔离（未进入宽表），涉及 tag: %s",
                len(invalid_records),
                sorted(set(r["tag"] for r in invalid_records)),
            )

        # ── 不可变版本层双写（失败不阻塞旧宽表）──
        if context is not None and fact_records:
            facts_hash = self._compute_content_hash(facts)
            if facts_hash != context.content_hash:
                logger.error(
                    "%s %s: facts content_hash 与 context 不匹配（版本层跳过）: %s vs %s",
                    context.stock_code, statement, facts_hash, context.content_hash,
                )
            else:
                try:
                    self._write_version_layer(
                        fact_records,
                        invalid_records=invalid_records,
                        statement=statement,
                        context=context,
                    )
                except Exception as exc:
                    logger.error(
                        "%s %s: 不可变版本层写入失败（旧宽表继续写入）: %s",
                        context.stock_code, statement, exc,
                    )

        if not records:
            return pd.DataFrame()

        df = pd.DataFrame(records)

        # ── 关键去重：修正 fp=FY 但实际是季度数据的情况 ──
        # 某些公司（如 MELI）改财年后，SEC 把季度数据的 fp 标为 FY，
        # 但 frame 字段（如 CY2017Q1）能正确标识。
        # 两种情况：
        #   A) 同一 (tag, end) 有 frame=CY20xx（年度）也有 CY20xxQ4（季度）
        #      → 保留 FY 和 Q4 各自独立
        #   B) 同一 (tag, end) 只有 frame=CY20xxQ?（季度），没有纯年度 frame
        #      → 只修正 frame 含 Q 的 FY 条目为对应季度（保留 frame=None 的真正年报）
        #   C) frame 含 QxI 后缀（如 CY2025Q3I）表示 interim，正则需匹配
        # 排除 instant frame（Q4I），这些是 10-K 年报资产负债表，应保持 FY
        fy_needs_fix = (
            (df["fp"] == "FY") & (df["_frame_has_q"]) & (~df["_frame_is_instant"])
        )
        if fy_needs_fix.any():
            tags_with_fix = df.loc[fy_needs_fix, ["tag", "end"]].drop_duplicates()
            for _, row in tags_with_fix.iterrows():
                tag_key, end_key = row["tag"], row["end"]
                grp = df[(df["tag"] == tag_key) & (df["end"] == end_key)]
                frame_q_rows = grp[grp["_frame_has_q"]]
                has_annual_frame = (
                    grp["frame"]
                    .apply(lambda f: bool(f) and "CY" in str(f) and "Q" not in str(f))
                    .any()
                )
                if frame_q_rows.empty or has_annual_frame:
                    continue
                correct_fps = set(frame_q_rows["fp"])
                if correct_fps:
                    correct_fp = list(correct_fps)[0]
                    mask = (
                        (df["tag"] == tag_key)
                        & (df["end"] == end_key)
                        & (df["fp"] == "FY")
                        & (df["_frame_has_q"])
                    )
                    df.loc[mask, "fp"] = correct_fp

        # Split cumulative vs standalone entries BEFORE dedup.
        #
        # Cumulative (old method, backward compatible):
        #   Within each (tag, end, fp) group, the earliest start = cumulative
        #   (start = fiscal year start). This correctly handles IS data where
        #   both versions exist in the same group.
        #
        # Standalone (duration-based, fixes CF coverage gap):
        #   end - start ≤ ~1 quarter (100 days) = standalone single quarter.
        #   The old method alone fails for CF because many (tag, end, fp) groups
        #   contain only standalone entries — all share the same start and get
        #   misclassified as cumulative, leaving _standalone columns empty.
        df["_start_dt"] = pd.to_datetime(df["start"], errors="coerce")
        df["_end_dt"] = pd.to_datetime(df["end"], errors="coerce")
        df["_duration_days"] = (df["_end_dt"] - df["_start_dt"]).dt.days
        QUARTER_DAYS_MAX = 100
        min_starts = df.groupby(["tag", "end", "fp"])["_start_dt"].transform("min")
        df["_is_cum"] = df["_start_dt"].isna() | (df["_start_dt"] == min_starts)
        df["_is_std"] = df["_start_dt"].notna() & (df["_duration_days"] <= QUARTER_DAYS_MAX)

        standalone_fields_in_df = [
            f for f in self.STANDALONE_FIELDS if f in df["field"].values
        ]

        cum_df = df[df["_is_cum"]].drop(columns=["_is_cum", "_is_std"])
        std_df = df[df["_is_std"]].drop(columns=["_is_cum", "_is_std"])

        def _dedup_and_pivot(sub_df, suffix=""):
            """Dedup within (tag, end, fp) groups and pivot to wide format."""
            if sub_df.empty:
                return pd.DataFrame()
            sub_df = sub_df.copy()
            sub_df["_start_order"] = pd.to_datetime(sub_df["start"], errors="coerce")
            sub_df = sub_df.sort_values(
                ["_start_order", "filed", "accn"],
                ascending=[True, False, True],
                na_position="last",
            ).drop_duplicates(subset=["tag", "end", "fp"], keep="first")
            sub_df = sub_df.drop(
                columns=["_start_order", "_start_dt", "_end_dt", "_duration_days"], errors="ignore"
            )
            sub_df = sub_df.dropna(subset=["val"])
            if sub_df.empty:
                return pd.DataFrame()
            # Preserve frame and form before pivot_table drops them (neither
            # index, columns, nor values, so pandas silently discards them).
            meta_cols = ["frame", "form", "_period_kind", "_quality_flag"]
            meta_map = sub_df.groupby(["end", "fp", "filed", "accn"])[meta_cols].first().reset_index()
            wide = sub_df.pivot_table(
                index=["end", "fp", "filed", "accn"],
                columns="field",
                values="val",
                aggfunc="first",
            ).reset_index()
            wide = wide.merge(meta_map, on=["end", "fp", "filed", "accn"], how="left")
            if suffix:
                renames = {
                    c: f"{c}{suffix}"
                    for c in wide.columns
                    if c not in ("end", "fp", "filed", "accn")
                    and c in standalone_fields_in_df
                }
                wide = wide.rename(columns=renames)
            return wide

        wide_cum = _dedup_and_pivot(cum_df)
        wide_std = _dedup_and_pivot(std_df, suffix="_standalone")

        if not wide_std.empty and not wide_cum.empty:
            # Keep only _standalone columns + join keys from wide_std to avoid
            # _x/_y suffix conflicts on non-standalone columns (e.g. amortization)
            std_only_cols = [c for c in wide_std.columns
                           if c.endswith("_standalone") or c in ("end", "fp", "filed", "accn")]
            wide_std = wide_std[std_only_cols]
            wide = wide_cum.merge(
                wide_std, on=["end", "fp", "filed", "accn"], how="outer"
            )
        elif not wide_cum.empty:
            wide = wide_cum
        elif not wide_std.empty:
            wide = wide_std
        else:
            return pd.DataFrame()

        # FY/Q4 去重 + 合并：同一 (end, fp) 可能有多个 filed/accn 的行（不同 tag 去重后 filed 不同）
        # 对每个字段取第一个非空值，而不是简单丢弃
        wide["_date"] = pd.to_datetime(wide["end"])
        wide["_fp_order"] = wide["fp"].map(
            {"FY": 0, "Q4": 1, "Q3": 2, "Q2": 3, "Q1": 4}
        )

        # groupby (end, fp) 合并：每个字段取第一个非空值
        # 策略：把 NaN 替换为占位值 → first() → 换回 NaN，比逐列 lambda 快 100x+
        val_cols = [
            c
            for c in wide.columns
            if c not in ["end", "fp", "filed", "accn", "_date", "_fp_order", "frame"]
        ]
        # 优先选非空列最多的行，让 first() 拿到最完整的数据
        wide["_non_null_count"] = wide[val_cols].notna().sum(axis=1)
        wide = wide.sort_values(
            ["_date", "_fp_order", "_non_null_count", "filed"],
            ascending=[True, True, False, True],
        )

        _FILLNA_SENTINEL = -999999999999
        for c in val_cols:
            wide[c] = wide[c].fillna(_FILLNA_SENTINEL)
        agg_dict = {c: "first" for c in val_cols}
        agg_dict["filed"] = "last"
        agg_dict["accn"] = "last"
        agg_dict["frame"] = "first"
        wide = wide.drop(columns=["_non_null_count"])
        wide = wide.groupby(["end", "fp"], sort=False).agg(agg_dict).reset_index()
        for c in val_cols:
            wide[c] = wide[c].replace(_FILLNA_SENTINEL, float("nan"))

        # ── CF 表过滤空壳行 ──
        # SEC 10-K 包含对比期和季度分解上下文，这些额外的 (end, fp) 只有
        # 1-2 个 tag（如 cash_ending），核心 CF 字段全空，需丢弃。
        if "net_cash_from_operations" in tag_mapping.values():
            core_cols = [
                c
                for c in [
                    "net_cash_from_operations",
                    "net_cash_from_investing",
                    "net_cash_from_financing",
                ]
                if c in wide.columns
            ]
            if core_cols:
                wide = wide[~wide[core_cols].isna().all(axis=1)]

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

        # ── 自动计算 gross_profit（如果 tag_mapping 是 INCOME_TAGS）──
        # 如果 gross_profit 为空，但有 revenues 和 cost_of_goods_sold，
        # 则计算 GP = Revenue - COGS
        # 说明：部分公司（如 PG、WMT）不直接报告 GrossProfit tag，但有 COGS
        # 银行/金融/部分能源公司无标准 COGS，GP 保持为空（这是正常的）
        if "gross_profit" in tag_mapping.values():
            if "gross_profit" not in wide.columns:
                wide["gross_profit"] = pd.Series(dtype=float)

            mask = wide["gross_profit"].isna()
            if mask.any():
                rev = wide.get("revenues")
                cogs = wide.get("cost_of_goods_sold")

                if rev is not None and cogs is not None:
                    calculated_gp = rev - cogs
                    # 只在 revenues 和 cogs 都有值时计算
                    both_present = mask & rev.notna() & cogs.notna()
                    wide.loc[both_present, "gross_profit"] = calculated_gp[both_present]

        # ── D&A 补齐：Depreciation-only 的公司需加上 AmortizationOfIntangibleAssets ──
        # DepreciationAndAmortization / DepreciationDepletionAndAmortization 已包含
        # 摊销，无需额外加。但不少公司（如 MSFT）只报 Depreciation 标签，
        # AmortizationOfIntangibleAssets 需单独加上才能得到完整 D&A。
        if "depreciation_amortization" in tag_mapping.values():
            has_combined_da = (
                "DepreciationAndAmortization" in usgaap
                or "DepreciationDepletionAndAmortization" in usgaap
            )
            if not has_combined_da and "amortization_of_intangibles" in wide.columns:
                if "depreciation_amortization" not in wide.columns:
                    wide["depreciation_amortization"] = pd.Series(dtype=float)
                mask = wide["amortization_of_intangibles"].notna()
                if mask.any():
                    wide.loc[mask, "depreciation_amortization"] = (
                        wide.loc[mask, "depreciation_amortization"].fillna(0)
                        + wide.loc[mask, "amortization_of_intangibles"]
                    )
                wide = wide.drop(columns=["amortization_of_intangibles"])

        return wide

    # ── 不可变版本层写入辅助 ──────────────────────────────

    # 允许进入正式 fact version 的 form/fp 矩阵。
    # 其他 form（8-K / DEF 14A / 6-K 等）或无法识别的 fp 进入 staging。
    ACCEPTED_FORMS: set[str] = {
        "10-K", "10-K/A",
        "10-Q", "10-Q/A",
        "10-QT", "10-QT/A",
        "20-F", "20-F/A",
        "40-F", "40-F/A",
    }
    ACCEPTED_FP: set[str] = {"FY", "Q1", "Q2", "Q3", "Q4", "H1", "H2"}
    ACCEPTED_FP.update({f"M{i}" for i in range(1, 13)})

    def _infer_statement(self, tag_mapping: dict[str, str]) -> str:
        """根据 tag_mapping 推断报表类型。"""
        if tag_mapping is self.INCOME_TAGS:
            return "income"
        if tag_mapping is self.BALANCE_TAGS:
            return "balance"
        if tag_mapping is self.CASHFLOW_TAGS:
            return "cashflow"
        return "unknown"

    @staticmethod
    def _supplement_total_liabilities_records(
        records: list[dict],
        fact_records: list[dict],
        context: Any,
    ) -> tuple[list[dict], list[dict]]:
        """当 Company Facts 缺 Liabilities tag 时，从 filing XBRL instance 补入。

        仅对年报（10-K/20-F/40-F）执行。保持 records 字段格式一致。
        """
        # 收集需要补充的 (accn, end, form) — 每个唯一的 end 可能值不同
        annual_forms = {"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"}
        annual_pairs: set[tuple] = set()  # (accn, end, form)
        for r in records:
            form = str(r.get("form", "")).upper()
            if form in annual_forms and r.get("accn") and r.get("end"):
                annual_pairs.add((str(r["accn"]), str(r["end"])[:10], form))

        if not annual_pairs:
            return records, fact_records

        from core.us_financial_xbrl_fallback import fetch_total_liabilities_from_instance

        cik = context.cik if context else ""

        for accn, end_str, form in annual_pairs:
            # 检查该 (accn, end) 是否已有 total_liabilities
            already_has = any(
                r.get("field") == "total_liabilities"
                and str(r.get("accn", "")) == accn
                and str(r.get("end", ""))[:10] == end_str
                for r in records
            )
            if already_has:
                continue

            result = fetch_total_liabilities_from_instance(
                accession_no=accn,
                cik=cik,
                report_date=end_str,
                form=form,
            )

            if result and result.get("value_numeric") is not None:
                # 为该 (accn, end) 构造 record
                matching = [r for r in records
                            if str(r.get("accn", "")) == accn
                            and str(r.get("end", ""))[:10] == end_str]
                if matching:
                    tl_rec = dict(matching[0])
                    tl_rec["tag"] = result.get("sec_tag", "Liabilities")
                    tl_rec["field"] = "total_liabilities"
                    tl_rec["val"] = float(result["value_numeric"])
                    flag = result.get(
                        "reconstruction_flag", "RECONSTRUCTED_FROM_FILING_XBRL"
                    )
                    ctx_ref = result.get("context_ref")
                    tl_rec["_quality_flag"] = (
                        f"{flag},FILING_XBRL_CONTEXT={ctx_ref}"
                        if ctx_ref else flag
                    )
                    records.append(tl_rec)

                    matching_fact = next(
                        (
                            r for r in fact_records
                            if str(r.get("accn", "")) == accn
                            and str(r.get("end", ""))[:10] == end_str
                        ),
                        None,
                    )
                    if matching_fact:
                        fact_rec = dict(matching_fact)
                        fact_rec["tag"] = tl_rec["tag"]
                        fact_rec["field"] = "total_liabilities"
                        fact_rec["val"] = result["value_numeric"]
                        unit_ref = str(result.get("unit_ref") or "USD")
                        normalized_unit = unit_ref.lower()
                        fact_rec["unit"] = (
                            "USD"
                            if normalized_unit == "usd"
                            or normalized_unit.endswith(":usd")
                            else unit_ref
                        )
                        fact_rec["start"] = None
                        fact_rec["_period_kind"] = "instant"
                        fact_rec["_quality_flag"] = tl_rec["_quality_flag"]
                        fact_rec["dimensions"] = {}
                        fact_records.append(fact_rec)
                    logger.info(
                        "Filing XBRL fallback: %s total_liabilities=%s for %s accn=%s",
                        context.stock_code if context else "?",
                        result["value_numeric"], end_str, accn,
                    )

        return records, fact_records

    def _classify_record(self, rec: dict) -> tuple[str, str | None]:
        """对有效 period 的事实做 form/fp/period_kind 允许矩阵分类。"""
        from core.us_financial_versioning import classify_record
        return classify_record(rec)

    # ── 不可变版本层写入辅助 ──────────────────────────────

    def _write_version_layer(
        self,
        fact_records: list[dict],
        invalid_records: list[dict],
        statement: str,
        context: FetchContext,
    ) -> None:
        """把原始 fact 写入 us_filing + us_financial_fact_version。

        通过 USFactVersionWriter 与 Phase 2 backfill 共用同一写入逻辑，
        包含 filing 推断、repeat/conflict 分流、staging、fact_source 关系写入。
        失败直接抛出，由 extract_table 捕获并记录 error，不阻塞旧宽表。
        """
        if not fact_records and not invalid_records:
            return

        started_at = datetime.now()
        parser_git_sha = self._get_parser_git_sha()
        run_id = None

        try:
            with Connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO us_ingest_run (snapshot_id, parser_git_sha, started_at, status)
                        VALUES (%s, %s, %s, 'running')
                        RETURNING run_id
                        """,
                        (context.snapshot_id, parser_git_sha, started_at),
                    )
                    run_id = cur.fetchone()[0]

                # run 记录先单独提交，确保后续数据事务失败时仍能更新为 failed。
                conn.commit()

                # 对同一 snapshot 的 ingest 加事务级 advisory lock，避免并发
                # 竞争导致 existing 查询与 INSERT 之间出现重复/异值静默丢失。
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_xact_lock(%s)", (context.snapshot_id,))

                writer = USFactVersionWriter(parser_git_sha=parser_git_sha)
                result = writer.write_facts(
                    conn=conn,
                    context=context,
                    run_id=run_id,
                    fact_records=fact_records,
                    invalid_records=invalid_records,
                    statement=statement,
                    derive_filing_meta_func=self._derive_filing_meta,
                )

                self._finish_run(
                    conn, run_id, "success",
                    inserted=result["facts_inserted"],
                    repeated=result["facts_repeated"],
                    conflicted=result["facts_conflicted"],
                    reviewed=result["facts_staged"],
                )
                conn.commit()

                logger.info(
                    "%s %s: ingest_run=%s new=%d repeated=%d conflict=%d staging=%d",
                    context.stock_code, statement, run_id,
                    result["facts_inserted"], result["facts_repeated"],
                    result["facts_conflicted"], result["facts_staged"],
                )

        except Exception as exc:
            # 数据事务已失败；用独立事务记录 ingest run 失败状态，
            # 避免在 aborted 事务中 UPDATE 导致 InFailedSqlTransaction。
            logger.error(
                "%s %s: ingest failed before completion: %s",
                context.stock_code, statement, exc,
            )
            if run_id is not None:
                try:
                    with Connection() as fail_conn:
                        self._finish_run(fail_conn, run_id, "failed", error=str(exc))
                        fail_conn.commit()
                except Exception as inner_exc:
                    logger.error(
                        "Failed to persist failed status for run_id=%s: %s",
                        run_id, inner_exc,
                    )
            raise

    # ── 版本层内部 helper ─────────────────────────────────

    # ── 版本层内部 helper（已迁移到 core.us_financial_versioning，保留薄包装） ──

    @staticmethod
    def _fact_key(row: dict) -> tuple:
        from core.us_financial_versioning import fact_key
        return fact_key(row)

    def _dedup_batch(
        self,
        fact_rows: list[dict],
        run_id: int,
    ) -> tuple[list[dict], int, list[dict]]:
        from core.us_financial_versioning import USFactVersionWriter
        return USFactVersionWriter._dedup_batch(fact_rows, run_id)

    def _build_conflict_row(
        self,
        run_id: int,
        existing_row: dict,
        new_row: dict,
    ) -> dict:
        from core.us_financial_versioning import _build_conflict_row_static
        return _build_conflict_row_static(run_id, existing_row, new_row)

    @staticmethod
    def _split_value(val: Any) -> tuple[Decimal | None, str | None]:
        from core.us_financial_versioning import split_value
        return split_value(val)

    @staticmethod
    def _reject_reason(rec: dict) -> str | None:
        from core.us_financial_versioning import reject_reason
        return reject_reason(rec)

    def _staging_row(
        self,
        rec: dict,
        statement: str,
        context: FetchContext,
        reject_reason: str,
        run_id: int,
    ) -> dict:
        from core.us_financial_versioning import USFactVersionWriter
        writer = USFactVersionWriter()
        return writer._staging_row(rec, statement, context, reject_reason, run_id)

    def _flush_staging(self, conn_or_cur, rows: list[dict]) -> None:
        from core.us_financial_versioning import USFactVersionWriter
        if hasattr(conn_or_cur, "cursor"):
            with conn_or_cur.cursor() as cur:
                USFactVersionWriter._flush_staging(cur, rows)
        else:
            USFactVersionWriter._flush_staging(conn_or_cur, rows)

    def _finish_run(
        self,
        conn,
        run_id: int,
        status: str,
        inserted: int = 0,
        repeated: int = 0,
        conflicted: int = 0,
        reviewed: int = 0,
        error: str | None = None,
    ) -> None:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE us_ingest_run
                SET status = %s,
                    finished_at = NOW(),
                    facts_inserted = %s,
                    facts_repeated = %s,
                    facts_conflicted = %s,
                    facts_reviewed = %s,
                    error_message = %s
                WHERE run_id = %s
                """,
                (status, inserted, repeated, conflicted, reviewed, error, run_id),
            )

    def _derive_filing_meta(self, records: list[dict]) -> dict[str, dict]:
        """从 fact 记录推断 filing-level 当前报告期。"""
        from core.us_financial_versioning import derive_filing_meta
        return derive_filing_meta(records)

    @staticmethod
    def _int_or_none(val: Any) -> int | None:
        from core.us_financial_versioning import _int_or_none
        return _int_or_none(val)

    def _load_existing_value_hashes(
        self,
        conn,
        fact_rows: list[dict],
    ) -> dict[tuple, dict]:
        """批量查询已存在的 fact value_hash，用于 repeat/conflict 判断。"""
        from core.us_financial_versioning import USFactVersionWriter
        existing = USFactVersionWriter()._load_existing_facts(conn, fact_rows)
        return {
            key: {
                "value_hash": info["value_hash"],
                "value_numeric": info.get("value_numeric"),
                "value_text": info.get("value_text"),
            }
            for key, info in existing.items()
        }

    def _compute_context_hash(
        self,
        period_kind: str,
        period_start: str | None,
        report_date: str,
        frame: str | None,
        fp: str | None,
        dimensions: dict,
    ) -> str:
        """由 period、frame、fp、dimensions 生成稳定 context_hash。"""
        from core.us_financial_versioning import compute_context_hash
        return compute_context_hash(period_kind, period_start, report_date, frame, fp, dimensions)

    def _compute_value_hash(self, value: Any, unit: str) -> str:
        """由 value + unit 生成稳定 value_hash。"""
        from core.us_financial_versioning import compute_value_hash
        return compute_value_hash(value, unit)

    def fetch_income(self, ticker: str) -> pd.DataFrame:
        """获取利润表宽表。"""
        facts, ctx = self.fetch_company_facts_with_context(ticker)
        return self.extract_table(facts, self.INCOME_TAGS, context=ctx)

    def fetch_balance(self, ticker: str) -> pd.DataFrame:
        """获取资产负债表宽表。"""
        facts, ctx = self.fetch_company_facts_with_context(ticker)
        return self.extract_table(facts, self.BALANCE_TAGS, context=ctx)

    def fetch_cashflow(self, ticker: str) -> pd.DataFrame:
        """获取现金流量表宽表。"""
        facts, ctx = self.fetch_company_facts_with_context(ticker)
        return self.extract_table(facts, self.CASHFLOW_TAGS, context=ctx)

    # ── 内部工具方法 ──────────────────────────────────────

    def _request_sec(self, url: str) -> requests.Response:
        """向 SEC API 发送请求，带限流和重试。

        Returns:
            requests.Response 对象，调用方自行取 .json()
        """
        resp = requests.get(url, headers=HEADERS, timeout=30)
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", "10"))
            logger.warning("SEC 429 限流，等待 %ds...", retry_after)
            time.sleep(retry_after)
            self._rate_limiter.wait()
            resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        return resp

    def _compute_content_hash(self, data: dict) -> str:
        """计算 SEC 响应的 SHA-256 content_hash（稳定 JSON 序列化）。"""
        canonical = json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _get_parser_git_sha() -> str:
        """获取当前代码 Git SHA，用于版本追溯。"""
        try:
            return subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=Path(__file__).resolve().parent.parent.parent,
                stderr=subprocess.DEVNULL,
                text=True,
            ).strip()
        except Exception:
            return "unknown"

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
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )

    fetcher = USFinancialFetcher()

    # 测试限流器
    print("\n=== 测试限流器 ===")
    rl = SECRateLimiter(rate=10)
    t0 = time.time()
    for i in range(12):
        rl.wait()
        print(f"  请求 {i + 1}: {time.time() - t0:.3f}s")
    print(f"12 次请求耗时: {time.time() - t0:.2f}s（应 >1s）")

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
