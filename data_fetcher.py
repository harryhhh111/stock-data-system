"""
数据获取模块 - 使用akshare获取A股/港股基本面数据
"""
import os
import logging
import time
import threading
import signal
from datetime import datetime, timedelta
from functools import wraps
from typing import Dict, List, Optional, Any, Tuple, Set
import pandas as pd

# 禁用 akshare 东方财富 API 的 tqdm 进度条
os.environ["TQDM_DISABLE"] = "1"

import akshare as ak
from sqlalchemy.orm import Session

import config
import models
from models import get_engine, init_db
from database import get_db_session

logger = logging.getLogger(__name__)

# 配置常量
QUARTERS = config.SYNC_CONFIG["quarters"]


# ============================================================
# 工具函数
# ============================================================

def date_to_quarter(date_val) -> str:
    """
    将日期值转换为 YYYY-QN 格式
    支持格式: "20251231", "2025-12-31", datetime, pd.Timestamp
    """
    if pd.isna(date_val) or date_val is None:
        return None
    d = pd.to_datetime(str(date_val))
    month = d.month
    if month in (1, 2, 3):
        q = "Q1"
    elif month in (4, 5, 6):
        q = "Q2"
    elif month in (7, 8, 9):
        q = "Q3"
    else:
        q = "Q4"
    return f"{d.year}-{q}"


def _safe_float(value):
    """安全转换为浮点数"""
    try:
        if pd.isna(value) or value is None or str(value).strip() in ('False', '', '-'):
            return None
        return float(value)
    except (ValueError, TypeError):
        return None


def _is_hk(stock_code: str) -> bool:
    """判断是否为港股代码"""
    return len(stock_code) == 5 and stock_code.isdigit()


# ============================================================
# 熔断器
# ============================================================

class CircuitBreaker:
    """熔断器：连续失败超过阈值后进入断路状态，暂停一段时间后恢复"""

    def __init__(self, threshold: int = 5, reset_minutes: int = 30):
        self.threshold = threshold
        self.reset_minutes = reset_minutes
        self.failure_count = 0
        self.last_failure_time: Optional[float] = None
        self._lock = threading.Lock()

    def is_open(self) -> bool:
        """检查熔断器是否打开（是否应拒绝请求）"""
        with self._lock:
            if self.failure_count < self.threshold:
                return False
            if self.last_failure_time is not None:
                elapsed = time.time() - self.last_failure_time
                if elapsed >= self.reset_minutes * 60:
                    self.failure_count = 0
                    self.last_failure_time = None
                    logger.info("Circuit breaker reset after cooldown period")
                    return False
            return True

    def record_success(self):
        """记录成功，重置计数器"""
        with self._lock:
            if self.failure_count > 0:
                logger.debug(f"Circuit breaker failure count reset: {self.failure_count} -> 0")
            self.failure_count = 0
            self.last_failure_time = None

    def record_failure(self):
        """记录失败"""
        with self._lock:
            self.failure_count += 1
            self.last_failure_time = time.time()
            if self.failure_count >= self.threshold:
                logger.warning(
                    f"Circuit breaker OPEN: {self.failure_count} consecutive failures. "
                    f"Will retry after {self.reset_minutes} minutes."
                )


# 全局熔断器实例
circuit_breaker = CircuitBreaker(
    threshold=config.RETRY_CONFIG["circuit_threshold"],
    reset_minutes=config.RETRY_CONFIG["circuit_reset_minutes"],
)


# ============================================================
# 重试装饰器
# ============================================================

def with_retry(max_retries: int = None, timeout: int = None):
    """
    带指数退避重试和熔断的装饰器
    """
    if max_retries is None:
        max_retries = config.RETRY_CONFIG["max_retries"]
    if timeout is None:
        timeout = config.RETRY_CONFIG["timeout"]

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            if circuit_breaker.is_open():
                logger.warning(
                    f"Circuit breaker is OPEN, skipping call: {func.__name__}"
                )
                return None

            last_exception = None
            for attempt in range(1, max_retries + 1):
                try:
                    def _timeout_handler(signum, frame):
                        raise TimeoutError(
                            f"Call to {func.__name__} timed out after {timeout}s"
                        )

                    old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
                    signal.alarm(timeout)
                    try:
                        result = func(*args, **kwargs)
                    finally:
                        signal.alarm(0)
                        signal.signal(signal.SIGALRM, old_handler)

                    circuit_breaker.record_success()
                    return result

                except TimeoutError as e:
                    last_exception = e
                    logger.warning(
                        f"[{func.__name__}] Timeout on attempt {attempt}/{max_retries}: {e}"
                    )
                except Exception as e:
                    last_exception = e
                    logger.warning(
                        f"[{func.__name__}] Error on attempt {attempt}/{max_retries}: {e}"
                    )

                if attempt < max_retries:
                    delay = min(
                        config.RETRY_CONFIG["base_delay"] * (2 ** (attempt - 1)),
                        config.RETRY_CONFIG["max_delay"],
                    )
                    logger.info(f"[{func.__name__}] Retrying in {delay:.1f}s...")
                    time.sleep(delay)

            circuit_breaker.record_failure()
            logger.error(
                f"[{func.__name__}] All {max_retries} attempts failed. "
                f"Circuit breaker failures: {circuit_breaker.failure_count}"
            )
            return None

        return wrapper
    return decorator


# ============================================================
# A股 API 封装 (东方财富 _by_report_em 系列)
# ============================================================

@with_retry(timeout=60)
def _fetch_a_financial_report_em(stock_code: str, report_type: str) -> Optional[pd.DataFrame]:
    """
    获取A股财务报表 - 东方财富 API
    report_type: "profit" | "balance" | "cash_flow"
    """
    prefix = "SZ" if stock_code.startswith(("0", "3")) else "SH"
    symbol = f"{prefix}{stock_code}"

    try:
        if report_type == "profit":
            df = ak.stock_profit_sheet_by_report_em(symbol=symbol)
        elif report_type == "balance":
            df = ak.stock_balance_sheet_by_report_em(symbol=symbol)
        elif report_type == "cash_flow":
            df = ak.stock_cash_flow_sheet_by_report_em(symbol=symbol)
        else:
            return None

        if df is None or df.empty:
            return None

        # 只取最近 N 个季度
        df = df.head(QUARTERS).copy()

        # 添加 report_date 列（YYYY-QN 格式）
        df["report_date"] = df["REPORT_DATE"].apply(date_to_quarter)
        return df
    except Exception as e:
        logger.warning(f"Failed to fetch A-share {report_type} for {stock_code}: {e}")
        return None


