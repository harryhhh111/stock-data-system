"""fetchers/hkex_news.py — 港交所披露易（HKEXnews）公告检索 + PDF 下载。

数据源（未文档化接口，须存原始快照，T2 方案 §2）：
  1. 代码→stockId：GET /search/partial.do?callback=cb&lang=ZH&type=A&name={纯数字}&market=SEHK
     （JSONP 包裹，取 stockId；8 开头人民币柜台跳过——由调用方过滤）
  2. 公告检索：GET /search/titleSearchServlet.do?t1code=10000&t2code={13400 中期業績|13300 末期業績|40200 中期報告}
     （response.result 是**字符串包裹的 JSON 数组**，需二次 json.loads；
     DATE_TIME 为 dd/mm/yyyy HH:MM，必须解析成日期排序；FILE_LINK 拼前缀）
  3. 下载：PDF URL 长期有效；请求间隔 ≥3s；>20MB 跳过并记录；境外带宽慢，超时 ≥120s。

升级链（escalation，T2 方案 §3）：
  中期業績(13400) → 末期業績(13300) → 中期報告(40000/40200) → lang=EN 重查以上全部。
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator, Optional
from urllib.parse import urljoin

import requests

from .base import AdaptiveRateLimiter, BaseFetcher, retry_with_backoff

logger = logging.getLogger(__name__)

_BASE = "https://www1.hkexnews.hk"
_PARTIAL_URL = f"{_BASE}/search/partial.do"
_SEARCH_URL = f"{_BASE}/search/titleSearchServlet.do"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www1.hkexnews.hk/index_c.htm",
}

# 披露易保守限流：请求间隔 ≥3s（调研实测 15 请求无 429；方案 §2 要求 ≥3s）
_hkex_rate_limiter = AdaptiveRateLimiter(base_delay=3.0, max_delay=20.0)

# 大文件跳过阈值（境外 ~230KB/s，>20MB 夜间全量时再补，试点直接跳过并记录）
MAX_PDF_BYTES = 20 * 1024 * 1024

# t2code 常量（公告类别）
T2_INTERIM_RESULTS = "13400"   # 中期業績
T2_FINAL_RESULTS = "13300"     # 末期業績
T2_INTERIM_REPORT = "40200"    # 中期報告（t1code 用 40000）
T2_ANNUAL_REPORT = "40100"     # 年報（t1code 用 40000，best-effort）
_T1_RESULTS = "10000"
_T1_REPORTS = "40000"


class HkexNewsError(RuntimeError):
    """披露易接口错误（业务级失败，带上下文）。"""


class HkexNewsFetcher(BaseFetcher):
    """披露易公告拉取器（T2 capex PDF 数据源）。"""

    source_name = "hkexnews"

    def __init__(self) -> None:
        super().__init__()
        self._stock_id_cache: dict[str, int] = {}

    # ---- 代码 → stockId ----
    @retry_with_backoff
    def get_stock_id(self, stock_code: str) -> int:
        """港股代码（5 位数字）→ 披露易 stockId。进程内缓存。"""
        code = stock_code.strip()
        if code in self._stock_id_cache:
            return self._stock_id_cache[code]
        params = {
            "callback": "cb", "lang": "ZH", "type": "A",
            "name": code, "market": "SEHK",
        }
        logger.info("披露易代码查询: stock=%s", code)
        _hkex_rate_limiter.wait()
        try:
            resp = requests.get(_PARTIAL_URL, params=params, headers=_HEADERS, timeout=20)
            resp.raise_for_status()
        except Exception:
            _hkex_rate_limiter.record_failure()
            raise
        _hkex_rate_limiter.record_success()

        m = re.search(r"\w+\((.*)\)\s*;?\s*$", resp.text, re.S)
        if not m:
            raise HkexNewsError(f"披露易 partial.do 响应非 JSONP: stock={code} body={resp.text[:120]!r}")
        payload = json.loads(m.group(1))
        stock_id = None
        for info in payload.get("stockInfo", []):
            if info.get("code") == code:
                stock_id = info.get("stockId")
                break
        if stock_id is None:
            raise HkexNewsError(f"披露易无 stockId: stock={code} payload={payload!r:.200}")
        self._stock_id_cache[code] = int(stock_id)
        logger.info("披露易 stockId: stock=%s id=%s", code, stock_id)
        return int(stock_id)

    # ---- 公告检索 ----
    @retry_with_backoff
    def search_announcements(
        self,
        stock_code: str,
        stock_id: int,
        t1code: str,
        t2code: str,
        lang: str = "zh",
        from_date: Optional[date | str] = None,
        to_date: Optional[date | str] = None,
    ) -> list[dict[str, Any]]:
        """检索公告列表，按日期倒序。元数据存 raw_snapshot（PDF 本体不进库）。

        Returns:
            [{title, url, date, datetime, lang, t1code, t2code, file_info}, ...]
        """
        def _fmt(d: Optional[date | str]) -> str:
            if d is None:
                return ""
            if isinstance(d, date):
                return d.strftime("%Y%m%d")
            return d

        params = {
            "sortDir": "0", "sortByOptions": "DateTime", "category": "0",
            "market": "SEHK", "stockId": stock_id, "documentType": "-1",
            "fromDate": _fmt(from_date), "toDate": _fmt(to_date),
            "title": "", "searchType": "1",
            "t1code": t1code, "t2code": t2code,
            "rowRange": "50", "lang": lang,
        }
        logger.info("披露易公告检索: stock=%s t1=%s t2=%s lang=%s", stock_code, t1code, t2code, lang)
        _hkex_rate_limiter.wait()
        try:
            resp = requests.get(_SEARCH_URL, params=params, headers=_HEADERS, timeout=30)
            resp.raise_for_status()
        except Exception:
            _hkex_rate_limiter.record_failure()
            raise
        _hkex_rate_limiter.record_success()

        payload = resp.json()
        raw = payload.get("result", "")
        try:
            items = json.loads(raw) if isinstance(raw, str) and raw.strip() else (raw or [])
        except json.JSONDecodeError as exc:
            raise HkexNewsError(
                f"披露易 result 二次解析失败: stock={stock_code} t2={t2code} err={exc} raw={raw[:120]!r}"
            )

        out = []
        for it in items:
            dt_str = it.get("DATE_TIME", "")
            try:
                dt = datetime.strptime(dt_str.strip(), "%d/%m/%Y %H:%M")
            except ValueError:
                dt = None  # 不静默：保留 None，排序落到末尾
            link = it.get("FILE_LINK", "")
            out.append({
                "title": it.get("TITLE", ""),
                "url": urljoin(_BASE, link) if link else "",
                "date": dt.date().isoformat() if dt else None,
                "datetime": dt_str,
                "lang": lang,
                "t1code": t1code,
                "t2code": t2code,
                "file_info": it.get("FILE_INFO", ""),
            })
        out.sort(key=lambda x: (x["date"] or "0000-00-00"), reverse=True)

        # 快照：元数据（FILE_LINK/TITLE/DATE_TIME/FILE_INFO），PDF 本体不进库
        prev_skip = self.skip_snapshot
        self.skip_snapshot = False
        try:
            self.save_raw_snapshot(
                stock_code=stock_code,
                data_type="announcement",
                source="hkexnews",
                api_params={"stock_id": stock_id, "t1code": t1code, "t2code": t2code,
                            "lang": lang, "from_date": params["fromDate"], "to_date": params["toDate"]},
                raw_data=out,
            )
        finally:
            self.skip_snapshot = prev_skip

        logger.info("披露易公告检索完成: stock=%s t2=%s 命中 %d 条", stock_code, t2code, len(out))
        return out

    # ---- PDF 下载 ----
    @retry_with_backoff
    def download_pdf(self, url: str, dest: str | Path) -> Path:
        """下载公告 PDF 到 dest。>20MB 抛 HkexNewsError('file_too_large')。"""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        _hkex_rate_limiter.wait()
        t0 = time.time()
        try:
            with requests.get(url, headers=_HEADERS, timeout=(15, 180), stream=True) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("Content-Length") or 0)
                if total > MAX_PDF_BYTES:
                    raise HkexNewsError(f"file_too_large: {total/1e6:.1f}MB > {MAX_PDF_BYTES/1e6:.0f}MB url={url}")
                with open(dest, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=1 << 16):
                        f.write(chunk)
        except HkexNewsError:
            raise
        except Exception:
            _hkex_rate_limiter.record_failure()
            raise
        _hkex_rate_limiter.record_success()
        size = dest.stat().st_size
        if size > MAX_PDF_BYTES:
            dest.unlink(missing_ok=True)
            raise HkexNewsError(f"file_too_large: {size/1e6:.1f}MB > {MAX_PDF_BYTES/1e6:.0f}MB url={url}")
        if size < 10_000:
            dest.unlink(missing_ok=True)
            raise HkexNewsError(f"pdf_too_small: {size}B url={url}（可能非 PDF 或下载截断）")
        logger.info("PDF 下载完成: %s %.2fMB 耗 %.0fs", url, size / 1e6, time.time() - t0)
        return dest

    # ---- 升级链 ----
    @staticmethod
    def escalation_steps(target_months: Optional[int], lang: str = "zh") -> list[dict[str, str]]:
        """按目标期间生成升级链（先浅后深，调用方逐级尝试）。

        中报(6)：中期業績 → 末期業績 → 中期報告；
        年报(12)：末期業績 → 年報；
        其他：中期業績 → 末期業績。
        lang='en' 时同链重查（英文版 FILE_LINK 为 _e.pdf）。
        """
        if target_months == 12:
            zh = [
                {"t1code": _T1_RESULTS, "t2code": T2_FINAL_RESULTS, "label": "末期業績"},
                {"t1code": _T1_REPORTS, "t2code": T2_ANNUAL_REPORT, "label": "年報"},
            ]
        elif target_months == 6:
            zh = [
                {"t1code": _T1_RESULTS, "t2code": T2_INTERIM_RESULTS, "label": "中期業績"},
                {"t1code": _T1_RESULTS, "t2code": T2_FINAL_RESULTS, "label": "末期業績"},
                {"t1code": _T1_REPORTS, "t2code": T2_INTERIM_REPORT, "label": "中期報告"},
            ]
        else:
            zh = [
                {"t1code": _T1_RESULTS, "t2code": T2_INTERIM_RESULTS, "label": "中期業績"},
                {"t1code": _T1_RESULTS, "t2code": T2_FINAL_RESULTS, "label": "末期業績"},
            ]
        return [{**s, "lang": lang} for s in zh]


def fetch_stock_id(stock_code: str) -> int:
    return HkexNewsFetcher().get_stock_id(stock_code)
