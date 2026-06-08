"""同步 A 股/港股指数日线行情到 daily_quote (market = CN_IDX)。

用法:
    python scripts/sync_index_quote.py              # 增量同步
    python scripts/sync_index_quote.py --full        # 全量同步
    python scripts/sync_index_quote.py --list        # 列出支持的指数

数据源:
  - A 股指数: akshare stock_zh_index_daily (东方财富)
  - 港股指数: 腾讯 K 线接口 (web.ifzq.gtimg.cn)
"""

import argparse
import logging
import requests
from datetime import date, datetime, timedelta

import akshare as ak
import pandas as pd

from db import Connection, upsert

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

_TENCENT_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"

# 指数代码 -> (名称, 数据源类型, 源 symbol)
# 数据源类型: "ak_zh" (akshare A股), "tx_hk" (腾讯港股)
SUPPORTED_INDEXES: dict[str, tuple[str, str, str]] = {
    "000300": ("沪深300", "ak_zh", "sh000300"),
    "399905": ("中证500", "ak_zh", "sz399905"),
    "399006": ("创业板指", "ak_zh", "sz399006"),
    "HSI": ("恒生指数", "tx_hk", "hkHSI"),
}


def _safe_float(val) -> float | None:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    return float(val)


def _safe_int(val) -> int | None:
    if val is None:
        return None
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return None


def _fetch_a_index(ak_symbol: str) -> pd.DataFrame:
    """拉取 A 股指数历史日线（akshare）。"""
    df = ak.stock_zh_index_daily(symbol=ak_symbol)
    if df is None or df.empty:
        return pd.DataFrame()
    if "amount" not in df.columns:
        df["amount"] = None
    return df


def _fetch_hk_index(tx_code: str) -> list[dict]:
    """拉取港股指数历史日线（腾讯 K 线，自动分段）。"""
    all_records: list[dict] = []
    seg_end = date.today().isoformat()
    seen_dates: set[str] = set()

    for _ in range(10):
        params = {"param": f"{tx_code},day,2015-01-01,{seg_end},800,"}
        resp = requests.get(
            _TENCENT_KLINE_URL, params=params,
            headers={"User-Agent": "Mozilla/5.0"}, timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        day_data: list = []
        raw = data.get("data", {})
        if isinstance(raw, dict):
            for v in raw.values():
                if isinstance(v, dict) and v.get("day"):
                    day_data = v["day"]
                    break

        if not day_data:
            break

        for row in day_data:
            d = row[0]  # date
            if d not in seen_dates:
                seen_dates.add(d)
                all_records.append({
                    "trade_date": d,
                    "open": _safe_float(row[1]),
                    "close": _safe_float(row[2]),
                    "high": _safe_float(row[3]),
                    "low": _safe_float(row[4]),
                    "volume": _safe_int(row[5]) if len(row) > 5 else None,
                    "amount": None,
                })

        # 最早日期 > 2020-01-01 则继续往前拉
        earliest = day_data[-1][0]
        if earliest <= "2015-01-01":
            break
        seg_end = earliest

    return all_records


def sync_index_quote(index_code: str, full: bool = False) -> int:
    """同步单只指数。返回写入行数。"""
    name, source_type, ak_symbol = SUPPORTED_INDEXES[index_code]
    market = "CN_IDX"

    # 确定起始日期
    if full:
        start_date = date(2015, 1, 1)
    else:
        with Connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT MAX(trade_date) FROM daily_quote WHERE stock_code = %s AND market = %s",
                (index_code, market),
            )
            row = cur.fetchone()
            cur.close()
        if row and row[0]:
            start_date = row[0] + timedelta(days=1)
        else:
            start_date = date(2015, 1, 1)

    if start_date >= date.today():
        logger.info("%s (%s): 数据已是最新 (%s)", index_code, name, start_date)
        return 0

    logger.info("拉取 %s (%s) 从 %s 开始...", index_code, name, start_date)

    if source_type == "ak_zh":
        df = _fetch_a_index(ak_symbol)
        if df.empty:
            logger.warning("%s: 拉取失败或无数据", index_code)
            return 0
        df = df[pd.to_datetime(df["date"]).dt.date >= start_date]
        if df.empty:
            logger.info("%s: 无需更新", index_code)
            return 0
        records = []
        for _, row in df.iterrows():
            records.append({
                "stock_code": index_code,
                "trade_date": pd.Timestamp(row["date"]).date(),
                "market": market,
                "open": _safe_float(row.get("open")),
                "high": _safe_float(row.get("high")),
                "low": _safe_float(row.get("low")),
                "close": _safe_float(row.get("close")),
                "volume": _safe_int(row.get("volume")),
                "amount": _safe_float(row.get("amount")),
                "currency": "CNY",
            })
    else:  # tx_hk
        raw_records = _fetch_hk_index(ak_symbol)
        records = [
            {
                "stock_code": index_code,
                "trade_date": r["trade_date"],
                "market": market,
                "open": r["open"],
                "high": r["high"],
                "low": r["low"],
                "close": r["close"],
                "volume": r["volume"],
                "amount": r["amount"],
                "currency": "HKD",
            }
            for r in raw_records
            if r["trade_date"] >= start_date.isoformat()
        ]

    if not records:
        logger.info("%s: 无需更新", index_code)
        return 0

    n = upsert("daily_quote", records, ["stock_code", "trade_date"])
    logger.info("%s (%s): 写入 %d 条 (%s ~ %s)", index_code, name, n,
                records[0]["trade_date"], records[-1]["trade_date"])
    return n


def main():
    parser = argparse.ArgumentParser(description="同步指数日线行情 (A股+港股)")
    parser.add_argument("--full", action="store_true", help="全量同步（默认增量）")
    parser.add_argument("--list", action="store_true", help="列出支持的指数")
    args = parser.parse_args()

    if args.list:
        print("支持的指数:")
        for code, (name, _, _) in SUPPORTED_INDEXES.items():
            with Connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT MAX(trade_date) FROM daily_quote WHERE stock_code = %s AND market = 'CN_IDX'",
                    (code,),
                )
                row = cur.fetchone()
                cur.close()
            last = row[0].isoformat() if row and row[0] else "无数据"
            print(f"  {code}  {name}  最后日期: {last}")
        return

    total = 0
    for code in SUPPORTED_INDEXES:
        try:
            n = sync_index_quote(code, full=args.full)
            total += n
        except Exception as e:
            logger.error("同步 %s 失败: %s", code, e)

    logger.info("全部完成，共写入 %d 行", total)


if __name__ == "__main__":
    main()
