"""从 SEC EDGAR Company Facts API 回填美股历史股本数据。

数据源: data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json
标签优先级:
  1. EntityCommonStockSharesOutstanding (PIT, from footnotes)
  2. CommonStockSharesOutstanding (balance sheet)
  3. WeightedAverageNumberOfSharesOutstandingBasic (income stmt, 近似)

速率: 10 req/s (SEC 官方限制)
预计: ~1000 只 × 0.1s ≈ 2 分钟

Usage:
    python scripts/backfill_us_equity.py           # 全部 US 股票
    python scripts/backfill_us_equity.py --dry-run  # 预览不写入
"""

import json
import logging
import sys
import time
from collections import deque
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import execute, upsert as db_upsert

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

# SEC XBRL tags for shares outstanding, in priority order
SHARES_TAGS = [
    "EntityCommonStockSharesOutstanding",
    "CommonStockSharesOutstanding",
    "WeightedAverageNumberOfSharesOutstandingBasic",
]

COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

import os

_SEC_USER_AGENT = os.environ.get(
    "STOCK_SEC_USER_AGENT", "stock-data-system user@example.com"
)
HEADERS = {
    "User-Agent": _SEC_USER_AGENT,
    "Accept-Encoding": "gzip, deflate",
}


class SECRateLimiter:
    """Sliding window rate limiter for SEC API (10 req/s)."""

    def __init__(self, rate: int = 10, window: float = 1.0):
        self._rate = rate
        self._window = window
        self._timestamps: deque[float] = deque()

    def wait(self):
        now = time.time()
        while self._timestamps and self._timestamps[0] < now - self._window:
            self._timestamps.popleft()
        if len(self._timestamps) >= self._rate:
            sleep_time = self._timestamps[0] + self._window - now + 0.05
            if sleep_time > 0:
                time.sleep(sleep_time)
            now = time.time()
            while self._timestamps and self._timestamps[0] < now - self._window:
                self._timestamps.popleft()
        self._timestamps.append(time.time())


rate_limiter = SECRateLimiter()


