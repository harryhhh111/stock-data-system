"""
数据获取模块 - 使用akshare获取A股/港股基本面数据
"""
import logging
import time
import threading
import signal
from datetime import datetime, timedelta
from functools import wraps
from typing import Dict, List, Optional, Any
import pandas as pd
import akshare as ak
from sqlalchemy.orm import Session

import config
import models
from models import get_engine, init_db
from database import get_db_session

logger = logging.getLogger(__name__)


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
            # 检查是否已过恢复期
            if self.last_failure_time is not None:
                elapsed = time.time() - self.last_failure_time
                if elapsed >= self.reset_minutes * 60:
                    # 重置熔断器
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
    - 检查熔断器状态
    - 最多重试 max_retries 次，每次延迟指数增长
    - 通过 signal.alarm 实现 timeout（仅 Unix）
    """
    if max_retries is None:
        max_retries = config.RETRY_CONFIG["max_retries"]
    if timeout is None:
        timeout = config.RETRY_CONFIG["timeout"]

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            # 熔断检查
            if circuit_breaker.is_open():
                logger.warning(
                    f"Circuit breaker is OPEN, skipping call: {func.__name__}"
                )
                return None

            last_exception = None
            for attempt in range(1, max_retries + 1):
                try:
                    # 设置超时（仅 Unix 系统）
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

                    # 成功：重置熔断器
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

                # 最后一次不再等待
                if attempt < max_retries:
                    delay = min(
                        config.RETRY_CONFIG["base_delay"] * (2 ** (attempt - 1)),
                        config.RETRY_CONFIG["max_delay"],
                    )
                    logger.info(f"[{func.__name__}] Retrying in {delay:.1f}s...")
                    time.sleep(delay)

            # 所有重试都失败
            circuit_breaker.record_failure()
            logger.error(
                f"[{func.__name__}] All {max_retries} attempts failed. "
                f"Circuit breaker failures: {circuit_breaker.failure_count}"
            )
            return None

        return wrapper
    return decorator


# ============================================================
# 数据获取函数
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
        # stock_hk_spot 返回的列名含中文
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
    获取财务指标
    period: "季度" 或 "年报"
    """
    result = _fetch_financial_indicator_impl(stock_code, period)
    return result if isinstance(result, pd.DataFrame) else pd.DataFrame()


@with_retry()
def _fetch_financial_indicator_impl(stock_code: str, period: str = "季度") -> pd.DataFrame:
    """获取财务指标，A股和港股使用不同的API"""
    try:
        # 判断是否为港股（港股代码通常是5位数字，如 00700）
        is_hk = len(stock_code) == 5 and stock_code.isdigit()
        
        if is_hk:
            # 港股财务指标
            df = ak.stock_hk_financial_indicator_em(symbol=stock_code)
            if df is None or df.empty:
                return pd.DataFrame()
            # 转换为统一格式：一行一条记录
            record = {
                "indicator_date": datetime.now().date(),
                "pe": df.iloc[0].get("市盈率"),
                "pb": df.iloc[0].get("市净率"),
                "roe": df.iloc[0].get("股东权益回报率(%)"),
                "market_cap": df.iloc[0].get("总市值(港元)"),
            }
            result_df = pd.DataFrame([record])
            return result_df
        else:
            # A股财务指标 - 使用同花顺数据源（新浪源不稳定）
            df = ak.stock_financial_abstract_ths(symbol=stock_code, indicator="按报告期")
            if df is None or df.empty:
                return pd.DataFrame()
            
            # 同花顺源按报告期从早到晚排列，取最后4行（最近4个季度）
            df_recent = df.tail(4).copy()
            
            # 获取市值（腾讯财经API）
            market_caps = _fetch_a_market_cap([stock_code])
            mkt_cap = market_caps.get(stock_code)
            
            def _parse_percent(value):
                """解析百分比字符串为浮点数"""
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
                    "net_margin": _parse_percent(row.get("销售净利率")),
                    "revenue_growth": _parse_percent(row.get("营业总收入同比增长率")),
                    "profit_growth": _parse_percent(row.get("净利润同比增长率")),
                    "debt_ratio": _parse_percent(row.get("资产负债率")),
                    "market_cap": mkt_cap,
                }
                # 同花顺源没有毛利率，但可以从销售净利率间接参考
                records.append(record)
            
            return pd.DataFrame(records)
    except Exception as e:
        logger.warning(f"Failed to fetch financial indicator for {stock_code}: {e}")
        return pd.DataFrame()


def _safe_float(value):
    """安全转换为浮点数"""
    try:
        if pd.isna(value):
            return None
        return float(value)
    except (ValueError, TypeError):
        return None