def fetch_a_income_statement(stock_code: str) -> Optional[pd.DataFrame]:
    """获取A股利润表（东方财富）"""
    return _fetch_a_financial_report_em(stock_code, "profit")


def fetch_a_balance_sheet(stock_code: str) -> Optional[pd.DataFrame]:
    """获取A股资产负债表（东方财富）"""
    return _fetch_a_financial_report_em(stock_code, "balance")


def fetch_a_cash_flow_statement(stock_code: str) -> Optional[pd.DataFrame]:
    """获取A股现金流量表（东方财富，完整报表写入 cash_flow_statement 表）"""
    return _fetch_a_financial_report_em(stock_code, "cash_flow")


# ============================================================
# 港股 API 封装 (stock_financial_hk_report_em)
# ============================================================

@with_retry(timeout=60)
def _fetch_hk_financial_report(stock_code: str, symbol: str, indicator: str = "季度") -> Optional[pd.DataFrame]:
    """
    获取港股财务报表
    返回长格式 DataFrame，需要转换为宽格式
    """
    try:
        df = ak.stock_financial_hk_report_em(stock=stock_code, symbol=symbol, indicator=indicator)
        if df is None or df.empty:
            return None
        return df
    except Exception as e:
        logger.warning(f"Failed to fetch HK {symbol} for {stock_code}: {e}")
        return None


def _pivot_hk_report(df: pd.DataFrame, quarters: int) -> pd.DataFrame:
    """
    将港股长格式报表转换为宽格式，并只保留最近 N 个季度
    输入: 长格式 (STD_ITEM_NAME, REPORT_DATE, AMOUNT)
    输出: 宽格式 (REPORT_DATE 为行, STD_ITEM_NAME 为列)
    """
    if df is None or df.empty:
        return pd.DataFrame()
    
    # 转换 REPORT_DATE 为日期
    df = df.copy()
    df["REPORT_DATE_DT"] = pd.to_datetime(df["REPORT_DATE"])
    
    # 获取最近 N 个不同的报告期
    dates = sorted(df["REPORT_DATE_DT"].unique(), reverse=True)[:quarters]
    df = df[df["REPORT_DATE_DT"].isin(dates)]
    
    # 转换为宽格式
    result = df.pivot_table(index="REPORT_DATE_DT", columns="STD_ITEM_NAME", values="AMOUNT", aggfunc="first")
    result = result.sort_index(ascending=False)
    result["report_date"] = result.index.to_series().apply(date_to_quarter)
    
    return result.reset_index(drop=True)


def fetch_hk_income_statement(stock_code: str) -> Optional[pd.DataFrame]:
    """获取港股利润表"""
    df = _fetch_hk_financial_report(stock_code, "利润表")
    if df is None:
        return None
    return _pivot_hk_report(df, QUARTERS)


def fetch_hk_balance_sheet(stock_code: str) -> Optional[pd.DataFrame]:
    """获取港股资产负债表"""
    df = _fetch_hk_financial_report(stock_code, "资产负债表")
    if df is None:
        return None
    return _pivot_hk_report(df, QUARTERS)


def fetch_hk_cash_flow_statement(stock_code: str) -> Optional[pd.DataFrame]:
    """获取港股现金流量表"""
    df = _fetch_hk_financial_report(stock_code, "现金流量表")
    if df is None:
        return None
    return _pivot_hk_report(df, QUARTERS)


# ============================================================
# 数据获取函数 - 股票列表 / 财务指标 / 市值
# ============================================================

def fetch_a_stock_list() -> pd.DataFrame:
    """获取A股股票列表"""
    logger.info("Fetching A-share stock list...")
    result = _fetch_a_stock_list_impl()
    if result is None or (isinstance(result, pd.DataFrame) and result.empty):
        logger.warning("fetch_a_stock_list returned no data after retries")
    return result if isinstance(result, pd.DataFrame) else pd.DataFrame()


@with_retry()
def _fetch_a_stock_list_impl() -> pd.DataFrame:
    try:
        df = ak.stock_info_a_code_name()
        df["market"] = "CN_A"
        if "exchange" not in df.columns:
            df["exchange"] = df["code"].apply(
                lambda x: "shanghai" if x.startswith(("6", "9")) else "shenzhen"
            )
        logger.info(f"Fetched {len(df)} A-share stocks")
        return df
    except Exception as e:
        logger.error(f"Failed to fetch A-share list: {e}")
        return pd.DataFrame()


def fetch_hk_stock_list() -> pd.DataFrame:
    """获取港股股票列表"""
    logger.info("Fetching HK stock list...")
    result = _fetch_hk_stock_list_impl()
    return result if isinstance(result, pd.DataFrame) else pd.DataFrame()


@with_retry(timeout=120)
def _fetch_hk_stock_list_impl() -> pd.DataFrame:
    """获取港股股票列表（使用新浪数据源，国内可用）"""
    try:
        df = ak.stock_hk_spot()
        code_col = "代码" if "代码" in df.columns else df.columns[1]
        name_col = "中文名称" if "中文名称" in df.columns else "名称"
        df = df[[code_col, name_col]].copy()
        df.columns = ["code", "name"]
        df["market"] = "HK"
        df["exchange"] = "hk"
        logger.info(f"Fetched {len(df)} HK stocks")
        return df
    except Exception as e:
        logger.error(f"Failed to fetch HK stock list: {e}")
        return pd.DataFrame()


def fetch_stock_info(stock_code: str, market: str) -> Optional[Dict]:
    """获取股票基本信息"""
    result = _fetch_stock_info_impl(stock_code, market)
    return result


