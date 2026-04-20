"""fetchers/share.py — 股本数据拉取（腾讯行情接口）

腾讯 qt.gtimg.cn 批量接口字段映射：
  A 股：[72] = 流通股(股)，[73] = 总股本(股)
  港股：[69] = 流通股(股)，[70] = 总股本(股)

接口地址：https://qt.gtimg.cn/q={codes}
编码：GB18030（腾讯统一返回 GBK 兼容编码）
"""

from __future__ import annotations

import logging
import time
from datetime import date
from typing import Optional

import requests

from .base import rate_limiter, retry_with_backoff

logger = logging.getLogger(__name__)


def _safe_int(val) -> Optional[int]:
    """安全转换为 int，无效值返回 None。"""
    if val is None or val == "" or val == "-" or val == "0":
        return None
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return None


def _to_tencent_code(code: str, market: str) -> str:
    """将标准股票代码转为腾讯格式。"""
    if market == "CN_A":
        if code.startswith(("6", "9")):
            return f"sh{code}"
        elif code.startswith(("0", "1", "2", "3", "4", "5", "7")):
            return f"sz{code}"
        # 北交所 8/4 开头
        return f"sz{code}"
    elif market == "CN_HK":
        return f"hk{code}"
    raise ValueError(f"不支持的市场: {market}")


@retry_with_backoff(max_retries=3)
def fetch_share_batch(batch: list[str]) -> list[dict]:
    """请求腾讯接口的一个批次，返回原始行数据列表。

    Args:
        batch: 腾讯格式代码列表，如 ['sh600000', 'sz000001']

    Returns:
        list of str（原始 ~ 分隔行）
    """
    q = ",".join(batch)
    url = f"https://qt.gtimg.cn/q={q}"
    rate_limiter.wait()
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    # 腾讯统一返回 GBK/GB18030 编码
    text = resp.content.decode("gb18030", errors="replace")
    return [l for l in text.strip().split(";") if '="' in l]


def fetch_share_tencent(codes: list[str], market: str) -> list[dict]:
    """从腾讯接口批量获取股本数据。

    限流：每批之间 2-5 秒随机间隔（复用 rate_limiter，
    base_delay 已由调用方设为 2.0s）。

    Args:
        codes: 腾讯格式代码列表（由 get_a_share_codes / get_hk_share_codes 返回）
        market: CN_A 或 CN_HK

    Returns:
        upsert 记录列表，每条包含：
        stock_code, trade_date, market, total_shares, float_shares,
        currency, source
    """
    batch_size = 700
    results: list[dict] = []
    today = date.today()

    for i in range(0, len(codes), batch_size):
        batch = codes[i : i + batch_size]
        batch_num = i // batch_size + 1
        total_batches = (len(codes) + batch_size - 1) // batch_size

        try:
            lines = fetch_share_batch(batch)
        except Exception as e:
            logger.error("批次 %d/%d 失败: %s", batch_num, total_batches, e)
            # 限流后继续下一批次
            time.sleep(2.0)
            continue

        parsed = 0
        for line in lines:
            try:
                idx = line.index('="')
                content = line[idx + 1 : line.rindex('"')]
            except (ValueError, IndexError):
                continue

            parts = content.split("~")
            if len(parts) < 74:
                continue

            try:
                code_raw = parts[2].strip()

                if market == "CN_A":
                    # A 股：[72] = 流通股(股)，[73] = 总股本(股)
                    total = _safe_int(parts[73])
                    float_s = _safe_int(parts[72])
                    currency = "CNY"
                elif market == "CN_HK":
                    # 港股：[69] = 流通股(股)，[70] = 总股本(股)
                    total = _safe_int(parts[70])
                    float_s = _safe_int(parts[69])
                    currency = "HKD"
                else:
                    continue

                if total is None and float_s is None:
                    continue

                results.append({
                    "stock_code": code_raw,
                    "trade_date": today,
                    "market": market,
                    "total_shares": total,
                    "float_shares": float_s,
                    "currency": currency,
                    "change_reason": None,
                    "source": "tencent",
                })
                parsed += 1

            except (IndexError, ValueError):
                continue

        if parsed > 0:
            logger.debug("批次 %d/%d: 解析 %d 条", batch_num, total_batches, parsed)

        if batch_num % 10 == 0 or batch_num == total_batches:
            logger.info("股本拉取进度: %d/%d", min(i + batch_size, len(codes)), len(codes))

    return results


def get_a_share_codes() -> list[str]:
    """从 stock_info 获取全市场 A 股代码列表（腾讯格式）。"""
    from db import query

    rows = query(
        "stock_info",
        where="market = 'CN_A'",
        columns=["stock_code"],
    )
    codes = []
    for r in rows:
        code = r["stock_code"]
        try:
            codes.append(_to_tencent_code(code, "CN_A"))
        except ValueError:
            continue
    logger.info("A 股代码: %d 只", len(codes))
    return codes


def get_hk_share_codes() -> list[str]:
    """从 stock_info 获取全市场港股代码列表（腾讯格式）。"""
    from db import query

    rows = query(
        "stock_info",
        where="market = 'CN_HK'",
        columns=["stock_code"],
    )
    codes = [f"hk{r['stock_code']}" for r in rows]
    logger.info("港股代码: %d 只", len(codes))
    return codes
