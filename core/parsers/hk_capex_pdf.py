"""港股 capex PDF 解析（T2 方案 §4，移植自 /tmp/hkex_samples/parse_pymupdf.py）。

纯函数层：只读 PDF 文件，不访问 DB / 网络。

流程：页定位（现金流量表标题 + 投資活動兜底 + 相邻页吞并）→ 行重建（words 按 y 聚类）
→ 别名匹配（分级 A/B/C）→ 数值提取（数字 run 不合并、R3 角注号剔除、R2 双币种列过滤）
→ 单位/币种规范化（币种+量级分离）→ R1/R5/R7/R8 校验。

返回 dict 的 status 区分：
  ok                抽取成功（value = capex 原值×倍率；value_abs 供 DB 写入）
  no_cf_page        全文无现金流量表页（升级链应升到中期報告/英文版）
  no_capex_row      有 CF 页但无 capex 行（摘要表型：仅净额三行）
  period_mismatch   有 capex 行但期间与目标不符（如 MD&A 季度表 vs 中报目标）
  period_unverified 有 capex 行但期间无法解析（不写库，进人工）
  unit_unknown      单位量级无法确定（不写库，进人工）
  low_confidence    仅散文/破折号级（C 级）命中
"""
from __future__ import annotations

import re
import time
from datetime import date
from pathlib import Path
from typing import Any, Optional

from .hk_capex_aliases import (
    HQ_PROPERTY_PAT,
    NUM_TOKEN,
    PPE_PAT,
    detect_currency,
    detect_magnitude,
    match_alias,
    parse_num,
    unit_scale,
)

import pymupdf

CF_PAT = re.compile(
    r"現金流量表|现金流量表|現金流動表|现金流動表|cash\s?flows?\s+statement|statement\s+of\s+cash\s?flows?",
    re.I,
)
# 投資活動兜底：簡明摘要表缺标题的页（港交所"現金流動表"变体已在 CF_PAT 内）
INVEST_PAT = re.compile(r"投資活動|投資業務|investing activities", re.I)
PROSE_PAT = re.compile(r"[。，；]|同比|期內|截至.*止.*(增加|減少|為)")

_CN_DIGIT = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_int(s: str) -> Optional[int]:
    """中文数字 → int。支持 二零二六(逐位) / 十二(additive) / 三十一 / 六。"""
    if not s:
        return None
    if all(c in _CN_DIGIT for c in s) and "零" not in s and len(s) > 1:
        return int("".join(str(_CN_DIGIT[c]) for c in s))  # 二零二六 handled below; 十二月→?
    if "零" in s and all(c in _CN_DIGIT for c in s):
        return int("".join(str(_CN_DIGIT[c]) for c in s))  # 二零二六
    if "十" in s:
        left, _, right = s.partition("十")
        tens = _CN_DIGIT.get(left, 1) if left else 1
        units = _CN_DIGIT.get(right, 0) if right else 0
        return tens * 10 + units
    if len(s) == 1 and s in _CN_DIGIT:
        return _CN_DIGIT[s]
    if all(c in _CN_DIGIT for c in s):
        return int("".join(str(_CN_DIGIT[c]) for c in s))
    return None


_MONTHS_CN = {"三": 3, "六": 6, "九": 9, "十二": 12}