@with_retry()
def _fetch_stock_info_impl(stock_code: str, market: str) -> Optional[Dict]:
    try:
        if market == "CN_A":
            df = ak.stock_info_sh_name_sym(symbol="全部") if stock_code.startswith(("6", "9")) else ak.stock_info_sz_name_sym(symbol="全部")
            row = df[df["code"] == stock_code]
            if row.empty:
                return None
            return {
                "stock_code": stock_code,
                "stock_name": row.iloc[0]["name"],
                "industry": row.get("industry", [None])[0] if "industry" in row.columns else None,
                "list_date": None,
            }
        elif market == "HK":
            df = ak.stock_hk_spot()
            code_col = "代码" if "代码" in df.columns else df.columns[1]
            name_col = "中文名称" if "中文名称" in df.columns else "名称"
            row = df[df[code_col] == stock_code]
            if row.empty:
                return None
            return {
                "stock_code": stock_code,
                "stock_name": row.iloc[0]["名称"],
                "industry": None,
                "list_date": None,
            }
    except Exception as e:
        logger.warning(f"Failed to fetch info for {stock_code}: {e}")
        return None


def fetch_financial_indicator(stock_code: str, period: str = "季度") -> pd.DataFrame:
    """
    获取财务指标（多季度）
    A股: stock_financial_abstract_ths (多季度)
    港股: stock_hk_financial_indicator_em (单快照) + stock_financial_hk_report_em 推导多季度指标
    """
    result = _fetch_financial_indicator_impl(stock_code, period)
    return result if isinstance(result, pd.DataFrame) else pd.DataFrame()


@with_retry()
def _fetch_financial_indicator_impl(stock_code: str, period: str = "季度") -> pd.DataFrame:
    """获取财务指标，A股和港股使用不同的API"""
    try:
        if _is_hk(stock_code):
            return _fetch_hk_financial_indicator_multi(stock_code)
        else:
            return _fetch_a_financial_indicator(stock_code, period)
    except Exception as e:
        logger.warning(f"Failed to fetch financial indicator for {stock_code}: {e}")
        return pd.DataFrame()


def _fetch_a_financial_indicator(stock_code: str, period: str) -> pd.DataFrame:
    """获取A股财务指标（多季度）"""
    df = ak.stock_financial_abstract_ths(symbol=stock_code, indicator="按报告期")
    if df is None or df.empty:
        return pd.DataFrame()
    
    # 取最近 N 个季度
    df_recent = df.tail(QUARTERS).copy()
    
    # 获取市值（腾讯财经API）
    market_caps = _fetch_a_market_cap([stock_code])
    mkt_cap = market_caps.get(stock_code)
    
    def _parse_percent(value):
        if pd.isna(value) or value is None or str(value).strip() in ('False', '', '-'):
            return None
        try:
            s = str(value).strip().rstrip('%')
            return float(s)
        except (ValueError, TypeError):
            return None
    
    records = []
    for _, row in df_recent.iterrows():
        record = {
            "indicator_date": pd.to_datetime(row["报告期"]).date(),
            "roe": _parse_percent(row.get("净资产收益率")),
            "gross_margin": _parse_percent(row.get("销售毛利率")),
            "net_margin": _parse_percent(row.get("销售净利率")),
            "revenue_growth": _parse_percent(row.get("营业总收入同比增长率")),
            "profit_growth": _parse_percent(row.get("净利润同比增长率")),
            "debt_ratio": _parse_percent(row.get("资产负债率")),
            "market_cap": mkt_cap,
        }
        records.append(record)
    
    return pd.DataFrame(records)


def _fetch_hk_financial_indicator_multi(stock_code: str) -> pd.DataFrame:
    """
    获取港股多季度财务指标
    使用 stock_financial_hk_report_em 利润表 + 资产负债表来推导指标
    同时用 stock_hk_financial_indicator_em 获取当前市值/PE/PB
    """
    records = []
    
    # 获取当前快照（含市值、PE、PB）
    try:
        snapshot = ak.stock_hk_financial_indicator_em(symbol=stock_code)
        if snapshot is not None and not snapshot.empty:
            _hk_snapshot_cache[stock_code] = snapshot.iloc[0].to_dict()
    except Exception:
        pass
    
    snap = _hk_snapshot_cache.get(stock_code, {})
    market_cap = _safe_float(snap.get("总市值(港元)"))
    
    # 获取利润表数据
    try:
        inc_df = _fetch_hk_financial_report(stock_code, "利润表", "季度")
        if inc_df is None or inc_df.empty:
            return pd.DataFrame()
        
        # 获取资产负债表数据
        bs_df = _fetch_hk_financial_report(stock_code, "资产负债表", "季度")
        
        # 获取最近 N 个季度
        dates = sorted(inc_df["REPORT_DATE"].unique(), reverse=True)[:QUARTERS]
        
        for i, date in enumerate(dates):
            inc_rows = inc_df[inc_df["REPORT_DATE"] == date]
            bs_rows = bs_df[bs_df["REPORT_DATE"] == date] if bs_df is not None else pd.DataFrame()
            
            # 提取利润表指标
            revenue = _safe_float(inc_rows[inc_rows["STD_ITEM_NAME"] == "营业额"]["AMOUNT"].values[0]) if "营业额" in inc_rows["STD_ITEM_NAME"].values else None
            net_profit = None
            for name in ["本公司权益持有人应占溢利", "股东应占溢利", "净利润"]:
                rows = inc_rows[inc_rows["STD_ITEM_NAME"] == name]
                if not rows.empty:
                    net_profit = _safe_float(rows["AMOUNT"].values[0])
                    break
            
            # 提取资产负债表指标
            total_assets = _safe_float(bs_rows[bs_rows["STD_ITEM_NAME"] == "总资产"]["AMOUNT"].values[0]) if bs_rows is not None and "总资产" in bs_rows["STD_ITEM_NAME"].values else None
            total_equity = None
            for name in ["股东权益", "总权益"]:
                rows = bs_rows[bs_rows["STD_ITEM_NAME"] == name]
                if not rows.empty:
                    total_equity = _safe_float(rows["AMOUNT"].values[0])
                    break
            total_liabilities = _safe_float(bs_rows[bs_rows["STD_ITEM_NAME"] == "总负债"]["AMOUNT"].values[0]) if bs_rows is not None and "总负债" in bs_rows["STD_ITEM_NAME"].values else None
            
            # 计算 ROE 和负债率
            roe = (float(net_profit) / float(total_equity) * 100) if net_profit and total_equity and total_equity > 0 else None
            debt_ratio = (float(total_liabilities) / float(total_assets) * 100) if total_liabilities and total_assets and total_assets > 0 else None
            
            # 计算净利率
            net_margin = (float(net_profit) / float(revenue) * 100) if net_profit and revenue and revenue > 0 else None
            
            # 前一期数据用于计算增长率
            if i < len(dates) - 1:
                prev_date = dates[i + 1]
                prev_rows = inc_df[inc_df["REPORT_DATE"] == prev_date]
                prev_revenue = _safe_float(prev_rows[prev_rows["STD_ITEM_NAME"] == "营业额"]["AMOUNT"].values[0]) if "营业额" in prev_rows["STD_ITEM_NAME"].values else None
                prev_net_profit = None
                for name in ["本公司权益持有人应占溢利", "股东应占溢利", "净利润"]:
                    rows = prev_rows[prev_rows["STD_ITEM_NAME"] == name]
                    if not rows.empty:
                        prev_net_profit = _safe_float(rows["AMOUNT"].values[0])
                        break
                
                revenue_growth = ((float(revenue) - float(prev_revenue)) / abs(float(prev_revenue)) * 100) if revenue and prev_revenue and prev_revenue != 0 else None
                profit_growth = ((float(net_profit) - float(prev_net_profit)) / abs(float(prev_net_profit)) * 100) if net_profit and prev_net_profit and prev_net_profit != 0 else None
            else:
                revenue_growth = None
                profit_growth = None
            
            # PE/PB 只对最新一期使用当前快照值
            pe = _safe_float(snap.get("市盈率")) if i == 0 else None
            pb = _safe_float(snap.get("市净率")) if i == 0 else None
            
            record = {
                "indicator_date": pd.to_datetime(date).date(),
                "pe": pe,
                "pb": pb,
                "roe": roe,
                "net_margin": net_margin,
                "revenue_growth": revenue_growth,
                "profit_growth": profit_growth,
                "debt_ratio": debt_ratio,
                "market_cap": market_cap,
            }
            records.append(record)
        
        return pd.DataFrame(records)
    
    except Exception as e:
        logger.warning(f"Failed to fetch HK multi-quarter indicators for {stock_code}: {e}")
        return pd.DataFrame()