def _fetch_a_market_cap(stock_codes: list) -> dict:
    """
    批量获取A股市值（腾讯财经API）
    stock_codes: ['000001', '600519']（不带市场前缀）
    返回: {'000001': 209001738992.46, '600519': 18095300000000.0, ...} 单位：元
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
                    # 市值字段在 index 45，单位是亿元，转换为元
                    result[code] = float(parts[45]) * 1e8
                except (ValueError, TypeError):
                    result[code] = None
        return result
    except Exception as e:
        logger.warning(f"Failed to fetch A-share market cap: {e}")
        return {}


def fetch_income_statement(stock_code: str) -> pd.DataFrame:
    """获取利润表"""
    result = _fetch_income_statement_impl(stock_code)
    return result if isinstance(result, pd.DataFrame) else pd.DataFrame()


@with_retry()
def _fetch_income_statement_impl(stock_code: str) -> pd.DataFrame:
    try:
        df = ak.stock_financial_report_sina_by_report_em(symbol=stock_code)
        if df is None or df.empty:
            return pd.DataFrame()
        return df
    except Exception as e:
        logger.warning(f"Failed to fetch income statement for {stock_code}: {e}")
        return pd.DataFrame()


def fetch_balance_sheet(stock_code: str) -> pd.DataFrame:
    """获取资产负债表"""
    result = _fetch_balance_sheet_impl(stock_code)
    return result if isinstance(result, pd.DataFrame) else pd.DataFrame()


@with_retry()
def _fetch_balance_sheet_impl(stock_code: str) -> pd.DataFrame:
    try:
        df = ak.stock_financial_report_sina_by_balance_sheet_em(symbol=stock_code)
        if df is None or df.empty:
            return pd.DataFrame()
        return df
    except Exception as e:
        logger.warning(f"Failed to fetch balance sheet for {stock_code}: {e}")
        return pd.DataFrame()


def fetch_cash_flow(stock_code: str) -> pd.DataFrame:
    """获取现金流量表"""
    result = _fetch_cash_flow_impl(stock_code)
    return result if isinstance(result, pd.DataFrame) else pd.DataFrame()


@with_retry()
def _fetch_cash_flow_impl(stock_code: str) -> pd.DataFrame:
    try:
        df = ak.stock_financial_report_sina_by_cash_flow_em(symbol=stock_code)
        if df is None or df.empty:
            return pd.DataFrame()
        return df
    except Exception as e:
        logger.warning(f"Failed to fetch cash flow for {stock_code}: {e}")
        return pd.DataFrame()


def fetch_and_save_cash_flow(stock_code: str, market: str, pre_market_cap: float = None) -> bool:
    """
    获取现金流量表，计算FCF，更新到 financial_indicator 表
    
    Args:
        stock_code: 股票代码
        market: 市场标识，'CN_A' 或 'HK'
        pre_market_cap: 预获取的市值（避免重复请求）
    
    Returns:
        bool: 是否成功
    """
    return _fetch_and_save_cash_flow_impl(stock_code, market, pre_market_cap)


@with_retry(timeout=30)
def _fetch_a_cash_flow_impl(stock_code: str) -> Optional[pd.DataFrame]:
    """获取A股现金流量表（只取最新1期）"""
    try:
        if stock_code.startswith(("6", "9")):
            symbol = f"SH{stock_code}"
        else:
            symbol = f"SZ{stock_code}"
        
        df = ak.stock_cash_flow_sheet_by_report_em(symbol=symbol)
        if df is not None and not df.empty:
            df = df.head(1)  # 只取最新1期
        return df
    except Exception as e:
        logger.warning(f"Failed to fetch A-share cash flow for {stock_code}: {e}")
        return None


@with_retry(timeout=60)
def _fetch_hk_cash_flow_impl(stock_code: str) -> Optional[pd.DataFrame]:
    """获取港股现金流量表（最新季度数据）"""
    try:
        # 港股使用 stock_financial_hk_report_em，优先取季度数据以获取最新一期
        # 年度数据可能滞后（如2025年报未出时只能拿到2024年报）
        try:
            df = ak.stock_financial_hk_report_em(
                stock=stock_code,
                symbol="现金流量表",
                indicator="季度"
            )
        except Exception:
            # 季度接口失败时回退到年度
            df = ak.stock_financial_hk_report_em(
                stock=stock_code,
                symbol="现金流量表",
                indicator="年度"
            )
        
        if df is not None and not df.empty:
            # 按 REPORT_DATE 降序排序，只保留最新报告期的数据
            df = df.sort_values("REPORT_DATE", ascending=False)
            latest_date = df["REPORT_DATE"].iloc[0]
            df = df[df["REPORT_DATE"] == latest_date]
            logger.info(f"港股 cash flow for {stock_code}: {len(df)} rows, latest report: {latest_date}")
        return df
    except Exception as e:
        logger.warning(f"Failed to fetch HK cash flow for {stock_code}: {e}")
        return None


@with_retry(timeout=30)
def _fetch_and_save_cash_flow_impl(stock_code: str, market: str, pre_market_cap: float = None) -> bool:
    """获取现金流量表并计算FCF更新到数据库"""
    try:
        if market == "CN_A":
            cf_df = _fetch_a_cash_flow_impl(stock_code)
        elif market == "HK":
            cf_df = _fetch_hk_cash_flow_impl(stock_code)
        else:
            logger.warning(f"Unknown market: {market}")
            return False
        
        if cf_df is None or cf_df.empty:
            logger.warning(f"Empty cash flow data for {stock_code}")
            return False
        
        # 提取经营现金流和资本支出
        operating_cf = None
        capital_expense = None
        
        if market == "CN_A":
            # A股是宽格式：每行是一个报告期，列是指标
            # 取第一行（最新报告期）
            if "NETCASH_OPERATE" in cf_df.columns:
                operating_cf = _safe_float(cf_df["NETCASH_OPERATE"].iloc[0])
            if "CONSTRUCT_LONG_ASSET" in cf_df.columns:
                capital_expense = _safe_float(cf_df["CONSTRUCT_LONG_ASSET"].iloc[0])
        
        elif market == "HK":
            # 港股是长格式：每行是一个指标，STD_ITEM_NAME是指标名，AMOUNT是值
            # 找"经营业务现金净额"和"购建固定资产"
            for _, row in cf_df.iterrows():
                item_name = str(row.get("STD_ITEM_NAME", ""))
                amount = _safe_float(row.get("AMOUNT"))
                
                if item_name == "经营业务现金净额" and operating_cf is None:
                    operating_cf = amount
                elif item_name == "购建固定资产" and capital_expense is None:
                    capital_expense = amount
        
        # 计算FCF
        if operating_cf is None:
            logger.warning(f"Cannot find operating cash flow for {stock_code}")
            return False
        
        if capital_expense is None:
            capital_expense = 0  # 如果没有资本支出数据，假设为0
        
        fcf = operating_cf - capital_expense
        
        # 获取市值用于计算FCF收益率
        market_cap = pre_market_cap
        
        # 如果预获取没有市值，从数据库查
        if market_cap is None:
            with get_db_session() as session:
                latest = session.query(models.FinancialIndicator).filter(
                    models.FinancialIndicator.stock_code == stock_code,
                    models.FinancialIndicator.market_cap.isnot(None)
                ).order_by(
                    models.FinancialIndicator.indicator_date.desc()
                ).first()
                market_cap = latest.market_cap if latest else None
        
        # 如果数据库也没有，尝试从腾讯API获取（仅A股）
        if market_cap is None and market == "CN_A":
            caps = _fetch_a_market_cap([stock_code])
            market_cap = caps.get(stock_code)
        
        # 计算FCF收益率 (%)
        fcf_yield = None
        if market_cap and market_cap > 0:
            fcf_yield = (fcf / market_cap) * 100  # 转为百分比
        
        logger.info(
            f"Cash flow for {stock_code}: operating_cf={operating_cf}, "
            f"capex={capital_expense}, fcf={fcf}, market_cap={market_cap}, "
            f"fcf_yield={fcf_yield}%"
        )
        
        # 保存到数据库
        with get_db_session() as session:
            # 找到该股票最新的财务指标记录
            record = session.query(models.FinancialIndicator).filter(
                models.FinancialIndicator.stock_code == stock_code
            ).order_by(
                models.FinancialIndicator.indicator_date.desc()
            ).first()
            
            if record:
                record.fcf = fcf
                record.fcf_yield = fcf_yield
                logger.info(f"Updated FCF for {stock_code}: fcf={fcf}, fcf_yield={fcf_yield}%")
            else:
                logger.warning(f"No financial indicator record found for {stock_code}")
        
        return True
        
    except Exception as e:
        logger.error(f"Error in fetch_and_save_cash_flow for {stock_code}: {e}")
        return False


def fetch_index_constituent(index_code: str) -> pd.DataFrame:
    """获取指数成分股"""
    result = _fetch_index_constituent_impl(index_code)
    return result if isinstance(result, pd.DataFrame) else pd.DataFrame()


@with_retry()
def _fetch_index_constituent_impl(index_code: str) -> pd.DataFrame:
    try:
        if index_code == "000300":  # 沪深300
            df = ak.index_stock_cons_hs300()
        elif index_code == "000905":  # 中证500
            df = ak.index_stock_cons_zz500()
        elif index_code.startswith("00"):  # 沪深指数
            df = ak.index_stock_cons(symbol=index_code)
        elif index_code.startswith("hk") or index_code.startswith("HSI"):  # 恒生指数
            df = ak.hk_stocks_hs_const()
        else:
            logger.warning(f"Unknown index code: {index_code}")
            return pd.DataFrame()
        
        if df is None or df.empty:
            return pd.DataFrame()
        return df
    except Exception as e:
        logger.warning(f"Failed to fetch index constituent for {index_code}: {e}")
        return pd.DataFrame()


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
        
        # 幂等：先查是否已存在
        existing = session.query(models.FinancialIndicator).filter(
            models.FinancialIndicator.stock_code == stock_code,
            models.FinancialIndicator.indicator_date == indicator_date,
        ).first()
        
        if existing:
            # 更新已有记录
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
        # A股
        a_df = fetch_a_stock_list()
        if not a_df.empty:
            with get_db_session() as session:
                save_stock_info(session, a_df, "CN_A")
            total_synced += len(a_df)
            logger.info(f"Saved {len(a_df)} A-share stocks")
        else:
            logger.warning("A-share list returned empty")
            status = "partial"
        
        # 港股
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
    
    # 记录同步日志
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


def sync_financial_data(days: int = 30):
    """
    同步财务数据
    days: 回溯天数
    """
    logger.info("=" * 50)
    logger.info(f"Starting financial data sync (last {days} days)...")
    
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
    
    # 预获取所有A股市值（批量，<1秒）
    a_share_codes = [code for code, mkt in stock_list if mkt == "CN_A"]
    all_market_caps = {}
    if a_share_codes:
        all_market_caps = _fetch_a_market_cap(a_share_codes)
        logger.info(f"Batch fetched market caps for {len(all_market_caps)} A-shares")
    
    total = len(stock_list)
    success = 0
    failed = []
    
    for i, (stock_code, market) in enumerate(stock_list):
        if (i + 1) % 100 == 0:
            logger.info(f"Progress: {i+1}/{total}")
        
        # 运行时再次检查熔断器
        if circuit_breaker.is_open():
            logger.warning(f"Circuit breaker opened during sync at stock {i+1}/{total}, stopping")
            failed.extend([s[0] for s in stock_list[i:]])
            break
        
        try:
            # 财务指标
            indicator_df = fetch_financial_indicator(stock_code)
            if not indicator_df.empty:
                with get_db_session() as session:
                    save_financial_indicator(session, stock_code, indicator_df)
                success += 1
            else:
                failed.append(stock_code)
            
            # 现金流量表（计算FCF）
            mkt_cap = all_market_caps.get(stock_code)
            fetch_and_save_cash_flow(stock_code, market, pre_market_cap=mkt_cap)
            
            # 限流（已批量获取市值，减少等待）
            time.sleep(0.2)
            
        except Exception as e:
            logger.warning(f"Failed to sync {stock_code}: {e}")
            failed.append(stock_code)
    
    end_time = datetime.now()
    status = "success" if len(failed) == 0 else ("partial" if success > 0 else "failed")
    error_msg = f"Failed stocks (first 10): {failed[:10]}" if failed else None
    
    # 记录同步日志
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
    
    logger.info(f"Financial data sync completed: {success} success, {len(failed)} failed")
    if failed[:10]:
        logger.info(f"Failed stocks (first 10): {failed[:10]}")


def sync_index_constituent(index_codes: List[str] = None):
    """同步指数成分股"""
    if index_codes is None:
        index_codes = ["000300", "000905", "HSI"]  # 沪深300、中证500、恒生
    
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
    
    # 记录同步日志
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

def run_initial_sync(days: int = 30):
    """
    执行初始同步
    1. 股票列表
    2. 指数成分
    3. 财务数据
    """
    logger.info("=" * 60)
    logger.info(f"Starting INITIAL SYNC (lookback: {days} days)")
    logger.info("=" * 60)
    
    start_time = time.time()
    
    # Step 1: 同步股票列表
    sync_stock_list()
    
    # Step 2: 同步指数成分
    sync_index_constituent()
    
    # Step 3: 同步财务数据
    sync_financial_data(days=days)
    
    elapsed = time.time() - start_time
    logger.info("=" * 60)
    logger.info(f"INITIAL SYNC completed in {elapsed/60:.1f} minutes")
    logger.info("=" * 60)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    run_initial_sync(days=config.SYNC_CONFIG["lookback_days"])
