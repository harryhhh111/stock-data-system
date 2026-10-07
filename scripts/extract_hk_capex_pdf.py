"""T2 港股 capex 披露易 PDF 解析回填（方案 docs/core/HK_CAPEX_PDF_EXTRACTION_PLAN.md）。

编排：DB 查「最新期 capex 为 NULL」的 CN_HK 股票（排除 8 开头人民币柜台）
→ 披露易升级链取公告 PDF（中期業績→末期業績→中期報告→英文版重查）
→ core.parsers.hk_capex_pdf.extract_capex 抽取
→ 仅当 grade A/B 且校验通过（status=ok）且 DB 仍为 NULL 才 upsert（只写 capex 列）
→ 审计 data/hk_capex_pdf_audit.jsonl（含 PDF URL/页码/行名原文/单位/币种/重列/grade/evidence，
   兼作回滚清单）。PDF 下载到 /tmp，用完即删。

Usage:
    python scripts/extract_hk_capex_pdf.py --dry-run                # 试点模式：只抽取+审计，不写库
    python scripts/extract_hk_capex_pdf.py --codes 02020,02331 --dry-run
    python scripts/extract_hk_capex_pdf.py --limit 20 --dry-run
    python scripts/extract_hk_capex_pdf.py                          # 全量（tmux 内跑，夜间）
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.fetchers.hkex_news import HkexNewsError, HkexNewsFetcher
from core.parsers.hk_capex_pdf import extract_capex
from db import Connection, upsert

logger = logging.getLogger(__name__)

AUDIT_PATH = Path(__file__).resolve().parent.parent / "data" / "hk_capex_pdf_audit.jsonl"
TMP_DIR = Path("/tmp/hk_capex_pdf")

# 人工队列状态：不自动写库，审计记录后由人工复核
REVIEW_STATUSES = {"period_unverified", "unit_unknown"}
# 可升级状态：本级未命中，继续升级链
ESCALATE_STATUSES = {"no_cf_page", "no_capex_row", "period_mismatch", "low_confidence"}
# 每级最多尝试的公告份数（最新 N 份，应对目标期与最新公告错位）
ANN_PER_STEP = 3


def _is_rmb_counter(stock_code: str) -> bool:
    return stock_code.strip().startswith("8")


def _load_targets(codes_arg: str | None, limit: int | None) -> list[dict]:
    """最新期 capex 为 NULL 的 CN_HK 股票 + 其最新报告期。"""
    with Connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH latest AS (
                  SELECT DISTINCT ON (c.stock_code)
                    c.stock_code, c.report_date, c.report_type, c.capex
                  FROM cash_flow_statement c
                  JOIN stock_info s ON s.stock_code = c.stock_code AND s.market = 'CN_HK'
                  WHERE c.stock_code NOT LIKE '8%%'
                  ORDER BY c.stock_code, c.report_date DESC, c.report_type DESC
                )
                SELECT l.stock_code, s.stock_name, l.report_date, l.report_type
                FROM latest l JOIN stock_info s ON s.stock_code = l.stock_code
                WHERE l.capex IS NULL
                ORDER BY l.stock_code
                """
            )
            rows = [
                {"stock_code": r[0], "stock_name": r[1], "report_date": r[2], "report_type": r[3]}
                for r in cur.fetchall()
            ]
    if codes_arg:
        wanted = {c.strip() for c in codes_arg.split(",") if c.strip()}
        rows = [r for r in rows if r["stock_code"] in wanted]
        missing = wanted - {r["stock_code"] for r in rows}
        for c in sorted(missing):
            logger.warning("--codes %s 不在最新期 capex NULL 列表内（可能已回填或无此行），跳过", c)
    if limit is not None:
        rows = rows[:limit]
    return rows


def _append_audit(entry: dict) -> None:
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(AUDIT_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


def _select_capex(conn, stock_code: str, report_date, report_type: str):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT capex FROM cash_flow_statement "
            "WHERE stock_code = %s AND report_date = %s AND report_type = %s",
            (stock_code, report_date, report_type),
        )
        row = cur.fetchone()
    if row is None:
        return False, None
    return True, row[0]