# 港股快照缓存（避免重复请求）
_hk_snapshot_cache: Dict[str, dict] = {}


def _fetch_a_market_cap(stock_codes: list) -> dict:
    """
    批量获取A股市值（腾讯财经API）
    stock_codes: ['000001', '600519']（不带市场前缀）
    返回: {'000001': 209001738992.46, ...} 单位：元
    """
    qt_codes = []
    for code in stock_codes:
        prefix = 'sz' if code.startswith(('0', '3')) else 'sh'
        qt_codes.append(f'{prefix}{code}')
    
    if not qt_codes:
        return {}
    
    try:
        import requests
        url = 'https://qt.gtimg.cn/q=' + ','.join(qt_codes)
        resp = requests.get(url, timeout=10)
        result = {}
        lines = resp.text.strip().split(';')
        for line in lines:
            if not line.strip():
                continue
            parts = line.split('~')
            if len(parts) > 45:
                code = parts[2]
                try:
                    result[code] = float(parts[45]) * 1e8
                except (ValueError, TypeError):
                    result[code] = None
        return result
    except Exception as e:
        logger.warning(f"Failed to fetch A-share market cap: {e}")
        return {}


def fetch_hk_market_cap_batch(stock_codes: List[str]) -> dict:
    """
    批量获取港股市值
    利用 stock_hk_financial_indicator_em 中的总市值字段
    返回: {'00700': 4.547e+12, ...} 单位：港元
    """
    result = {}
    for code in stock_codes:
        try:
            snap = ak.stock_hk_financial_indicator_em(symbol=code)
            if snap is not None and not snap.empty:
                cap = snap.iloc[0].get("总市值(港元)")
                if cap is not None:
                    result[code] = float(cap)
        except Exception:
            pass
        time.sleep(0.3)  # 限流
    return result


# ============================================================
# FCF 计算（保留现有逻辑）
# ============================================================

@with_retry(timeout=30)
def _fetch_a_cash_flow_for_fcf(stock_code: str) -> Optional[pd.DataFrame]:
    """获取A股现金流量表（只取最新1期，用于FCF计算）"""
    try:
        if stock_code.startswith(("6", "9")):
            symbol = f"SH{stock_code}"
        else:
            symbol = f"SZ{stock_code}"
        
        df = ak.stock_cash_flow_sheet_by_report_em(symbol=symbol)
        if df is not None and not df.empty:
            df = df.head(1)
        return df
    except Exception as e:
        logger.warning(f"Failed to fetch A-share cash flow for {stock_code}: {e}")
        return None


@with_retry(timeout=60)
def _fetch_hk_cash_flow_for_fcf(stock_code: str) -> Optional[pd.DataFrame]:
    """获取港股现金流量表（最新季度数据，用于FCF计算）"""
    try:
        try:
            df = ak.stock_financial_hk_report_em(
                stock=stock_code, symbol="现金流量表", indicator="季度"
            )
        except Exception:
            df = ak.stock_financial_hk_report_em(
                stock=stock_code, symbol="现金流量表", indicator="年度"
            )
        
        if df is not None and not df.empty:
            df = df.sort_values("REPORT_DATE", ascending=False)
            latest_date = df["REPORT_DATE"].iloc[0]
            df = df[df["REPORT_DATE"] == latest_date]
        return df
    except Exception as e:
        logger.warning(f"Failed to fetch HK cash flow for {stock_code}: {e}")
        return None


def fetch_and_save_cash_flow(stock_code: str, market: str, pre_market_cap: float = None) -> bool:
    """
    获取现金流量表，计算FCF，更新到 financial_indicator 表（保留现有逻辑）
    """
    return _fetch_and_save_cash_flow_impl(stock_code, market, pre_market_cap)


