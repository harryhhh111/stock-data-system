"""财务事实审核 Agent MVP。

只生成审核提案；数据库变更必须由用户显式执行 approve。
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import subprocess
from urllib.parse import unquote
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import requests

import config
from core.relations.us_financial import build_economic_fact_key
from core.selectors.us_financial import USFactSelector
from db import Connection, execute


OUTPUT_ROOT = Path("build/financial_review")
ALLOWED_CLASSIFICATIONS = {
    "ACCOUNTING_STANDARD_CHANGE",
    "PRESENTATION_RECLASSIFICATION",
    "DISCONTINUED_OPERATIONS",
    "CORPORATE_REORGANIZATION",
    "ERROR_CORRECTION_RESTATEMENT",
    "TAG_MAPPING_ERROR",
    "INSUFFICIENT_EVIDENCE",
}
ALLOWED_TRANSITION_METHODS = {
    "FULL_RETROSPECTIVE",
    "MODIFIED_RETROSPECTIVE",
    "PROSPECTIVE",
    "NOT_APPLICABLE",
    "UNKNOWN",
}
ANNUAL_FORMS = {"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"}
KEYWORDS = (
    "revision of previously issued financial statements", "as revised",
    "as reported", "identified errors",
    "reclassified to discontinued operations", "historical results",
    "discontinued operation", "restat", "reclass", "retrospective",
    "adopt", "asc 606", "business combination", "merger",
    "correction of an error", "intersegment", "segment",
    "comparative periods", "will not be restated", "prior period",
    "other income (expense)",
)


def _json_value(value: Any) -> Any:
    if isinstance(value, (date, datetime, Decimal)):
        return str(value)
    return value


def _annual_fact(fact: dict[str, Any]) -> bool:
    form = str(fact.get("form") or "").upper()
    if form not in ANNUAL_FORMS:
        return False
    if fact.get("period_kind") == "instant":
        return str(fact.get("fiscal_period_raw") or "").upper() == "FY"
    start, end = fact.get("period_start"), fact.get("report_date")
    return bool(start and end and 330 <= (end - start).days <= 385)


def _case_id(group: list[dict[str, Any]]) -> str:
    key = build_economic_fact_key(group[0])
    raw = json.dumps(key, default=str, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass
class ReviewCase:
    case_id: str
    stock_code: str
    standard_field: str
    report_date: str
    selected_fact_version_id: int
    timeline: list[dict[str, Any]]


class ReviewCandidateFinder:
    """从 selector 的未决组中提取年度 revenue 审核案例。"""

    def __init__(self, selector: USFactSelector | None = None) -> None:
        self.selector = selector or USFactSelector()

    def find(
        self,
        stock_codes: list[str] | None = None,
        limit: int = 3,
        skip_case_ids: set[str] | None = None,
    ) -> list[ReviewCase]:
        if not 1 <= limit <= 10:
            raise ValueError("MVP limit must be between 1 and 10")
        facts = self.selector._load_facts(stock_codes, ["revenues"], date.today())
        groups: dict[tuple, list[dict[str, Any]]] = {}
        for fact in facts:
            groups.setdefault(build_economic_fact_key(fact), []).append(fact)

        cases: list[ReviewCase] = []
        for group in groups.values():
            group = self.selector._filter_canonical_tag_candidates(group)
            if not group or not _annual_fact(group[0]):
                continue
            group.sort(key=lambda f: (f["filed_date"], f["accession_no"], f["fact_version_id"]))
            selected, _, flags = self.selector._select_latest_restated(group)
            if "LATEST_RESTATED_APPROVED_ONLY" not in flags:
                continue
            timeline = [
                {k: _json_value(v) for k, v in fact.items() if k != "value_hash"}
                for fact in group
            ]
            case_id = _case_id(group)
            if case_id in (skip_case_ids or set()):
                continue
            cases.append(
                ReviewCase(
                    case_id=case_id,
                    stock_code=group[0]["stock_code"],
                    standard_field=group[0]["standard_field"],
                    report_date=str(group[0]["report_date"]),
                    selected_fact_version_id=selected["fact_version_id"],
                    timeline=timeline,
                )
            )
        cases.sort(key=lambda c: (-len(c.timeline), c.stock_code, c.report_date))
        return cases[:limit]


class SECEvidenceCollector:
    """按 accession 直接读取 SEC filing，并提取解释性上下文。"""

    def __init__(self, timeout: int = 30) -> None:
        self.timeout = timeout
        self.headers = {"User-Agent": config.sec.user_agent}

    def collect(self, case: ReviewCase) -> list[dict[str, str]]:
        evidence: list[dict[str, str]] = []
        accessions = list(dict.fromkeys(row["accession_no"] for row in case.timeline))[-3:]
        amount_keywords: list[str] = []
        for row in case.timeline:
            value = row.get("value_numeric")
            if value in (None, ""):
                continue
            numeric = int(Decimal(str(value)))
            amount_keywords.extend([f"{numeric:,}", f"{numeric // 1000:,}"])
        amount_keywords = list(dict.fromkeys(amount_keywords))
        for accession in accessions:
            cik_rows = execute(
                "SELECT cik FROM us_filing WHERE accession_no=%s LIMIT 1",
                (accession,),
                fetch=True,
            ) or []
            if not cik_rows:
                continue
            item = self._fetch_filing(
                str(cik_rows[0][0]), accession, amount_keywords,
            )
            if item:
                evidence.append(item)
        return evidence

    def _fetch_filing(
        self,
        cik: str,
        accession: str,
        extra_keywords: list[str] | None = None,
    ) -> dict[str, str] | None:
        compact = accession.replace("-", "")
        base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{compact}"
        index_url = f"{base}/{accession}-index.html"
        try:
            index = requests.get(index_url, headers=self.headers, timeout=self.timeout)
            index.raise_for_status()
            links = re.findall(r'href="([^"]+\.html?)"', index.text, re.I)
            links = [
                link for link in links
                if f"/{compact}/" in link and "-index." not in link
            ]
            if not links:
                return {"accession_no": accession, "url": index_url, "snippets": ""}
            document_url = links[0] if links[0].startswith("http") else (
                f"https://www.sec.gov{links[0]}" if links[0].startswith("/") else f"{base}/{links[0]}"
            )
            document_url = _direct_sec_document_url(document_url)
            response = requests.get(document_url, headers=self.headers, timeout=self.timeout)
            response.raise_for_status()
            text = html.unescape(re.sub(r"<[^>]+>", " ", response.text))
            text = re.sub(r"\s+", " ", text)
            snippets: list[str] = []
            lowered = text.lower()
            for keyword in tuple(extra_keywords or []) + KEYWORDS:
                start = 0
                for _ in range(2):
                    pos = lowered.find(keyword, start)
                    if pos < 0:
                        break
                    snippet = text[max(0, pos - 350): pos + 850]
                    if snippet not in snippets:
                        snippets.append(snippet)
                    start = pos + len(keyword)
            return {
                "accession_no": accession,
                "url": document_url,
                "snippets": "\n---\n".join(snippets[:8])[:12000],
            }
        except requests.RequestException:
            return None


def _direct_sec_document_url(url: str) -> str:
    """把 Inline XBRL viewer URL 转成真实 filing 文档 URL。"""
    marker = "/ix?doc="
    if marker not in url:
        return url
    path = unquote(url.split(marker, 1)[1])
    if not path.startswith("/"):
        path = "/" + path
    return "https://www.sec.gov" + path


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            raise ValueError("MiniMax response does not contain JSON")
        value = json.loads(match.group())
    if isinstance(value, dict) and isinstance(value.get("content"), str):
        return _extract_json(value["content"])
    if not isinstance(value, dict):
        raise ValueError("MiniMax response JSON must be an object")
    return value


class MiniMaxReviewer:
    SYSTEM = """你是美股 SEC 财报附注的语义阅读器。只依据输入的 SEC 证据判断变化原因。
