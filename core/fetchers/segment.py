"""
fetchers/segment.py — A股分业务收入构成（主营构成分析）

数据源：东方财富 F10 经营分析
  GET https://emweb.securities.eastmoney.com/PC_HSF10/BusinessAnalysis/PageAjax?code={EM_CODE}
  → zygcfx 数组：REPORT_DATE / MAINOP_TYPE / ITEM_NAME /
    MAIN_BUSINESS_INCOME（元）/ MBI_RATIO（0~1）/ GROSS_RPOFIT_RATIO（0~1）

MAINOP_TYPE 映射：1=行业(industry)、2=产品(product)、3=地区(region)

产出结构：[{stock_code, report_date, dimension, item_name,
           revenue, revenue_ratio, gross_margin}] → 写入 stock_segment（source='em_f10'）
"""

import logging
from typing import Any, Optional

import requests

from .base import retry_with_backoff, rate_limiter

logger = logging.getLogger(__name__)

_EASTMONEY_BA_URL = (
    "https://emweb.securities.eastmoney.com/"
    "PC_HSF10/BusinessAnalysis/PageAjax?code={em_code}"
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://emweb.securities.eastmoney.com/",
}

_MAINOP_TYPE_MAP = {"1": "industry", "2": "product", "3": "region"}


def to_em_code(stock_code: str) -> str:
    """stock_code → 东财代码。

    实测前缀分布（stock_info, 2026-08）：
      SH: 600/601/603/605/688/689；SZ: 000/001/002/003/300/301/302；BJ: 920
    stock_info.em_code 对 CN_A 全为 NULL，必须推导。
    """
    code = stock_code.strip()
    if code.startswith("6"):
        return f"SH{code}"
    if code.startswith(("0", "3")):
        return f"SZ{code}"
    # 北交所：920 为主，8xx/4xx 兜底
    return f"BJ{code}"


@retry_with_backoff
def _fetch_business_analysis(em_code: str) -> dict[str, Any]:
    rate_limiter.wait()
    resp = requests.get(_EASTMONEY_BA_URL.format(em_code=em_code), headers=_HEADERS, timeout=15)
    resp.raise_for_status()
    return resp.json()


def fetch_cn_a_segment(stock_code: str) -> tuple[list[dict[str, Any]], Optional[dict[str, Any]]]:
    """拉取一只 A 股的主营构成分析（全历史期）。

    Returns:
        (records, raw): records 为解析后的 stock_segment 行（不含 source）；
        raw 为原始 JSON（存 raw_snapshot，Layer 0）；接口无数据时 raw 为 None。
    """
    em_code = to_em_code(stock_code)
    data = _fetch_business_analysis(em_code)
    items = data.get("zygcfx") or []
    if not items:
        logger.warning("%s(%s) 无主营构成数据", stock_code, em_code)
        return [], None

    records: list[dict[str, Any]] = []
    for it in items:
        dimension = _MAINOP_TYPE_MAP.get(str(it.get("MAINOP_TYPE") or ""))
        report_date = (it.get("REPORT_DATE") or "")[:10]
        item_name = (it.get("ITEM_NAME") or "").strip()
        if not dimension or not report_date or not item_name:
            continue
        records.append({
            "stock_code": stock_code,
            "report_date": report_date,
            "dimension": dimension,
            "item_name": item_name,
            "revenue": it.get("MAIN_BUSINESS_INCOME"),
            "revenue_ratio": _sanitize_ratio(it.get("MBI_RATIO"), lo=-1.0, hi=1.5),
            # 东财存在成本近零导致的垃圾毛利率（如实测 -6767 万%），置 None
            "gross_margin": _sanitize_ratio(it.get("GROSS_RPOFIT_RATIO"), lo=-10.0, hi=1.0),
        })
    return records, data


def _sanitize_ratio(v: Any, lo: float, hi: float) -> Any:
    """比率字段越界置 None，防止脏数据顶爆 NUMERIC 精度。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if lo <= f <= hi else None