@with_retry(timeout=30)
def _fetch_and_save_cash_flow_impl(stock_code: str, market: str, pre_market_cap: float = None) -> bool:
    """获取现金流量表并计算FCF更新到数据库"""
    try:
        if market == "CN_A":
            cf_df = _fetch_a_cash_flow_for_fcf(stock_code)
        elif market == "HK":
            cf_df = _fetch_hk_cash_flow_for_fcf(stock_code)
        else:
            return False
        
        if cf_df is None or cf_df.empty:
            return False
        
        operating_cf = None
        capital_expense = None
        
        if market == "CN_A":
            if "NETCASH_OPERATE" in cf_df.columns:
                operating_cf = _safe_float(cf_df["NETCASH_OPERATE"].iloc[0])
            if "CONSTRUCT_LONG_ASSET" in cf_df.columns:
                capital_expense = _safe_float(cf_df["CONSTRUCT_LONG_ASSET"].iloc[0])
        
        elif market == "HK":
            for _, row in cf_df.iterrows():
                item_name = str(row.get("STD_ITEM_NAME", ""))
                amount = _safe_float(row.get("AMOUNT"))
                if item_name == "经营业务现金净额" and operating_cf is None:
                    operating_cf = amount
                elif item_name == "购建固定资产" and capital_expense is None:
                    capital_expense = amount
        
        if operating_cf is None:
            return False
        if capital_expense is None:
            capital_expense = 0
        
        fcf = operating_cf - capital_expense
        
        market_cap = pre_market_cap
        if market_cap is None:
            with get_db_session() as session:
                latest = session.query(models.FinancialIndicator).filter(
                    models.FinancialIndicator.stock_code == stock_code,
                    models.FinancialIndicator.market_cap.isnot(None)
                ).order_by(
                    models.FinancialIndicator.indicator_date.desc()
                ).first()
                market_cap = latest.market_cap if latest else None
        
        if market_cap is None and market == "CN_A":
            caps = _fetch_a_market_cap([stock_code])
            market_cap = caps.get(stock_code)
        
        fcf_yield = None
        if market_cap and market_cap > 0:
            fcf_yield = (fcf / market_cap) * 100
        
        logger.info(
            f"Cash flow for {stock_code}: operating_cf={operating_cf}, "
            f"capex={capital_expense}, fcf={fcf}, market_cap={market_cap}, "
            f"fcf_yield={fcf_yield}%"
        )
        
        with get_db_session() as session:
            record = session.query(models.FinancialIndicator).filter(
                models.FinancialIndicator.stock_code == stock_code
            ).order_by(
                models.FinancialIndicator.indicator_date.desc()
            ).first()
            
            if record:
                record.fcf = fcf
                record.fcf_yield = fcf_yield
        
        return True
        
    except Exception as e:
        logger.error(f"Error in fetch_and_save_cash_flow for {stock_code}: {e}")
        return False


# ============================================================
# 旧版 fetch 接口保留（已不再使用，但保持兼容）
# ============================================================

def fetch_income_statement(stock_code: str) -> pd.DataFrame:
    """获取利润表（统一入口）"""
    if _is_hk(stock_code):
        result = fetch_hk_income_statement(stock_code)
    else:
        result = fetch_a_income_statement(stock_code)
    return result if isinstance(result, pd.DataFrame) else pd.DataFrame()


def fetch_balance_sheet(stock_code: str) -> pd.DataFrame:
    """获取资产负债表（统一入口）"""
    if _is_hk(stock_code):
        result = fetch_hk_balance_sheet(stock_code)
    else:
        result = fetch_a_balance_sheet(stock_code)
    return result if isinstance(result, pd.DataFrame) else pd.DataFrame()


def fetch_cash_flow(stock_code: str) -> pd.DataFrame:
    """获取现金流量表（统一入口）"""
    if _is_hk(stock_code):
        result = fetch_hk_cash_flow_statement(stock_code)
    else:
        result = fetch_a_cash_flow_statement(stock_code)
    return result if isinstance(result, pd.DataFrame) else pd.DataFrame()


def fetch_index_constituent(index_code: str) -> pd.DataFrame:
    """获取指数成分股"""
    result = _fetch_index_constituent_impl(index_code)
    return result if isinstance(result, pd.DataFrame) else pd.DataFrame()


@with_retry()
def _fetch_index_constituent_impl(index_code: str) -> pd.DataFrame:
    try:
        if index_code == "000300":
            df = ak.index_stock_cons_hs300()
        elif index_code == "000905":
            df = ak.index_stock_cons_zz500()
        elif index_code.startswith("00"):
            df = ak.index_stock_cons(symbol=index_code)
        elif index_code.startswith("hk") or index_code.startswith("HSI"):
            df = ak.hk_stocks_hs_const()
        else:
            return pd.DataFrame()
        
        if df is None or df.empty:
            return pd.DataFrame()
        return df
    except Exception as e:
        logger.warning(f"Failed to fetch index constituent for {index_code}: {e}")
        return pd.DataFrame()


# ============================================================
# 三张报表保存函数
# ============================================================

# A股列名映射：东方财富英文列名 → ORM字段名
_EM_INCOME_MAP = {
    "OPERATE_INCOME": "revenue",
    "OPERATE_INCOME_YOY": "revenue_yoy",
    "OPERATE_COST": "cost",
    "SALE_EXPENSE": "selling_expense",
    "MANAGE_EXPENSE": "managing_expense",
    "RESEARCH_EXPENSE": "rd_expense",
    "FINANCE_EXPENSE": "financial_expense",
    "OPERATE_PROFIT": "operating_profit",
    "OPERATE_PROFIT_YOY": "operating_profit_yoy",
    "TOTAL_PROFIT": "total_profit",
    "NETPROFIT": "net_profit",
    "NETPROFIT_YOY": "net_profit_yoy",
    "PARENT_NETPROFIT": "attr_profit",
    "BASIC_EPS": "eps",
}

# 港股列名映射
_HK_INCOME_MAP = {
    "营业额": "revenue",
    "营运收入": "revenue",  # 营业额不可用时回退
}

_HK_INCOME_PROFIT_MAP = {
    "本公司权益持有人应占溢利": "attr_profit",
    "股东应占溢利": "attr_profit",
    "净利润": "net_profit",
    "除税前溢利": "operating_profit",
    "营业利润": "operating_profit",
}

# A股资产负债表映射（东方财富英文列名）
_EM_BS_MAP = {
    "TOTAL_ASSETS": "total_assets",
    "ACCOUNTS_RECE": "current_assets",       # 应收账款作为流动资产代表
    "FIXED_ASSET": "fixed_assets",
    "TOTAL_LIABILITIES": "total_liabilities",
    "ACCOUNTS_PAYABLE": "current_liabilities",  # 应付账款作为流动负债代表
    "BOND_PAYABLE": "longterm_liabilities",      # 应付债券作为非流动负债代表
    "TOTAL_EQUITY": "total_equity",
    "TOTAL_PARENT_EQUITY": "attr_equity",
    "GOODWILL": "goodwill",
}

