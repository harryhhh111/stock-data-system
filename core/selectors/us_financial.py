"""美股财报事实版本选择器。

支持 first-reported、latest-restated、as-of 三种选择语义。
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import psycopg2.extras

from core.relations.us_financial import build_economic_fact_key
from db import Connection, execute

logger = logging.getLogger(__name__)


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

        facts = self._load_facts(stock_codes, fields, as_of_date if basis == "as-of" else None)

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
        """选择最新 restated 版本。

        第一版策略：
        - 同值 repeat 保留最早 filed_date 的事实来源；
        - 后续 amendment_candidate 若值不同，选择 latest；
        - unknown_change 标记复核。
        """
        # 按 filed_date 升序
        sorted_group = sorted(
            group,
            key=lambda f: (f["filed_date"] or "", f["accession_no"] or "", f["fact_version_id"]),
        )

        # 默认选择 filed_date 最新的
        latest = sorted_group[-1]
        earliest = sorted_group[0]

        if latest["value_hash"] == earliest["value_hash"]:
            # 同值 repeat：保留最早披露来源
            return earliest, "same value repeat; preserve first filed date", []

        later_form = str(latest.get("form") or "").upper()
        if "/A" in later_form:
            return latest, f"amendment {latest['form']} restates value", ["AMENDMENT_CANDIDATE"]

        # 保守处理：标记 unknown_change 待复核
        return latest, f"latest filed date with value change; review needed", ["UNKNOWN_CHANGE_REVIEW_NEEDED"]

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
        as_of_date: date | None,
    ) -> list[dict[str, Any]]:
        sql = """
            SELECT
                fact_version_id,
                stock_code,
                statement,
                standard_field,
                period_kind,
                period_start,
                report_date,
                unit,
                value_hash,
                value_numeric,
                value_text,
                accession_no,
                form,
                filed_date
            FROM us_financial_fact_version
            WHERE 1=1
        """
        params: list[Any] = []

        if stock_codes:
            placeholders = ", ".join(["%s"] * len(stock_codes))
            sql += f" AND stock_code IN ({placeholders})"
            params.extend(stock_codes)

        if fields:
            placeholders = ", ".join(["%s"] * len(fields))
            sql += f" AND standard_field IN ({placeholders})"
            params.extend(fields)

        sql += " ORDER BY stock_code, standard_field, period_kind, report_date, filed_date, fact_version_id"

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

        with Connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO us_fact_selection_run (
                        run_id, selection_basis, as_of_date, selector_version,
                        stock_scope, started_at, finished_at, status,
                        selected_count, rejected_count, checksum_algorithm,
                        result_checksum, error_message
                    ) VALUES (%s, %s, %s, %s, %s, %s, NOW(), %s, %s, %s, %s, %s, %s)
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
                        "md5",
                        checksum,
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
                            period_kind, period_start, report_date, selection_basis,
                            as_of_date, selected_fact_id, selected_accession,
                            selected_filed_date, candidate_count, selection_reason,
                            quality_flags, selector_version
                        ) VALUES (
                            %(run_id)s, %(stock_code)s, %(statement)s, %(standard_field)s,
                            %(period_kind)s, %(period_start)s, %(report_date)s, %(selection_basis)s,
                            %(as_of_date)s, %(selected_fact_id)s, %(selected_accession)s,
                            %(selected_filed_date)s, %(candidate_count)s, %(selection_reason)s,
                            %(quality_flags)s, %(selector_version)s
                        )
                        ON CONFLICT (run_id, stock_code, statement, standard_field,
                                     period_kind, period_start, report_date) DO NOTHING
                        """,
                        audit_rows,
                    )

            conn.commit()

    @staticmethod
    def _compute_checksum(selected: list[SelectedFact]) -> str:
        lines = []
        for s in sorted(
            selected,
            key=lambda x: (
                x.stock_code,
                x.statement,
                x.standard_field,
                x.period_kind,
                x.report_date.isoformat() if x.report_date else "",
                x.period_start.isoformat() if x.period_start else "",
            ),
        ):
            value = s.value_numeric if s.value_numeric is not None else s.value_text
            lines.append(
                f"{s.stock_code}|{s.statement}|{s.standard_field}|{s.period_kind}|"
                f"{s.report_date}|{s.period_start}|{value}|{s.accession_no}|{s.filed_date}"
            )
        canonical = "\n".join(lines)
        return hashlib.md5(canonical.encode("utf-8")).hexdigest()