# 截至 YYYY年M月D日 止（六個月|三個月|年度） —— 容忍空白
_RE_DATE_CN = re.compile(
    r"(?:截至)?\s*(20\d{2}|[二零一二三四五六七八九]{4})\s*年\s*"
    r"([一二三四五六七八九十]{1,3}|\d{1,2})\s*月\s*"
    r"([一二三四五六七八九十]{1,4}|\d{1,2})\s*日?"
)
_RE_MONTHS = re.compile(
    r"止\s*之?\s*([三六九]|[十二]{2}|\d{1,2})\s*個?月|(six|three|nine|twelve|\d{1,2})\s*months?\s+ended|"
    r"(year\s+ended|止年度|全年)|(half-?years?\s+(?:ended|to))",
    re.I,
)
_EN_MONTHS = {"three": 3, "six": 6, "nine": 9, "twelve": 12}
_EN_MONTH_NAME = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], start=1,
)}
_EN_MON_ABBR = {m[:3]: i for m, i in _EN_MONTH_NAME.items()} | {"sept": 9}
_RE_DATE_EN = re.compile(
    r"(\d{1,2})(?:st|nd|rd|th)?\s+"
    r"(january|february|march|april|may|june|july|august|september|october|november|december|"
    r"jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\s*,?\s*(\d{4})|"
    r"(january|february|march|april|may|june|july|august|september|october|november|december|"
    r"jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\s+"
    r"(\d{1,2})(?:st|nd|rd|th)?\s*,?\s*(\d{4})",
    re.I,
)


def parse_period_header(text: str) -> tuple[Optional[date], Optional[int]]:
    """从 CF 页表头文本解析 (期末日, 月数)。解析失败返回 (None, None)。"""
    flat = re.sub(r"\s+", " ", text)
    end: Optional[date] = None
    m = _RE_DATE_CN.search(flat)
    if m:
        y_raw, mo_raw, d_raw = m.groups()
        y = int(y_raw) if y_raw.isdigit() else _cn_int(y_raw)
        mo = int(mo_raw) if mo_raw.isdigit() else _cn_int(mo_raw)
        d = int(d_raw) if d_raw.isdigit() else _cn_int(d_raw)
        if y and mo and d:
            try:
                end = date(y, mo, d)
            except ValueError:
                end = None
    if end is None:
        m = _RE_DATE_EN.search(flat)
        if m:
            mon_name = (m.group(2) or m.group(4)).lower()
            mon = _EN_MONTH_NAME.get(mon_name) or _EN_MON_ABBR.get(mon_name)
            d = int(m.group(1) or m.group(5))
            y = int(m.group(3) or m.group(6))
            if mon and y:
                try:
                    end = date(y, mon, d)
                except ValueError:
                    end = None
    months: Optional[int] = None
    m = _RE_MONTHS.search(flat)
    if m:
        if m.group(1):
            tok = m.group(1)
            months = _MONTHS_CN.get(tok, int(tok) if tok.isdigit() else None)
        elif m.group(2):
            tok = m.group(2).lower()
            months = _EN_MONTHS.get(tok, int(tok) if tok.isdigit() else None)
        elif m.group(3):
            months = 12  # year ended
        else:
            months = 6  # half-year ended/to（该备选无捕获组）
    return end, months


def locate_cf_pages(pdf_path: str | Path) -> dict[str, Any]:
    """定位现金流量表候选页。返回 1-based 页码列表。

    scan_pages = CF 标题页 ∪ 投資活動兜底页 ∪ CF 页后相邻吞并页（跨页表续页）。
    """
    doc = pymupdf.open(pdf_path)
    try:
        n = doc.page_count
        cf_pages: list[int] = []
        invest_pages: list[int] = []
        for pno in range(n):
            text = doc[pno].get_text("text")
            if CF_PAT.search(text):
                cf_pages.append(pno + 1)
            elif INVEST_PAT.search(text):
                invest_pages.append(pno + 1)
    finally:
        doc.close()
    scan = set(cf_pages) | set(invest_pages)
    for p in list(cf_pages):
        if p + 1 <= n and (p + 1) in invest_pages:
            scan.add(p + 1)  # 相邻页吞并：续页含投資活動但无标题
    return {
        "pages": n,
        "cf_pages": sorted(cf_pages),
        "scan_pages": sorted(scan),
    }


