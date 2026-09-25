"""
transformers/tencent_hk.py — 腾讯财经港股报表标准化转换器（T1：仅现金流量表 capex）

响应结构（GET .../hkcwbb/detail?type=xjll）：
  {code:0, data:{data:[按报告期的表, 最新在前], rttype:[...]}}
  每张表 row0 是表头: [['', ''], ['20251231', {font, sep, reportype, fiscalYear}]]
    reportype: '0'=年报 '1'=中报 '2'/'3'=季报（归期以此为准，勿用 1231 后缀；
               fiscalYear 是财年截止日，可能非 12-31，如阿里 3 月止）
  数据行: [['购买固定资产', {font}], ['13.37亿元', {font}, '-12.29']]
    值是展示字符串（"13.37亿元"/"--"/"9,000.00元"），第三元素是同比%（不映射）
    cell 可能是 list 也可能是字符串化 list（防御处理，先试 ast.literal_eval）

注意：腾讯 capex 行 ≈ 仅固定资产（东财 = 固定资产+无形资产，实测差 ~6%），
因此下游只用于"补 NULL、不覆盖已有值"（方案 §2/§4）。
"""
from __future__ import annotations

import ast
import logging
import re
from datetime import date
from typing import Any

logger = logging.getLogger(__name__)

# reportype → DB report_type（方案 §4）
_REPORT_TYPE_MAP = {
    "0": "annual",
    "1": "semi",
    "2": "quarterly",
    "3": "quarterly",
}

# capex 行正则族（方案 §4）；排除 出售/处置/折旧 行
_CAPEX_RE = re.compile(r"(购买|購買|购建|購建).{0,14}(固定资产|固定資產|資本支出?)")
_CAPEX_EXCLUDE_RE = re.compile(r"出售|处置|處置|折旧|折舊")

_AMOUNT_RE = re.compile(r"^(-?[\d,]+(?:\.\d+)?)(亿元|万元|元)?$")
_UNIT_MULT = {"亿元": 1e8, "万元": 1e4, "元": 1.0, None: 1.0}


def _as_cell(cell: Any) -> list:
    """cell 可能是 list 也可能是字符串化 list，统一为 list。"""
    if isinstance(cell, list):
        return cell
    if isinstance(cell, tuple):
        return list(cell)
    if isinstance(cell, str):
        try:
            parsed = ast.literal_eval(cell)
        except (ValueError, SyntaxError):
            return [cell]
        if isinstance(parsed, (list, tuple)):
            return list(parsed)
        return [parsed]
    return [cell]


def _parse_amount(raw: Any) -> float | None:
    """解析展示字符串为数值（元）：'13.37亿元'→1.337e9；'--'/空→None。"""
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s or s == "--":
        return None
    m = _AMOUNT_RE.match(s)
    if not m:
        logger.debug("腾讯港股金额无法解析: %r", raw)
        return None
    try:
        val = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return val * _UNIT_MULT[m.group(2)]


def _is_capex_label(label: Any) -> bool:
    if not isinstance(label, str):
        return False
    if _CAPEX_EXCLUDE_RE.search(label):
        return False
    return bool(_CAPEX_RE.search(label))


def transform_cashflow(payload: dict[str, Any], stock_code: str) -> list[dict[str, Any]]:
    """标准化腾讯港股现金流量表，提取 capex。

    Args:
        payload: fetch_cashflow 返回的原始 dict
        stock_code: 港股代码（5 位，如 '02020'）

    Returns:
        [{stock_code, report_date(date), report_type, capex_raw, capex?}, ...]
        capex 缺失（'--'）时不设该键；不映射 currency/cfo_net 等列（方案 §4）。
    """
    tables = ((payload or {}).get("data") or {}).get("data") or []

    results: list[dict[str, Any]] = []
    for table in tables:
        if not isinstance(table, list) or len(table) < 2:
            continue

        # ---- 表头：报告期 + reportype ----
        header = _as_cell(table[0][1]) if isinstance(table[0], list) and len(table[0]) > 1 else []
        period_str = str(header[0]).strip() if header else ""
        meta = header[1] if len(header) > 1 and isinstance(header[1], dict) else {}
        report_type = _REPORT_TYPE_MAP.get(str(meta.get("reportype", "")).strip())
        try:
            report_date = date(
                int(period_str[0:4]), int(period_str[4:6]), int(period_str[6:8])
            )
        except (ValueError, IndexError):
            logger.warning(
                "腾讯港股现金流量表表头异常: stock=%s header=%r", stock_code, table[0]
            )
            continue
        if not report_type:
            logger.warning(
                "腾讯港股现金流量表未知 reportype: stock=%s period=%s reportype=%r",
                stock_code, period_str, meta.get("reportype"),
            )
            continue

        # ---- 数据行：找 capex 行（取第一个命中） ----
        capex_raw: str | None = None
        for row in table[1:]:
            if not isinstance(row, list) or len(row) < 2:
                continue
            label_cell = _as_cell(row[0])
            label = label_cell[0] if label_cell else None
            if not _is_capex_label(label):
                continue
            value_cell = _as_cell(row[1])
            capex_raw = value_cell[0] if value_cell else None
            if not isinstance(capex_raw, str):
                capex_raw = str(capex_raw) if capex_raw is not None else None
            break

        record: dict[str, Any] = {
            "stock_code": stock_code,
            "report_date": report_date,
            "report_type": report_type,
            "capex_raw": capex_raw,
        }
        capex = _parse_amount(capex_raw)
        if capex is not None:
            record["capex"] = capex
        results.append(record)

    return results


if __name__ == "__main__":
    import json
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    path, code = sys.argv[1], sys.argv[2]
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    for r in transform_cashflow(payload, code):
        print(r)
