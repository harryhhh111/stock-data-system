"""sync/manager.py — SyncManager 协调器，委托各模块执行同步任务。"""

from __future__ import annotations

from ._utils import logger


class SyncManager:
    def __init__(self, max_workers: int = 4, force: bool = False):
        self.max_workers = max_workers
        self.force = force
        self._shutdown = False

    def sync_stock_list(self) -> dict:
        from .stock_list import sync_stock_list

        return sync_stock_list()

    def sync_financial(self, market: str) -> dict:
        from .financial import sync_financial

        return sync_financial(
            max_workers=self.max_workers,
            force=self.force,
            is_shutdown=lambda: self._shutdown,
            market=market,
        )

    def sync_index(self) -> dict:
        from .index_constituent import sync_index

        return sync_index()

    def sync_dividend(self, market: str | None = None) -> dict:
        from .dividend import sync_dividend

        return sync_dividend(market=market)

    def sync_industry(self) -> dict:
        from .industry import sync_industry

        return sync_industry()

    def sync_us_industry(self) -> dict:
        from .industry import sync_us_industry

        return sync_us_industry()

    def sync_hk_industry(self, force: bool = False) -> dict:
        from .industry import sync_hk_industry

        return sync_hk_industry(force=force)

    def sync_daily_quote(self, market: str) -> dict:
        from .daily_quote import sync_daily_quote

        return sync_daily_quote(
            force=self.force,
            is_shutdown=lambda: self._shutdown,
            market=market,
        )

    def shutdown(self):
        """标记关闭，让正在运行的同步优雅退出。"""
        self._shutdown = True