def _lines_from_words(page) -> list[list]:
    """按 y 坐标聚类成行(跨 block/字体), 行内按 x 排序。

    用 word 中心 y 聚类（非顶部 y）：中期报告里标签字框常比数字高出一截
    （李宁「購入物業、機器及設備」字框 22.8pt，数字顶部差 7pt），
    按顶部聚类会把数值拆到下一行。
    """
    words = page.get_text("words")
    words = sorted(words, key=lambda w: ((w[1] + w[3]) / 2, w[0]))
    lines, cur, cur_cy = [], [], None
    for w in words:
        cy = (w[1] + w[3]) / 2
        if cur_cy is None or abs(cy - cur_cy) <= 4.5:
            cur.append(w)
            cur_cy = cy if cur_cy is None else (cur_cy * 0.5 + cy * 0.5)
        else:
            cur.sort(key=lambda w: w[0])
            lines.append(cur)
            cur, cur_cy = [w], cy
    if cur:
        cur.sort(key=lambda w: w[0])
        lines.append(cur)
    return lines


def _split_runs(ws):
    """行内词切分为交替 run: [(kind 't'/'n', text, x0, x1)]。数字 run 不合并(逐 token)。"""
    runs = []
    for w in ws:
        kind = "n" if NUM_TOKEN.match(w[4]) else "t"
        if kind == "t" and runs and runs[-1][0] == "t":
            runs[-1][1].append(w[4])
            runs[-1][3] = w[2]
        else:
            runs.append([kind, [w[4]], w[0], w[2]])
    return [(k, " ".join(t), x0, x1) for k, t, x0, x1 in runs]


def _values_after(runs, label_idx):
    """取 label run 之后的数字 run（R3 角注号剔除: <30 裸整数且后续还有 >=2 个数）。

    返回 [(value, x_center, x0, x1, raw_token), ...]，保留 raw_token 供全零判定。
    """
    nums = []
    for k, t, x0, x1 in runs[label_idx + 1:]:
        if k != "n":
            continue
        v = parse_num(t)
        if v is not None:
            nums.append((v, (x0 + x1) / 2, x0, x1, t))
    if len(nums) >= 3 and float(nums[0][0]).is_integer() and 0 < nums[0][0] < 30:
        nums = nums[1:]
    return nums


def _currency_columns(lines) -> dict[str, Any]:
    """R2 双币种列检测：表头区(前 25 行)含币种的行 → [(currency, x_center)]。"""
    cols = []
    for ws in lines[:25]:
        text = "".join(w[4] for w in ws)
        cur = detect_currency(text)
        if cur is None:
            continue
        mult, mag = detect_magnitude(text)
        if mult is None and "元" not in text and "million" not in text.lower() and "dollar" not in text.lower():
            continue
        x = sum((w[0] + w[2]) / 2 for w in ws) / len(ws)
        cols.append((cur, round(x, 1), text.strip()))
    return cols


def _year_columns(lines) -> list[tuple[int, float]]:
    """R2 增强：表头区(前 30 行)的阿拉伯年份 token → [(year, x_center)]。

    阿里型报表先列上年列（2024 | 2025 | 美元）；识别列头年份后按 x 归属数值列，
    取目标年列的值（而非盲目取第一个数值）。
    """
    cols: list[tuple[int, float]] = []
    for ws in lines[:30]:
        for w in ws:
            m = re.fullmatch(r"(20\d{2})年?", w[4].strip())
            if m:
                cols.append((int(m.group(1)), round((w[0] + w[2]) / 2, 1)))
    return cols


def _filter_reporting_currency(nums, cols):
    """R2：多币种表头时，仅保留报告币种列的数值。

    报告币种 = 表头中出现次数最多的币种（如长和「港幣百萬元」×2 vs 「百萬美元」×1）。
    每个数值按最近表头列归属币种；归属非报告币种的值剔除。
    返回 (过滤后 nums, 报告币种 或 None)。单币种/未检出时原样返回。
    """
    if not cols:
        return nums, None
    from collections import Counter
    freq = Counter(c for c, _, _ in cols)
    reporting = freq.most_common(1)[0][0]
    if len(freq) < 2:
        return nums, reporting
    col_xs: dict[str, list[float]] = {}
    for c, x, _ in cols:
        col_xs.setdefault(c, []).append(x)
    kept = []
    for v, xc, x0, x1, tok in nums:
        best_c, best_d = None, None
        for c, xs in col_xs.items():
            d = min(abs(xc - x) for x in xs)
            if best_d is None or d < best_d:
                best_c, best_d = c, d
        if best_c == reporting:
            kept.append((v, xc, x0, x1, tok))
    return (kept if kept else nums), reporting


