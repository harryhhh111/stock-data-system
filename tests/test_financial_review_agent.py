from datetime import date

import pytest

from core.financial_review_agent import (
    FinancialReviewRuleEngine,
    MiniMaxReviewer,
    ReviewCandidateFinder,
    ReviewCase,
    _annual_fact,
    _compact_evidence_passages,
    _extract_json,
    _direct_sec_document_url,
    build_proposal,
    normalize_analysis,
    validate_analysis,
    validate_proposal,
)


def _case() -> ReviewCase:
    return ReviewCase(
        case_id="case1",
        stock_code="MKTX",
        standard_field="revenues",
        report_date="2017-12-31",
        selected_fact_version_id=1,
        timeline=[
            {
                "fact_version_id": 1, "value_numeric": "397471000",
                "value_text": None, "form": "10-K", "filed_date": "2018-02-21",
                "accession_no": "old",
            },
            {
                "fact_version_id": 2, "value_numeric": "393422000",
                "value_text": None, "form": "10-K", "filed_date": "2019-02-20",
                "accession_no": "new-1",
            },
            {
                "fact_version_id": 3, "value_numeric": "393422000",
                "value_text": None, "form": "10-K", "filed_date": "2020-02-18",
                "accession_no": "new-2",
            },
        ],
    )


def _analysis(
    classification: str = "PRESENTATION_RECLASSIFICATION",
    transition: str = "NOT_APPLICABLE",
) -> dict:
    return {
        "classification": classification,
        "transition_method": transition,
        "confidence": 0.9,
        "summary": "prior periods reclassified for current presentation",
        "evidence_passage_ids": [],
    }


def test_annual_fact_requires_real_annual_duration():
    fact = {
        "form": "10-K", "period_kind": "duration",
        "period_start": date(2023, 2, 1), "report_date": date(2024, 1, 31),
    }
    assert _annual_fact(fact)
    assert not _annual_fact({**fact, "period_start": date(2023, 11, 1)})


def test_candidate_batch_limit_is_fifty():
    class EmptySelector:
        def _load_facts(self, *args):
            return []

    finder = ReviewCandidateFinder(EmptySelector())
    assert finder.find(limit=50) == []
    with pytest.raises(ValueError, match="between 1 and 50"):
        finder.find(limit=51)


def test_extract_json_accepts_fenced_or_wrapped_text():
    assert _extract_json('prefix {"a": 1} suffix') == {"a": 1}
    assert _extract_json('{"content": "{\\"a\\": 2}"}') == {"a": 2}


def test_minimax_invalid_json_becomes_manual_review(monkeypatch):
    case = _case()
    monkeypatch.setattr(
        "core.financial_review_agent.subprocess.run",
        lambda *args, **kwargs: type(
            "Result", (), {"stdout": '{"classification": broken}'}
        )(),
    )
    analysis = MiniMaxReviewer().review(
        case, [{"url": "u", "snippets": "Topic 606 evidence"}]
    )
    assert analysis["classification"] == "INSUFFICIENT_EVIDENCE"
    assert analysis["_model_response_invalid"] is True


def test_inline_xbrl_viewer_url_is_unwrapped():
    assert _direct_sec_document_url(
        "https://www.sec.gov/ix?doc=/Archives/edgar/data/1/report.htm"
    ) == "https://www.sec.gov/Archives/edgar/data/1/report.htm"


def test_analysis_passage_id_must_come_from_sec_evidence():
    analysis = {**_analysis(), "evidence_passage_ids": ["invented"]}
    with pytest.raises(ValueError, match="unknown"):
        validate_analysis(analysis, [{"url": "u", "snippets": "official wording"}])


