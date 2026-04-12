#!/usr/bin/env python3
"""全量 reparse 美股财务数据（从 raw_snapshot 重新解析）"""
import sys, json, time, logging
sys.path.insert(0, '/root/projects/stock_data')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')

from db import execute, upsert
from transformers.us_gaap import USGAAPTransformer
from fetchers.us_financial import USFinancialFetcher

transformer = USGAAPTransformer()
fetcher = USFinancialFetcher()

tables = [
    ('us_income_statement', fetcher.INCOME_TAGS, transformer.transform_income),
    ('us_balance_sheet', fetcher.BALANCE_TAGS, transformer.transform_balance),
    ('us_cash_flow_statement', fetcher.CASHFLOW_TAGS, transformer.transform_cashflow),
]

rows = execute(
    "SELECT stock_code, cik FROM stock_info WHERE market='US' AND cik IS NOT NULL ORDER BY stock_code",
    fetch=True
)
print(f"共 {len(rows)} 只股票，3 张表")

t0 = time.time()
total_ok = 0

# 先清空旧数据，避免 COALESCE 保护导致 None 不被覆盖
for table_name, _, _ in tables:
    execute(f"DELETE FROM {table_name}")
print("旧数据已清空")

for i, (ticker, cik) in enumerate(rows):
    try:
        raw = execute(
            "SELECT raw_data::text FROM raw_snapshot WHERE stock_code=%s AND data_type='company_facts'",
            (ticker,), fetch=True
        )
        if not raw:
            continue
        facts = json.loads(raw[0][0])

        for table_name, tag_map, transform_fn in tables:
            df = fetcher.extract_table(facts, tag_map)
            if df.empty:
                continue
            records = transform_fn(df, stock_code=ticker, cik=cik)
            if records:
                upsert(table_name, records, ["stock_code", "report_date", "report_type"])
                total_ok += 1

    except Exception as e:
        print(f"  {ticker}: {e}")

    if (i + 1) % 100 == 0:
        print(f"  进度: {i+1}/{len(rows)}, 耗时 {time.time()-t0:.0f}s")

elapsed = time.time() - t0
print(f"\n完成! {total_ok} 次写入, 耗时 {elapsed:.1f}s")

# 验证 MELI
r = execute("SELECT report_date, report_type, net_income FROM us_income_statement WHERE stock_code='MELI' AND report_type='annual' ORDER BY report_date", fetch=True)
print(f"\nMELI annual net_income:")
for row in r:
    print(f"  {row[0]} {row[1]} {row[2]}")
