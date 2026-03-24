#!/usr/bin/env python3
"""
数据库查看脚本 - 查看数据统计和信息
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
import models

def main():
    print("=" * 50)
    print("数据库统计")
    print("=" * 50)
    
    tables = [
        ("股票信息 (StockInfo)", models.StockInfo),
        ("财务指标 (FinancialIndicator)", models.FinancialIndicator),
        ("利润表 (IncomeStatement)", models.IncomeStatement),
        ("资产负债表 (BalanceSheet)", models.BalanceSheet),
        ("现金流量表 (CashFlowStatement)", models.CashFlowStatement),
        ("指数成分 (IndexConstituent)", models.IndexConstituent),
    ]
    
    for name, model in tables:
        count = database.get_table_count(model.__tablename__)
        print(f"  {name}: {count:,}")
    
    # 按市场统计
    print("\n" + "-" * 50)
    print("市场分布")
    print("-" * 50)
    
    with database.get_db_session() as session:
        cn_a = session.query(models.StockInfo).filter(
            models.StockInfo.market == "CN_A"
        ).count()
        hk = session.query(models.StockInfo).filter(
            models.StockInfo.market == "HK"
        ).count()
        print(f"  A股: {cn_a:,}")
        print(f"  港股: {hk:,}")

if __name__ == "__main__":
    main()