def test_explicit_prior_period_revenue_reclassification_overrides_modified_adoption():
    analysis = {
        **_analysis("ACCOUNTING_STANDARD_CHANGE", "MODIFIED_RETROSPECTIVE"),
        "confidence": "HIGH",
    }
    evidence = [{
        "url": "u",
        "snippets": (
            "Certain reclassifications have been made to the prior period's "
            "Consolidated Financial Statements. The Company reclassified these "
            "amounts from the various revenue and expense line items to Other "
            "income (expense)."
        ),
    }]
    result = normalize_analysis(analysis, evidence)
    assert result["confidence"] == 0.9
    assert result["classification"] == "PRESENTATION_RECLASSIFICATION"


def test_mktx_reclassification_selects_one_first_confirmed_later_fact():
    decision = FinancialReviewRuleEngine().decide(_case(), _analysis())
    assert decision["decision"] == "approve"
    assert decision["actions"] == [{
        "action": "approve_restatement",
        "fact_version_id": 2,
        "reason": (
            "PRESENTATION_RECLASSIFICATION; target value confirmed by "
            "2 eligible official filings"
        ),
    }]


def test_modified_retrospective_does_not_rewrite_history():
    decision = FinancialReviewRuleEngine().decide(
        _case(),
        _analysis("ACCOUNTING_STANDARD_CHANGE", "MODIFIED_RETROSPECTIVE"),
    )
    assert decision["decision"] == "manual_review"
    assert decision["actions"] == []


def test_full_retrospective_can_adopt_confirmed_later_value():
    decision = FinancialReviewRuleEngine().decide(
        _case(),
        _analysis("ACCOUNTING_STANDARD_CHANGE", "FULL_RETROSPECTIVE"),
        [{
            "url": "u",
            "snippets": (
                "We adopted using the full retrospective method and applied "
                "the standard to each prior year presented."
            ),
        }],
    )
    assert decision["decision"] == "approve"
    assert decision["actions"][0]["fact_version_id"] == 2


def test_full_retrospective_recast_reporting_period_wording():
    decision = FinancialReviewRuleEngine().decide(
        _case(),
        _analysis("ACCOUNTING_STANDARD_CHANGE", "FULL_RETROSPECTIVE"),
        [{
            "url": "u",
            "snippets": (
                "We adopted using the full retrospective method, which resulted "
                "in the recast of each prior reporting period presented."
            ),
        }],
    )
    assert decision["decision"] == "approve"


def test_full_retrospective_to_restate_wording():
    decision = FinancialReviewRuleEngine().decide(
        _case(),
        _analysis("ACCOUNTING_STANDARD_CHANGE", "FULL_RETROSPECTIVE"),
        [{
            "url": "u",
            "snippets": (
                "We adopted Topic 606 using the full retrospective method "
                "to restate 2017 and 2016."
            ),
        }],
    )
    assert decision["decision"] == "approve"


def test_retrospective_application_adjusted_amounts_wording():
    decision = FinancialReviewRuleEngine().decide(
        _case(),
        _analysis("ACCOUNTING_STANDARD_CHANGE", "FULL_RETROSPECTIVE"),
        [{
            "url": "u",
            "snippets": (
                "2017 and 2016 amounts adjusted to reflect the retrospective "
                "application of ASU 2014-09, Revenue from Contracts with Customers."
            ),
        }],
    )
    assert decision["decision"] == "approve"


def test_full_retrospective_prior_periods_adjusted_wording():
    decision = FinancialReviewRuleEngine().decide(
        _case(),
        _analysis("ACCOUNTING_STANDARD_CHANGE", "FULL_RETROSPECTIVE"),
        [{
            "url": "u",
            "snippets": (
                "We adopted Topic 606 utilizing the full retrospective method. "
                "Prior period amounts have been adjusted accordingly."
            ),
        }],
    )
    assert decision["decision"] == "approve"


def test_full_retrospective_as_previously_reported_as_adjusted_table():
    decision = FinancialReviewRuleEngine().decide(
        _case(),
        _analysis("ACCOUNTING_STANDARD_CHANGE", "FULL_RETROSPECTIVE"),
        [{
            "url": "u",
            "snippets": (
                "We adopted Topic 606 under the full retrospective method. "
                "As Previously Reported Adoption of Topic 606 As Adjusted."
            ),
        }],
    )
    assert decision["decision"] == "approve"