# 港股资产负债表映射
_HK_BS_MAP = {
    "总资产": "total_assets",
    "流动资产合计": "current_assets",
    "非流动资产合计": "fixed_assets",
    "总负债": "total_liabilities",
    "流动负债合计": "current_liabilities",
    "非流动负债合计": "longterm_liabilities",
    "总权益": "total_equity",
    "股东权益": "total_equity",
    "净资产": "attr_equity",
}

# A股现金流量表映射（东方财富英文列名）
_EM_CF_MAP = {
    "NETCASH_OPERATE": "operating_cash_flow",
    "NETCASH_OPERATE_YOY": "operating_cash_flow_yoy",
    "NETCASH_INVEST": "investing_cash_flow",
    "NETCASH_FINANCE": "financing_cash_flow",
    "CCE_ADD": "net_cash_flow",
    "END_CCE": "cash_balance",
}

# 港股现金流量表映射
_HK_CF_MAP = {
    "经营业务现金净额": "operating_cash_flow",
    "投资业务现金净额": "investing_cash_flow",
    "融资业务现金净额": "financing_cash_flow",
    "现金净额": "net_cash_flow",
    "期末现金": "cash_balance",
}


def _save_report_upsert(session: Session, model_class, stock_code: str, report_date: str, field_dict: dict):
    """
    通用幂等写入：按 stock_code + report_date 查找，存在则更新，不存在则插入
    """
    existing = session.query(model_class).filter(
        model_class.stock_code == stock_code,
        model_class.report_date == report_date,
    ).first()
    
    if existing:
        for k, v in field_dict.items():
            if v is not None:
                setattr(existing, k, v)
    else:
        record = model_class(
            stock_code=stock_code,
            report_date=report_date,
            **{k: v for k, v in field_dict.items() if v is not None},
        )
        session.add(record)


def save_income_statement(session: Session, stock_code: str, df: pd.DataFrame, market: str):
    """保存利润表（A股宽格式 / 港股宽格式已由 _pivot 转换）"""
    if df is None or df.empty:
        return
    
    if market == "CN_A":
        _save_a_income_statement(session, stock_code, df)
    elif market == "HK":
        _save_hk_income_statement(session, stock_code, df)


def _save_a_income_statement(session: Session, stock_code: str, df: pd.DataFrame):
    """保存A股利润表（东方财富列名）"""
    for _, row in df.iterrows():
        report_date = row.get("report_date")
        if not report_date:
            continue
        
        field_dict = {}
        for api_col, orm_field in _EM_INCOME_MAP.items():
            val = _safe_float(row.get(api_col))
            if val is not None:
                field_dict[orm_field] = val
        
        # 毛利润 = 营业收入 - 营业成本
        revenue = _safe_float(row.get("OPERATE_INCOME"))
        cost = _safe_float(row.get("OPERATE_COST"))
        if revenue is not None and cost is not None:
            field_dict["gross_profit"] = revenue - cost
        
        _save_report_upsert(session, models.IncomeStatement, stock_code, report_date, field_dict)


def _save_hk_income_statement(session: Session, stock_code: str, df: pd.DataFrame):
    """保存港股利润表"""
    for _, row in df.iterrows():
        report_date = row.get("report_date")
        if not report_date:
            continue
        
        field_dict = {}
        for api_col, orm_field in _HK_INCOME_MAP.items():
            val = _safe_float(row.get(api_col))
            if val is not None:
                field_dict[orm_field] = val
        
        for api_col, orm_field in _HK_INCOME_PROFIT_MAP.items():
            val = _safe_float(row.get(api_col))
            if val is not None:
                field_dict[orm_field] = val
        
        # EPS
        eps = _safe_float(row.get("基本每股收益"))
        if eps is not None:
            field_dict["eps"] = eps
        
        _save_report_upsert(session, models.IncomeStatement, stock_code, report_date, field_dict)


def save_balance_sheet(session: Session, stock_code: str, df: pd.DataFrame, market: str):
    """保存资产负债表"""
    if df is None or df.empty:
        return
    
    col_map = _EM_BS_MAP if market == "CN_A" else _HK_BS_MAP
    
    for _, row in df.iterrows():
        report_date = row.get("report_date")
        if not report_date:
            continue
        
        field_dict = {}
        for api_col, orm_field in col_map.items():
            val = _safe_float(row.get(api_col))
            if val is not None:
                field_dict[orm_field] = val
        
        _save_report_upsert(session, models.BalanceSheet, stock_code, report_date, field_dict)


def save_cash_flow_statement(session: Session, stock_code: str, df: pd.DataFrame, market: str):
    """保存现金流量表"""
    if df is None or df.empty:
        return
    
    col_map = _EM_CF_MAP if market == "CN_A" else _HK_CF_MAP
    
    for _, row in df.iterrows():
        report_date = row.get("report_date")
        if not report_date:
            continue
        
        field_dict = {}
        for api_col, orm_field in col_map.items():
            val = _safe_float(row.get(api_col))
            if val is not None:
                field_dict[orm_field] = val
        
        _save_report_upsert(session, models.CashFlowStatement, stock_code, report_date, field_dict)


# ============================================================
# 数据保存函数
# ============================================================

def save_stock_info(session: Session, df: pd.DataFrame, market: str):
    """保存股票基本信息"""
    for _, row in df.iterrows():
        code = str(row.get("code", row.get("代码", "")))
        name = str(row.get("name", row.get("名称", "")))
        
        existing = session.query(models.StockInfo).filter(
            models.StockInfo.stock_code == code
        ).first()
        
        if existing:
            existing.stock_name = name
            existing.market = market
            existing.exchange = row.get("exchange", market)
        else:
            stock_info = models.StockInfo(
                stock_code=code,
                stock_name=name,
                market=market,
                exchange=row.get("exchange", market),
                industry=row.get("industry"),
            )
            session.add(stock_info)


