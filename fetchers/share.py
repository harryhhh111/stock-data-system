"""股本数据拉取 — 腾讯行情接口（批量）

腾讯接口返回字段：
  A 股：[72]=流通股(股), [73]=总股本(股)
  港股：[69]=流通股(股), [70]=总股本(股)

接口地址：https://qt.gtimg.cn/q={codes}
编码：GBK（A 股） / GB18030（港股）
"""

from __future__ import annotations

import logging
import time
from datetime import date

from .base import rate_limiter, retry_with_backoff

logger = logging.getLogger(__name__)


def _safe_int(val) -> int | None:
    """安全转换为 int，无效值返回 None。"""
    if val is None or val == "" or val == "-":
        return None
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return None


@retry_with_backoff(max_retries=3)
def fetch_share_tencent(codes: list[str], market: str) -> list[dict]:
    """从腾讯接口批量获取股本数据。

    Args:
        codes: 腾讯格式的代码列表（A 股: sh600000 / 港股: hk00700）
        market: CN_A 或 CN_HK

    Returns:
        字典列表，每项包含 stock_code, market, total_shares, float_shares
    """
    batch_size = 700
    results: list[dict] = []

    for i in range(0, len(codes), batch_size):
        batch = codes[i : i + batch_size]
        q = ",".join(batch)
        url = f"https://qt.gtimg.cn/q={q}"

        try:
            rate_limiter.wait()
            import requests
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            text = resp.content.decode("gb18030", errors="replace")
        except Exception as e:
            logger.error("腾讯股本接口批次 %d 失败: %s", i // batch_size + 1, e)
            continue

        lines = [l for l in text.strip().split(";") if '="' in l]

        for line in lines:
            try:
                fields = line.split('="')[1].rstrip('"').split("~")
                if not fields or len(fields) < 74:
                    continue

                if market == "CN_A":
                    code_raw = fields[2]  # 纯代码，如 000001
                    # A 股: [72]=流通股, [73]=总股本
                    total = _safe_int(fields[73])
                    float_s = _safe_int(fields[72])
                elif market == "CN_HK":
                    code_raw = fields[2]  # 纯代码，如 00700
                    # 港股: [69]=流通股, [70]=总股本
                    total = _safe_int(fields[70])
                    float_s = _safe_int(fields[69])
                else:
                    continue

                if total is None and float_s is None:
                    continue

                results.append({
                    "stock_code": code_raw,
                    "market": market,
                    "total_shares": total,
                    "float_shares": float_s,
                    "trade_date": date.today(),
                    "currency": "CNY" if market == "CN_A" else "HKD",
                    "source": "tencent",
                })
            except (IndexError, ValueError) as e:
                continue

        if (i // batch_size + 1) % 10 == 0:
            logger.info("股本拉取进度: %d/%d", min(i + batch_size, len(codes)), len(codes))

    return results


def get_a_share_codes() -> list[str]:
    """获取全市场 A 股腾讯格式的代码列表。"""
    from db import query as db_query
    rows = db_query("stock_info", where="market = 'CN_A'", columns=["stock_code"])
    codes = []
    for r in rows:
        code = r["stock_code"]
        if code.startswith(("6", "9")):
            codes.append(f"sh{code}")
        elif code.startswith(("0", "1", "2", "3", "4")):
            codes.append(f"sz{code}")
    return codes


def get_hk_share_codes() -> list[str]:
    """获取全市场港股腾讯格式的代码列表。"""
    from db import query as db_query
    rows = db_query("stock_info", where="market = 'CN_HK'", columns=["stock_code"])
    return [f"hk{r['stock_code']}" for r in rows]