def test_modified_retrospective_explicit_adjustment_table_can_adopt():
    case = _case()
    case.timeline = case.timeline[:2]
    decision = FinancialReviewRuleEngine().decide(
        case,
        _analysis("ACCOUNTING_STANDARD_CHANGE", "MODIFIED_RETROSPECTIVE"),
        [{
            "url": "u",
            "snippets": (
                "Revenue from Contracts with Customers. As Previously Reported "
                "Adoption of Topic 606 As Adjusted 397,471 393,422."
            ),
        }],
    )
    assert decision["decision"] == "approve"


def test_explicit_presentation_recast_needs_only_one_annual_confirmation():
    case = _case()
    case.timeline = case.timeline[:2]
    decision = FinancialReviewRuleEngine().decide(
        case,
        _analysis("PRESENTATION_RECLASSIFICATION", "NOT_APPLICABLE"),
        [{
            "url": "u",
            "snippets": (
                "Prior period amounts have been adjusted to conform to the "
                "current presentation and recast to reflect the change. "
                "Revenue 397,471 393,422."
            ),
        }],
    )
    assert decision["decision"] == "approve"


def test_compact_evidence_keeps_high_signal_passages():
    case = _case()
    evidence = [{
        "url": "u",
        "snippets": "\n---\n".join(
            [f"generic passage {index}" for index in range(12)]
            + [
                "As Previously Reported Adoption of Topic 606 As Adjusted "
                "397,471 393,422."
            ]
        ),
    }]
    passages = _compact_evidence_passages(case, evidence, limit=3)
    assert len(passages) == 3
    assert any("Topic 606" in item["text"] for item in passages)


def test_discontinued_operations_recast_can_adopt_one_official_annual_value():
    case = _case()
    case.timeline = case.timeline[:2]
    decision = FinancialReviewRuleEngine().decide(
        case,
        _analysis("DISCONTINUED_OPERATIONS", "NOT_APPLICABLE"),
        [{
            "url": "u",
            "snippets": (
                "The historical results of Apergy have been reclassified to "
                "discontinued operations for all periods presented herein."
            ),
        }],
    )
    assert decision["decision"] == "approve"
    assert decision["actions"][0]["fact_version_id"] == 2


def test_explicit_error_correction_table_can_adopt_quarterly_revised_value():
    case = _case()
    case.timeline[1]["form"] = "10-Q"
    case.timeline = case.timeline[:2]
    decision = FinancialReviewRuleEngine().decide(
        case,
        _analysis("ERROR_CORRECTION_RESTATEMENT", "NOT_APPLICABLE"),
        [{
            "url": "u",
            "snippets": (
                "We identified errors in our previously issued financial statements. "
                "As Reported Revision As Revised Net sales 397,471 4,049 393,422."
            ),
        }],
    )
    assert decision["decision"] == "approve"
    assert decision["actions"][0]["fact_version_id"] == 2


def test_material_misstatement_revision_table_is_error_correction():
    case = _case()
    case.timeline = case.timeline[:2]
    decision = FinancialReviewRuleEngine().decide(
        case,
        _analysis("ERROR_CORRECTION_RESTATEMENT", "NOT_APPLICABLE"),
        [{
            "url": "u",
            "snippets": (
                "The statements contained material misstatements. "
                "As Previously Reported Adjustments As Revised "
                "Net sales 397,471 4,049 393,422."
            ),
        }],
    )
    assert decision["decision"] == "approve"


def test_as_reported_as_restated_table_is_error_correction():
    case = _case()
    case.timeline = case.timeline[:2]
    decision = FinancialReviewRuleEngine().decide(
        case,
        _analysis("ERROR_CORRECTION_RESTATEMENT", "FULL_RETROSPECTIVE"),
        [{
            "url": "u",
            "snippets": (
                "As Reported Restatement Adjustments As Restated "
                "Net sales 397,471 4,049 393,422."
            ),
        }],
    )
    assert decision["decision"] == "approve"


