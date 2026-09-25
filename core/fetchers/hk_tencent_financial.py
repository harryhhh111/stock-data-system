"""
fetchers/hk_tencent_financial.py — 港股财务报表拉取（腾讯财经兜底源）

数据源：腾讯财经港股三大报表接口（未文档化，须存原始快照）
  GET https://proxy.finance.qq.com/ifzqgtimg/stock/corp/hkcwbb/detail
  参数：num=12&type=xjll(现金流量表|zcfz|zhsy)&rttype=all&symbol=hk{5位代码}

T1 方案（docs/core/HK_CAPEX_TENCENT_FALLBACK_PLAN.md）要求：
- 腾讯快照全量存：即使增量同步设置 skip_snapshot=True，本 fetcher 也强制保存快照；
- 请求间隔 ≥0.5s（独立限流器，不复用全局 0.3s 的共享 limiter）。
"""
from __future__ import annotations

import logging
import time
from typing import Any

import requests

from .base import AdaptiveRateLimiter, BaseFetcher, retry_with_backoff

logger = logging.getLogger(__name__)

_TENCENT_HKCWBB_URL = "https://proxy.finance.qq.com/ifzqgtimg/stock/corp/hkcwbb/detail"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://gu.qq.com/",
}

# 腾讯接口保守限流：请求间隔 ≥0.5s（方案 §2 风险节）
_tencent_rate_limiter = AdaptiveRateLimiter(base_delay=0.5, max_delay=10.0)


class TencentHkFinancialFetcher(BaseFetcher):
    """腾讯财经港股财务报表拉取器（T1 capex 兜底源）。

    响应结构：{code:0, data:{data:[按报告期的表], rttype:[...]}}，
    详见 core/transformers/tencent_hk.py。
    """

    source_name = "tencent_hk"

    @retry_with_backoff
    def fetch_cashflow(self, stock_code: str) -> dict[str, Any]:
        """拉取港股现金流量表原始 JSON。

        Args:
            stock_code: 港股代码（5 位，如 '02020'，与 DB stock_code 一致）

        Returns:
            原始响应 dict（code=0 成功）

        Raises:
            RuntimeError: 业务返回码非 0
        """
        symbol = f"hk{stock_code.strip()}"
        params = {"num": 12, "type": "xjll", "rttype": "all", "symbol": symbol}
        logger.info("拉取腾讯港股现金流量表: stock=%s symbol=%s", stock_code, symbol)

        t0 = time.time()
        _tencent_rate_limiter.wait()
        try:
            resp = requests.get(
                _TENCENT_HKCWBB_URL, params=params, headers=_HEADERS, timeout=15
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception:
            _tencent_rate_limiter.record_failure()
            raise
        _tencent_rate_limiter.record_success()
        elapsed = time.time() - t0

        if payload.get("code") != 0:
            raise RuntimeError(
                f"腾讯港股现金流量表返回异常: stock={stock_code} code={payload.get('code')} "
                f"msg={payload.get('msg')!r}"
            )

        n_tables = len((payload.get("data") or {}).get("data") or [])
        logger.info(
            "腾讯港股现金流量表拉取完成: %s, %d 期, 耗 %.2fs",
            stock_code, n_tables, elapsed,
        )

        # 方案要求腾讯快照全量存：即使 skip_snapshot=True（增量同步）也强制保存
        prev_skip = self.skip_snapshot
        self.skip_snapshot = False
        try:
            self.save_raw_snapshot(
                stock_code=stock_code,
                data_type="cashflow",
                source="tencent_hk",
                api_params=params,
                raw_data=payload,
            )
        finally:
            self.skip_snapshot = prev_skip

        return payload


# 便捷函数
def fetch_hk_tencent_cashflow(stock_code: str) -> dict[str, Any]:
    return TencentHkFinancialFetcher().fetch_cashflow(stock_code)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    fetcher = TencentHkFinancialFetcher()
    print("=== 测试腾讯港股现金流量表 (02020 安踏) ===")
    payload = fetcher.fetch_cashflow("02020")
    tables = (payload.get("data") or {}).get("data") or []
    print(f"期数: {len(tables)}")
    for t in tables:
        header = t[0][1]
        print(f"  报告期={header[0]} reportype={header[1].get('reportype')} "
              f"fiscalYear={header[1].get('fiscalYear')}")
