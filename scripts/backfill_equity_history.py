"""从东方财富 F10 回填 A 股历史股本变动数据。

数据源: RPT_F10_EH_EQUITY API
速率: 5s/只（保守，避免封 IP）
预计: ~5200 只 × 5s ≈ 7 小时

Usage:
    python scripts/backfill_equity_history.py           # 全部 A 股
    python scripts/backfill_equity_history.py --dry-run  # 预览不写入
"""

import logging
import sys
import time
import random
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import Connection, execute, upsert as db_upsert

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

API_URL = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://emweb.securities.eastmoney.com/",
}
COLUMNS = "SECUCODE,SECURITY_CODE,END_DATE,TOTAL_SHARES,CHANGE_REASON"


def _code_to_secu(stock_code: str) -> str:
    """A 股代码 → EastMoney SECUCODE 格式。"""
    if stock_code.startswith(("0", "3")):
        return f"{stock_code}.SZ"
    elif stock_code.startswith("6"):
        return f"{stock_code}.SH"
    elif stock_code.startswith(("4", "8")):
        return f"{stock_code}.BJ"
    return f"{stock_code}.SZ"


def fetch_equity_history(secu: str, max_pages: int = 10) -> list[dict]:
    """拉取单只股票的完整股本变动历史。

    Returns:
        [{END_DATE, TOTAL_SHARES, CHANGE_REASON}, ...] 按日期升序
    """
    all_items = []
    for page in range(1, max_pages + 1):
        params = {
            "reportName": "RPT_F10_EH_EQUITY",
            "columns": COLUMNS,
            "filter": f'(SECUCODE="{secu}")',
            "pageNumber": page,
            "pageSize": 50,
            "sortTypes": -1,
            "sortColumns": "END_DATE",
            "source": "HSF10",
            "client": "PC",
        }
        try:
            r = requests.get(API_URL, params=params, headers=HEADERS, timeout=30)
            r.raise_for_status()
            data = r.json()
            if not data.get("success"):
                logger.warning("%s API 返回失败: %s", secu, data.get("message"))
                break
            result = data.get("result")
            if not result or not result.get("data"):
                break
            all_items.extend(result["data"])
            if len(result["data"]) < 50:
                break
        except requests.RequestException as e:
            logger.error("%s 请求异常: %s", secu, e)
            break

    # 按日期升序排列
    all_items.sort(key=lambda x: x.get("END_DATE", ""))
    return all_items


def filter_material_changes(items: list[dict], min_change_pct: float = 0.005) -> list[dict]:
    """只保留 TOTAL_SHARES 真正变化的记录。

    Args:
        items: API 返回的股本记录
        min_change_pct: 最小变动阈值（默认 0.5%，过滤自主行权等日级微调）
    """
    filtered = []
    prev_shares = None
    for item in items:
        shares = item.get("TOTAL_SHARES")
        if shares is None:
            continue
        shares = int(shares)
        if prev_shares is None:
            filtered.append(item)
        elif shares != prev_shares:
            # 跳过微小变动（自主行权等日常微调）
            pct = abs(shares - prev_shares) / prev_shares
            if pct >= min_change_pct:
                filtered.append(item)
        prev_shares = shares
    return filtered


def backfill_equity_history(dry_run: bool = False, start_time: float = None) -> dict:
    """主入口：回填 A 股历史股本。

    Returns:
        {total, success, failed, no_data, upserted_accepted, upserted_write}
    """
    codes = [
        r[0] for r in execute(
            "SELECT stock_code FROM stock_info WHERE market = 'CN_A' ORDER BY stock_code",
            fetch=True,
        )
    ]
    total = len(codes)
    result = {"total": total, "success": 0, "failed": 0, "no_data": 0, "upserted": 0}
    t0 = start_time if start_time else time.time()

    logger.info("开始回填 A 股历史股本: %d 只, 预计 %.0f 小时",
                total, total * 5 / 3600)

    for i, code in enumerate(codes):
        secu = _code_to_secu(code)
        try:
            items = fetch_equity_history(secu)
            if not items:
                result["no_data"] += 1
                logger.warning("[%d/%d] %s (%s) 无数据", i + 1, total, code, secu)
            else:
                material = filter_material_changes(items)
                logger.debug("[%d/%d] %s (%s): %d 条原始, %d 条有效变动",
                           i + 1, total, code, secu, len(items), len(material))

                if not dry_run and material:
                    records = []
                    for item in material:
                        end_date = item.get("END_DATE", "")[:10]
                        shares = int(item.get("TOTAL_SHARES", 0))
                        reason = item.get("CHANGE_REASON")
                        if shares <= 0:
                            continue
                        records.append({
                            "stock_code": code,
                            "trade_date": end_date,
                            "market": "CN_A",
                            "total_shares": shares,
                            "source": "eastmoney_f10",
                            "change_reason": reason,
                        })

                    if records:
                        n = db_upsert(
                            "stock_share",
                            records,
                            ["stock_code", "trade_date", "market"],
                        )
                        result["upserted"] += n

                result["success"] += 1
        except Exception as e:
            result["failed"] += 1
            logger.error("[%d/%d] %s (%s) 异常: %s", i + 1, total, code, secu, e)

        # 进度报告
        if (i + 1) % 100 == 0 or (i + 1) == total:
            elapsed = time.time() - t0
            pct = (i + 1) / total * 100
            eta = elapsed / (i + 1) * (total - i - 1) / 60
            logger.info(
                "进度: %d/%d (%.0f%%) 成功=%d 失败=%d 无数据=%d 写入=%d ETA=%.0fmin",
                i + 1, total, pct,
                result["success"], result["failed"],
                result["no_data"], result["upserted"], eta,
            )

        # 速率控制
        if i < total - 1:
            time.sleep(random.uniform(4.5, 5.5))

    return result


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        logger.info("=== DRY RUN 模式（不写入）===")

    t0 = time.time()
    result = backfill_equity_history(dry_run=dry_run, start_time=t0)
    elapsed = time.time() - t0

    logger.info("=" * 60)
    logger.info("回填完成: total=%d success=%d failed=%d no_data=%d upserted=%d 耗时=%.1fmin",
                result["total"], result["success"], result["failed"],
                result["no_data"], result["upserted"], elapsed / 60)
    logger.info("=" * 60)
