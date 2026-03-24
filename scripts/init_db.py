#!/usr/bin/env python3
"""
初始化脚本 - 初始化数据库并执行首次同步
"""
import sys
import os

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging
import database
import data_fetcher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

def main():
    print("=" * 50)
    print("A股/港股基本面数据同步系统 - 初始化")
    print("=" * 50)
    
    # 初始化数据库
    print("\n[1/3] 初始化数据库...")
    database.init_database()
    print("✓ 数据库初始化完成")
    
    # 同步股票列表
    print("\n[2/3] 同步股票列表...")
    data_fetcher.sync_stock_list()
    print("✓ 股票列表同步完成")
    
    # 同步财务数据
    print("\n[3/3] 同步财务数据（最近30天）...")
    print("   （首次运行可能需要10-30分钟，请耐心等待）")
    data_fetcher.sync_financial_data(days=30)
    print("✓ 财务数据同步完成")
    
    # 统计
    print("\n" + "=" * 50)
    print("初始化完成！数据统计：")
    print("=" * 50)
    stats = database.get_table_count
    print(f"  股票信息: {stats('StockInfo')}")
    print(f"  财务指标: {stats('FinancialIndicator')}")
    print(f"  利润表: {stats('IncomeStatement')}")
    print(f"  资产负债表: {stats('BalanceSheet')}")
    print(f"  现金流量表: {stats('CashFlowStatement')}")
    print(f"  指数成分: {stats('IndexConstituent')}")
    print("\n启动API服务: python api.py")

if __name__ == "__main__":
    main()