def test_multistage_error_uses_any_prior_value_and_as_restated_wording():
    case = _case()
    case.timeline.insert(1, {
        "fact_version_id": 4, "value_numeric": "410000000",
        "value_text": None, "form": "10-K", "filed_date": "2018-06-01",
        "accession_no": "middle",
    })
    decision = FinancialReviewRuleEngine().decide(
        case,
        _analysis("ERROR_CORRECTION_RESTATEMENT", "FULL_RETROSPECTIVE"),
        [{
            "url": "u",
            "snippets": (
                "We are restating our previously issued statements. "
                "As Reported Adjustments As Restated 410,000 16,578 393,422."
            ),
        }],
    )
    assert decision["decision"] == "approve"


def test_error_correction_matches_values_presented_in_millions():
    case = _case()
    case.timeline = [
        {
            **case.timeline[0],
            "value_numeric": "13327700000",
        },
        {
            **case.timeline[1],
            "value_numeric": "13274200000",
        },
    ]
    decision = FinancialReviewRuleEngine().decide(
        case,
        _analysis("ERROR_CORRECTION_RESTATEMENT", "FULL_RETROSPECTIVE"),
        [{
            "url": "u",
            "snippets": (
                "The statements have been revised to correct the amounts "
                "previously reported on a gross basis to a net basis. "
                "As reported Revision As revised TOTAL REVENUES "
                "13,327.7 (53.5) 13,274.2."
            ),
        }],
    )
    assert decision["decision"] == "approve"


def test_discontinued_operations_recast_wording_variant():
    case = _case()
    case.timeline = case.timeline[:2]
    decision = FinancialReviewRuleEngine().decide(
        case,
        _analysis("DISCONTINUED_OPERATIONS", "NOT_APPLICABLE"),
        [{
            "url": "u",
            "snippets": (
                "The financial information is reflected as discontinued operations. "
                "All prior periods presented have been recast to reflect the "
                "discontinued operations."
            ),
        }],
    )
    assert decision["decision"] == "approve"


def test_discontinued_operations_classified_all_periods_wording():
    case = _case()
    case.timeline = case.timeline[:2]
    decision = FinancialReviewRuleEngine().decide(
        case,
        _analysis("DISCONTINUED_OPERATIONS", "FULL_RETROSPECTIVE"),
        [{
            "url": "u",
            "snippets": (
                "Results of operations related to the disposed businesses "
                "have been classified as discontinued operations in all "
                "periods presented in this Form 10-K."
            ),
        }],
    )
    assert decision["decision"] == "approve"


def test_validate_proposal_rejects_fact_from_other_case():
    proposal = build_proposal(_case(), _analysis())
    proposal["decision"]["actions"][0]["fact_version_id"] = 999
    with pytest.raises(ValueError, match="outside"):
        validate_proposal(_case(), proposal)


def test_minimax_reviewer_parses_semantic_output(monkeypatch):
    class Result:
        stdout = (
            '{"content":[{"type":"text","text":"{'
            '\\"classification\\":\\"INSUFFICIENT_EVIDENCE\\",'
            '\\"transition_method\\":\\"UNKNOWN\\",'
            '\\"confidence\\":0.2,\\"summary\\":\\"x\\",'
            '\\"evidence_passage_ids\\":[]}"}]}'
        )

    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: Result())
    result = MiniMaxReviewer().review(
        _case(), [{"url": "https://sec.example/filing", "snippets": "restated"}]
    )
    assert result["classification"] == "INSUFFICIENT_EVIDENCE"
    assert "actions" not in result


def test_minimax_reviewer_does_not_call_model_without_sec_evidence(monkeypatch):
    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: pytest.fail("model must not run without evidence"),
    )
    result = MiniMaxReviewer().review(_case(), [])
    assert result["classification"] == "INSUFFICIENT_EVIDENCE"
    assert result["_minimax_usage"]["input_tokens"] == 0
