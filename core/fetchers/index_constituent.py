"""
指数成分股拉取模块

从中证指数官网（csindex.com.cn）拉取指数成分股列表，返回标准化的 DataFrame。
依赖 akshare 的 index_stock_cons_csindex 接口。

Usage:
    >>> from core.fetchers.index_constituent import fetch_index_constituents
    >>> df = fetch_index_constituents("000300")
    >>> print(df.columns.tolist())
    ['index_code', 'stock_code', 'effective_date', 'index_name', 'stock_name', 'exchange']
"""

import logging
from typing import Optional

import pandas as pd
from akshare import index_stock_cons_csindex

logger = logging.getLogger(__name__)

# ── 支持的指数代码映射 ──────────────────────────────────────────
SUPPORTED_INDEXES: dict[str, str] = {
    "000300": "沪深300",
    "000905": "中证500",
    "000016": "上证50",
    "000852": "中证1000",
    "399006": "创业板指",
    "000688": "科创50",
    "HSI": "恒生指数",
}


def fetch_index_constituents(
    index_code: str,
    retry: int = 2,
) -> pd.DataFrame:
    """拉取指数成分股列表。

    Parameters
    ----------
    index_code : str
        指数代码，如 "000300"（沪深300）、"000905"（中证500）。
    retry : int
        失败重试次数（默认 2）。

    Returns
    -------
    pd.DataFrame
        标准化后的成分股数据，列：
        - index_code    : 指数代码（str）
        - stock_code    : 成分券代码（str），如 "000001"
        - effective_date: 成分生效日期（str），如 "2026-03-24"
        - index_name    : 指数名称（str），如 "沪深300"
        - stock_name    : 成分券名称（str），如 "平安银行"
        - exchange      : 交易所（str），如 "深圳证券交易所"

    Raises
    ------
    ValueError
        不支持的指数代码。
    RuntimeError
        多次重试后仍然拉取失败。

    Notes
    -----
    数据源：中证指数官网 https://www.csindex.com.cn
    API: akshare.index_stock_cons_csindex(symbol=...)
    返回原始列名：日期, 指数代码, 指数名称, 指数英文名称,
                  成分券代码, 成分券名称, 成分券英文名称, 交易所, 交易所英文名称
    """
    index_code = index_code.strip()

    if index_code not in SUPPORTED_INDEXES:
        logger.warning(
            "指数代码 '%s' 不在预设列表中，仍尝试拉取。预设: %s",
            index_code,
            list(SUPPORTED_INDEXES.keys()),
        )

    for attempt in range(1, retry + 2):
        try:
            logger.info(
                "拉取指数成分股: %s (%s) [attempt %d]",
                index_code,
                SUPPORTED_INDEXES.get(index_code, "未知"),
                attempt,
            )
            raw_df = index_stock_cons_csindex(symbol=index_code)

            if raw_df is None or raw_df.empty:
                logger.warning("指数 %s 返回空数据", index_code)
                return pd.DataFrame(
                    columns=[
                        "index_code",
                        "stock_code",
                        "effective_date",
                        "index_name",
                        "stock_name",
                        "exchange",
                    ]
                )

            # ── 标准化列映射 ───────────────────────────────────────
            # 原始列: 日期, 指数代码, 指数名称, 指数英文名称,
            #         成分券代码, 成分券名称, 成分券英文名称, 交易所, 交易所英文名称
            col_map = {
                "日期": "effective_date",
                "指数代码": "index_code",
                "指数名称": "index_name",
                "成分券代码": "stock_code",
                "成分券名称": "stock_name",
                "交易所": "exchange",
            }

            df = raw_df[list(col_map.keys())].rename(columns=col_map)

            # 只保留标准化后的列
            keep_cols = [
                "index_code",
                "stock_code",
                "effective_date",
                "index_name",
                "stock_name",
                "exchange",
            ]
            df = df[keep_cols]

            # 确保字符串类型
            df["stock_code"] = df["stock_code"].astype(str).str.strip()
            df["index_code"] = df["index_code"].astype(str).str.strip()
            df["effective_date"] = pd.to_datetime(df["effective_date"]).dt.strftime(
                "%Y-%m-%d"
            )

            logger.info(
                "指数 %s 成分股拉取完成，共 %d 只", index_code, len(df)
            )
            return df

        except Exception as exc:
            logger.error(
                "拉取指数 %s 成分股失败 (attempt %d/%d): %s",
                index_code,
                attempt,
                retry + 1,
                exc,
            )
            if attempt == retry + 1:
                raise RuntimeError(
                    f"拉取指数 {index_code} 成分股失败，已重试 {retry + 1} 次"
                ) from exc


def fetch_all_index_constituents(
    index_codes: Optional[list[str]] = None,
) -> dict[str, pd.DataFrame]:
    """批量拉取多个指数的成分股。

    Parameters
    ----------
    index_codes : list[str] | None
        要拉取的指数代码列表。默认拉取全部支持的指数。

    Returns
    -------
    dict[str, pd.DataFrame]
        {指数代码: 标准化后的 DataFrame}
    """
    if index_codes is None:
        index_codes = list(SUPPORTED_INDEXES.keys())

    results: dict[str, pd.DataFrame] = {}
    for code in index_codes:
        try:
            results[code] = fetch_index_constituents(code)
        except Exception as exc:
            logger.error("拉取指数 %s 失败，跳过: %s", code, exc)
            results[code] = pd.DataFrame()

    return results
