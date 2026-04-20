#!/usr/bin/env python3
"""
sync.py — 兼容旧入口。所有逻辑已迁移到 sync/ 包。
scheduler.py 和测试文件通过此路径继续导入，无需修改。
"""
from sync import main, SyncManager, sync_us_market, sync_us_market_reparse

if __name__ == "__main__":
    main()
