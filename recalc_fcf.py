"""
只重新计算 FCF（现金流量表），不重新拉取财务指标。
修复：港股从"年度"数据改为"季度"数据。
"""
import sys
import time
import logging
import os
from datetime import datetime
from sqlalchemy import text

# 禁用 tqdm 进度条，避免吞掉日志
import tqdm
tqdm.tqdm.disable = True

sys.path.insert(0, '/root/projects/stock_data')

from database import get_db_session, get_engine
import models
from data_fetcher import _fetch_a_cash_flow_impl, _fetch_hk_cash_flow_impl, _safe_float

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler('/root/projects/stock_data/data/recalc_fcf.log', mode='w')],
    force=True
)
logger = logging.getLogger(__name__)


def recalc_fcf():
    """重新计算所有股票的 FCF"""
    logger.info("=" * 60)
    logger.info("Starting FCF recalculation (HK: quarterly, A-share: latest report)")
    
    start_time = datetime.now()
    
    with get_db_session() as session:
        stock_list = [(s.stock_code, s.market) for s in session.query(models.StockInfo).all()]
    
    total = len(stock_list)
    success = 0
    skipped = 0
    failed = 0
    failed_stocks = []
    
    for i, (stock_code, market) in enumerate(stock_list):
        if (i + 1) % 50 == 0:
            elapsed = (datetime.now() - start_time).total_seconds()
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            remaining = (total - i - 1) / rate / 60 if rate > 0 else 0
            logger.info(f"Progress: {i+1}/{total} ({rate:.1f} stocks/s, ~{remaining:.0f}min remaining)")
        
        try:
            # 拉取现金流量表
            if market == "CN_A":
                cf_df = _fetch_a_cash_flow_impl(stock_code)
            elif market == "HK":
                cf_df = _fetch_hk_cash_flow_impl(stock_code)
            else:
                skipped += 1
                continue
            
            if cf_df is None or cf_df.empty:
                failed += 1
                continue
            
            # 提取经营现金流和资本支出
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
                failed += 1
                continue
            
            if capital_expense is None:
                capital_expense = 0
            
            fcf = operating_cf - capital_expense
            
            # 获取市值
            market_cap = None
            with get_db_session() as session2:
                latest = session2.query(models.FinancialIndicator).filter(
                    models.FinancialIndicator.stock_code == stock_code,
                    models.FinancialIndicator.market_cap.isnot(None)
                ).order_by(
                    models.FinancialIndicator.indicator_date.desc()
                ).first()
                if latest:
                    market_cap = latest.market_cap
            
            fcf_yield = None
            if market_cap and market_cap > 0:
                fcf_yield = (fcf / market_cap) * 100
            
            # 更新数据库
            with get_db_session() as session3:
                record = session3.query(models.FinancialIndicator).filter(
                    models.FinancialIndicator.stock_code == stock_code
                ).order_by(
                    models.FinancialIndicator.indicator_date.desc()
                ).first()
                
                if record:
                    record.fcf = fcf
                    record.fcf_yield = fcf_yield
                    session3.commit()
                    success += 1
                else:
                    skipped += 1
            
            # 限流
            time.sleep(0.2)
            
        except Exception as e:
            failed += 1
            if len(failed_stocks) < 20:
                failed_stocks.append(f"{stock_code}: {e}")
            logger.warning(f"Failed: {stock_code} - {e}")
    
    end_time = datetime.now()
    elapsed = (end_time - start_time).total_seconds()
    
    logger.info("=" * 60)
    logger.info(f"FCF recalculation completed in {elapsed/60:.1f} minutes")
    logger.info(f"  Total: {total}")
    logger.info(f"  Success: {success}")
    logger.info(f"  Skipped: {skipped}")
    logger.info(f"  Failed: {failed}")
    if failed_stocks:
        logger.info(f"  Failed stocks (first 20): {failed_stocks}")


if __name__ == "__main__":
    recalc_fcf()
