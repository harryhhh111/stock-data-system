"""只重新计算 FCF（现金流量表），不重新拉取财务指标。"""
import sys, time, logging
from datetime import datetime

# 禁用 tqdm
import os
os.environ["TQDM_DISABLE"] = "1"

sys.path.insert(0, '/root/projects/stock_data')

from database import get_db_session
import models
from data_fetcher import _fetch_a_cash_flow_impl, _fetch_hk_cash_flow_impl, _safe_float

LOG = '/root/projects/stock_data/data/recalc_fcf_v2.log'
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                    handlers=[logging.FileHandler(LOG, mode='w'), logging.StreamHandler()])
logger = logging.getLogger(__name__)

def main():
    logger.info("Starting FCF recalculation...")
    start = datetime.now()

    with get_db_session() as session:
        stock_list = [(s.stock_code, s.market) for s in session.query(models.StockInfo).all()]
    
    total = len(stock_list)
    ok = skip = fail = 0
    
    for i, (code, market) in enumerate(stock_list):
        if (i+1) % 50 == 0:
            elapsed = (datetime.now() - start).total_seconds()
            rate = (i+1)/elapsed if elapsed > 0 else 0
            eta = (total-i-1)/rate/60 if rate > 0 else 0
            logger.info(f"Progress: {i+1}/{total}  {rate:.1f}/s  ETA {eta:.0f}min")
        
        try:
            cf_df = _fetch_a_cash_flow_impl(code) if market == "CN_A" else _fetch_hk_cash_flow_impl(code)
            if cf_df is None or cf_df.empty:
                fail += 1; continue
            
            op_cf = capex = None
            if market == "CN_A":
                if "NETCASH_OPERATE" in cf_df.columns:
                    op_cf = _safe_float(cf_df["NETCASH_OPERATE"].iloc[0])
                if "CONSTRUCT_LONG_ASSET" in cf_df.columns:
                    capex = _safe_float(cf_df["CONSTRUCT_LONG_ASSET"].iloc[0])
            elif market == "HK":
                for _, r in cf_df.iterrows():
                    n = str(r.get("STD_ITEM_NAME", ""))
                    a = _safe_float(r.get("AMOUNT"))
                    if n == "经营业务现金净额" and op_cf is None: op_cf = a
                    elif n == "购建固定资产" and capex is None: capex = a
            
            if op_cf is None:
                fail += 1; continue
            if capex is None: capex = 0
            
            fcf = op_cf - capex
            
            with get_db_session() as s:
                rec = s.query(models.FinancialIndicator).filter(
                    models.FinancialIndicator.stock_code == code
                ).order_by(models.FinancialIndicator.indicator_date.desc()).first()
                if rec:
                    rec.fcf = fcf
                    if rec.market_cap and rec.market_cap > 0:
                        rec.fcf_yield = (fcf / rec.market_cap) * 100
                    s.commit()
                    ok += 1
                else:
                    skip += 1
        except Exception as e:
            fail += 1
        
        time.sleep(0.2)
    
    elapsed = (datetime.now() - start).total_seconds()
    logger.info(f"Done in {elapsed/60:.1f}min | ok={ok} skip={skip} fail={fail} total={total}")

if __name__ == "__main__":
    main()
