"""core/us_financial_versioning.py — 美股财报不可变版本层共享写入器。

USFactVersionWriter 封装 fact_version、conflict、staging、fact_source 的写入逻辑，
供在线 ingest（core/fetchers/us_financial.py）与 Phase 2 历史回填共用，避免规则分叉。
"""
from __future__ import annotations

import hashlib
import json
import logging
from decimal import Decimal
from typing import Any

import psycopg2.extras

logger = logging.getLogger(__name__)


# ── 纯函数工具 ─────────────────────────────────────────────


def split_value(val: Any) -> tuple[Decimal | None, str | None]:
    """把 SEC value 拆成精确 NUMERIC 或 TEXT，不损失精度。"""
    if val is None:
        return None, None
    try:
        return Decimal(str(val)), None
    except Exception:
        return None, str(val)


def compute_value_hash(value: Any, unit: str) -> str:
    """由 value + unit 生成稳定 value_hash。"""
    canonical = json.dumps(
        {"value": value, "unit": unit},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_context_hash(
    period_kind: str,
    period_start: str | None,
    report_date: str,
    frame: str | None,
    fp: str | None,
    dimensions: dict,
) -> str:
    """由 period、frame、fp、dimensions 生成稳定 context_hash。"""
    canonical = json.dumps(
        {
            "period_kind": period_kind,
            "period_start": period_start,
            "report_date": report_date,
            "frame": frame,
            "fp": fp,
            "dimensions": dimensions,
        },
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def fact_key(row: dict) -> tuple:
    """由已重命名为 DB 列名的事实行生成唯一键元组。"""
    return (
        row["stock_code"],
        row["accession_no"],
        row["taxonomy"],
        row["sec_tag"],
        row["period_kind"],
        row["report_date"],
        row["context_hash"],
        row["unit"],
    )


def reject_reason(rec: dict) -> str | None:
    """判断一条有效 period 的 fact 是否因缺少关键元数据进 staging。"""
    if not str(rec.get("accn") or "").strip():
        return "MISSING_ACCESSION"
    if not rec.get("end"):
        return "MISSING_REPORT_DATE"
    if not rec.get("filed"):
        return "MISSING_FILED_DATE"
    if rec.get("val") is None:
        return "MISSING_VALUE"
    return None


# ── 分类与 filing 推断 ─────────────────────────────────────


ACCEPTED_FORMS = {
    "10-K", "10-K/A",
    "10-Q", "10-Q/A", "10-QT", "10-QT/A",
    "20-F", "20-F/A",
    "40-F", "40-F/A",
}

ACCEPTED_FP: set[str] = {"FY", "Q1", "Q2", "Q3", "Q4", "H1", "H2"}
ACCEPTED_FP.update({f"M{i}" for i in range(1, 13)})

ANNUAL_FORMS = {"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"}
QUARTERLY_FORMS = {"10-Q", "10-Q/A"}


def classify_record(rec: dict) -> tuple[str, str | None]:
    """对有效 period 的事实做 form/fp/period_kind 允许矩阵分类。

    Returns:
        (decision, flag)，decision 取值：
        - ACCEPT：直接进入正式 fact_version
        - ACCEPT_WITH_FLAG：进入正式表，但附加质量标记
        - STAGING_UNKNOWN_FORM_FP：未知 form/fp，进 staging
        - STAGING_UNKNOWN_PERIOD_KIND：period_kind 异常，进 staging
    """
    period_kind = rec.get("_period_kind")
    if period_kind not in ("instant", "duration"):
        return "STAGING_UNKNOWN_PERIOD_KIND", None

    form = str(rec.get("form") or "").strip().upper()
    fp_raw = str(rec.get("fp") or "").strip().upper() or None

    if form not in ACCEPTED_FORMS:
        return "STAGING_UNKNOWN_FORM_FP", None

    if fp_raw is None:
        return "STAGING_UNKNOWN_FORM_FP", None
    if fp_raw not in ACCEPTED_FP:
        return "STAGING_UNKNOWN_FORM_FP", None

    if form in ANNUAL_FORMS:
        if fp_raw != "FY":
            return "ACCEPT_WITH_FLAG", "FISCAL_PERIOD_MISMATCH_ANNUAL"
    elif form in QUARTERLY_FORMS:
        if fp_raw not in {"Q1", "Q2", "Q3", "Q4"}:
            return "ACCEPT_WITH_FLAG", "FISCAL_PERIOD_MISMATCH_QUARTERLY"

    return "ACCEPT", None


def derive_filing_meta(records: list[dict]) -> dict[str, dict]:
    """从 fact 记录推断 filing-level 当前报告期。"""
    from collections import Counter

    by_accn: dict[str, list[dict]] = {}
    for rec in records:
        accn = str(rec.get("accn") or "").strip()
        if not accn:
            continue
        by_accn.setdefault(accn, []).append(rec)

    meta: dict[str, dict] = {}
    for accn, recs in by_accn.items():
        forms = Counter(str(r.get("form") or "").strip() for r in recs if r.get("form"))
        form = forms.most_common(1)[0][0] if forms else ""
        filed = next((r.get("filed") for r in recs if r.get("filed")), None)

        if form.upper() in ANNUAL_FORMS:
            current_fps = {"FY"}
        elif form.upper() in QUARTERLY_FORMS:
            current_fps = {"Q1", "Q2", "Q3", "Q4"}
        else:
            current_fps = set()

        candidates = [r for r in recs if str(r.get("fp") or "").strip() in current_fps]
        if not candidates:
            candidates = recs
            derived = True
        else:
            derived = form.upper() not in ANNUAL_FORMS and form.upper() not in QUARTERLY_FORMS

        current = max(candidates, key=lambda r: r.get("end") or "")
        meta[accn] = {
            "form": form,
            "filed_date": filed,
            "report_date": current.get("end"),
            "fiscal_year": _int_or_none(current.get("fy")),
            "fiscal_period": str(current.get("fp") or "").strip() or None,
            "frame": current.get("frame"),
            "derived": derived,
        }
    return meta


def _int_or_none(val: Any) -> int | None:
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


# ── dedup key 计算 ─────────────────────────────────────────


def compute_conflict_dedup_key(row: dict) -> str:
    """为 conflict 行生成稳定 dedup key。"""
    canonical = json.dumps(
        {
            "stock_code": row.get("stock_code"),
            "accession_no": row.get("accession_no"),
            "taxonomy": row.get("taxonomy"),
            "sec_tag": row.get("sec_tag"),
            "period_kind": row.get("period_kind"),
            "period_start": str(row.get("period_start") or ""),
            "report_date": str(row.get("report_date") or ""),
            "context_hash": row.get("context_hash"),
            "unit": row.get("unit"),
            "existing_value_hash": row.get("existing_value_hash"),
            "new_value_hash": row.get("new_value_hash"),
        },
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_staging_dedup_key(row: dict) -> str:
    """为 staging 行生成稳定 dedup key。"""
    canonical = json.dumps(
        {
            "source_snapshot_id": row.get("source_snapshot_id"),
            "accession_no": row.get("accession_no"),
            "sec_tag": row.get("sec_tag"),
            "period_kind": row.get("period_kind"),
            "period_start": str(row.get("period_start") or ""),
            "report_date": str(row.get("report_date") or ""),
            "context_hash": row.get("context_hash") or "",
            "unit": row.get("unit"),
            "value_hash": compute_value_hash(
                row.get("value_numeric") if row.get("value_numeric") is not None else row.get("value_text"),
                row.get("unit") or "",
            ),
            "reason_code": row.get("reject_reason"),
        },
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ── 共享写入器 ─────────────────────────────────────────────


class USFactVersionWriter:
    """把原始 fact 记录写入不可变版本层，并维护 fact_source 证据关系。"""

    def __init__(self, parser_git_sha: str | None = None) -> None:
        self.parser_git_sha = parser_git_sha

    def write_facts(
        self,
        conn,
        context: Any,
        run_id: int | None,
        fact_records: list[dict],
        invalid_records: list[dict],
        statement: str,
        reconstruction_flag: str | None = None,
        batch_item_id: int | None = None,
        derive_filing_meta_func: Any = None,
    ) -> dict[str, Any]:
        """写入 filing/fact/conflict/staging/fact_source，返回计数字典。

        Args:
            conn: psycopg2 连接对象（在事务中）。
            context: FetchContext 或具有 stock_code/cik/snapshot_id/content_hash 的对象。
            run_id: us_ingest_run.run_id，在线写入时必填；回填可为 None。
            fact_records: 已分类的有效 fact 候选记录。
            invalid_records: period 无效等需要进 staging 的记录。
            statement: 'income'/'balance'/'cashflow'。
            reconstruction_flag: 重建来源标记，如 RECONSTRUCTED_FROM_LEGACY_SNAPSHOT。
            batch_item_id: Phase 2 backfill item id；在线写入时可为 None。
            derive_filing_meta_func: 可选的 filing meta 推断函数；在线写入时传入 fetcher 的方法以兼容 monkeypatch 测试。

        Returns:
            {
                'facts_inserted': int,
                'facts_repeated': int,
                'facts_conflicted': int,
                'facts_staged': int,
                'fact_version_ids': list[int],
                'conflict_count': int,
                'staging_count': int,
            }
        """
        if not fact_records and not invalid_records:
            return {
                "facts_inserted": 0,
                "facts_repeated": 0,
                "facts_conflicted": 0,
                "facts_staged": 0,
                "fact_version_ids": [],
                "conflict_count": 0,
                "staging_count": 0,
            }

        stock_code = context.stock_code
        cik = context.cik
        snapshot_id = context.snapshot_id

        # 1. 拆分有效候选 vs staging
        candidate_rows: list[dict] = []
        staging_rows: list[dict] = []

        for rec in fact_records:
            reason = reject_reason(rec)
            if reason:
                staging_rows.append(self._staging_row(rec, statement, context, reason, run_id))
                continue

            decision, flag = classify_record(rec)
            if decision.startswith("STAGING"):
                staging_rows.append(self._staging_row(rec, statement, context, decision, run_id))
                continue

            if flag:
                rec = dict(rec)
                existing_flag = rec.get("_quality_flag")
                rec["_quality_flag"] = flag if existing_flag is None else f"{existing_flag},{flag}"
            candidate_rows.append(rec)

        for rec in invalid_records:
            staging_rows.append(self._staging_row(rec, statement, context, "INVALID_PERIOD", run_id))

        # 2. 推断 filing meta 并写入 us_filing
        if candidate_rows:
            meta_func = derive_filing_meta_func if derive_filing_meta_func is not None else derive_filing_meta
            filing_meta = meta_func(candidate_rows)
            self._flush_filings(conn, context, filing_meta)
        else:
            filing_meta = {}

        # 3. 构建完整 fact 行
        fact_rows = self._build_fact_rows(candidate_rows, statement, context, filing_meta, run_id)

        # 4. 同批去重
        unique_rows, batch_repeats, batch_conflicts = self._dedup_batch(fact_rows, run_id)

        # 5. 检测与已存在事实的 repeat/conflict，并获取 fact_version_id
        existing = self._load_existing_facts(conn, unique_rows)
        new_rows: list[dict] = []
        conflict_rows: list[dict] = batch_conflicts
        repeated = batch_repeats
        for row in unique_rows:
            key = fact_key(row)
            existing_info = existing.get(key)
            if existing_info is None:
                new_rows.append(row)
            elif existing_info["value_hash"] == row["value_hash"]:
                repeated += 1
            else:
                conflict_rows.append(
                    self._build_conflict_row(
                        run_id,
                        {
                            "value_hash": existing_info["value_hash"],
                            "value_numeric": existing_info.get("value_numeric"),
                            "value_text": existing_info.get("value_text"),
                        },
                        row,
                    )
                )

        # 6. 写入新 fact，使用 RETURNING 得到真实插入数
        facts_inserted = 0
        inserted_fact_ids: list[int] = []
        if new_rows:
            fact_columns = [
                "stock_code", "cik", "accession_no", "statement", "taxonomy", "sec_tag", "standard_field",
                "period_kind", "period_start", "report_date", "fiscal_year", "fiscal_period_raw", "form",
                "filed_date", "frame", "unit", "value_numeric", "value_text", "dimensions", "context_hash",
                "source_snapshot_id", "ingest_run_id", "value_hash", "quality_flags"
            ]
            fact_sql = f"""
                INSERT INTO us_financial_fact_version (
                    {', '.join(fact_columns)}
                ) VALUES %s
                ON CONFLICT DO NOTHING
                RETURNING fact_version_id
            """
            with conn.cursor() as cur:
                inserted = psycopg2.extras.execute_values(
                    cur,
                    fact_sql,
                    [tuple(row[c] for c in fact_columns) for row in new_rows],
                    template="(" + ", ".join(["%s"] * len(fact_columns)) + ")",
                    page_size=1000,
                    fetch=True,
                )
                facts_inserted = len(inserted or [])
                inserted_fact_ids = [row[0] for row in (inserted or [])]

            # 将插入的 fact_version_id 回填到对应行（按顺序，execute_values 返回顺序与输入一致）
            for i, fid in enumerate(inserted_fact_ids):
                new_rows[i]["_inserted_fact_version_id"] = fid

        # 7. 写入 conflict 与 staging
        with conn.cursor() as cur:
            if conflict_rows:
                for c in conflict_rows:
                    c["conflict_dedup_key"] = compute_conflict_dedup_key(c)
                cur.executemany(
                    """
                    INSERT INTO us_financial_fact_conflict (
                        run_id, stock_code, cik, accession_no, statement, taxonomy, sec_tag,
                        period_kind, period_start, report_date, fiscal_year, fiscal_period_raw,
                        form, filed_date, frame, unit, existing_value_hash, new_value_hash,
                        existing_value_numeric, existing_value_text, new_value_numeric, new_value_text,
                        dimensions, context_hash, source_snapshot_id, conflict_dedup_key
                    ) VALUES (
                        %(run_id)s, %(stock_code)s, %(cik)s, %(accession_no)s, %(statement)s, %(taxonomy)s, %(sec_tag)s,
                        %(period_kind)s, %(period_start)s, %(report_date)s, %(fiscal_year)s, %(fiscal_period_raw)s,
                        %(form)s, %(filed_date)s, %(frame)s, %(unit)s, %(existing_value_hash)s, %(new_value_hash)s,
                        %(existing_value_numeric)s, %(existing_value_text)s, %(new_value_numeric)s, %(new_value_text)s,
                        %(dimensions)s, %(context_hash)s, %(source_snapshot_id)s, %(conflict_dedup_key)s
                    )
                    ON CONFLICT (conflict_dedup_key) DO NOTHING
                    """,
                    conflict_rows,
                )
            self._flush_staging(cur, staging_rows)

        # 8. 写入 fact_source 证据关系（仅 inserted/reconstructed，repeated 只计数不写 source）
        self._write_fact_sources(
            conn,
            snapshot_id=snapshot_id,
            ingest_run_id=run_id,
            batch_item_id=batch_item_id,
            inserted_rows=new_rows,
            reconstruction_flag=reconstruction_flag,
        )

        return {
            "facts_inserted": facts_inserted,
            "facts_repeated": repeated,
            "facts_conflicted": len(conflict_rows),
            "facts_staged": len(staging_rows),
            "fact_version_ids": inserted_fact_ids,
            "conflict_count": len(conflict_rows),
            "staging_count": len(staging_rows),
        }

    # ── 内部 helper ─────────────────────────────────────────

    def _build_fact_rows(
        self,
        candidate_rows: list[dict],
        statement: str,
        context: Any,
        filing_meta: dict[str, dict],
        run_id: int | None,
    ) -> list[dict]:
        """把候选记录转成完整的事实行。"""
        fact_rows: list[dict] = []
        for rec in candidate_rows:
            end = rec["end"]
            val = rec["val"]
            value_numeric, value_text = split_value(val)
            context_hash = compute_context_hash(
                period_kind=rec["_period_kind"],
                period_start=rec.get("start"),
                report_date=end,
                frame=rec.get("frame"),
                fp=rec.get("fp"),
                dimensions=rec.get("dimensions", {}),
            )
            value_hash = compute_value_hash(val, rec["unit"])
            quality_flags = [f for f in [rec.get("_quality_flag")] if f]
            meta = filing_meta.get(str(rec.get("accn") or "").strip(), {})
            if meta.get("derived"):
                quality_flags.append("REPORT_DATE_DERIVED")

            row = {
                "stock_code": context.stock_code,
                "cik": context.cik,
                "accession_no": rec["accn"],
                "statement": statement,
                # companyfacts 记录不带 taxonomy,默认 us-gaap(行为不变);
                # 受限 filing-XBRL 链路显式传入发行人扩展 taxonomy(如 adt)。
                "taxonomy": rec.get("taxonomy") or "us-gaap",
                "sec_tag": rec["tag"],
                "standard_field": rec.get("field"),
                "period_kind": rec["_period_kind"],
                "period_start": rec.get("start"),
                "report_date": end,
                "fiscal_year": meta.get("fiscal_year"),
                "fiscal_period_raw": rec.get("fp") or None,
                "form": rec.get("form"),
                "filed_date": rec.get("filed"),
                "frame": rec.get("frame"),
                "unit": rec["unit"],
                "value_numeric": value_numeric,
                "value_text": value_text,
                "dimensions": psycopg2.extras.Json(rec.get("dimensions", {})),
                "context_hash": context_hash,
                "source_snapshot_id": context.snapshot_id,
                "ingest_run_id": run_id,
                "value_hash": value_hash,
                "quality_flags": quality_flags,
            }
            row["_key"] = fact_key(row)
            fact_rows.append(row)
        return fact_rows

    @staticmethod
    def _dedup_batch(
        fact_rows: list[dict],
        run_id: int | None,
    ) -> tuple[list[dict], int, list[dict]]:
        """对同一输入批次内的事实按唯一键归并。

        Returns:
            (unique_rows, batch_repeat_count, batch_conflict_rows)
        """
        by_key: dict[tuple, list[dict]] = {}
        for row in fact_rows:
            key = row.pop("_key")
            by_key.setdefault(key, []).append(row)

        unique_rows: list[dict] = []
        batch_repeats = 0
        batch_conflicts: list[dict] = []

        for key, rows in by_key.items():
            base = rows[0]
            unique_rows.append(base)
            base_hash = base["value_hash"]
            for dup in rows[1:]:
                if dup["value_hash"] == base_hash:
                    batch_repeats += 1
                else:
                    batch_conflicts.append(_build_conflict_row_static(run_id, base, dup))

        return unique_rows, batch_repeats, batch_conflicts

    def _load_existing_facts(
        self,
        conn,
        fact_rows: list[dict],
    ) -> dict[tuple, dict]:
        """批量查询已存在的 fact value_hash 与 fact_version_id。"""
        if not fact_rows:
            return {}

        keys = [fact_key(r) for r in fact_rows]

        with conn.cursor() as cur:
            cur.execute("""
                CREATE TEMP TABLE IF NOT EXISTS _tmp_fact_keys (
                    stock_code VARCHAR(20),
                    accession_no VARCHAR(30),
                    taxonomy VARCHAR(30),
                    sec_tag VARCHAR(200),
                    period_kind VARCHAR(10),
                    report_date DATE,
                    context_hash CHAR(64),
                    unit VARCHAR(50)
                ) ON COMMIT DROP
            """)
            cur.execute("TRUNCATE _tmp_fact_keys")
            psycopg2.extras.execute_values(
                cur,
                "INSERT INTO _tmp_fact_keys VALUES %s",
                keys,
                template="(%s, %s, %s, %s, %s, %s, %s, %s)",
            )
            cur.execute("""
                SELECT v.stock_code, v.accession_no, v.taxonomy, v.sec_tag,
                       v.period_kind, v.report_date::text, v.context_hash, v.unit,
                       v.value_hash, v.value_numeric, v.value_text, v.fact_version_id
                FROM us_financial_fact_version v
                INNER JOIN _tmp_fact_keys k
                  ON v.stock_code = k.stock_code
                 AND v.accession_no = k.accession_no
                 AND v.taxonomy = k.taxonomy
                 AND v.sec_tag = k.sec_tag
                 AND v.period_kind = k.period_kind
                 AND v.report_date = k.report_date
                 AND v.context_hash = k.context_hash
                 AND v.unit = k.unit
            """)
            rows = cur.fetchall()

        result: dict[tuple, dict] = {}
        for row in rows:
            key = tuple(row[:8])
            result[key] = {
                "value_hash": row[8],
                "value_numeric": row[9],
                "value_text": row[10],
                "fact_version_id": row[11],
            }
        return result

    def _build_conflict_row(
        self,
        run_id: int | None,
        existing_row: dict,
        new_row: dict,
    ) -> dict:
        return _build_conflict_row_static(run_id, existing_row, new_row)

    def _staging_row(
        self,
        rec: dict,
        statement: str,
        context: Any,
        reject_reason: str,
        run_id: int | None,
    ) -> dict:
        value_numeric, value_text = split_value(rec.get("val"))
        row = {
            "run_id": run_id,
            "stock_code": context.stock_code,
            "cik": context.cik,
            "accession_no": str(rec.get("accn") or "").strip() or None,
            "statement": statement,
            "taxonomy": rec.get("taxonomy") or "us-gaap",
            "sec_tag": rec.get("tag"),
            "period_kind": rec.get("_period_kind"),
            "period_start": rec.get("start"),
            "report_date": rec.get("end"),
            "fiscal_year": rec.get("fy"),
            "fiscal_period_raw": rec.get("fp") or None,
            "form": rec.get("form"),
            "filed_date": rec.get("filed"),
            "frame": rec.get("frame"),
            "unit": rec.get("unit"),
            "value_numeric": value_numeric,
            "value_text": value_text,
            "dimensions": psycopg2.extras.Json(rec.get("dimensions", {})),
            "context_hash": None,
            "source_snapshot_id": context.snapshot_id,
            "reject_reason": reject_reason,
            "raw_fact": psycopg2.extras.Json(rec),
        }
        row["staging_dedup_key"] = compute_staging_dedup_key(row)
        return row

    @staticmethod
    def _flush_filings(conn, context: Any, filing_meta: dict[str, dict]) -> None:
        if not filing_meta:
            return
        filing_rows = []
        for accn, meta in filing_meta.items():
            filing_metadata = {"report_date_source": "derived_from_company_facts"}
            if meta.get("frame"):
                filing_metadata["frame"] = meta["frame"]
            if meta.get("derived"):
                filing_metadata["report_date_derived"] = True

            filing_rows.append({
                "accession_no": accn,
                "stock_code": context.stock_code,
                "cik": context.cik,
                "form": meta["form"],
                "filed_date": meta["filed_date"],
                "report_date": meta["report_date"],
                "fiscal_year": meta["fiscal_year"],
                "fiscal_period": meta["fiscal_period"] or None,
                "is_amendment": "/A" in meta["form"],
                "source_snapshot_id": context.snapshot_id,
                "metadata": psycopg2.extras.Json(filing_metadata),
            })

        filing_sql = """
            INSERT INTO us_filing (
                accession_no, stock_code, cik, form, filed_date, report_date,
                fiscal_year, fiscal_period, is_amendment, source_snapshot_id, metadata
            ) VALUES (
                %(accession_no)s, %(stock_code)s, %(cik)s, %(form)s, %(filed_date)s, %(report_date)s,
                %(fiscal_year)s, %(fiscal_period)s, %(is_amendment)s, %(source_snapshot_id)s, %(metadata)s
            )
            ON CONFLICT (accession_no) DO UPDATE SET
                metadata = COALESCE(us_filing.metadata, '{}'::jsonb) || COALESCE(EXCLUDED.metadata, '{}'::jsonb),
                updated_at = NOW()
        """
        with conn.cursor() as cur:
            cur.executemany(filing_sql, filing_rows)

    @staticmethod
    def _flush_staging(cur, rows: list[dict]) -> None:
        if not rows:
            return
        sql = """
            INSERT INTO us_financial_fact_staging (
                run_id, stock_code, cik, accession_no, statement, taxonomy, sec_tag,
                period_kind, period_start, report_date, fiscal_year, fiscal_period_raw,
                form, filed_date, frame, unit, value_numeric, value_text, dimensions,
                context_hash, source_snapshot_id, reject_reason, raw_fact, staging_dedup_key
            ) VALUES (
                %(run_id)s, %(stock_code)s, %(cik)s, %(accession_no)s, %(statement)s, %(taxonomy)s, %(sec_tag)s,
                %(period_kind)s, %(period_start)s, %(report_date)s, %(fiscal_year)s, %(fiscal_period_raw)s,
                %(form)s, %(filed_date)s, %(frame)s, %(unit)s, %(value_numeric)s, %(value_text)s, %(dimensions)s,
                %(context_hash)s, %(source_snapshot_id)s, %(reject_reason)s, %(raw_fact)s, %(staging_dedup_key)s
            )
            ON CONFLICT (staging_dedup_key) DO NOTHING
        """
        cur.executemany(sql, rows)

    def _write_fact_sources(
        self,
        conn,
        snapshot_id: int,
        ingest_run_id: int | None,
        batch_item_id: int | None,
        inserted_rows: list[dict],
        reconstruction_flag: str | None,
    ) -> None:
        """为新事实/reconstruction 写 fact_source 证据关系。

        repeated 事实仍会被计数（facts_repeated），但不再逐条持久化 fact_source。
        追溯途径不变：raw_snapshot_version + raw_snapshot_observation + ingest_run
        仍可确定某 snapshot 是否观察到了已知事实。
        """
        observation_kind = "reconstructed" if reconstruction_flag else "inserted"
        source_rows: list[dict] = []

        for row in inserted_rows:
            fact_version_id = row.get("_inserted_fact_version_id")
            if fact_version_id is None:
                continue
            source_rows.append({
                "fact_version_id": fact_version_id,
                "snapshot_id": snapshot_id,
                "ingest_run_id": ingest_run_id,
                "batch_item_id": batch_item_id,
                "observation_kind": observation_kind,
                "observed_value_hash": row["value_hash"],
                "reconstruction_flag": reconstruction_flag,
            })

        if not source_rows:
            return

        sql = """
            INSERT INTO us_financial_fact_source (
                fact_version_id, snapshot_id, ingest_run_id, batch_item_id,
                observation_kind, observed_value_hash, reconstruction_flag
            ) VALUES (
                %(fact_version_id)s, %(snapshot_id)s, %(ingest_run_id)s, %(batch_item_id)s,
                %(observation_kind)s, %(observed_value_hash)s, %(reconstruction_flag)s
            )
            ON CONFLICT (fact_version_id, snapshot_id, observation_kind) DO NOTHING
        """
        with conn.cursor() as cur:
            cur.executemany(sql, source_rows)


def _build_conflict_row_static(
    run_id: int | None,
    existing_row: dict,
    new_row: dict,
) -> dict:
    """构造 conflict 表行（existing_row 为基准，new_row 为冲突值）。"""
    return {
        "run_id": run_id,
        "stock_code": new_row["stock_code"],
        "cik": new_row["cik"],
        "accession_no": new_row["accession_no"],
        "statement": new_row["statement"],
        "taxonomy": new_row["taxonomy"],
        "sec_tag": new_row["sec_tag"],
        "period_kind": new_row["period_kind"],
        "period_start": new_row["period_start"],
        "report_date": new_row["report_date"],
        "fiscal_year": new_row["fiscal_year"],
        "fiscal_period_raw": new_row["fiscal_period_raw"],
        "form": new_row["form"],
        "filed_date": new_row["filed_date"],
        "frame": new_row["frame"],
        "unit": new_row["unit"],
        "existing_value_hash": existing_row["value_hash"],
        "new_value_hash": new_row["value_hash"],
        "existing_value_numeric": existing_row.get("value_numeric"),
        "existing_value_text": existing_row.get("value_text"),
        "new_value_numeric": new_row["value_numeric"],
        "new_value_text": new_row["value_text"],
        "dimensions": new_row["dimensions"],
        "context_hash": new_row["context_hash"],
        "source_snapshot_id": new_row["source_snapshot_id"],
    }