def save_financial_indicator(session: Session, stock_code: str, df: pd.DataFrame):
    """保存财务指标（幂等：按 stock_code + indicator_date 去重）"""
    if df.empty:
        return
    
    for _, row in df.iterrows():
        indicator_date = row.get("indicator_date")
        if hasattr(indicator_date, 'date'):
            indicator_date = indicator_date.date() if isinstance(indicator_date, datetime) else indicator_date
        
        existing = session.query(models.FinancialIndicator).filter(
            models.FinancialIndicator.stock_code == stock_code,
            models.FinancialIndicator.indicator_date == indicator_date,
        ).first()
        
        if existing:
            for field in ["pe", "pb", "roe", "gross_margin", "net_margin", 
                         "revenue_growth", "profit_growth", "debt_ratio", "market_cap"]:
                if field in row.index:
                    existing.__setattr__(field, row.get(field))
        else:
            indicator = models.FinancialIndicator(
                stock_code=stock_code,
                indicator_date=indicator_date,
            )
            for field in ["pe", "pb", "roe", "gross_margin", "net_margin",
                         "revenue_growth", "profit_growth", "debt_ratio", "market_cap"]:
                if field in row.index:
                    indicator.__setattr__(field, row.get(field))
            session.add(indicator)


# ============================================================
# 增量同步辅助
# ============================================================

def _get_latest_report_dates_bulk(session: Session) -> Dict[str, str]:
    """
    批量获取所有股票的最新财务指标日期
    返回: {stock_code: indicator_date_str, ...}
    """
    from sqlalchemy import func as sql_func
    results = session.query(
        models.FinancialIndicator.stock_code,
        sql_func.max(models.FinancialIndicator.indicator_date).label("latest_date"),
    ).group_by(
        models.FinancialIndicator.stock_code,
    ).all()
    
    return {
        row.stock_code: row.latest_date.isoformat() if hasattr(row.latest_date, 'isoformat') else str(row.latest_date)
        for row in results
    }


def _quarter_sort_key(quarter_str: str) -> int:
    """将 YYYY-QN 格式转换为可排序的整数，如 '2025-Q3' → 20253"""
    if not quarter_str:
        return 0
    try:
        year, q = quarter_str.split("-")
        q_num = int(q.replace("Q", ""))
        return int(year) * 10 + q_num
    except (ValueError, AttributeError):
        return 0


def _date_to_sort_key(date_str: str) -> int:
    """将各种日期字符串转换为可排序的整数"""
    if not date_str:
        return 0
    s = str(date_str).replace("-", "")[:8]
    try:
        return int(s)
    except ValueError:
        return 0


# ============================================================
# 主同步函数
# ============================================================

def sync_stock_list():
    """同步股票列表"""
    logger.info("=" * 50)
    logger.info("Starting stock list sync...")
    
    start_time = datetime.now()
    total_synced = 0
    status = "success"
    error_msg = None
    
    try:
        a_df = fetch_a_stock_list()
        if not a_df.empty:
            with get_db_session() as session:
                save_stock_info(session, a_df, "CN_A")
            total_synced += len(a_df)
            logger.info(f"Saved {len(a_df)} A-share stocks")
        else:
            logger.warning("A-share list returned empty")
            status = "partial"
        
        hk_df = fetch_hk_stock_list()
        if not hk_df.empty:
            with get_db_session() as session:
                save_stock_info(session, hk_df, "HK")
            total_synced += len(hk_df)
            logger.info(f"Saved {len(hk_df)} HK stocks")
        else:
            logger.warning("HK stock list returned empty")
            status = "partial" if status == "success" else status
    except Exception as e:
        status = "failed"
        error_msg = str(e)
        logger.error(f"Stock list sync failed: {e}")
    
    end_time = datetime.now()
    
    try:
        with get_db_session() as session:
            log = models.SyncLog(
                sync_type="stock_list",
                status=status,
                start_time=start_time,
                end_time=end_time,
                records_synced=total_synced,
                error_message=error_msg,
            )
            session.add(log)
    except Exception as e:
        logger.error(f"Failed to write sync log: {e}")
    
    logger.info(f"Stock list sync completed: status={status}, records={total_synced}")


