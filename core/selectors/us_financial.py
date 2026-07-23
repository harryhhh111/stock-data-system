"""美股财报事实版本选择器。

支持 first-reported、latest-restated、as-of 三种选择语义。
"""
from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg2.extras

from core.relations.us_financial import build_economic_fact_key, compute_economic_key_hash
from core.us_financial_exclusion import BUSINESS_REASON_CODES, TECHNICAL_REASON_CODES
from db import Connection, execute

logger = logging.getLogger(__name__)


_CHECKSUM_SCHEMA_VERSION = "v2"


@dataclass(frozen=True)
class SelectedFact:
    """选择器输出的一条选定事实。"""

    fact_version_id: int
    stock_code: str
    statement: str
    standard_field: str
    period_kind: str
    period_start: date | None
    report_date: date
    value_numeric: Decimal | None
    value_text: str | None
    unit: str
    accession_no: str
    filed_date: date
    sec_tag: str | None
    context_hash: str
    dimensions: dict
    economic_key_hash: str
    selection_basis: str
    selection_reason: str
    quality_flags: list[str] = field(default_factory=list)
    candidate_count: int = 0


class USFactSelector:
    """从 us_financial_fact_version 中选择事实版本。"""

    VERSION = "us_fact_selector_v1"

    def __init__(self) -> None:
        pass

    def select(
        self,
        stock_codes: list[str] | None = None,
        basis: str = "latest-restated",
        as_of_date: date | str | None = None,
        fields: list[str] | None = None,
    ) -> list[SelectedFact]:
        """选择事实版本。

        Args:
            stock_codes: 股票列表，None 表示全部。
            basis: 'first-reported' | 'latest-restated' | 'as-of'
            as_of_date: basis='as-of' 时必填，格式 'YYYY-MM-DD' 或 date。
            fields: 限定 standard_field 列表，None 表示全部。

        Returns:
            SelectedFact 列表。
        """
        if basis == "as-of" and as_of_date is None:
            raise ValueError("as-of selection requires as_of_date")

        if isinstance(as_of_date, str):
            as_of_date = date.fromisoformat(as_of_date)

        reference_date = as_of_date if basis == "as-of" else datetime.now().date()
        facts = self._load_facts(stock_codes, fields, reference_date)

        by_key: dict[tuple, list[dict[str, Any]]] = {}
        for fact in facts:
            key = build_economic_fact_key(fact)
            by_key.setdefault(key, []).append(fact)

        selected: list[SelectedFact] = []
        for key, group in by_key.items():
            group.sort(key=lambda f: (f["filed_date"] or "", f["accession_no"] or "", f["fact_version_id"]))
            candidate_count = len(group)

            if basis == "first-reported":
                fact = group[0]
                reason = "first filed date"
                flags = []
            elif basis == "latest-restated":
                fact, reason, flags = self._select_latest_restated(group)
            elif basis == "latest-observed":
                fact, reason, flags = self._select_latest_observed(group)
            else:  # as-of
                result = self._select_as_of(group, as_of_date)
                if result is None:
                    continue
                fact, reason, flags = result

            selected.append(self._to_selected_fact(fact, basis, reason, flags, candidate_count))

        return selected

    def _select_latest_restated(
        self,
        group: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], str, list[str]]:
        """选择最新已审核/可信 restated 版本。

        安全策略：
        - 同值 repeat 保留最早 filed_date 的事实来源；
        - amendment_candidate / unknown_change 未经审核不得替代旧版；
        - 因此 effective latest-restated 退化为最后一个可信版本。
        """
        sorted_group = sorted(
            group,
            key=lambda f: (f["filed_date"] or "", f["accession_no"] or "", f["fact_version_id"]),
        )

        # 至少保留最早披露
        approved = sorted_group[0]
        pending_review: list[dict[str, Any]] = []

        for fact in sorted_group[1:]:
            if fact["value_hash"] == approved["value_hash"]:
                # 同值 repeat：不改变 approved（保留首次披露）
                continue

            later_form = str(fact.get("form") or "").upper()
            if "/A" in later_form:
                # amendment candidate：未审核，不替代
                pending_review.append(fact)
                continue

            # 普通后续 filing 的异值：未审核，不替代
            pending_review.append(fact)

        if pending_review:
            latest_pending = pending_review[-1]
            reason = (
                f"preserving last approved version {approved['accession_no']} "
                f"({approved['filed_date']}); {len(pending_review)} pending review "
                f"up to {latest_pending['accession_no']} ({latest_pending['filed_date']})"
            )
            flags = ["LATEST_RESTATED_APPROVED_ONLY", f"PENDING_REVIEW_COUNT_{len(pending_review)}"]
        else:
            reason = "no subsequent revision; first filed date preserved"
            flags = []

        return approved, reason, flags

    def _select_latest_observed(
        self,
        group: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], str, list[str]]:
        """选择最新 observed 版本（包含未审核 candidate）。

        仅用于影子观察，不应用于正式当前分析。
        """
        sorted_group = sorted(
            group,
            key=lambda f: (f["filed_date"] or "", f["accession_no"] or "", f["fact_version_id"]),
        )

        latest = sorted_group[-1]
        earliest = sorted_group[0]

        if latest["value_hash"] == earliest["value_hash"]:
            return earliest, "same value repeat; preserve first filed date", []

        later_form = str(latest.get("form") or "").upper()
        if "/A" in later_form:
            return latest, f"latest observed amendment {latest['form']}", ["AMENDMENT_CANDIDATE"]

        return latest, "latest observed value with unreviewed change", ["UNKNOWN_CHANGE_REVIEW_NEEDED"]

    def _select_as_of(
        self,
        group: list[dict[str, Any]],
        as_of_date: date | None,
    ) -> tuple[dict[str, Any], str, list[str]] | None:
        """PIT 选择：只选 filed_date <= as_of_date 的，再应用 latest-restated 规则。

        若该经济事实在 as_of_date 前无披露，返回 None（跳过）。
        """
        candidates = [
            f for f in group
            if f["filed_date"] is not None and f["filed_date"] <= as_of_date
        ]
        if not candidates:
            return None

        candidates.sort(key=lambda f: (f["filed_date"] or "", f["accession_no"] or "", f["fact_version_id"]))
        return self._select_latest_restated(candidates)

    @staticmethod
    def _to_selected_fact(
        fact: dict[str, Any],
        basis: str,
        reason: str,
        flags: list[str],
        candidate_count: int,
    ) -> SelectedFact:
        dimensions = fact.get("dimensions", {})
        if isinstance(dimensions, psycopg2.extras.Json):
            dimensions = dimensions.adapted
        if isinstance(dimensions, str):
            dimensions = json.loads(dimensions)
        return SelectedFact(
            fact_version_id=fact["fact_version_id"],
            stock_code=fact["stock_code"],
            statement=fact["statement"],
            standard_field=fact["standard_field"],
            period_kind=fact["period_kind"],
            period_start=fact.get("period_start"),
            report_date=fact["report_date"],
            value_numeric=fact.get("value_numeric"),
            value_text=fact.get("value_text"),
            unit=fact["unit"],
            sec_tag=fact.get("sec_tag"),
            context_hash=fact.get("context_hash", ""),
            dimensions=dimensions or {},
            economic_key_hash=compute_economic_key_hash(fact),
            accession_no=fact["accession_no"],
            filed_date=fact["filed_date"],
            selection_basis=basis,
            selection_reason=reason,
            quality_flags=flags,
            candidate_count=candidate_count,
        )

    def _load_facts(
        self,
        stock_codes: list[str] | None,
        fields: list[str] | None,
        reference_date: date | datetime,
    ) -> list[dict[str, Any]]:
        sql = """
            SELECT
                f.fact_version_id,
                f.stock_code,
                f.statement,
                f.standard_field,
                f.period_kind,
                f.period_start,
                f.report_date,
                f.unit,
                f.value_hash,
                f.value_numeric,
                f.value_text,
                f.accession_no,
                f.form,
                f.filed_date,
                f.dimensions,
                f.sec_tag,
                f.context_hash
            FROM us_financial_fact_version f
            LEFT JOIN us_financial_fact_exclusion e
              ON e.fact_version_id = f.fact_version_id
             AND e.status = 'active'
             AND (
                 e.reason_code = ANY(%s)
                 OR (
                     e.reason_code = ANY(%s)
                     AND e.effective_from::date <= %s
                 )
             )
        """
        params: list[Any] = [
            list(TECHNICAL_REASON_CODES),
            list(BUSINESS_REASON_CODES),
            reference_date,
        ]

        sql += " WHERE e.fact_version_id IS NULL"

        if stock_codes:
            placeholders = ", ".join(["%s"] * len(stock_codes))
            sql += f" AND f.stock_code IN ({placeholders})"
            params.extend(stock_codes)

        if fields:
            placeholders = ", ".join(["%s"] * len(fields))
            sql += f" AND f.standard_field IN ({placeholders})"
            params.extend(fields)

        sql += " ORDER BY f.stock_code, f.standard_field, f.period_kind, f.report_date, f.filed_date, f.fact_version_id"

        rows = execute(sql, tuple(params), fetch=True)
        if rows is None:
            return []

        cols = [
            "fact_version_id",
            "stock_code",
            "statement",
            "standard_field",
            "period_kind",
            "period_start",
            "report_date",
            "unit",
            "value_hash",
            "value_numeric",
            "value_text",
            "accession_no",
            "form",
            "filed_date",
            "dimensions",
            "sec_tag",
            "context_hash",
        ]
        return [dict(zip(cols, row)) for row in rows]

    def select_and_audit(
        self,
        stock_codes: list[str] | None = None,
        basis: str = "latest-restated",
        as_of_date: date | str | None = None,
        fields: list[str] | None = None,
        persist: bool = True,
    ) -> tuple[uuid.UUID, list[SelectedFact]]:
        """选择事实并保存 selection run + audit。

        Returns:
            (run_id, selected_facts)
        """
        run_id = uuid.uuid4()
        started_at = datetime.now()

        try:
            selected = self.select(stock_codes, basis, as_of_date, fields)
            status = "success"
            error_message = None
        except Exception as exc:
            selected = []
            status = "failed"
            error_message = str(exc)
            logger.error("Selector failed: %s", exc)

        if persist:
            self._persist_run(
                run_id=run_id,
                basis=basis,
                as_of_date=as_of_date,
                stock_codes=stock_codes,
                selected=selected,
                status=status,
                started_at=started_at,
                error_message=error_message,
            )

        return run_id, selected

    def _persist_run(
        self,
        run_id: uuid.UUID,
        basis: str,
        as_of_date: date | str | None,
        stock_codes: list[str] | None,
        selected: list[SelectedFact],
        status: str,
        started_at: datetime,
        error_message: str | None,
    ) -> None:
        checksum = self._compute_checksum(selected)
        git_sha = self._get_selector_git_sha()

        from core.us_financial_exclusion import EXCLUSION_POLICY_VERSION

        manifest = {
            "checksum_schema_version": _CHECKSUM_SCHEMA_VERSION,
            "checksum_algorithm": "sha256",
            "sort_keys": [
                "stock_code", "statement", "standard_field", "period_kind",
                "report_date", "period_start", "unit", "economic_key_hash",
                "sec_tag",
            ],
            "value_normalization": "Decimal/str",
            "selector_git_sha": git_sha,
            "mapping_version": None,
            "exclusion_policy_version": EXCLUSION_POLICY_VERSION,
        }

        with Connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO us_fact_selection_run (
                        run_id, selection_basis, as_of_date, selector_version,
                        stock_scope, started_at, finished_at, status,
                        selected_count, rejected_count, checksum_algorithm,
                        result_checksum, manifest, error_message
                    ) VALUES (%s, %s, %s, %s, %s, %s, NOW(), %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        str(run_id),
                        basis,
                        as_of_date,
                        self.VERSION,
                        psycopg2.extras.Json({"stock_codes": stock_codes or []}),
                        started_at,
                        status,
                        len(selected),
                        0,
                        "sha256",
                        checksum,
                        psycopg2.extras.Json(manifest),
                        error_message,
                    ),
                )

                if selected:
                    audit_rows = [
                        {
                            "run_id": str(run_id),
                            "stock_code": s.stock_code,
                            "statement": s.statement,
                            "standard_field": s.standard_field,
                            "period_kind": s.period_kind,
                            "period_start": s.period_start,
                            "report_date": s.report_date,
                            "unit": s.unit,
                            "sec_tag": s.sec_tag,
                            "context_hash": s.context_hash,
                            "dimensions": psycopg2.extras.Json(s.dimensions),
                            "economic_key_hash": s.economic_key_hash,
                            "selection_basis": s.selection_basis,
                            "as_of_date": as_of_date,
                            "selected_fact_id": s.fact_version_id,
                            "selected_accession": s.accession_no,
                            "selected_filed_date": s.filed_date,
                            "candidate_count": s.candidate_count,
                            "selection_reason": s.selection_reason,
                            "quality_flags": s.quality_flags,
                            "selector_version": self.VERSION,
                        }
                        for s in selected
                    ]
                    cur.executemany(
                        """
                        INSERT INTO us_fact_selection_audit (
                            run_id, stock_code, statement, standard_field,
                            period_kind, period_start, report_date, unit, sec_tag,
                            context_hash, dimensions, economic_key_hash, selection_basis,
                            as_of_date, selected_fact_id, selected_accession,
                            selected_filed_date, candidate_count, selection_reason,
                            quality_flags, selector_version
                        ) VALUES (
                            %(run_id)s, %(stock_code)s, %(statement)s, %(standard_field)s,
                            %(period_kind)s, %(period_start)s, %(report_date)s, %(unit)s, %(sec_tag)s,
                            %(context_hash)s, %(dimensions)s, %(economic_key_hash)s, %(selection_basis)s,
                            %(as_of_date)s, %(selected_fact_id)s, %(selected_accession)s,
                            %(selected_filed_date)s, %(candidate_count)s, %(selection_reason)s,
                            %(quality_flags)s, %(selector_version)s
                        )
                        ON CONFLICT (run_id, stock_code, statement, standard_field,
                                     period_kind, period_start, report_date, economic_key_hash) DO NOTHING
                        """,
                        audit_rows,
                    )

            conn.commit()

    def _compute_checksum(self, selected: list[SelectedFact]) -> str:
        """计算稳定 checksum，包含 schema version、selector version 和 context。"""
        lines = [f"schema_version:{_CHECKSUM_SCHEMA_VERSION}", f"selector_version:{self.VERSION}"]
        for s in sorted(
            selected,
            key=lambda x: (
                x.stock_code,
                x.statement,
                x.standard_field,
                x.period_kind,
                x.report_date.isoformat() if x.report_date else "",
                x.period_start.isoformat() if x.period_start else "",
                x.unit,
                x.economic_key_hash,
                x.sec_tag or "",
            ),
        ):
            value = s.value_numeric if s.value_numeric is not None else s.value_text
            # Decimal 统一用 str 规范化
            lines.append(
                f"{s.stock_code}|{s.statement}|{s.standard_field}|{s.period_kind}|"
                f"{s.report_date}|{s.period_start}|{s.unit}|{s.economic_key_hash}|{s.sec_tag or ''}|"
                f"{value}|{s.accession_no}|{s.filed_date}|"
                f"{s.selection_basis}|{s.candidate_count}"
            )
        canonical = "\n".join(lines)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _get_selector_git_sha() -> str | None:
        """获取当前代码 Git SHA，用于 checksum/manifest 追溯。"""
        try:
            return subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=Path(__file__).resolve().parent.parent.parent,
                stderr=subprocess.DEVNULL,
                text=True,
            ).strip()
        except Exception:
            return None