def process_stock(
    fetcher: HkexNewsFetcher,
    target: dict,
    tmp_dir: Path = TMP_DIR,
) -> dict:
    """单只股票走完整升级链。返回审计条目（不写库，调用方决定）。"""
    code = target["stock_code"]
    report_date = target["report_date"]
    report_type = target["report_type"]
    months = {"semi": 6, "annual": 12, "quarterly": 3}.get(report_type)
    audit: dict = {
        "stock_code": code,
        "stock_name": target.get("stock_name"),
        "report_date": str(report_date),
        "report_type": report_type,
        "target_months": months,
        "action": "extract",
        "status": None,
        "escalation_level": None,
        "pdf_url": None,
        "pdf_title": None,
        "page": None,
        "label_raw": None,
        "unit_label": None,
        "multiplier": None,
        "currency": None,
        "restated": None,
        "grade": None,
        "value_raw": None,
        "capex": None,
        "sign_convention": None,
        "evidence": [],
        "ms": None,
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    stock_id = fetcher.get_stock_id(code)
    search_from = (report_date - timedelta(days=45)) if isinstance(report_date, date) else None
    search_to = date.today()

    steps = fetcher.escalation_steps(months, "zh") + fetcher.escalation_steps(months, "en")
    last_status, last_detail = None, None
    for level, step in enumerate(steps, start=1):
        try:
            anns = fetcher.search_announcements(
                code, stock_id, step["t1code"], step["t2code"], step["lang"],
                from_date=search_from, to_date=search_to,
            )
        except Exception as exc:
            last_status, last_detail = "search_failed", f"{step['label']}/{step['lang']}: {exc}"
            logger.error("检索失败 stock=%s level=%d %s: %s", code, level, step, exc)
            continue
        if not anns:
            last_status, last_detail = "no_announcement", f"{step['label']}/{step['lang']}"
            continue
        for ann in anns[:ANN_PER_STEP]:
            pdf_path = None
            try:
                fname = f"{code}_{step['t2code']}{'_e' if step['lang'] == 'en' else ''}_{Path(ann['url']).name}"
                pdf_path = fetcher.download_pdf(ann["url"], tmp_dir / fname)
                result = extract_capex(pdf_path, target_period=report_date, target_months=months)
            except HkexNewsError as exc:
                if "file_too_large" in str(exc):
                    last_status, last_detail = "skipped_large_pdf", f"{ann['url']} ({exc})"
                    logger.warning("大文件跳过 stock=%s: %s", code, exc)
                    continue
                last_status, last_detail = "download_failed", str(exc)
                logger.error("下载失败 stock=%s url=%s: %s", code, ann["url"], exc)
                continue
            except Exception as exc:
                last_status, last_detail = "extract_failed", f"{ann['url']}: {exc}"
                logger.error("解析异常 stock=%s url=%s: %s", code, ann["url"], exc)
                continue
            finally:
                if pdf_path:
                    Path(pdf_path).unlink(missing_ok=True)  # PDF 用完即删

            last_status, last_detail = result["status"], f"{step['label']}/{step['lang']}"
            if result["status"] == "ok" and result.get("grade") in ("A", "B"):
                audit.update({
                    "status": "ok",
                    "escalation_level": level,
                    "pdf_url": ann["url"],
                    "pdf_title": ann["title"],
                    "page": result.get("page"),
                    "label_raw": result.get("label_raw"),
                    "unit_label": result.get("unit_label"),
                    "multiplier": result.get("multiplier"),
                    "currency": result.get("currency"),
                    "restated": result.get("restated"),
                    "grade": result.get("grade"),
                    "value_raw": result.get("value_raw"),
                    "capex": result.get("value_abs"),
                    "sign_convention": result.get("sign_convention"),
                    "evidence": result.get("evidence", []),
                    "ms": result.get("ms"),
                })
                if result.get("magnitude_jump"):
                    audit["status"] = "review_magnitude_jump"  # R8：>5x 进人工队列
                if result.get("restated"):
                    audit["restated"] = True
                return audit
            if result["status"] in REVIEW_STATUSES:
                audit.update({
                    "status": result["status"],
                    "escalation_level": level,
                    "pdf_url": ann["url"],
                    "page": result.get("page"),
                    "label_raw": result.get("label_raw"),
                    "evidence": result.get("evidence", []),
                })
                return audit
            # ESCALATE_STATUSES：继续下一份公告/下一级

    audit["status"] = last_status or "exhausted"
    audit["detail"] = last_detail
    return audit


def run(targets: list[dict], dry_run: bool) -> dict:
    fetcher = HkexNewsFetcher()
    stats = {
        "stocks_total": len(targets),
        "extract_ok": 0,
        "written": 0,
        "review_queue": 0,
        "escalate_exhausted": 0,
        "failed": 0,
    }
    for i, target in enumerate(targets):
        code = target["stock_code"]
        try:
            audit = process_stock(fetcher, target)
        except Exception as exc:
            # 禁止静默失败：记录上下文后继续下一只
            stats["failed"] += 1
            logger.error("处理失败 stock=%s (%d/%d): %s", code, i + 1, len(targets), exc)
            _append_audit({
                "stock_code": code, "report_date": str(target["report_date"]),
                "report_type": target["report_type"], "action": "extract",
                "status": "failed", "detail": str(exc),
                "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            continue

        status = audit["status"]
        if status == "ok":
            stats["extract_ok"] += 1
            with Connection() as conn:
                exists, old = _select_capex(
                    conn, code, target["report_date"], target["report_type"]
                )
            if not exists:
                audit["status"] = "skipped_no_row"
                logger.info("DB 无此行跳过: %s %s %s", code, target["report_date"], target["report_type"])
            elif old is not None:
                audit["status"] = "skipped_non_null"
                logger.info("已有值跳过: %s old=%s new=%s", code, old, audit["capex"])
            elif dry_run:
                audit["action"] = "dry_run"
                logger.info(
                    "[dry-run] 将写入: %s %s %s capex=%s (%s %s, p%s, L%s, %sms)",
                    code, target["report_date"], target["report_type"], audit["capex"],
                    audit["value_raw"], audit["unit_label"], audit["page"],
                    audit["escalation_level"], audit["ms"],
                )
            else:
                upsert(
                    "cash_flow_statement",
                    [{
                        "stock_code": code,
                        "report_date": target["report_date"],
                        "report_type": target["report_type"],
                        "capex": audit["capex"],
                    }],
                    ["stock_code", "report_date", "report_type"],
                )
                audit["action"] = "upsert"
                stats["written"] += 1
                logger.info(
                    "已回填: %s %s %s capex=%s (p%s, L%s)",
                    code, target["report_date"], target["report_type"],
                    audit["capex"], audit["page"], audit["escalation_level"],
                )
        elif status in REVIEW_STATUSES or status == "review_magnitude_jump":
            stats["review_queue"] += 1
            logger.info("进人工队列: %s status=%s", code, status)
        else:
            stats["escalate_exhausted"] += 1
            logger.info("升级链耗尽: %s status=%s detail=%s", code, status, audit.get("detail"))

        _append_audit(audit)
        if (i + 1) % 50 == 0:
            logger.info(
                "进度 %d/%d: ok=%d written=%d review=%d exhausted=%d failed=%d",
                i + 1, len(targets), stats["extract_ok"], stats["written"],
                stats["review_queue"], stats["escalate_exhausted"], stats["failed"],
            )
    return stats


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    parser = argparse.ArgumentParser(description="港股 capex 披露易 PDF 解析回填（T2）")
    parser.add_argument("--dry-run", action="store_true", help="只抽取+审计，不写库")
    parser.add_argument("--codes", type=str, default=None, help="逗号分隔的 5 位港股代码")
    parser.add_argument("--limit", type=int, default=None, help="最多处理多少只")
    args = parser.parse_args()

    t0 = time.time()
    targets = _load_targets(args.codes, args.limit)
    logger.info("待处理 %d 只（最新期 capex NULL）, dry_run=%s", len(targets), args.dry_run)
    if not targets:
        return

    stats = run(targets, args.dry_run)
    logger.info("=== 汇总 ===")
    for k, v in stats.items():
        logger.info("  %s: %s", k, v)
    logger.info("耗时 %.0fs", time.time() - t0)


if __name__ == "__main__":
    main()