def sync_financial_data(quarters: int = None):
    """
    同步财务数据（增量 + 三张报表 + 港股多季度）
    
    quarters: 每只股票保留最近几个季度（默认从配置读取）
    """
    if quarters is None:
        quarters = QUARTERS
    
    logger.info("=" * 50)
    logger.info(f"Starting financial data sync (last {quarters} quarters)...")
    
    start_time = datetime.now()
    
    # 检查熔断器状态
    if circuit_breaker.is_open():
        logger.warning("Circuit breaker is OPEN, skipping financial data sync")
        try:
            with get_db_session() as session:
                log = models.SyncLog(
                    sync_type="financial_data",
                    status="failed",
                    start_time=start_time,
                    end_time=datetime.now(),
                    records_synced=0,
                    error_message="Circuit breaker is open, sync skipped",
                )
                session.add(log)
        except Exception:
            pass
        return
    
    # 获取所有股票代码
    with get_db_session() as session:
        stock_list = [(s.stock_code, s.market) for s in session.query(models.StockInfo).all()]
    
    # 批量获取已有最新日期（增量判断）
    with get_db_session() as session:
        latest_dates = _get_latest_report_dates_bulk(session)
    
    logger.info(f"Loaded {len(latest_dates)} existing latest dates for incremental check")
    
    # 预获取所有A股市值
    a_share_codes = [code for code, mkt in stock_list if mkt == "CN_A"]
    all_market_caps = {}
    if a_share_codes:
        all_market_caps = _fetch_a_market_cap(a_share_codes)
        logger.info(f"Batch fetched market caps for {len(all_market_caps)} A-shares")
    
    # 预获取港股市值（填充缓存）
    hk_codes = [code for code, mkt in stock_list if mkt == "HK"]
    if hk_codes:
        # 先获取港股市值填充缓存
        try:
            for code in hk_codes[:10]:  # 先缓存几只，其余在同步时按需获取
                snap = ak.stock_hk_financial_indicator_em(symbol=code)
                if snap is not None and not snap.empty:
                    _hk_snapshot_cache[code] = snap.iloc[0].to_dict()
                time.sleep(0.3)
            logger.info(f"Pre-cached {min(10, len(hk_codes))} HK snapshots")
        except Exception as e:
            logger.warning(f"Failed to pre-cache HK snapshots: {e}")
    
    total = len(stock_list)
    success = 0
    skipped = 0
    failed = []
    
    for i, (stock_code, market) in enumerate(stock_list):
        if (i + 1) % 100 == 0:
            logger.info(f"Progress: {i+1}/{total} (skipped: {skipped})")
        
        if circuit_breaker.is_open():
            logger.warning(f"Circuit breaker opened during sync at stock {i+1}/{total}, stopping")
            failed.extend([s[0] for s in stock_list[i:]])
            break
        
        # 港股快照缓存按需获取
        if market == "HK" and stock_code not in _hk_snapshot_cache:
            try:
                snap = ak.stock_hk_financial_indicator_em(symbol=stock_code)
                if snap is not None and not snap.empty:
                    _hk_snapshot_cache[stock_code] = snap.iloc[0].to_dict()
            except Exception:
                pass
        
        try:
            # 获取财务指标
            indicator_df = fetch_financial_indicator(stock_code)
            
            if indicator_df.empty:
                failed.append(stock_code)
                time.sleep(0.2)
                continue
            
            # 增量判断：如果返回的最新指标日期 <= 数据库已有的，跳过
            if stock_code in latest_dates:
                db_latest = latest_dates[stock_code]
                api_latest = indicator_df["indicator_date"].max()
                api_latest_str = api_latest.isoformat() if hasattr(api_latest, 'isoformat') else str(api_latest)
                
                # 比较
                if _date_to_sort_key(api_latest_str) <= _date_to_sort_key(db_latest):
                    skipped += 1
                    if (i + 1) % 100 == 0:
                        pass  # 静默跳过，不打印
                    continue
            
            # 保存财务指标
            with get_db_session() as session:
                save_financial_indicator(session, stock_code, indicator_df)
            
            # FCF 计算（保留现有逻辑）
            mkt_cap = all_market_caps.get(stock_code)
            if market == "HK":
                snap = _hk_snapshot_cache.get(stock_code, {})
                mkt_cap = _safe_float(snap.get("总市值(港元)"))
            fetch_and_save_cash_flow(stock_code, market, pre_market_cap=mkt_cap)
            
            # ★ 三张报表同步
            if market == "CN_A":
                # A股：stock_financial_report_sina
                inc_df = fetch_a_income_statement(stock_code)
                bs_df = fetch_a_balance_sheet(stock_code)
                cf_df = fetch_a_cash_flow_statement(stock_code)
            else:
                # 港股：stock_financial_hk_report_em
                inc_df = fetch_hk_income_statement(stock_code)
                bs_df = fetch_hk_balance_sheet(stock_code)
                cf_df = fetch_hk_cash_flow_statement(stock_code)
            
            with get_db_session() as session:
                if inc_df is not None and not inc_df.empty:
                    save_income_statement(session, stock_code, inc_df, market)
                if bs_df is not None and not bs_df.empty:
                    save_balance_sheet(session, stock_code, bs_df, market)
                if cf_df is not None and not cf_df.empty:
                    save_cash_flow_statement(session, stock_code, cf_df, market)
            
            success += 1
            time.sleep(0.2)
            
        except Exception as e:
            logger.warning(f"Failed to sync {stock_code}: {e}")
            failed.append(stock_code)
    
    end_time = datetime.now()
    status = "success" if len(failed) == 0 else ("partial" if success > 0 else "failed")
    error_msg = f"Failed stocks (first 10): {failed[:10]}" if failed else None
    
    try:
        with get_db_session() as session:
            log = models.SyncLog(
                sync_type="financial_data",
                status=status,
                start_time=start_time,
                end_time=end_time,
                records_synced=success,
                records_failed=len(failed),
                error_message=error_msg,
            )
            session.add(log)
    except Exception as e:
        logger.error(f"Failed to write sync log: {e}")
    
    logger.info(f"Financial data sync completed: {success} synced, {skipped} skipped, {len(failed)} failed")
    if failed[:10]:
        logger.info(f"Failed stocks (first 10): {failed[:10]}")


def sync_index_constituent(index_codes: List[str] = None):
    """同步指数成分股"""
    if index_codes is None:
        index_codes = ["000300", "000905", "HSI"]
    
    logger.info("=" * 50)
    logger.info(f"Starting index constituent sync: {index_codes}")
    
    start_time = datetime.now()
    total_synced = 0
    status = "success"
    error_msg = None
    
    try:
        for index_code in index_codes:
            df = fetch_index_constituent(index_code)
            if df.empty:
                logger.warning(f"No data for index {index_code}")
                status = "partial" if status == "success" else status
                continue
            
            with get_db_session() as session:
                for _, row in df.iterrows():
                    constituent = models.IndexConstituent(
                        index_code=index_code,
                        index_name=row.get("名称", index_code),
                        stock_code=str(row.get("代码", row.get("code", ""))),
                        stock_name=str(row.get("名称", row.get("name", ""))),
                        effective_date=datetime.now().date(),
                        is_active=1,
                    )
                    session.add(constituent)
            
            total_synced += len(df)
            logger.info(f"Synced index {index_code}: {len(df)} constituents")
    except Exception as e:
        status = "failed"
        error_msg = str(e)
        logger.error(f"Index constituent sync failed: {e}")
    
    end_time = datetime.now()
    
    try:
        with get_db_session() as session:
            log = models.SyncLog(
                sync_type="index_constituent",
                status=status,
                start_time=start_time,
                end_time=end_time,
                records_synced=total_synced,
                error_message=error_msg,
            )
            session.add(log)
    except Exception as e:
        logger.error(f"Failed to write sync log: {e}")
    
    logger.info(f"Index constituent sync completed: status={status}, records={total_synced}")


# ============================================================
# 入口函数
# ============================================================

def run_initial_sync(quarters: int = None):
    """
    执行初始同步
    1. 股票列表
    2. 指数成分
    3. 财务数据（含三张报表）
    """
    if quarters is None:
        quarters = config.SYNC_CONFIG["quarters"]
    
    logger.info("=" * 60)
    logger.info(f"Starting INITIAL SYNC (last {quarters} quarters)")
    logger.info("=" * 60)
    
    start_time = time.time()
    
    sync_stock_list()
    sync_index_constituent()
    sync_financial_data(quarters=quarters)
    
    elapsed = time.time() - start_time
    logger.info("=" * 60)
    logger.info(f"INITIAL SYNC completed in {elapsed/60:.1f} minutes")
    logger.info("=" * 60)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    run_initial_sync(quarters=config.SYNC_CONFIG["quarters"])