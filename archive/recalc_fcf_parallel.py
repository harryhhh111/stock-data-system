"""并行 FCF 重算 - 多线程版本"""
import sys, os, time, logging, sqlite3, threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

os.environ["TQDM_DISABLE"] = "1"
sys.path.insert(0, '/root/projects/stock_data')

LOG = '/root/projects/stock_data/data/recalc_fcf_parallel.log'
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                    handlers=[logging.FileHandler(LOG, mode='w'), logging.StreamHandler()])
logger = logging.getLogger(__name__)

DB = '/root/projects/stock_data/data/stock_data.db'

def get_stock_list():
    conn = sqlite3.connect(DB)
    rows = conn.execute("SELECT stock_code, market FROM stock_info ORDER BY market, stock_code").fetchall()
    conn.close()
    return rows

def get_market_cap(stock_code):
    conn = sqlite3.connect(DB)
    row = conn.execute(
        "SELECT market_cap FROM financial_indicator WHERE stock_code=? AND market_cap IS NOT NULL ORDER BY indicator_date DESC LIMIT 1",
        (stock_code,)
    ).fetchone()
    conn.close()
    return row[0] if row else None

def process_one(item):
    code, market = item
    try:
        if market == "CN_A":
            from data_fetcher import _fetch_a_cash_flow_impl, _safe_float
            cf_df = _fetch_a_cash_flow_impl(code)
            if cf_df is None or cf_df.empty:
                return (code, 'skip', None)
            op_cf = _safe_float(cf_df["NETCASH_OPERATE"].iloc[0]) if "NETCASH_OPERATE" in cf_df.columns else None
            capex = _safe_float(cf_df["CONSTRUCT_LONG_ASSET"].iloc[0]) if "CONSTRUCT_LONG_ASSET" in cf_df.columns else None
        else:
            from data_fetcher import _fetch_hk_cash_flow_impl, _safe_float
            cf_df = _fetch_hk_cash_flow_impl(code)
            if cf_df is None or cf_df.empty:
                return (code, 'skip', None)
            op_cf = capex = None
            for _, r in cf_df.iterrows():
                n = str(r.get("STD_ITEM_NAME", ""))
                a = _safe_float(r.get("AMOUNT"))
                if n == "经营业务现金净额" and op_cf is None: op_cf = a
                elif n == "购建固定资产" and capex is None: capex = a
        
        if op_cf is None:
            return (code, 'fail', None)
        if capex is None:
            capex = 0
        fcf = op_cf - capex
        
        # 写数据库
        market_cap = get_market_cap(code)
        fcf_yield = (fcf / market_cap * 100) if market_cap and market_cap > 0 else None
        
        conn = sqlite3.connect(DB)
        conn.execute(
            "UPDATE financial_indicator SET fcf=?, fcf_yield=? WHERE stock_code=? AND rowid = (SELECT rowid FROM financial_indicator WHERE stock_code=? ORDER BY indicator_date DESC LIMIT 1)",
            (fcf, fcf_yield, code, code)
        )
        conn.commit()
        conn.close()
        return (code, 'ok', fcf)
    except Exception as e:
        return (code, 'fail', str(e))

def main():
    logger.info("Starting PARALLEL FCF recalculation...")
    start = datetime.now()
    
    stock_list = get_stock_list()
    total = len(stock_list)
    logger.info(f"Total stocks: {total}")
    
    ok = skip = fail = 0
    done = 0
    
    # 10 个并发线程
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(process_one, item): item for item in stock_list}
        
        for future in as_completed(futures):
            code, status, fcf = future.result()
            done += 1
            if status == 'ok':
                ok += 1
            elif status == 'skip':
                skip += 1
            else:
                fail += 1
            
            if done % 100 == 0:
                elapsed = (datetime.now() - start).total_seconds()
                rate = done / elapsed if elapsed > 0 else 0
                eta = (total - done) / rate / 60 if rate > 0 else 0
                logger.info(f"Progress: {done}/{total} ({done/total*100:.1f}%)  {rate:.1f}/s  ETA {eta:.0f}min  ok={ok} skip={skip} fail={fail}")
    
    elapsed = (datetime.now() - start).total_seconds()
    logger.info(f"Done in {elapsed/60:.1f}min | ok={ok} skip={skip} fail={fail} total={total}")

if __name__ == "__main__":
    main()
