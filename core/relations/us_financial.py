"""美股财报事实版本关系构建。

提供经济事实兼容键、context 比较和 relation builder。
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import psycopg2.extras

from db import Connection, execute

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CompatibilityResult:
    """两个 fact 之间的兼容性判断结果。"""

    compatible: bool
    reason: str
    quality_flags: list[str]
    normalized_key: tuple | None


# ── 纯函数：经济事实键与 context 比较 ──────────────────


def build_economic_fact_key(fact: dict[str, Any]) -> tuple:
    """生成经济事实兼容键。

    键用于把同一经济事实（同字段、同期间、同单位、同范围）分组。
    """
    return (
        str(fact.get("stock_code") or "").upper(),
        str(fact.get("standard_field") or "").lower(),
        str(fact.get("period_kind") or "").lower(),
        _normalize_period_start(fact.get("period_start")),
        str(fact.get("report_date") or ""),
        _normalize_unit(fact.get("unit")),
        _normalize_dimensions_scope(fact.get("dimensions", {})),
    )


def _normalize_period_start(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _normalize_unit(value: Any) -> str:
    return str(value or "").lower()


def _normalize_dimensions_scope(value: Any) -> frozenset[tuple[str, str]]:
    """把 dimensions 转成可 hash 的规范形式。

    目前只支持简单键值维度；嵌套维度按 key=value 拍平。
    """
    if not value:
        return frozenset()

    if isinstance(value, psycopg2.extras.Json):
        obj = value.adapted
    else:
        obj = value

    if isinstance(obj, str):
        try:
            obj = json.loads(obj)
        except json.JSONDecodeError:
            return frozenset()

    if not isinstance(obj, dict):
        return frozenset()

    items: list[tuple[str, str]] = []
    for k, v in sorted(obj.items()):
        items.append((str(k).lower(), str(v).lower()))
    return frozenset(items)


def compare_fact_context(earlier: dict[str, Any], later: dict[str, Any]) -> CompatibilityResult:
    """比较两个 fact 的 context 是否兼容。

    第一版采用严格经济事实键匹配；后续可扩展 52/53 周容差、tag migration
    等受控兼容规则。
    """
    key_e = build_economic_fact_key(earlier)
    key_l = build_economic_fact_key(later)

    if key_e == key_l:
        return CompatibilityResult(
            compatible=True,
            reason="economic fact key matches",
            quality_flags=[],
            normalized_key=key_e,
        )

    reason = _explain_key_diff(key_e, key_l)
    return CompatibilityResult(
        compatible=False,
        reason=reason,
        quality_flags=["CONTEXT_INCOMPATIBLE"],
        normalized_key=None,
    )


def _explain_key_diff(key_e: tuple, key_l: tuple) -> str:
    labels = [
        "stock_code",
        "standard_field",
        "period_kind",
        "period_start",
        "report_date",
        "unit",
        "dimensions_scope",
    ]
    diffs = []
    for label, a, b in zip(labels, key_e, key_l):
        if a != b:
            diffs.append(f"{label}: {a!r} vs {b!r}")
    return "; ".join(diffs) if diffs else "economic fact key differs"


# ── Relation Builder ─────────────────────────────────


class USFactRelationBuilder:
    """从 us_financial_fact_version 构建版本关系。"""

    VERSION = "us_fact_relation_builder_v1"

    def __init__(self) -> None:
        pass

    def build(
        self,
        stock_codes: list[str] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """为指定股票构建 fact version 关系。

        Args:
            stock_codes: 股票列表，None 表示全部。
            dry_run: True 只统计不写入。

        Returns:
            manifest dict，包含各 relation_type 数量、checksum 等。
        """
        facts = self._load_facts(stock_codes)
        relations = self._derive_relations(facts)

        manifest = {
            "builder_version": self.VERSION,
            "stock_count": len({f["stock_code"] for f in facts}),
            "fact_count": len(facts),
            "relation_count": len(relations),
            "by_type": self._count_by_type(relations),
            "dry_run": dry_run,
        }

        if not dry_run:
            self._persist_relations(relations)

        return manifest

    def _load_facts(self, stock_codes: list[str] | None = None) -> list[dict[str, Any]]:
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
                filed_date,
                dimensions,
                sec_tag,
                context_hash
            FROM us_financial_fact_version
        """
        params: tuple[Any, ...] = ()
        if stock_codes:
            placeholders = ", ".join(["%s"] * len(stock_codes))
            sql += f" WHERE stock_code IN ({placeholders})"
            params = tuple(stock_codes)

        sql += " ORDER BY stock_code, standard_field, period_kind, report_date, filed_date, fact_version_id"

        rows = execute(sql, params, fetch=True)
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

    def _derive_relations(self, facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """按经济事实键分组，组内按 filed_date 排序建立相邻关系。"""
        by_key: dict[tuple, list[dict[str, Any]]] = {}
        for fact in facts:
            key = build_economic_fact_key(fact)
            by_key.setdefault(key, []).append(fact)

        relations: list[dict[str, Any]] = []
        for key, group in by_key.items():
            group.sort(key=lambda f: (f["filed_date"] or "", f["accession_no"] or "", f["fact_version_id"]))
            for i in range(1, len(group)):
                earlier = group[i - 1]
                later = group[i]
                relation = self._classify_relation(earlier, later, key)
                if relation is not None:
                    relations.append(relation)

        return relations

    def _classify_relation(
        self,
        earlier: dict[str, Any],
        later: dict[str, Any],
        key: tuple,
    ) -> dict[str, Any] | None:
        """对相邻两个事实分类 relation_type。"""
        earlier_val = earlier.get("value_numeric") if earlier.get("value_numeric") is not None else earlier.get("value_text")
        later_val = later.get("value_numeric") if later.get("value_numeric") is not None else later.get("value_text")

        value_changed = earlier["value_hash"] != later["value_hash"]
        change_amount: Decimal | None = None
        change_ratio: Decimal | None = None

        if value_changed:
            change_amount = self._diff_numeric(earlier_val, later_val)
            change_ratio = self._ratio_numeric(earlier_val, later_val)

        relation_type = self._relation_type(earlier, later, value_changed)
        reason = self._relation_reason(relation_type, earlier, later, value_changed)
        quality_flags = self._quality_flags(relation_type, value_changed)

        return {
            "stock_code": key[0],
            "standard_field": key[1],
            "period_kind": key[2],
            "period_start": key[3],
            "report_date": key[4],
            "earlier_fact_id": earlier["fact_version_id"],
            "later_fact_id": later["fact_version_id"],
            "relation_type": relation_type,
            "value_changed": value_changed,
            "change_amount": change_amount,
            "change_ratio": change_ratio,
            "classification_method": "automated_v1",
            "reason": reason,
            "quality_flags": quality_flags,
        }

    @staticmethod
    def _relation_type(earlier: dict[str, Any], later: dict[str, Any], value_changed: bool) -> str:
        later_form = str(later.get("form") or "").upper()

        if not value_changed:
            return "repeat"

        if "/A" in later_form:
            return "amendment_candidate"

        # tag 不同但 standard_field 相同
        if str(earlier.get("sec_tag") or "").lower() != str(later.get("sec_tag") or "").lower():
            return "tag_migration_candidate"

        # 值不同且后续正常 filing：保守标为 unknown_change
        return "unknown_change"

    @staticmethod
    def _relation_reason(
        relation_type: str,
        earlier: dict[str, Any],
        later: dict[str, Any],
        value_changed: bool,
    ) -> str:
        if relation_type == "repeat":
            return f"same value_hash from {earlier['accession_no']} to {later['accession_no']}"
        if relation_type == "amendment_candidate":
            return f"amendment form {later['form']} changes value from {earlier['accession_no']}"
        if relation_type == "tag_migration_candidate":
            return f"tag changed from {earlier.get('sec_tag')} to {later.get('sec_tag')}"
        return f"value changed from {earlier['accession_no']} to {later['accession_no']}"

    @staticmethod
    def _quality_flags(relation_type: str, value_changed: bool) -> list[str]:
        flags = []
        if relation_type.endswith("_candidate"):
            flags.append("CANDIDATE_REVIEW_NEEDED")
        if value_changed and relation_type == "unknown_change":
            flags.append("UNKNOWN_CHANGE_REVIEW_NEEDED")
        return flags

    @staticmethod
    def _diff_numeric(a: Any, b: Any) -> Decimal | None:
        try:
            return Decimal(str(b)) - Decimal(str(a))
        except Exception:
            return None

    @staticmethod
    def _ratio_numeric(a: Any, b: Any) -> Decimal | None:
        try:
            da = Decimal(str(a))
            if da == 0:
                return None
            return Decimal(str(b)) / da
        except Exception:
            return None

    @staticmethod
    def _count_by_type(relations: list[dict[str, Any]]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in relations:
            counts[r["relation_type"]] = counts.get(r["relation_type"], 0) + 1
        return counts

    def _persist_relations(self, relations: list[dict[str, Any]]) -> None:
        if not relations:
            return

        sql = """
            INSERT INTO us_fact_version_relation (
                stock_code, standard_field, period_kind, period_start, report_date,
                earlier_fact_id, later_fact_id, relation_type, value_changed,
                change_amount, change_ratio, classification_method, reason, quality_flags
            ) VALUES (
                %(stock_code)s, %(standard_field)s, %(period_kind)s, %(period_start)s, %(report_date)s,
                %(earlier_fact_id)s, %(later_fact_id)s, %(relation_type)s, %(value_changed)s,
                %(change_amount)s, %(change_ratio)s, %(classification_method)s, %(reason)s, %(quality_flags)s
            )
            ON CONFLICT (earlier_fact_id, later_fact_id, relation_type) DO NOTHING
        """
        with Connection() as conn:
            with conn.cursor() as cur:
                cur.executemany(sql, relations)
            conn.commit()

        logger.info("Persisted %d relations", len(relations))