def fetch_ticker_to_cik() -> dict[str, str]:
    """从 SEC 下载 ticker → CIK 映射。"""
    r = requests.get(COMPANY_TICKERS_URL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    data = r.json()
    return {
        item["ticker"].upper(): str(item["cik_str"]).zfill(10)
        for item in data.values()
    }


def fetch_company_facts(cik: str) -> dict | None:
    """拉取单个公司的 Company Facts JSON。"""
    url = COMPANY_FACTS_URL.format(cik=cik)
    rate_limiter.wait()
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        if r.status_code == 429:
            retry_after = int(r.headers.get("Retry-After", "10"))
            logger.warning("429 限流, 等待 %ds", retry_after)
            time.sleep(retry_after)
            rate_limiter.wait()
            r = requests.get(url, headers=HEADERS, timeout=30)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        logger.error("CIK %s 请求异常: %s", cik, e)
        return None


def extract_shares_outstanding(facts: dict) -> list[dict]:
    """从 Company Facts JSON 提取股本变动历史。

    Returns:
        [{end_date, total_shares, tag}, ...] 按日期升序
    """
    usgaap = facts.get("facts", {}).get("us-gaap", {})

    for tag in SHARES_TAGS:
        tag_data = usgaap.get(tag, {}).get("units", {}).get("shares", [])
        if not tag_data:
            continue

        # 过滤: 只保留有效的年度/季度数据点
        records = []
        for entry in tag_data:
            val = entry.get("val")
            end = entry.get("end")
            form = entry.get("form", "")
            # 跳过 amendment 和无效值
            if val is None or val <= 0 or not end:
                continue
            # 只保留 10-K, 10-Q, 20-F, 10-K/A, 10-Q/A 等标准表格
            if not any(f in form for f in ("10-K", "10-Q", "20-F", "40-F")):
                continue
            records.append({
                "end_date": end[:10],
                "total_shares": val,
                "tag": tag,
                "filed": entry.get("filed", "")[:10],
            })

        if records:
            # 按 end_date 排序，去重（同一天取最新 filed）
            records.sort(key=lambda x: (x["end_date"], x["filed"]))
            deduped = {}
            for rec in records:
                deduped[rec["end_date"]] = rec
            return sorted(deduped.values(), key=lambda x: x["end_date"])

    return []


def filter_material_changes(records: list[dict], min_change_pct: float = 0.005) -> list[dict]:
    """只保留 total_shares 真正变化的记录。"""
    filtered = []
    prev = None
    for rec in records:
        shares = rec["total_shares"]
        if prev is None:
            filtered.append(rec)
        elif shares != prev:
            pct = abs(shares - prev) / prev
            if pct >= min_change_pct:
                filtered.append(rec)
        prev = shares
    return filtered


def backfill_us_equity(dry_run: bool = False) -> dict:
    """主入口：回填美股历史股本。"""
    # 1. 获取 ticker → CIK 映射
    logger.info("正在下载 SEC ticker-CIK 映射...")
    ticker_to_cik = fetch_ticker_to_cik()
    logger.info("映射下载完成: %d 个 ticker", len(ticker_to_cik))

    # 2. 获取 US 股票列表
    rows = execute(
        "SELECT stock_code FROM stock_info WHERE market = 'US' ORDER BY stock_code",
        fetch=True,
    )
    codes = [r[0] for r in rows]
    total = len(codes)
    result = {"total": total, "success": 0, "failed": 0, "no_cik": 0, "no_data": 0, "upserted": 0}
    t0 = time.time()

    logger.info("开始回填美股历史股本: %d 只", total)

    for i, code in enumerate(codes):
        # 3. 解析 CIK
        cik = ticker_to_cik.get(code.upper())
        if not cik:
            result["no_cik"] += 1
            if (i + 1) % 100 == 0:
                logger.warning("[%d/%d] %s 无 CIK 映射", i + 1, total, code)
            continue

        # 4. 拉取 Company Facts
        facts = fetch_company_facts(cik)
        if not facts:
            result["no_data"] += 1
            continue

        # 5. 提取股本数据
        raw_records = extract_shares_outstanding(facts)
        if not raw_records:
            result["no_data"] += 1
            continue

        # 6. 过滤实质性变动
        material = filter_material_changes(raw_records)

        # 7. 写入
        if not dry_run and material:
            db_records = [
                {
                    "stock_code": code,
                    "trade_date": rec["end_date"],
                    "market": "US",
                    "total_shares": rec["total_shares"],
                    "source": f"sec_edgar:{rec['tag']}",
                }
                for rec in material
            ]
            n = db_upsert("stock_share", db_records, ["stock_code", "trade_date", "market"])
            result["upserted"] += n

        result["success"] += 1

        # 进度报告
        if (i + 1) % 100 == 0 or (i + 1) == total:
            elapsed = time.time() - t0
            pct = (i + 1) / total * 100
            eta = elapsed / (i + 1) * (total - i - 1) / 60
            logger.info(
                "进度: %d/%d (%.0f%%) 成功=%d 无CIK=%d 无数据=%d 写入=%d ETA=%.1fmin",
                i + 1, total, pct,
                result["success"], result["no_cik"],
                result["no_data"], result["upserted"], eta,
            )

    elapsed = time.time() - t0
    result["elapsed_min"] = elapsed / 60
    return result


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        logger.info("=== DRY RUN 模式（不写入）===")

    result = backfill_us_equity(dry_run=dry_run)

    logger.info("=" * 60)
    logger.info(
        "回填完成: total=%d success=%d no_cik=%d no_data=%d upserted=%d 耗时=%.1fmin",
        result["total"], result["success"], result["no_cik"],
        result["no_data"], result["upserted"], result["elapsed_min"],
    )
    logger.info("=" * 60)
