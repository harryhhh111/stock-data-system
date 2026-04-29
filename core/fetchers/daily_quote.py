"""
fetchers/daily_quote.py — 日线行情数据拉取

数据源：
  - A 股历史日线: ak.stock_zh_a_hist（OHLCV + 换手率，无市值）
  - A 股实时行情: ak.stock_zh_a_spot_em（含总市值、流通市值、PE、PB）
  - 港股历史日线: ak.stock_hk_hist（OHLCV + 换手率，无市值）
  - 港股实时行情: 东方财富 API 直调（含总市值、PE、PB）
  - 美股实时行情: 腾讯 qt.gtimg.cn（含总市值、PE、PB）

策略：
  - 历史回填（全量）：逐只股票拉取历史日线（OHLCV），市值留 NULL
  - 每日增量：用实时行情接口（含市值），批量拉全市场当天数据

港股市值说明（2026-03-30）：
  ak.stock_hk_spot_em() 内部调用东方财富 API 时请求了 f20(总市值)、f9(PE)、f23(PB)，
  但最终输出时丢弃了这些列。因此改用直接调 API 的方式获取完整数据。

美股单位验证（2026-04-02，基于 usAAPL 实际数据）：
  - 成交额 [37]: 原始 USD（vol*price ≈ amount，比率 ≈ 1.0），无需转换
  - 总市值 [45]: 单位亿美元（AAPL=37529.40 → ×1e8 = 3.75T USD），需 ×1e8
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Optional

import akshare as ak
import pandas as pd
import requests

from .base import BaseFetcher, retry_with_backoff, rate_limiter, AdaptiveRateLimiter, circuit_breaker

logger = logging.getLogger(__name__)


class DailyQuoteFetcher(BaseFetcher):
    """日线行情拉取器。"""

    source_name = "eastmoney_quote"

    # ── A 股历史日线（单只股票）──────────────────────────

    @retry_with_backoff
    def fetch_a_hist(
        self,
        symbol: str,
        start_date: str = "19700101",
        end_date: str = "20500101",
        adjust: str = "",
    ) -> pd.DataFrame:
        """拉取单只 A 股历史日线。

        Args:
            symbol: 股票代码，如 "000001"
            start_date: 开始日期 YYYYMMDD
            end_date: 结束日期 YYYYMMDD
            adjust: 复权方式 "" | "qfq" | "hfq"

        Returns:
            DataFrame with columns: 日期, 股票代码, 开盘, 收盘, 最高, 最低,
                                    成交量, 成交额, 振幅, 涨跌幅, 涨跌额, 换手率
        """
        rate_limiter.wait()
        df = ak.stock_zh_a_hist(
            symbol=symbol,
            period="daily",
            start_date=start_date,
            end_date=end_date,
            adjust=adjust,
        )
        return df

    # ── A 股实时行情（全市场，含市值）────────────────────

    @retry_with_backoff(max_retries=3)
    def fetch_a_spot(self) -> pd.DataFrame:
        """拉取 A 股全市场实时行情（含市值）。

        优先使用东方财富（ak.stock_zh_a_spot_em），失败时 fallback 到腾讯接口。
        腾讯接口通过 qt.gtimg.cn 批量查询，字段映射与东方财富输出一致。

        返回字段：代码, 名称, 最新价, 涨跌幅, 涨跌额, 成交量, 成交额,
                  换手率, 市盈率-动态, 市净率, 总市值, 流通市值, 今开, 最高, 最低, 昨收 等
        """
        # ── 先尝试东方财富 ──
        try:
            logger.info("开始拉取 A 股实时行情（东方财富）...")
            t0 = time.time()
            rate_limiter.wait()
            df = ak.stock_zh_a_spot_em()
            elapsed = time.time() - t0
            logger.info("A 股实时行情（东方财富）: %d 只, 耗 %.1fs", len(df), elapsed)
            return df
        except Exception as e:
            logger.warning("东方财富 A 股实时行情失败，fallback 到腾讯: %s", e)

        # ── Fallback: 腾讯接口 ──
        return self._fetch_a_spot_tencent()

    def _fetch_a_spot_tencent(self) -> pd.DataFrame:
        """通过腾讯 qt.gtimg.cn 拉取 A 股实时行情。

        腾讯接口字段映射（~分隔，索引从0开始）：
          [1]=名称, [2]=代码, [3]=最新价, [4]=昨收, [5]=今开,
          [6]=成交量(手), [31]=涨跌额, [32]=涨跌幅(%),
          [33]=最高, [34]=最低, [38]=换手率(%),
          [44]=流通市值(亿), [45]=总市值(亿), [46]=市净率(PB),
          [52]=市盈率(PE), [57]=成交额(万)

        注意单位转换：
          - 成交量: 手 → 股 (×100)
          - 总市值/流通市值: 亿 → 元 (×1e8)
          - 成交额: 万 → 元 (×1e4)
        """
        logger.info("开始拉取 A 股实时行情（腾讯 fallback）...")
        t0 = time.time()

        # 获取全市场 A 股代码列表
        stock_list = ak.stock_info_a_code_name()
        all_codes = stock_list["code"].tolist()

        # 转为腾讯格式
        def _to_tencent(code: str) -> str | None:
            if code.startswith(("6", "9")):
                return f"sh{code}"
            elif code.startswith(("0", "1", "2", "3", "4")):
                return f"sz{code}"
            return None

        tencent_codes = [c for c in (_to_tencent(x) for x in all_codes) if c]
        logger.info("腾讯 fallback: 共 %d 只 A 股待查询", len(tencent_codes))

        # 批量查询（每批 700，URL 长度不超限）
        batch_size = 700
        all_lines: list[str] = []
        for i in range(0, len(tencent_codes), batch_size):
            batch = tencent_codes[i : i + batch_size]
            q = ",".join(batch)
            url = f"https://qt.gtimg.cn/q={q}"
            try:
                rate_limiter.wait()
                resp = requests.get(url, timeout=15)
                resp.raise_for_status()
                lines = [l for l in resp.text.strip().split(";") if '="' in l]
                all_lines.extend(lines)
            except Exception as e:
                logger.error("腾讯 fallback 批次 %d 失败: %s", i // batch_size + 1, e)
                continue

        # 解析
        rows: list[dict] = []
        for line in all_lines:
            # 格式: v_sh600000="1~浦发银行~600000~..."
            try:
                # 提取引号内的内容
                idx = line.index('"')
                content = line[idx + 1 : line.rindex('"')]
            except (ValueError, IndexError):
                continue

            parts = content.split("~")
            if len(parts) < 65:
                continue

            try:
                code = parts[2].strip()
                name = parts[1].strip()
                price = _safe_float(parts[3])
                prev_close = _safe_float(parts[4])
                open_price = _safe_float(parts[5])
                vol_hand = _safe_float(parts[6])  # 手
                change_amt = _safe_float(parts[31])
                change_pct = _safe_float(parts[32])
                high = _safe_float(parts[33])
                low = _safe_float(parts[34])
                turnover_rate = _safe_float(parts[38])
                float_cap_yi = _safe_float(parts[44])  # 亿
                total_cap_yi = _safe_float(parts[45])  # 亿
                pb = _safe_float(parts[46])
                pe = _safe_float(parts[52])
                amount_wan = _safe_float(parts[57])  # 万

                # 单位转换
                volume = int(vol_hand * 100) if vol_hand is not None else None
                amount = amount_wan * 1e4 if amount_wan is not None else None
                total_cap = total_cap_yi * 1e8 if total_cap_yi is not None else None
                float_cap = float_cap_yi * 1e8 if float_cap_yi is not None else None

                # 跳过停牌或无数据的
                if price is None or price <= 0:
                    continue

                rows.append({
                    "代码": code,
                    "名称": name,
                    "最新价": price,
                    "涨跌额": change_amt,
                    "涨跌幅": change_pct,
                    "成交量": volume,
                    "成交额": amount,
                    "振幅": None,
                    "最高": high,
                    "最低": low,
                    "今开": open_price,
                    "昨收": prev_close,
                    "换手率": turnover_rate,
                    "市盈率-动态": pe,
                    "市净率": pb,
                    "总市值": total_cap,
                    "流通市值": float_cap,
                })
            except (IndexError, ValueError, TypeError) as e:
                logger.debug("腾讯 fallback 解析行失败: %s", e)
                continue

        df = pd.DataFrame(rows)
        elapsed = time.time() - t0
        has_cap = df["总市值"].notna().sum() if not df.empty else 0
        logger.info(
            "A 股实时行情（腾讯 fallback）: %d 只, 含市值 %d 只, 耗 %.1fs",
            len(df), has_cap, elapsed,
        )
        return df

    # ── 港股历史日线（单只股票）──────────────────────────

    @retry_with_backoff
    def fetch_hk_hist(
        self,
        symbol: str,
        start_date: str = "19700101",
        end_date: str = "20500101",
        adjust: str = "",
    ) -> pd.DataFrame:
        """拉取单只港股历史日线。

        Args:
            symbol: 港股代码，如 "00700"
            start_date: 开始日期 YYYYMMDD
            end_date: 结束日期 YYYYMMDD

        Returns:
            DataFrame: 日期, 开盘, 收盘, 最高, 最低, 成交量, 成交额, 换手率 等
        """
        rate_limiter.wait()
        df = ak.stock_hk_hist(
            symbol=symbol,
            period="daily",
            start_date=start_date,
            end_date=end_date,
            adjust=adjust,
        )
        return df

    # ── 港股实时行情（全市场，含市值）────────────────────

    @retry_with_backoff(max_retries=3)
    def fetch_hk_spot(self) -> pd.DataFrame:
        """拉取港股全市场实时行情（含市值）。

        优先使用东方财富 API 直调，失败时 fallback 到腾讯接口。
        腾讯接口通过 qt.gtimg.cn 批量查询（hk 前缀），字段映射与东方财富输出一致。

        返回字段：代码, 名称, 最新价, 涨跌额, 涨跌幅, 今开, 最高, 最低, 昨收,
                  成交量, 成交额, 总市值, 市盈率-动态, 市净率, 行业
        """
        # ── 先尝试东方财富 ──
        try:
            return self._fetch_hk_spot_eastmoney()
        except Exception as e:
            logger.warning("东方财富港股实时行情失败，fallback 到腾讯: %s", e)

        # ── Fallback: 腾讯接口 ──
        return self._fetch_hk_spot_tencent()

    def _fetch_hk_spot_eastmoney(self) -> pd.DataFrame:
        """通过东方财富 API 拉取港股实时行情（含市值）。"""
        logger.info("开始拉取港股实时行情（东方财富）...")
        t0 = time.time()

        url = "https://72.push2.eastmoney.com/api/qt/clist/get"
        params = {
            "pn": "1",
            "pz": "5000",
            "po": "1",
            "np": "1",
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": "2",
            "invt": "2",
            "fid": "f12",
            "fs": "m:128 t:3,m:128 t:4,m:128 t:1,m:128 t:2",
            "fields": "f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f14,f15,f16,f17,f18,f20,f23,f100",
        }

        all_items = []
        page = 1

        while True:
            params["pn"] = str(page)
            rate_limiter.wait()
            resp = requests.get(url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()

            if not data.get("data") or not data["data"].get("diff"):
                break

            diff = data["data"]["diff"]
            all_items.extend(diff)

            total = data["data"]["total"]
            if len(all_items) >= total:
                break

            page += 1

        # 构造 DataFrame
        df = pd.DataFrame(all_items)
        if df.empty:
            logger.warning("港股实时行情（东方财富）: 无数据")
            return pd.DataFrame(columns=self._hk_spot_columns())

        df = df.rename(columns={
            "f12": "代码",
            "f14": "名称",
            "f2": "最新价",
            "f4": "涨跌额",
            "f3": "涨跌幅",
            "f17": "今开",
            "f15": "最高",
            "f16": "最低",
            "f18": "昨收",
            "f5": "成交量",
            "f6": "成交额",
            "f20": "总市值",
            "f9": "市盈率-动态",
            "f23": "市净率",
            "f100": "行业",
        })

        df = self._finalize_hk_spot_df(df)
        elapsed = time.time() - t0
        has_cap = df["总市值"].notna().sum()
        logger.info("港股实时行情（东方财富）: %d 只, 含市值 %d 只, 耗 %.1fs", len(df), has_cap, elapsed)
        return df

    def _fetch_hk_spot_tencent(self) -> pd.DataFrame:
        """通过腾讯 qt.gtimg.cn 拉取港股实时行情。

        腾讯港股接口字段映射（~分隔，索引从0开始）：
          [1]=名称, [2]=代码, [3]=最新价, [4]=昨收, [5]=今开,
          [6]=成交量(股), [32]=涨跌幅(%),
          [33]=最高, [34]=最低, [37]=成交额(HKD),
          [39]=总市值(亿), [45]=流通市值(亿),
          [60]=市净率(PB), [59]=市盈率(PE)

        注意：港股字段与 A 股不同！
          - [6] 成交量单位是股（A 股是手）
          - [37] 成交额单位是元
          - [39] 总市值单位是亿
          - PE/PB 索引与 A 股不同，港股 PE=[59], PB=[60]
        """
        logger.info("开始拉取港股实时行情（腾讯 fallback）...")
        t0 = time.time()

        # 获取港股代码列表（新浪接口，稳定可访问）
        rate_limiter.wait()
        stock_list = ak.stock_hk_spot()
        all_codes = stock_list["代码"].tolist()

        # 转为腾讯格式：hk 前缀
        tencent_codes = [f"hk{code}" for code in all_codes if code.strip()]
        logger.info("腾讯 fallback: 共 %d 只港股待查询", len(tencent_codes))

        # 批量查询（每批 700，URL 长度不超限）
        batch_size = 700
        all_lines: list[str] = []
        for i in range(0, len(tencent_codes), batch_size):
            batch = tencent_codes[i : i + batch_size]
            q = ",".join(batch)
            url = f"https://qt.gtimg.cn/q={q}"
            try:
                rate_limiter.wait()
                resp = requests.get(url, timeout=15)
                resp.raise_for_status()
                # 腾讯港股返回 GBK 编码
                text = resp.content.decode("gb18030", errors="replace")
                lines = [l for l in text.strip().split(";") if '="' in l]
                all_lines.extend(lines)
            except Exception as e:
                logger.error("腾讯 fallback 批次 %d 失败: %s", i // batch_size + 1, e)
                continue

        # 解析
        rows: list[dict] = []
        for line in all_lines:
            try:
                idx = line.index('"')
                content = line[idx + 1 : line.rindex('"')]
            except (ValueError, IndexError):
                continue

            parts = content.split("~")
            if len(parts) < 65:
                continue

            try:
                code = parts[2].strip()
                name = parts[1].strip()
                price = _safe_float(parts[3])
                prev_close = _safe_float(parts[4])
                open_price = _safe_float(parts[5])
                volume = _safe_float(parts[6])  # 股（港股单位是股，非手）
                change_amt = _safe_float(parts[61])
                change_pct = _safe_float(parts[32])
                high = _safe_float(parts[33])
                low = _safe_float(parts[34])
                amount = _safe_float(parts[37])  # 元（港股单位是元，非万）
                total_cap_yi = _safe_float(parts[44])  # 亿
                pb = _safe_float(parts[58])
                pe = _safe_float(parts[57])

                # 单位转换：市值 亿 → 元
                total_cap = total_cap_yi * 1e8 if total_cap_yi is not None else None

                # 成交量：港股已是股单位，直接取整
                vol_int = int(volume) if volume is not None else None

                # 跳过停牌或无数据的
                if price is None or price <= 0:
                    continue

                rows.append({
                    "代码": code,
                    "名称": name,
                    "最新价": price,
                    "涨跌额": change_amt,
                    "涨跌幅": change_pct,
                    "今开": open_price,
                    "最高": high,
                    "最低": low,
                    "昨收": prev_close,
                    "成交量": vol_int,
                    "成交额": amount,
                    "总市值": total_cap,
                    "市盈率-动态": pe,
                    "市净率": pb,
                    "行业": None,  # 腾讯接口无行业字段
                })
            except (IndexError, ValueError, TypeError) as e:
                logger.debug("腾讯 fallback 解析行失败: %s", e)
                continue

        df = pd.DataFrame(rows)
        if df.empty:
            logger.warning("港股实时行情（腾讯 fallback）: 无数据")
            return pd.DataFrame(columns=self._hk_spot_columns())

        df = self._finalize_hk_spot_df(df)
        elapsed = time.time() - t0
        has_cap = df["总市值"].notna().sum() if not df.empty else 0
        logger.info(
            "港股实时行情（腾讯 fallback）: %d 只, 含市值 %d 只, 耗 %.1fs",
            len(df), has_cap, elapsed,
        )
        return df

    @staticmethod
    def _hk_spot_columns() -> list[str]:
        """港股实时行情标准列名。"""
        return [
            "代码", "名称", "最新价", "涨跌额", "涨跌幅",
            "今开", "最高", "最低", "昨收",
            "成交量", "成交额", "总市值", "市盈率-动态", "市净率", "行业",
        ]

    @staticmethod
    def _finalize_hk_spot_df(df: pd.DataFrame) -> pd.DataFrame:
        """港股 spot DataFrame 的公共后处理：补列、选列、类型转换。"""
        keep_cols = DailyQuoteFetcher._hk_spot_columns()
        for col in keep_cols:
            if col not in df.columns:
                df[col] = None
        df = df[keep_cols]

        numeric_cols = [
            "最新价", "涨跌额", "涨跌幅", "今开", "最高", "最低", "昨收",
            "成交量", "成交额", "总市值", "市盈率-动态", "市净率",
        ]
        for col in numeric_cols:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        return df

    # ── 美股实时行情（全市场，含市值）────────────────────

    @staticmethod
    def _us_spot_columns() -> list[str]:
        """美股实时行情标准列名。"""
        return [
            "代码", "名称", "最新价", "涨跌额", "涨跌幅",
            "今开", "最高", "最低", "昨收",
            "成交量", "成交额", "总市值", "市盈率-动态", "市净率",
        ]

    @retry_with_backoff(max_retries=3)
    def fetch_us_spot(self) -> pd.DataFrame:
        """通过腾讯 qt.gtimg.cn 拉取美股实时行情。

        腾讯美股接口字段映射（71 字段，~分隔，索引从 0 开始）：
          [1]=名称, [2]=代码(AAPL.OQ), [3]=最新价, [4]=昨收, [5]=今开,
          [6]=成交量(股), [31]=涨跌额, [32]=涨跌幅(%),
          [33]=最高, [34]=最低, [35]=货币(USD),
          [37]=成交额(USD, 原始), [38]=PB, [39]=PE,
          [45]=总市值(亿美元)

        单位说明（2026-04-02 交叉验证）：
          - 成交额 [37]: 原始 USD（vol*price ≈ amount），无需转换
          - 总市值 [45]: 亿美元，需 ×1e8 转为 USD

        Returns:
            DataFrame with columns: 代码, 名称, 最新价, 涨跌额, 涨跌幅,
            今开, 最高, 最低, 昨收, 成交量, 成交额, 总市值, 市盈率-动态, 市净率
        """
        logger.info("开始拉取美股实时行情（腾讯）...")
        t0 = time.time()

        # 从数据库获取美股代码列表
        from db import execute
        rows = execute(
            "SELECT stock_code FROM stock_info WHERE market = 'US'",
            fetch=True,
        )
        all_codes = [r[0] for r in rows]
        logger.info("美股 stock_info: %d 只", len(all_codes))

        if not all_codes:
            logger.warning("stock_info 中无美股，请先同步美股列表")
            return pd.DataFrame(columns=self._us_spot_columns())

        # 转为腾讯格式: AAPL → usAAPL，BRK-B → usBRK.B（腾讯用点号）
        tencent_codes = [f"us{code.replace('-', '.')}" for code in all_codes]
        # 反向映射：腾讯响应中的代码前缀 → DB stock_code（处理 BRK-B 等带连字符的代码）
        _code_reverse: dict[str, str] = {}
        for c in all_codes:
            _prefix = c.replace("-", ".").split(".")[0]
            _code_reverse[_prefix] = c

        # 批量查询（每批 300，避免 URL 过长）
        batch_size = 300
        all_lines: list[str] = []
        for i in range(0, len(tencent_codes), batch_size):
            batch = tencent_codes[i : i + batch_size]
            q = ",".join(batch)
            url = f"https://qt.gtimg.cn/q={q}"
            try:
                rate_limiter.wait()
                resp = requests.get(url, timeout=15)
                resp.raise_for_status()
                lines = [l for l in resp.text.strip().split(";") if '="' in l]
                all_lines.extend(lines)
            except Exception as e:
                logger.error("美股腾讯批次 %d 失败: %s", i // batch_size + 1, e)
                continue

        # 解析
        rows: list[dict] = []
        for line in all_lines:
            try:
                idx = line.index('"')
                content = line[idx + 1 : line.rindex('"')]
            except (ValueError, IndexError):
                continue

            parts = content.split("~")
            if len(parts) < 65:
                continue

            try:
                # 代码：去掉后缀 AAPL.OQ → AAPL，BRK.B.N → BRK → BRK-B
                raw_code = parts[2].strip()
                code = _code_reverse.get(raw_code.split(".")[0], raw_code.split(".")[0])
                name = parts[1].strip()
                price = _safe_float(parts[3])
                prev_close = _safe_float(parts[4])
                open_price = _safe_float(parts[5])
                volume = _safe_float(parts[6])  # 股
                change_amt = _safe_float(parts[31])
                change_pct = _safe_float(parts[32])
                high = _safe_float(parts[33])
                low = _safe_float(parts[34])
                amount = _safe_float(parts[37])  # USD 原始，无需转换
                pb = _safe_float(parts[38])
                pe = _safe_float(parts[39])
                total_cap_yi = _safe_float(parts[45])  # 亿美元

                # 单位转换：总市值 亿美元 → USD
                total_cap = total_cap_yi * 1e8 if total_cap_yi is not None else None

                # 成交量直接取整
                vol_int = int(volume) if volume is not None else None

                # 跳过停牌或无数据的
                if price is None or price <= 0:
                    continue

                rows.append({
                    "代码": code,
                    "名称": name,
                    "最新价": price,
                    "涨跌额": change_amt,
                    "涨跌幅": change_pct,
                    "今开": open_price,
                    "最高": high,
                    "最低": low,
                    "昨收": prev_close,
                    "成交量": vol_int,
                    "成交额": amount,
                    "总市值": total_cap,
                    "市盈率-动态": pe,
                    "市净率": pb,
                })
            except (IndexError, ValueError, TypeError) as e:
                logger.debug("美股腾讯解析行失败: %s", e)
                continue

        df = pd.DataFrame(rows)
        if df.empty:
            logger.warning("美股实时行情: 无数据")
            return pd.DataFrame(columns=self._us_spot_columns())

        df = self._finalize_us_spot_df(df)
        elapsed = time.time() - t0
        has_cap = df["总市值"].notna().sum() if not df.empty else 0
        logger.info(
            "美股实时行情（腾讯）: %d 只, 含市值 %d 只, 耗 %.1fs",
            len(df), has_cap, elapsed,
        )
        return df

    @staticmethod
    def _finalize_us_spot_df(df: pd.DataFrame) -> pd.DataFrame:
        """美股 spot DataFrame 的公共后处理：补列、选列、类型转换。"""
        keep_cols = DailyQuoteFetcher._us_spot_columns()
        for col in keep_cols:
            if col not in df.columns:
                df[col] = None
        df = df[keep_cols]

        numeric_cols = [
            "最新价", "涨跌额", "涨跌幅", "今开", "最高", "最低", "昨收",
            "成交量", "成交额", "总市值", "市盈率-动态", "市净率",
        ]
        for col in numeric_cols:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        return df

    @retry_with_backoff(max_retries=3)
    def fetch_us_exchange_suffixes(self) -> dict[str, str]:
        """批量获取美股交易所后缀映射。

        通过腾讯实时行情接口一次获取所有美股的完整代码（含交易所后缀），
        解析出 {stock_code: exchange_suffix} 映射，供 K 线接口使用。

        Returns:
            {"AAPL": ".OQ", "JPM": ".N", "BRK-B": ".N", ...}
            纳斯达克 → .OQ, 纽交所 → .N
        """
        from db import execute

        rows = execute(
            "SELECT stock_code FROM stock_info WHERE market = 'US'",
            fetch=True,
        )
        all_codes = [r[0] for r in rows]
        if not all_codes:
            return {}

        # Tencent 格式（BRK-B → usBRK.B）；反向映射
        tencent_codes = [f"us{code.replace('-', '.')}" for code in all_codes]
        _code_reverse: dict[str, str] = {}
        for c in all_codes:
            _prefix = c.replace("-", ".").split(".")[0]
            _code_reverse[_prefix] = c

        batch_size = 300
        result: dict[str, str] = {}

        for i in range(0, len(tencent_codes), batch_size):
            batch = tencent_codes[i : i + batch_size]
            q = ",".join(batch)
            url = f"https://qt.gtimg.cn/q={q}"
            try:
                rate_limiter.wait()
                resp = requests.get(url, timeout=15)
                resp.raise_for_status()
                for line in resp.text.strip().split(";"):
                    if '="' not in line:
                        continue
                    try:
                        idx = line.index('"')
                        content = line[idx + 1 : line.rindex('"')]
                    except (ValueError, IndexError):
                        continue
                    parts = content.split("~")
                    if len(parts) < 65:
                        continue
                    raw_code = parts[2].strip()
                    # BRK.B.N → .N, AAPL.OQ → .OQ
                    parts_list = raw_code.split(".")
                    suffix = f".{parts_list[-1]}" if len(parts_list) >= 2 else ""
                    prefix = parts_list[0]
                    db_code = _code_reverse.get(prefix, prefix)
                    result[db_code] = suffix
            except Exception as e:
                logger.error("获取美股交易所后缀失败 (batch %d): %s", i // batch_size + 1, e)
                continue

        logger.info("美股交易所后缀: %d 只", len(result))
        return result


# ── 标准化函数 ────────────────────────────────────────────

def transform_a_hist_to_records(df: pd.DataFrame, market: str = "CN_A") -> list[dict]:
    """将 A 股历史日线 DataFrame 转为 upsert 记录列表。

    Args:
        df: ak.stock_zh_a_hist 返回的 DataFrame
        market: 市场标识
    """
    records = []
    for _, row in df.iterrows():
        records.append({
            "stock_code": str(row.get("股票代码", "")).strip(),
            "trade_date": pd.to_datetime(row["日期"]).date(),
            "market": market,
            "open": _safe_float(row.get("开盘")),
            "high": _safe_float(row.get("最高")),
            "low": _safe_float(row.get("最低")),
            "close": _safe_float(row.get("收盘")),
            "volume": _safe_int(row.get("成交量")),
            "amount": _safe_float(row.get("成交额")),
            "turnover_rate": _safe_float(row.get("换手率")),
            "market_cap": None,  # 历史数据无市值
            "float_market_cap": None,
            "pe_ttm": None,
            "pb": None,
            "currency": "CNY",
            "updated_at": datetime.now(),
        })
    return records


def transform_a_spot_to_records(df: pd.DataFrame) -> list[dict]:
    """将 A 股实时行情 DataFrame 转为 upsert 记录列表。

    含市值、PE、PB 字段。trade_date 取当前日期（交易日收盘后调用）。
    """
    today = datetime.now().date()
    records = []
    for _, row in df.iterrows():
        code = str(row.get("代码", "")).strip()
        if not code:
            continue
        records.append({
            "stock_code": code,
            "trade_date": today,
            "market": "CN_A",
            "open": _safe_float(row.get("今开")),
            "high": _safe_float(row.get("最高")),
            "low": _safe_float(row.get("最低")),
            "close": _safe_float(row.get("最新价")),
            "volume": _safe_int(row.get("成交量")),
            "amount": _safe_float(row.get("成交额")),
            "turnover_rate": _safe_float(row.get("换手率")),
            "market_cap": _safe_float(row.get("总市值")),
            "float_market_cap": _safe_float(row.get("流通市值")),
            "pe_ttm": _safe_float(row.get("市盈率-动态")),
            "pb": _safe_float(row.get("市净率")),
            "currency": "CNY",
            "updated_at": datetime.now(),
        })
    return records


def transform_hk_hist_to_records(df: pd.DataFrame, stock_code: str, market: str = "CN_HK") -> list[dict]:
    """将港股历史日线 DataFrame 转为 upsert 记录列表。

    注意：港股历史数据无「股票代码」列，需外部传入。
    """
    records = []
    for _, row in df.iterrows():
        records.append({
            "stock_code": stock_code,
            "trade_date": pd.to_datetime(row["日期"]).date(),
            "market": market,
            "open": _safe_float(row.get("开盘")),
            "high": _safe_float(row.get("最高")),
            "low": _safe_float(row.get("最低")),
            "close": _safe_float(row.get("收盘")),
            "volume": _safe_int(row.get("成交量")),
            "amount": _safe_float(row.get("成交额")),
            "turnover_rate": _safe_float(row.get("换手率")),
            "market_cap": None,
            "float_market_cap": None,
            "pe_ttm": None,
            "pb": None,
            "currency": "HKD",
            "updated_at": datetime.now(),
        })
    return records


def transform_hk_spot_to_records(df: pd.DataFrame) -> tuple[list[dict], dict[str, str]]:
    """将港股实时行情 DataFrame 转为 upsert 记录列表。

    支持含市值（总市值、PE、PB）的新格式（来自东方财富 API 直调）
    和不含市值的旧格式（来自 ak.stock_hk_spot_em()，兼容回退）。

    Returns:
        (records, industry_map): records 为 upsert 记录列表，
        industry_map 为 {stock_code: industry_name} 映射（用于更新 stock_info）
    """
    today = datetime.now().date()
    records = []
    industry_map = {}
    for _, row in df.iterrows():
        code = str(row.get("代码", "")).strip()
        if not code:
            continue
        # 收集行业信息
        industry = row.get("行业")
        if pd.notna(industry) and str(industry).strip():
            industry_map[code] = str(industry).strip()
        records.append({
            "stock_code": code,
            "trade_date": today,
            "market": "CN_HK",
            "open": _safe_float(row.get("今开")),
            "high": _safe_float(row.get("最高")),
            "low": _safe_float(row.get("最低")),
            "close": _safe_float(row.get("最新价")),
            "volume": _safe_int(row.get("成交量")),
            "amount": _safe_float(row.get("成交额")),
            "turnover_rate": None,  # 港股实时接口无换手率
            "market_cap": _safe_float(row.get("总市值")),  # 东方财富 API f20
            "float_market_cap": None,
            "pe_ttm": _safe_float(row.get("市盈率-动态")),  # 东方财富 API f9
            "pb": _safe_float(row.get("市净率")),  # 东方财富 API f23
            "currency": "HKD",
            "updated_at": datetime.now(),
        })
    return records, industry_map


def transform_us_spot_to_records(df: pd.DataFrame) -> list[dict]:
    """将美股实时行情 DataFrame 转为 upsert 记录列表。

    含市值、PE、PB 字段。trade_date 取当前日期。
    成交额和市值已在 fetch_us_spot() 中转为 USD 原始单位。
    """
    today = datetime.now().date()
    records = []
    for _, row in df.iterrows():
        code = str(row.get("代码", "")).strip()
        if not code:
            continue
        records.append({
            "stock_code": code,
            "trade_date": today,
            "market": "US",
            "open": _safe_float(row.get("今开")),
            "high": _safe_float(row.get("最高")),
            "low": _safe_float(row.get("最低")),
            "close": _safe_float(row.get("最新价")),
            "volume": _safe_int(row.get("成交量")),
            "amount": _safe_float(row.get("成交额")),
            "turnover_rate": None,
            "market_cap": _safe_float(row.get("总市值")),
            "float_market_cap": None,
            "pe_ttm": _safe_float(row.get("市盈率-动态")),
            "pb": _safe_float(row.get("市净率")),
            "currency": "USD",
            "updated_at": datetime.now(),
        })
    return records


def validate_us_spot_records(
    records: list[dict],
) -> tuple[list[dict], list[dict]]:
    """校验美股实时行情记录，拦截异常数据。

    Returns:
        (valid, rejected) — rejected 每条附带 _reject_reason 字段
    """
    valid, rejected = [], []
    for r in records:
        close = r.get("close")
        o, h, l = r.get("open"), r.get("high"), r.get("low")
        vol = r.get("volume")

        # 规则 1: 退化行 — OHLCV 全为 0 或 None，但 close 有值
        # 典型：Tencent 返回空壳数据（HOLX case: close=0.008, open=high=low=vol=0）
        all_zero = all(
            v in (0, 0.0, None)
            for v in (o, h, l, vol)
        )
        if all_zero and close and close > 0:
            r["_reject_reason"] = f"degenerate_row(close={close}, OHLCV=0)"
            rejected.append(r)
            continue

        # 规则 2: 便士股 — 美股正常交易价 > $1
        if close is not None and close < 1.0:
            r["_reject_reason"] = f"penny_stock(close={close})"
            rejected.append(r)
            continue

        valid.append(r)

    if rejected:
        for r in rejected:
            logger.warning(
                "行情校验拒绝: %s reason=%s",
                r.get("stock_code"), r.get("_reject_reason"),
            )
    return valid, rejected


def fetch_finnhub_quotes(stock_codes: list[str]) -> list[dict]:
    """Finnhub API fallback — 逐只获取美股实时行情。

    仅返回 OHLC + close，volume/market_cap/PE/PB 为 None。
    限速 ~55 次/分钟。
    """
    from config import finnhub as cfg

    if not cfg.api_key:
        logger.warning("Finnhub API key 未配置，跳过 fallback")
        return []

    if not stock_codes:
        return []

    finnhub_limiter = AdaptiveRateLimiter(base_delay=1.1, max_delay=5.0)
    today = datetime.now().date()
    records = []
    url = f"{cfg.base_url}/quote"

    for code in stock_codes:
        if not circuit_breaker.allow("finnhub"):
            logger.warning("Finnhub 已熔断，跳过剩余 %d 只", len(stock_codes) - len(records))
            break

        finnhub_limiter.wait()
        try:
            resp = requests.get(
                url,
                params={"symbol": code, "token": cfg.api_key},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

            # Finhub 返回 {"c": current, "d": change, "dp": pct, "h": high, "l": low, "o": open, "pc": prev_close, "t": ts}
            c = data.get("c")
            if c is None or c <= 0:
                logger.debug("Finnhub 无有效价格: %s", code)
                continue

            records.append({
                "stock_code": code,
                "trade_date": today,
                "market": "US",
                "currency": "USD",
                "open": data.get("o"),
                "high": data.get("h"),
                "low": data.get("l"),
                "close": c,
                "volume": None,
                "amount": None,
                "turnover_rate": None,
                "market_cap": None,
                "float_market_cap": None,
                "pe_ttm": None,
                "pb": None,
                "updated_at": datetime.now(),
            })
            circuit_breaker.record_success("finnhub")
        except Exception as e:
            logger.warning("Finnhub 请求失败: %s — %s", code, e)
            circuit_breaker.record_failure("finnhub")

    logger.info("Finnhub fallback: 请求 %d 只，成功 %d 只", len(stock_codes), len(records))
    return records


def transform_us_hist_to_records(rows: list[dict]) -> list[dict]:
    """将腾讯 K 线（美股）返回的记录列表转为 upsert 格式。

    K 线数据只有 OHLCV，无市值/PE/PB/换手率。
    """
    records = []
    for r in rows:
        records.append({
            "stock_code": r["stock_code"],
            "trade_date": r["trade_date"],
            "market": "US",
            "open": r.get("open"),
            "high": r.get("high"),
            "low": r.get("low"),
            "close": r.get("close"),
            "volume": r.get("volume"),
            "amount": None,
            "turnover_rate": None,
            "market_cap": None,
            "float_market_cap": None,
            "pe_ttm": None,
            "pb": None,
            "currency": "USD",
            "updated_at": datetime.now(),
        })
    return records


# ── 辅助函数 ──────────────────────────────────────────────

def _safe_float(val) -> Optional[float]:
    """安全转换浮点数，处理 NaN/None。"""
    if val is None:
        return None
    try:
        f = float(val)
        if pd.isna(f):
            return None
        return f
    except (ValueError, TypeError):
        return None


def _safe_int(val) -> Optional[int]:
    """安全转换整数，处理 NaN/None。"""
    if val is None:
        return None
    try:
        f = float(val)
        if pd.isna(f):
            return None
        return int(f)
    except (ValueError, TypeError):
        return None


# ── 腾讯历史 K 线接口 ─────────────────────────────────────

_TENCENT_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"


def fetch_tencent_hist(
    stock_code: str,
    market: str,
    start_date: str = "2021-01-04",
    end_date: str | None = None,
    max_k: int = 800,
    exchange_suffix: str | None = None,
) -> list[dict]:
    """通过腾讯 K 线接口获取单只股票历史日线（原始数据，不复权）。

    腾讯单次最多返回 ~800 条，超过部分自动分段请求。

    Args:
        stock_code: 股票代码，如 "000001"、"00700"、"AAPL"
        market: "CN_A" / "CN_HK" / "US"
        start_date: 开始日期 YYYY-MM-DD
        end_date: 结束日期 YYYY-MM-DD，默认今天
        max_k: 单次请求最大条数（腾讯限制 ~800）
        exchange_suffix: 美股交易所后缀（如 ".OQ" / ".N"），仅 market="US" 时需要

    Returns:
        daily_quote 格式的记录列表
    """
    if end_date is None:
        end_date = datetime.now().strftime("%Y-%m-%d")

    # 腾讯代码格式
    if market == "CN_A":
        prefix = "sh" if stock_code.startswith(("6", "9")) else "sz"
        tencent_code = f"{prefix}{stock_code}"
        currency = "CNY"
    elif market == "CN_HK":
        tencent_code = f"hk{stock_code}"
        currency = "HKD"
    elif market == "US":
        if not exchange_suffix:
            raise ValueError(f"美股需要提供 exchange_suffix 参数: {stock_code}")
        tencent_code = f"us{stock_code.replace('-', '.')}{exchange_suffix}"
        currency = "USD"
    else:
        raise ValueError(f"不支持的市场: {market}")
    # 腾讯返回数据按日期降序（最新在前），需要分段从 end_date 往前拉
    # 因为 start_date 参数不一定被遵守
    all_records: list[dict] = []
    seg_end = end_date
    seen_dates = set()
    max_segments = 10  # 安全阀：最多 10 段

    for _ in range(max_segments):
        params = {
            "param": f"{tencent_code},day,{start_date},{seg_end},{max_k},",
        }
        resp = requests.get(
            _TENCENT_KLINE_URL,
            params=params,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        day_data = []
        raw_data = data.get("data", {})
        if isinstance(raw_data, dict):
            for k, v in raw_data.items():
                if isinstance(v, dict) and "day" in v and v["day"]:
                    day_data = v["day"]
                    break

        if not day_data:
            break

        # 解析并去重
        seg_records = []
        for row in day_data:
            if len(row) < 6:
                continue
            trade_date = row[0]
            if trade_date in seen_dates:
                continue
            seen_dates.add(trade_date)
            seg_records.append({
                "stock_code": stock_code,
                "trade_date": trade_date,
                "market": market,
                "open": _safe_float(row[1]),
                "high": _safe_float(row[3]),
                "low": _safe_float(row[4]),
                "close": _safe_float(row[2]),
                "volume": _safe_int(row[5]),
                "amount": None,
                "turnover_rate": None,
                "market_cap": None,
                "float_market_cap": None,
                "pe_ttm": None,
                "pb": None,
                "currency": currency,
                "updated_at": datetime.now(),
            })

        if not seg_records:
            break

        all_records.extend(seg_records)

        # 如果返回数量 < max_k，说明已到最早
        if len(day_data) < max_k:
            break

        # 最早日期已经早于 start_date
        earliest = seg_records[0]["trade_date"]
        if isinstance(earliest, str):
            earliest_dt = pd.to_datetime(earliest).date()
        else:
            earliest_dt = earliest
        start_dt = pd.to_datetime(start_date).date()
        if earliest_dt <= start_dt:
            break

        # 下一段的 end_date = 本段最早日期的前一天
        seg_end = (earliest_dt - pd.Timedelta(days=1)).strftime("%Y-%m-%d")

    # 按日期排序
    all_records.sort(key=lambda r: r["trade_date"] if isinstance(r["trade_date"], str) else str(r["trade_date"]))

    # 过滤早于 start_date 的记录
    start_dt = pd.to_datetime(start_date).date()
    filtered = []
    for r in all_records:
        d = pd.to_datetime(r["trade_date"]).date() if isinstance(r["trade_date"], str) else r["trade_date"]
        if d >= start_dt:
            filtered.append(r)

    logger.debug("腾讯 K 线 %s: %d 条", tencent_code, len(filtered))
    return filtered


if __name__ == "__main__":
    import os
    os.environ["TQDM_DISABLE"] = "1"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    fetcher = DailyQuoteFetcher()

    print("=== 测试 A 股历史日线 ===")
    df = fetcher.fetch_a_hist("000001", start_date="20250101", end_date="20250110")
    print(f"行数: {len(df)}")
    records = transform_a_hist_to_records(df)
    for r in records[:2]:
        print(f"  {r['stock_code']} {r['trade_date']} close={r['close']} vol={r['volume']}")

    print("\n=== 测试港股实时行情（含市值） ===")
    df = fetcher.fetch_hk_spot()
    print(f"行数: {len(df)}")
    print(f"列: {list(df.columns)}")
    if len(df) > 0:
        # 过滤有市值的
        has_cap = df[df["总市值"].notna()]
        print(f"有市值的: {len(has_cap)}")
        if len(has_cap) > 0:
            for _, row in has_cap.head(3).iterrows():
                print(f"  {row['代码']} {row['名称']} close={row['最新价']} cap={row['总市值']} pe={row['市盈率-动态']} pb={row['市净率']}")

        records, industry_map = transform_hk_spot_to_records(df)
        cap_records = [r for r in records if r["market_cap"] is not None]
        print(f"\n转换后记录数: {len(records)}, 有市值: {len(cap_records)}, 行业映射: {len(industry_map)} 只")
        if cap_records:
            for r in cap_records[:3]:
                print(f"  {r['stock_code']} cap={r['market_cap']} pe={r['pe_ttm']} pb={r['pb']}")

    print("\n=== 测试港股历史日线 ===")
    df = fetcher.fetch_hk_hist("00700", start_date="20250101", end_date="20250110")
    print(f"行数: {len(df)}")
    records = transform_hk_hist_to_records(df, "00700")
    for r in records[:2]:
        print(f"  {r['stock_code']} {r['trade_date']} close={r['close']} vol={r['volume']}")
