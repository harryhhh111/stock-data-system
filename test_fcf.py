#!/usr/bin/env python3
"""
Test script to verify FCF calculation works correctly
"""
from fetchers.us_financial import USFinancialFetcher

import pandas as pd
import logging

logging.basicConfig(level=logging.INFO)

fetcher = USFinancialFetcher()
df = fetcher.fetch_cashflow('AAPL')

print(f'Columns: {df.columns.tolist()[:10]}')
print(f'Has free_cash_flow: {"free_cash_flow" in df.columns}')

print('\nFirst 5 rows:')
for idx in range(5):
    row = df.iloc[idx]
    cfo = row.get('net_cash_from_operations')
    capex = row.get('capital_expenditures')
    fcf = row.get('free_cash_flow')
    print(f'Row {idx}: end={row["end"]}, fp={row["fp"]}, CFO={cfo}, CapEx={capex}, FCF={fcf}')

    if fcf is not None:
        print(f'  -> FCF is None (not calculated)')

print('\nLast 5 rows:')
for idx in range(-1, -6):
    row = df.iloc[idx]
    cfo = row.get('net_cash_from_operations')
    capex = row.get('capital_expenditures')
    fcf = row.get('free_cash_flow')
    print(f'Row {idx}: end={row["end"]}, fp={row["fp"]}, CFO={cfo}, CapEx={capex}, FCF={fcf}')
    if fcf is not None:
        print(f'  -> FCF is None (not calculated)')