你不负责选择 fact、不负责批准或排除数据，也不要输出数据库动作或 fact ID。
输出 JSON 对象，字段必须为 classification、transition_method、confidence、summary、
evidence_passage_ids。evidence_passage_ids 只能引用输入中已有的 passage_id。
classification 与 transition_method 只能取输入枚举。
必须区分 full retrospective、modified retrospective 和 presentation reclassification。
modified retrospective 通常不追溯改写采用日前比较期；如果同时提到新准则和重分类，
以直接解释数值变化的原因为主。证据不足时返回 INSUFFICIENT_EVIDENCE/UNKNOWN。"""

    def review(self, case: ReviewCase, evidence: list[dict[str, str]]) -> dict[str, Any]:
        if not any(item.get("snippets", "").strip() for item in evidence):
            return {
                "classification": "INSUFFICIENT_EVIDENCE",
                "transition_method": "UNKNOWN",
                "confidence": 0.0,
                "summary": "未能从 SEC 原始 filing 提取到解释性证据。",
                "evidence_passage_ids": [],
                "_minimax_usage": {
                    "estimated": True, "input_tokens": 0, "output_tokens": 0,
                    "note": "model not called because SEC evidence was unavailable",
                },
            }
        payload = {
            "allowed_classifications": sorted(ALLOWED_CLASSIFICATIONS),
            "allowed_transition_methods": sorted(ALLOWED_TRANSITION_METHODS),
            "fact_change_summary": {
                "stock_code": case.stock_code,
                "standard_field": case.standard_field,
                "report_date": case.report_date,
                "timeline": [
                    {
                        key: row.get(key)
                        for key in (
                            "value_numeric", "value_text", "unit", "sec_tag",
                            "form", "filed_date", "accession_no",
                        )
                    }
                    for row in case.timeline
                ],
            },
            "sec_evidence_passages": _evidence_passages(evidence),
        }
        payload_text = json.dumps(payload, ensure_ascii=False)
        result = subprocess.run(
            [
                "mmx", "text", "chat", "--output", "json", "--temperature", "0.1",
                "--quiet", "--non-interactive", "--max-tokens", "1400",
                "--system", self.SYSTEM, "--message", payload_text,
            ],
            capture_output=True, text=True, check=True,
        )
        raw_envelope = _extract_json(result.stdout)
        usage = raw_envelope.get("usage", {}) or {
            "estimated": True,
            "input_tokens": round((len(self.SYSTEM) + len(payload_text)) / 4),
            "output_tokens": round(len(result.stdout) / 4),
            "note": "mmx --quiet omits exact API usage; character/4 estimate",
        }
        envelope = raw_envelope
        content = envelope.get("content")
        if isinstance(content, list):
            envelope = _extract_json("\n".join(
                item.get("text", "") for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            ))
        for key in ("data", "message", "text", "output"):
            if key in envelope and isinstance(envelope[key], str):
                envelope = _extract_json(envelope[key])
                break
        envelope = normalize_analysis(envelope, evidence)
        try:
            validate_analysis(envelope, evidence)
            envelope["_minimax_usage"] = usage
            return envelope
        except ValueError as exc:
            return {
                "classification": "INSUFFICIENT_EVIDENCE",
                "transition_method": "UNKNOWN",
                "confidence": 0.0,
                "summary": f"MiniMax 语义分析未通过证据校验：{exc}",
                "evidence_passage_ids": [],
                "_minimax_usage": usage,
                "_rejected_model_analysis": envelope,
            }


def validate_analysis(analysis: dict[str, Any], evidence: list[dict[str, str]]) -> None:
    if analysis.get("classification") not in ALLOWED_CLASSIFICATIONS:
        raise ValueError("invalid classification")
    if analysis.get("transition_method") not in ALLOWED_TRANSITION_METHODS:
        raise ValueError("invalid transition_method")
    if not isinstance(analysis.get("confidence"), (int, float)):
        raise ValueError("confidence must be numeric")
    allowed_ids = {item["passage_id"] for item in _evidence_passages(evidence)}
    passage_ids = analysis.get("evidence_passage_ids")
    if not isinstance(passage_ids, list):
        raise ValueError("evidence_passage_ids must be a list")
    if not set(passage_ids).issubset(allowed_ids):
        raise ValueError("analysis cites unknown SEC evidence passage")


def _evidence_passages(evidence: list[dict[str, str]]) -> list[dict[str, str]]:
    passages: list[dict[str, str]] = []
    for filing_index, item in enumerate(evidence, 1):
        chunks = [chunk.strip() for chunk in item.get("snippets", "").split("\n---\n")]
        for passage_index, chunk in enumerate(filter(None, chunks), 1):
            passages.append({
                "passage_id": f"F{filing_index}P{passage_index}",
                "url": item["url"],
                "text": chunk,
            })
    return passages


def _normalize_evidence_text(value: str) -> str:
    """只消除 HTML/模型常见的排版差异，不改变词义。"""
    return re.sub(
        r"\s+", " ",
        value.replace("’", "'").replace("“", '"').replace("”", '"'),
    ).strip()


def normalize_analysis(
    analysis: dict[str, Any],
    evidence: list[dict[str, str]],
) -> dict[str, Any]:
    """应用不依赖公司的通用结构化规则。"""
    normalized = dict(analysis)
    confidence = normalized.get("confidence")
    if isinstance(confidence, str):
        mapped = {"HIGH": 0.9, "MEDIUM": 0.6, "LOW": 0.3}
        normalized["confidence"] = mapped.get(confidence.upper(), confidence)

    source = _normalize_evidence_text(
        " ".join(item.get("snippets", "") for item in evidence)
    ).lower()
    explicit_revenue_reclassification = (
        "reclassifications have been made to the prior period" in source
        and "reclassified these amounts from the various revenue" in source
        and "other income (expense)" in source
    )
    if (
        explicit_revenue_reclassification
        and normalized.get("transition_method") == "MODIFIED_RETROSPECTIVE"
    ):
        original = normalized.get("classification")
        normalized["classification"] = "PRESENTATION_RECLASSIFICATION"
        normalized.setdefault("rule_adjustments", []).append(
            f"{original} -> PRESENTATION_RECLASSIFICATION: "
            "SEC explicitly says prior-period revenue items were reclassified "
            "to Other income (expense)"
        )
    return normalized


class FinancialReviewRuleEngine:
    """用通用规则把语义分类转换为唯一动作。"""

    @staticmethod
    def _value_key(row: dict[str, Any]) -> tuple[str, str]:
        return str(row.get("value_numeric")), str(row.get("value_text"))

    def decide(
        self,
        case: ReviewCase,
        analysis: dict[str, Any],
        evidence: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        classification = analysis["classification"]
        transition = analysis["transition_method"]
        source = _normalize_evidence_text(
            " ".join(item.get("snippets", "") for item in (evidence or []))
        ).lower()
        explicit_full_retrospective = (
            (
                "full retrospective" in source
                or "retrospectively adopted" in source
                or "retrospective application" in source
            )
            and (
                "each prior year presented" in source
                or "all periods presented" in source
                or "each period presented" in source
                or "recast of each prior reporting period presented" in source
                or "recast of prior reporting periods" in source
                or "have been restated" in source
                or "to restate" in source
                or "required restating" in source
                or "amounts adjusted to reflect" in source
                or "have been adjusted to reflect" in source
                or "prior period amounts have been adjusted" in source
                or (
                    "as previously reported" in source
                    and "as adjusted" in source
                )
            )
        )
        explicit_discontinued_recast = (
            (
                "historical results" in source
                and "reclassified to discontinued operations" in source
                and "all periods presented" in source
            )
            or (
                "reflected as discontinued operations" in source
                and "all prior periods presented have been recast" in source
            )
            or (
                "classified as discontinued operations" in source
                and "all periods presented" in source
            )
        )
        new_value = self._value_key(case.timeline[-1])[0]
        def _markers(value: str) -> set[str]:
            number = Decimal(value)
            markers = {
                f"{int(number):,}",
                f"{int(number / 1000):,}",
            }
            # SEC tables commonly state that figures are in millions and render
            # values such as 13,274.2 rather than the raw 13,274,200,000.
            for divisor in (Decimal("1000"), Decimal("1000000")):
                scaled = number / divisor
                markers.add(f"{scaled:,.1f}")
                markers.add(f"{scaled:,.2f}")
            return markers

        prior_marker_groups = [
            _markers(self._value_key(row)[0])
            for row in case.timeline[:-1]
            if self._value_key(row)[0] not in {"None", ""}
        ]
        new_markers = (
            _markers(new_value) if new_value not in {"None", ""} else set()
        )
        explicit_error_correction = (
            (
                "identified errors in our previously issued financial statements" in source
                or "material misstatements" in source
                or "restating our previously issued" in source
                or (
                    "revised to correct" in source
                    and "previously reported" in source
                )
                or (
                    "as previously reported" in source
                    and "as restated" in source
                )
                or (
                    "as reported" in source
                    and "as restated" in source
                )
            )
            and ("as reported" in source or "as previously reported" in source)
            and ("as revised" in source or "as restated" in source)
            and any(
                any(marker in source for marker in group)
                for group in prior_marker_groups
            )
            and any(marker in source for marker in new_markers)
        )
        auto_adopt = classification == "PRESENTATION_RECLASSIFICATION" or (
            classification == "ACCOUNTING_STANDARD_CHANGE"
            and transition == "FULL_RETROSPECTIVE"
            and explicit_full_retrospective
        ) or (
            classification == "DISCONTINUED_OPERATIONS"
            and explicit_discontinued_recast
        ) or (
            classification == "ERROR_CORRECTION_RESTATEMENT"
            and explicit_error_correction
        )
        if not auto_adopt:
            return {
                "decision": "manual_review",
                "reason": f"规则不自动处理 {classification}/{transition}",
                "actions": [],
            }
        eligible_forms = ANNUAL_FORMS
        if classification == "ERROR_CORRECTION_RESTATEMENT":
            eligible_forms = ANNUAL_FORMS | {"10-Q", "10-Q/A", "6-K", "6-K/A"}
        annual = [
            row for row in case.timeline
            if str(row.get("form") or "").upper() in eligible_forms
        ]
        annual.sort(key=lambda row: (
            row["filed_date"], row["accession_no"], row["fact_version_id"],
        ))
        if not annual:
            return {"decision": "manual_review", "reason": "没有符合规则的正式 filing", "actions": []}
        target_value = self._value_key(annual[-1])
        confirmations = [row for row in annual if self._value_key(row) == target_value]
        minimum_confirmations = (
            1 if classification in {
                "ACCOUNTING_STANDARD_CHANGE", "DISCONTINUED_OPERATIONS",
                "ERROR_CORRECTION_RESTATEMENT",
            } else 2
        )
        confirmation_count = len({row["accession_no"] for row in confirmations})
        if confirmation_count < minimum_confirmations:
            return {
                "decision": "manual_review",
                "reason": (
                    f"后值只有 {confirmation_count} 份符合规则的正式 filing 确认，"
                    f"规则要求至少 {minimum_confirmations} 份"
                ),
                "actions": [],
            }
        target = confirmations[0]
        if target["fact_version_id"] == case.selected_fact_version_id:
            return {"decision": "no_change", "reason": "当前已选择目标值", "actions": []}
        return {
            "decision": "approve",
            "reason": (
                f"{classification}; 后值被 {len(confirmations)} 份符合规则的正式 filing 确认；"
                "只批准首次出现该后值的事实"
            ),
            "actions": [{
                "action": "approve_restatement",
                "fact_version_id": target["fact_version_id"],
                "reason": (
                    f"{classification}; target value confirmed by "
                    f"{len(confirmations)} eligible official filings"
                ),
            }],
        }


def build_proposal(
    case: ReviewCase,
    analysis: dict[str, Any],
    evidence: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    analysis = dict(analysis)
    usage = analysis.pop("_minimax_usage", {})
    return {
        "analysis": analysis,
        "decision": FinancialReviewRuleEngine().decide(case, analysis, evidence),
        "_minimax_usage": usage,
    }


def validate_proposal(case: ReviewCase, proposal: dict[str, Any]) -> None:
    decision = proposal.get("decision")
    if not isinstance(decision, dict) or decision.get("decision") not in {
        "approve", "manual_review", "no_change",
    }:
        raise ValueError("invalid rule decision")
    actions = decision.get("actions")
    if not isinstance(actions, list):
        raise ValueError("decision actions must be a list")
    allowed_ids = {row["fact_version_id"] for row in case.timeline}
    for action in actions:
        if action.get("action") != "approve_restatement":
            raise ValueError("MVP only permits approve_restatement")
        if action.get("fact_version_id") not in allowed_ids:
            raise ValueError("action references fact outside this case")


class ReviewStore:
    def __init__(self, root: Path = OUTPUT_ROOT) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, case: ReviewCase, evidence: list[dict[str, str]], proposal: dict[str, Any]) -> Path:
        path = self.root / f"{case.case_id}.json"
        document = {
            "schema_version": "financial_review_mvp_v2",
            "status": "proposed",
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "case": asdict(case),
            "evidence": evidence,
            "proposal": proposal,
        }
        path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def load(self, case_id: str) -> tuple[Path, dict[str, Any]]:
        path = self.root / f"{case_id}.json"
        return path, json.loads(path.read_text(encoding="utf-8"))

    def update_status(self, path: Path, document: dict[str, Any], status: str, by: str) -> None:
        document["status"] = status
        document["reviewed_by"] = by
        document["reviewed_at"] = datetime.now().isoformat(timespec="seconds")
        path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")


def approve_case(case_id: str, reviewed_by: str, store: ReviewStore | None = None) -> None:
    store = store or ReviewStore()
    path, document = store.load(case_id)
    if document["status"] != "proposed":
        raise ValueError(f"case is already {document['status']}")
    proposal = document["proposal"]
    case = ReviewCase(**document["case"])
    validate_proposal(case, proposal)
    if proposal["decision"]["decision"] != "approve":
        raise ValueError("case has no rule-approved database action")
    for action in proposal["decision"]["actions"]:
        kind = action["action"]
        if kind == "approve_restatement":
            with Connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO us_financial_restatement_review
                           (fact_version_id, decision, notes, reviewed_by)
                           VALUES (%s, 'approved', %s, %s)
                           ON CONFLICT (fact_version_id) DO UPDATE SET
                             decision='approved', notes=EXCLUDED.notes,
                             reviewed_by=EXCLUDED.reviewed_by, created_at=NOW()""",
                        (action["fact_version_id"], action["reason"], reviewed_by),
                    )
                conn.commit()
    store.update_status(path, document, "approved", reviewed_by)
