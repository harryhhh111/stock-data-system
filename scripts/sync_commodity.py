"""同步国际商品期货日线数据到 commodity_price 表。

数据源: akshare futures_foreign_hist (国际期货)
用法:
    python scripts/sync_commodity.py              # 增量同步
    python scripts/sync_commodity.py --full        # 全量同步
    python scripts/sync_commodity.py --list        # 列出品种
"""

import argparse
import logging
from datetime import date, timedelta

import akshare as ak
import pandas as pd

from db import Connection, upsert

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# 品种配置: code → (描述, 币种)
SUPPORTED_COMMODITIES: dict[str, tuple[str, str]] = {
    "XAU": ("黄金", "USD"),
    "CL":  ("WTI原油", "USD"),
    "SI":  ("白银", "USD"),
    "HG":  ("铜", "USD"),
    "NG":  ("天然气", "USD"),
}


def fetch_commodity(symbol: str) -> pd.DataFrame:
    """拉取商品期货日线。akshare 不支持日期参数，返回全量。"""
    df = ak.futures_foreign_hist(symbol)
    if df is None or df.empty:
        raise ValueError(f"akshare 返回空数据: {symbol}")
    return df


def transform_to_records(df: pd.DataFrame, symbol: str, currency: str) -> list[dict]:
    """转为 commodity_price 格式。"""
    records = []
    for _, row in df.iterrows():
        records.append({
            "commodity_code": symbol,
            "trade_date": pd.Timestamp(row["date"]).date(),
            "open": _safe_float(row.get("open")),
            "high": _safe_float(row.get("high")),
            "low": _safe_float(row.get("low")),
            "close": _safe_float(row.get("close")),
            "volume": _safe_int(row.get("volume")),
            "currency": currency,
        })
    return records


def _safe_float(val) -> float | None:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _safe_int(val) -> int | None:
    if val is None:
        return None
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return None


def sync_commodity(symbol: str, full: bool = False) -> int:
    """同步单个商品。返回写入行数。"""
    name, currency = SUPPORTED_COMMODITIES[symbol]

    if full:
        start_date = date(2006, 1, 1)
    else:
        with Connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT MAX(trade_date) FROM commodity_price WHERE commodity_code = %s",
                (symbol,),
            )
            row = cur.fetchone()
            cur.close()
        if row and row[0]:
            start_date = row[0] + timedelta(days=1)
        else:
            start_date = date(2015, 1, 1)

    if start_date >= date.today():
        logger.info("%s (%s): 数据已是最新", symbol, name)
        return 0

    logger.info("拉取 %s (%s) ...", symbol, name)
    df = fetch_commodity(symbol)

    # 过滤只保留起始日期之后的数据
    df = df[pd.to_datetime(df["date"]).dt.date >= start_date]
    if df.empty:
        logger.info("%s: 无需更新", symbol)
        return 0

    records = transform_to_records(df, symbol, currency)
    n = upsert("commodity_price", records, ["commodity_code", "trade_date"])
    logger.info("%s (%s): 写入 %d 条 (%s ~ %s)", symbol, name, n,
                records[0]["trade_date"], records[-1]["trade_date"])
    return n


def main():
    parser = argparse.ArgumentParser(description="同步国际商品期货日线")
    parser.add_argument("--full", action="store_true", help="全量同步（默认增量）")
    parser.add_argument("--list", action="store_true", help="列出支持的品种")
    args = parser.parse_args()

    if args.list:
        print("支持的品种:")
        for code, (name, currency) in SUPPORTED_COMMODITIES.items():
            with Connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT MAX(trade_date) FROM commodity_price WHERE commodity_code = %s",
                    (code,),
                )
                row = cur.fetchone()
                cur.close()
            last = row[0].isoformat() if row and row[0] else "无数据"
            print(f"  {code}  {name}  ({currency})  最后日期: {last}")
        return

    total = 0
    for code in SUPPORTED_COMMODITIES:
        try:
            n = sync_commodity(code, full=args.full)
            total += n
        except Exception as e:
            logger.error("同步 %s 失败: %s", code, e)

    logger.info("全部完成，共写入 %d 行", total)


if __name__ == "__main__":
    main()