def extract_capex(
    pdf_path: str | Path,
    target_period: Optional[date | str] = None,
    target_months: Optional[int] = None,
) -> dict[str, Any]:
    """从业绩公告/中期报告 PDF 抽取现金流量表 capex 行。

    Args:
        pdf_path: PDF 文件路径。
        target_period: 目标报告期末日（date 或 'YYYY-MM-DD'），None 则跳过期间校验。
        target_months: 目标期间月数（6=中报, 12=年报, 3=季报），None 则跳过月数校验。

    Returns:
        见模块 docstring；ok 时含 value(带符号原值×倍率)/value_abs/multiplier/currency/
        unit_label/page/label_raw/restated/grade/evidence/candidates 等。
    """
    t0 = time.perf_counter()
    if isinstance(target_period, str):
        target_period = date.fromisoformat(target_period)

    located = locate_cf_pages(pdf_path)
    result: dict[str, Any] = {
        "status": "no_cf_page",
        "pdf": str(pdf_path),
        "pages": located["pages"],
        "cf_pages": located["cf_pages"],
        "candidates": [],
        "excluded": [],
        "rejected_period": [],
        "evidence": [],
        "ms": 0.0,
    }
    if not located["scan_pages"]:
        result["ms"] = round((time.perf_counter() - t0) * 1000, 1)
        return result

    doc = pymupdf.open(pdf_path)
    hits: list[dict[str, Any]] = []
    page_meta: dict[int, dict[str, Any]] = {}
    try:
        for pno in located["scan_pages"]:
            page = doc[pno - 1]
            text = page.get_text("text")
            is_cf_page = pno in located["cf_pages"]
            lines = _lines_from_words(page)
            # 期间/单位用原始文本解析：raw text 按几何列优先输出，重建行会把左右两列表头
            # 并成一行（如港交所「截至2026年截至2025年」），导致期间错配到上年列。
            header_text = text[:1500]
            end, months = parse_period_header(header_text)
            mult, unit_label = unit_scale(header_text)
            cur_cols = _currency_columns(lines)
            year_cols = _year_columns(lines)
            restated = bool(re.search(r"經重列|重列|restated", text, re.I))
            page_meta[pno] = {
                "period_end": end.isoformat() if end else None,
                "period_months": months,
                "unit_label": unit_label,
                "multiplier": mult,
                "restated": restated,
                "currency_cols": [{"currency": c, "x": x, "line": t} for c, x, t in cur_cols],
                "year_cols": [{"year": y, "x": x} for y, x in year_cols],
            }
            for li, ws in enumerate(lines):
                runs = _split_runs(ws)
                full = " ".join(t for _, t, _, _ in runs)
                prose = bool(PROSE_PAT.search(full))
                matched = None
                for ri, (k, t, x0, x1) in enumerate(runs):
                    if k != "t" or len(t.strip()) < 2:
                        continue
                    m = match_alias(t)
                    if not m:
                        # 跨run合并标签(中英双语同列): 与前后相邻 text run 拼起来试
                        for t2 in (
                            (runs[ri - 1][1] + " " + t) if ri >= 1 and runs[ri - 1][0] == "t" else None,
                            (t + " " + runs[ri + 1][1]) if ri + 1 < len(runs) and runs[ri + 1][0] == "t" else None,
                        ):
                            if not t2 or t2 == t:
                                continue
                            m2 = match_alias(t2)
                            if m2:
                                m, t = m2, t2
                                break
                        if not m:
                            continue
                    if HQ_PROPERTY_PAT.search(t):
                        result["excluded"].append({
                            "page": pno, "label_raw": t, "reason": "hq_property_row",
                            "raw_line": full[:200],
                        })
                        continue
                    nums = _values_after(runs, ri)
                    # 两行式标签: 本行无数, 向后看 1-2 行短标签行取数(比亚迪: 中英标签分行且 y 不同)
                    if not nums:
                        for lj in range(li + 1, min(li + 3, len(lines))):
                            nruns = _split_runs(lines[lj])
                            ntext = " ".join(t2 for k2, t2, _, _ in nruns if k2 == "t")
                            if len(ntext) > 60:
                                break
                            for rj, (k2, t2, _, _) in enumerate(nruns):
                                if k2 != "t":
                                    continue
                                nums = _values_after(nruns, rj)
                                if nums:
                                    t = (t + " " + ntext).strip()
                                    # 标签跨行补全后用完整标签重试别名，命中则升级（美团：and intangible assets 在次行）
                                    m2 = match_alias(t)
                                    if m2 and (m[1] or not m2[1]):
                                        m = m2
                                    break
                            if nums:
                                break
                    if not nums:
                        continue
                    # R2 增强(阿里型陷阱)：部分报表先列上年列（2024|2025|美元），
                    # 按列头年份把数值归属到目标年列；目标年不在列头年份中 → 期间不符。
                    prior_val = None
                    tgt_nums = nums
                    tgt_year = target_period.year if target_period is not None else None
                    if tgt_year is not None and len({y for y, _ in year_cols}) >= 2:
                        years_present = {y for y, _ in year_cols}
                        if tgt_year in years_present:
                            def _yr(xc, _yc=year_cols):
                                return min(_yc, key=lambda yx: abs(yx[1] - xc))[0]
                            tgt_nums = [n for n in nums if _yr(n[1]) == tgt_year]
                            oth_nums = [n for n in nums if _yr(n[1]) != tgt_year]
                            if tgt_nums and oth_nums:
                                oth_kept, _ = _filter_reporting_currency(oth_nums, cur_cols)
                                prior_pool = oth_kept or oth_nums
                                prior_val = prior_pool[0][0]
                        else:
                            result["rejected_period"].append({
                                "page": pno, "label_raw": t, "reason": "year_column_mismatch",
                                "years": sorted(years_present), "target_year": tgt_year,
                                "raw_line": full[:200],
                            })
                            continue
                    nums2, used_ccy = _filter_reporting_currency(tgt_nums, cur_cols)
                    # 散文判定: 标签 span 内嵌数字(如"集團於2026年上半年之資本開支…")
                    has_num_inside = any(
                        k2 == "n" and any(r2[0] == "t" for r2 in runs[:j])
                        and any(r2[0] == "t" for r2 in runs[j + 1:ri + 1])
                        for j, (k2, _, _, _) in enumerate(runs[:ri + 1])
                    )
                    is_prose = prose or has_num_inside
                    grade = "C" if is_prose else ("B" if m[1] else "A")
                    vals = [v for v, _, _, _, _ in nums2]
                    if not any(v != 0 for v in vals):
                        toks = [tok for *_, tok in nums2]
                        has_explicit_zero = any(
                            re.fullmatch(r"\(?-?0+(?:\.0+)?\)?", t.strip().replace(",", ""))
                            for t in toks
                        )
                        if not (has_explicit_zero or prior_val):
                            grade = "C"  # 全破折号且无上年佐证 → 无有效数值
                    hits.append({
                        "page": pno,
                        "cf_page": is_cf_page,
                        "grade": grade,
                        "label_raw": t,
                        "alias": m[0],
                        "weak": m[1],
                        "values": vals,
                        "prior": prior_val,
                        "currency_col": used_ccy,
                        "raw_line": full[:200],
                        "ppe_rank": 0 if PPE_PAT.search(t) else 1,
                        "period_end": end,
                        "period_months": months,
                        "multiplier": mult,
                        "unit_label": unit_label,
                        "restated": restated,
                    })
                    matched = True
                    break
                if matched:
                    continue
    finally:
        doc.close()

    result["page_meta"] = page_meta
    result["candidates"] = [
        {**h, "period_end": h["period_end"].isoformat() if h["period_end"] else None}
        for h in hits
    ]
    if not hits:
        # 有行但因列头年份与目标期不符被全部拒止 → 期间不符（优先于无行判定）
        if any(r.get("reason") == "year_column_mismatch" for r in result["rejected_period"]):
            result["status"] = "period_mismatch"
        else:
            result["status"] = "no_capex_row"
        result["ms"] = round((time.perf_counter() - t0) * 1000, 1)
        return result

    # R5 期间校验：目标给定时，先按 period_months 过滤，再按期末日过滤
    pool = hits
    if target_months is not None:
        matched_months = [h for h in pool if h["period_months"] == target_months]
        rejected = [h for h in pool if h["period_months"] is not None and h["period_months"] != target_months]
        unverified = [h for h in pool if h["period_months"] is None]
        result["rejected_period"] = result.get("rejected_period", []) + [
            {**h, "period_end": h["period_end"].isoformat() if h["period_end"] else None}
            for h in rejected
        ]
        pool = matched_months or unverified
        if not matched_months and unverified:
            result["status"] = "period_unverified"
        elif not matched_months and rejected:
            result["status"] = "period_mismatch"
            result["ms"] = round((time.perf_counter() - t0) * 1000, 1)
            return result
        if not pool:
            result["status"] = "period_mismatch"
            result["ms"] = round((time.perf_counter() - t0) * 1000, 1)
            return result
    if target_period is not None:
        with_end = [h for h in pool if h["period_end"] is not None]
        if with_end:
            pool2 = [h for h in with_end if h["period_end"] == target_period]
            if not pool2:
                result["status"] = "period_mismatch"
                result["ms"] = round((time.perf_counter() - t0) * 1000, 1)
                return result
            pool = pool2
        # 全部无期末日 → 保持 pool（月数已校验），不拒

    # R1 行级竞争：PPE 行 > 纯无形/土地行；A>B；主表>附注/MD&A；早页>晚页
    ab = [h for h in pool if h["grade"] in ("A", "B")]
    if not ab:
        result["status"] = "low_confidence"
        result["ms"] = round((time.perf_counter() - t0) * 1000, 1)
        return result
    ab.sort(key=lambda h: (h["ppe_rank"], 0 if h["grade"] == "A" else 1, 0 if h["cf_page"] else 1, h["page"]))
    best = ab[0]

    if best["multiplier"] is None:
        result["status"] = "unit_unknown"
        result.update({k: best[k] for k in ("label_raw", "unit_label", "page", "grade")})
        result["ms"] = round((time.perf_counter() - t0) * 1000, 1)
        return result

    cur_raw = best["values"][0]
    prior_raw = best.get("prior")
    if prior_raw is None and len(best["values"]) > 1:
        prior_raw = best["values"][1]
    value = cur_raw * best["multiplier"]
    result.update({
        "status": "ok" if result["status"] != "period_unverified" else "period_unverified",
        "value": value,
        "value_raw": cur_raw,
        "value_abs": abs(value),
        "prior_raw": prior_raw,
        "sign_convention": "negative_outflow" if cur_raw < 0 else "positive_outflow",
        "multiplier": best["multiplier"],
        "currency": best["unit_label"].split("_")[0] if best["unit_label"] else None,
        "unit_label": best["unit_label"],
        "page": best["page"],
        "label_raw": best["label_raw"],
        "alias": best["alias"],
        "weak": best["weak"],
        "grade": best["grade"],
        "restated": best["restated"],
        "period_end": best["period_end"].isoformat() if best["period_end"] else None,
        "period_months": best["period_months"],
        "evidence": [best["raw_line"]],
        "magnitude_jump": bool(
            prior_raw not in (None, 0) and abs(cur_raw / prior_raw) > 5
        ),
    })
    result["ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return result
