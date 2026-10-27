"""US 产品证券目录：既定同步范围内的 stock_info 补齐与只读预览。"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config
from db import Connection

logger = logging.getLogger(__name__)

CODE_RE = re.compile(r"[A-Z0-9-]{1,20}")
CIK_RE = re.compile(r"[0-9]{10}")
CACHE_CIK_RE = re.compile(r'"cik"\s*:\s*"?([0-9]+)"?')
DEFAULT_EVIDENCE_ROOT = Path("build/financial_comparison/us_security_directory")


class DirectorySyncError(RuntimeError):
    pass


def normalize_ticker(value: Any) -> str:
    return str(value or "").strip().upper().replace(".", "-")


def normalize_cik(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text or not text.isdigit():
        return None
    text = text.zfill(10)
    if not CIK_RE.fullmatch(text) or not text.strip("0"):
        return None
    return text


def _rows(cur, sql: str, params=()) -> list[dict]:
    cur.execute(sql, params)
    names = [d[0] for d in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def _source_snapshot(company_rows: list[dict]) -> dict:
    cache = config.DATA_DIR / "sec_cache" / "company_tickers.json"
    try:
        raw = cache.read_bytes()
        stat = cache.stat()
    except OSError as exc:
        raise DirectorySyncError(f"SEC 公司映射缓存不可读: {exc}") from exc
    return {
        "path": str(cache),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "snapshot_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        "row_count": len(company_rows),
    }


def _load_company_mapping() -> tuple[dict[str, list[dict]], dict]:
    from core.fetchers.us_financial import USFinancialFetcher

    frame = USFinancialFetcher().fetch_company_list()
    required = {"cik", "ticker", "title"}
    if frame.empty or not required.issubset(frame.columns):
        raise DirectorySyncError("SEC 公司映射为空或缺少 cik/ticker/title 字段")
    records = frame.to_dict(orient="records")
    grouped: dict[str, list[dict]] = {}
    for item in records:
        raw_ticker = str(item.get("ticker") or "").strip()
        ticker = normalize_ticker(raw_ticker)
        if not ticker:
            continue
        grouped.setdefault(ticker, []).append({
            "raw_ticker": raw_ticker,
            "ticker": ticker,
            "title": str(item.get("title") or "").strip(),
            "cik": normalize_cik(item.get("cik")),
        })
    if not grouped:
        raise DirectorySyncError("SEC 公司映射规范化后为空")
    return grouped, _source_snapshot(records)


def _load_db_state(conn, planned_scope: list[str]) -> dict:
    with conn.cursor() as cur:
        stocks = _rows(cur, """
            SELECT stock_code, stock_name, market, cik, currency, delist_date,
                   list_date, industry, exchange
            FROM stock_info
        """)
        symbols = _rows(cur, """
            SELECT ticker, canonical_stock_code, cik, symbol_role
            FROM us_security_ticker_symbol WHERE market='US'
        """)
        filings = _rows(cur, """
            SELECT DISTINCT stock_code, cik FROM us_filing
            WHERE stock_code = ANY(%s)
        """, (planned_scope,))
        facts = _rows(cur, """
            SELECT DISTINCT stock_code, cik FROM us_financial_fact_version
            WHERE stock_code = ANY(%s)
        """, (planned_scope,))
    return {"stocks": stocks, "symbols": symbols, "filings": filings, "facts": facts}


def _validate_identity_rows() -> None:
    from core.us_security_identity import validate_us_security_symbols

    conflicts = validate_us_security_symbols()
    if conflicts:
        raise DirectorySyncError(f"US 身份表冲突: {conflicts}")


def _cache_evidence(code: str) -> dict:
    path = config.DATA_DIR / "sec_cache" / f"{code}.json"
    if not path.exists():
        return {"path": str(path), "present": False, "cik": None}
    try:
        with path.open() as handle:
            prefix = handle.read(131072)
    except OSError as exc:
        return {"path": str(path), "present": True, "cik": None,
                "error": str(exc)}
    match = CACHE_CIK_RE.search(prefix)
    return {
        "path": str(path), "present": True,
        "cik": normalize_cik(match.group(1)) if match else None,
    }


def _candidate_rows(planned_scope: list[str], raw_index_tickers: set[str],
                    resolved_map: dict[str, str]) -> list[dict]:
    raws_by_code: dict[str, set[str]] = {code: set() for code in planned_scope}
    for raw in sorted(raw_index_tickers):
        normalized = normalize_ticker(raw)
        canonical = resolved_map.get(raw, resolved_map.get(normalized, normalized))
        canonical = normalize_ticker(canonical)
        if canonical in raws_by_code:
            raws_by_code[canonical].add(normalized)
    return [{"stock_code": code,
             "raw_tickers": sorted(raws_by_code[code] or {code})}
            for code in planned_scope]


def _decide(conn, *, planned_scope: list[str], raw_index_tickers: set[str],
            resolved_map: dict[str, str], company_map: dict[str, list[dict]],
            source_error: str | None) -> tuple[list[dict], list[str]]:
    state = _load_db_state(conn, planned_scope)
    _validate_identity_rows()
    stocks = {r["stock_code"]: r for r in state["stocks"]}
    us_stocks = {code: r for code, r in stocks.items() if r["market"] == "US"}
    symbols_by_ticker: dict[str, list[dict]] = {}
    for row in state["symbols"]:
        row = {**row, "ticker": normalize_ticker(row["ticker"]),
               "canonical_stock_code": normalize_ticker(row["canonical_stock_code"]),
               "cik": normalize_cik(row["cik"])}
        symbols_by_ticker.setdefault(row["ticker"], []).append(row)
    history: dict[str, set[str]] = {}
    for key in ("filings", "facts"):
        for row in state[key]:
            cik = normalize_cik(row["cik"])
            if cik:
                history.setdefault(row["stock_code"], set()).add(cik)

    candidates = _candidate_rows(planned_scope, raw_index_tickers, resolved_map)
    new_cik_owners: dict[str, list[str]] = {}
    for candidate in candidates:
        code = candidate["stock_code"]
        if code in us_stocks:
            continue
        raw = candidate["raw_tickers"][0]
        symbol = symbols_by_ticker.get(raw, [])
        sec_rows = company_map.get(raw, [])
        cik = symbol[0]["cik"] if symbol else (
            sec_rows[0]["cik"] if len(sec_rows) == 1 else None)
        if cik:
            new_cik_owners.setdefault(cik, []).append(code)

    items: list[dict] = []
    warnings: list[str] = []
    today = datetime.now(timezone.utc).date()
    for candidate in candidates:
        code = candidate["stock_code"]
        raws = candidate["raw_tickers"]
        raw = raws[0]
        info = stocks.get(code)
        us_info = us_stocks.get(code)
        symbol_rows = symbols_by_ticker.get(raw, [])
        sec_rows = company_map.get(raw, [])
        sec = sec_rows[0] if len(sec_rows) == 1 else None
        historical_ciks = history.get(code, set())
        expected_cik = (
            normalize_cik(us_info["cik"]) if us_info and us_info["cik"] else None)
        if expected_cik is None and symbol_rows:
            expected_cik = symbol_rows[0]["cik"]
        if expected_cik is None and sec:
            expected_cik = sec["cik"]
        reasons: list[str] = []
        notes: list[str] = []
        action = "existing" if us_info else "insert"

        if not CODE_RE.fullmatch(code):
            reasons.append("canonical code 非法")
        if info is not None and info["market"] != "US":
            reasons.append(f"stock_code 已被 {info['market']} 市场占用")
        if us_info and us_info["delist_date"] and us_info["delist_date"] <= today:
            reasons.append(f"已退市证券不自动复活: {us_info['delist_date']}")
        if len(sec_rows) > 1:
            reasons.append(f"SEC 规范化 ticker 碰撞: {raws} → {len(sec_rows)} 条")
        if not us_info:
            if source_error:
                reasons.append(f"SEC 公司映射不可用: {source_error}")
            elif not sec:
                reasons.append(f"SEC 公司映射缺少唯一 ticker: {raw}")
            if sec and not sec["title"]:
                reasons.append("SEC 公司名称为空")
            elif sec and len(sec["title"]) > 100:
                reasons.append("SEC 公司名称超过 stock_name 长度 100")
            if expected_cik is None:
                reasons.append("无法确认有效非零十位 CIK")
            if expected_cik and len(new_cik_owners.get(expected_cik, [])) > 1:
                reasons.append(f"本批多个新代码共用 CIK {expected_cik}: "
                               f"{new_cik_owners[expected_cik]}")
            if expected_cik:
                occupants = sorted(c for c, row in us_stocks.items()
                                   if c != code and normalize_cik(row["cik"]) == expected_cik)
                if occupants:
                    reasons.append(f"CIK {expected_cik} 已被 {occupants} 使用")
        elif expected_cik is None:
            reasons.append("已有 US 行但无法确认 CIK")
        elif us_info["cik"] is None:
            notes.append("已有行 cik 为空；本片不回填，使用已核对身份采集")

        if expected_cik and historical_ciks and historical_ciks != {expected_cik}:
            reasons.append(f"历史 filing/companyfacts CIK {sorted(historical_ciks)} "
                           f"与候选 CIK {expected_cik} 冲突")
        for symbol in symbol_rows:
            if symbol["canonical_stock_code"] != code:
                reasons.append(f"身份映射 {raw} 指向 {symbol['canonical_stock_code']} 而非 {code}")
            if expected_cik and symbol["cik"] != expected_cik:
                reasons.append(f"身份映射 CIK {symbol['cik']} 与候选 {expected_cik} 冲突")
        cache = _cache_evidence(code) if not us_info else {"present": False, "path": None,
                                                            "cik": None}
        if cache.get("error"):
            reasons.append(f"本地 companyfacts 缓存不可读: {cache['error']}")
        elif cache.get("present") and cache.get("cik") and expected_cik \
                and cache["cik"] != expected_cik:
            reasons.append(f"本地 companyfacts CIK {cache['cik']} 与候选 {expected_cik} 冲突")
        elif cache.get("present") and cache.get("cik") is None:
            notes.append("本地 companyfacts 缓存未能交叉核实 CIK")

        if reasons:
            action = "rejected"
        name = (us_info["stock_name"] if us_info else (sec or {}).get("title"))
        item = {
            "raw_ticker": raw,
            "raw_tickers": raws,
            "canonical_code": code,
            "stock_name": name,
            "cik": expected_cik,
            "action": action,
            "decision": "rejected" if reasons else ("existing" if us_info else "approved"),
            "reasons": reasons,
            "notes": notes,
            "evidence": {
                "sec_ticker": raw,
                "stock_info": "existing" if us_info else "missing",
                "history_ciks": sorted(historical_ciks),
                "companyfacts_cache": cache,
            },
            "insert_result": "not_attempted",
        }
        items.append(item)
        for note in notes:
            warnings.append(f"{code}: {note}")
    return items, warnings


def _summarize(result: dict) -> dict:
    items = result["items"]
    rejected = [item for item in items if item["decision"] == "rejected"]
    inserted = [item["canonical_code"] for item in items
                if item["insert_result"] == "inserted"]
    existing = [item["canonical_code"] for item in items
                if item["decision"] == "existing"]
    collectable = [] if result.get("fatal_error") or result.get("transaction_error") else [
        item["canonical_code"] for item in items
        if item["decision"] in ("approved", "existing")]
    blocked = sorted({ticker for item in rejected
                      for ticker in [item["canonical_code"], *item["raw_tickers"]]})
    result.update({
        "inserted": inserted,
        "existing": existing,
        "rejected": rejected,
        "collectable_scope": collectable,
        "blocked_tickers": blocked,
        "complete": not result.get("source_error") and not result.get("transaction_error"),
    })
    if result.get("fatal_error") or result.get("transaction_error"):
        result["status"] = "failed"
    elif result.get("source_error"):
        result["status"] = "failed"
    elif rejected:
        result["status"] = "partial"
    else:
        result["status"] = "success"
    return result


def _write_evidence(result: dict, root: Path) -> Path:
    run_dir = root / result["run_id"]
    run_dir.mkdir(parents=True, exist_ok=False)
    path = run_dir / "summary.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n")
    return path


def sync_us_security_directory(*, planned_scope: list[str],
                               raw_index_tickers: set[str],
                               resolved_map: dict[str, str],
                               index_sources: dict | None = None,
                               apply: bool = False,
                               run_id: str | None = None,
                               evidence_root: Path | None = None) -> dict:
    planned_scope = sorted({normalize_ticker(code) for code in planned_scope})
    if not planned_scope:
        raise DirectorySyncError("目录计划范围为空")
    run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    run_id = f"{run_id}_{uuid.uuid4().hex[:8]}"
    result: dict[str, Any] = {
        "run_id": run_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "mode": "apply" if apply else "preview",
        "planned_scope": planned_scope,
        "raw_index_tickers": sorted(raw_index_tickers),
        "index_sources": index_sources or {},
        "items": [], "warnings": [], "source_error": None,
        "transaction_error": None, "fatal_error": None,
    }
    try:
        company_map, snapshot = _load_company_mapping()
        result["sec_snapshot"] = snapshot
    except Exception as exc:
        logger.error("US 目录 SEC 公司映射加载失败: %s", exc)
        company_map = {}
        result["sec_snapshot"] = None
        result["source_error"] = f"{type(exc).__name__}: {exc}"

    try:
        with Connection() as conn:
            items, warnings = _decide(
                conn, planned_scope=planned_scope,
                raw_index_tickers=raw_index_tickers, resolved_map=resolved_map,
                company_map=company_map, source_error=result["source_error"])
            result["items"] = items
            result["warnings"] = warnings
        if apply:
            with Connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SET LOCAL lock_timeout = '5s'")
                    cur.execute("LOCK TABLE stock_info IN SHARE ROW EXCLUSIVE MODE")
                    cur.execute("LOCK TABLE us_security_ticker_symbol IN SHARE ROW EXCLUSIVE MODE")
                items, warnings = _decide(
                    conn, planned_scope=planned_scope,
                    raw_index_tickers=raw_index_tickers, resolved_map=resolved_map,
                    company_map=company_map, source_error=result["source_error"])
                result["items"] = items
                result["warnings"] = warnings
                with conn.cursor() as cur:
                    for item in items:
                        if item["decision"] != "approved":
                            continue
                        cur.execute("""
                            INSERT INTO stock_info
                                (stock_code, stock_name, market, cik, currency, updated_at)
                            VALUES (%s, %s, 'US', %s, NULL, NOW())
                        """, (item["canonical_code"], item["stock_name"], item["cik"]))
                        item["insert_result"] = "inserted"
                conn.commit()
        else:
            for item in result["items"]:
                if item["decision"] == "approved":
                    item["insert_result"] = "preview_not_applied"
    except Exception as exc:
        logger.exception("US 目录判定或写入失败")
        result["transaction_error"] = f"{type(exc).__name__}: {exc}"
        for item in result["items"]:
            if item["insert_result"] == "inserted":
                item["insert_result"] = "rolled_back"
            if apply and item["decision"] == "approved":
                item["action"] = "rejected"
                item["decision"] = "rejected"
                item["reasons"].append("事务内身份复核或写入未完成，预检批准失效")

    result = _summarize(result)
    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    root = evidence_root or DEFAULT_EVIDENCE_ROOT
    try:
        result["evidence_ref"] = str(_write_evidence(result, root))
    except Exception as exc:
        raise DirectorySyncError(f"目录证据写入失败: {exc}") from exc
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="US 证券目录预览（默认不写业务库）")
    parser.add_argument("--apply", action="store_true",
                        help="显式写入通过复核的新 stock_info 行")
    args = parser.parse_args()
    from core.scheduler import build_us_sync_scope

    built = build_us_sync_scope()
    if built["scope_result"].get("error"):
        raise SystemExit(f"无法构造 US 同步范围: {built['scope_result']}")
    scope_result = built["scope_result"]
    result = sync_us_security_directory(
        planned_scope=built["scope"],
        raw_index_tickers=built["raw_index"],
        resolved_map=built["resolved_map"],
        index_sources=scope_result.get("index_sources"),
        apply=args.apply,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if result["status"] != "success":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
