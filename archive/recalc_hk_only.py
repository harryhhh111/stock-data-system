"""只跑港股 FCF 重算，无限流，串行"""
import sys, os, time, logging, sqlite3
from datetime import datetime

os.environ["TQDM_DISABLE"] = "1"
sys.path.insert(0, '/root/projects/stock_data')

from data_fetcher import _fetch_hk_cash_flow_impl, _safe_float, circuit_breaker

LOG = '/root/projects/stock_data/data/recalc_hk_only.log'
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                    handlers=[logging.FileHandler(LOG, mode='w'), logging.StreamHandler()])
logger = logging.getLogger(__name__)

DB = '/root/projects/stock_data/data/stock_data.db'

def main():
    logger.info("Starting HK-only FCF recalculation (no sleep)...")
    start = datetime.now()
    
    conn = sqlite3.connect(DB)
    stock_list = conn.execute("SELECT stock_code FROM stock_info WHERE market='CN_HK' ORDER BY stock_code").fetchall()
    conn.close()
    
    total = len(stock_list)
    logger.info(f"HK stocks: {total}")
    
    ok = skip = fail = 0
    
    for i, (code,) in enumerate(stock_list):
        if (i+1) % 100 == 0:
            elapsed = (datetime.now() - start).total_seconds()
            rate = (i+1)/elapsed if elapsed > 0 else 0
            eta = (total-i-1)/rate/60 if rate > 0 else 0
            logger.info(f"Progress: {i+1}/{total} ({rate:.1f}/s, ETA {eta:.0f}min) ok={ok} fail={fail}")
        
        if circuit_breaker.is_open():
            logger.warning(f"Circuit breaker OPEN at {i+1}, waiting 60s...")
            time.sleep(60)
            circuit_breaker._failure_count = 0
            circuit_breaker._open_until = None
            continue
        
        try:
            cf_df = _fetch_hk_cash_flow_impl(code)
            if cf_df is None or cf_df.empty:
                fail += 1; continue
            
            op_cf = capex = None
            for _, r in cf_df.iterrows():
                n = str(r.get("STD_ITEM_NAME", ""))
                a = _safe_float(r.get("AMOUNT"))
                if n == "经营业务现金净额" and op_cf is None: op_cf = a
                elif n == "购建固定资产" and capex is None: capex = a
            
            if op_cf is None:
                fail += 1; continue
            if capex is None: capex = 0
            fcf = op_cf - capex
            
            c = sqlite3.connect(DB)
            row = c.execute(
                "SELECT market_cap FROM financial_indicator WHERE stock_code=? AND market_cap IS NOT NULL ORDER BY indicator_date DESC LIMIT 1",
                (code,)
            ).fetchone()
            market_cap = row[0] if row else None
            fcf_yield = (fcf / market_cap * 100) if market_cap and market_cap > 0 else None
            
            c.execute(
                "UPDATE financial_indicator SET fcf=?, fcf_yield=? WHERE stock_code=? AND indicator_date=(SELECT MAX(indicator_date) FROM financial_indicator WHERE stock_code=?)",
                (fcf, fcf_yield, code, code)
            )
            c.commit()
            c.close()
            ok += 1
            circuit_breaker.record_success()
        except Exception as e:
            fail += 1
            if i < 5:
                logger.error(f"Error {code}: {e}")
    
    elapsed = (datetime.now() - start).total_seconds()
    logger.info(f"Done in {elapsed/60:.1f}min | ok={ok} fail={fail} skip={total-ok-fail}")

if __name__ == "__main__":
    main()
