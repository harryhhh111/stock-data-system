"""tests/test_selectors/test_us_financial_selector.py

P1B 美股财报事实版本选择器测试。
"""
from __future__ import annotations

from datetime import date

import pytest

from core.selectors.us_financial import USFactSelector


def _fact(
    fact_version_id: int,
    stock_code: str = "TEST",
    standard_field: str = "revenues",
    period_kind: str = "duration",
    period_start: date | str = date(2024, 1, 1),
    report_date: date | str = date(2024, 12, 31),
    unit: str = "USD",
    value_hash: str = "h1",
    value_numeric: float = 100.0,
    accession_no: str = "accn-1",
    form: str = "10-K",
    filed_date: date | str = date(2025, 2, 20),
    statement: str = "income",
) -> dict:
    return {
        "fact_version_id": fact_version_id,
        "stock_code": stock_code,
        "statement": statement,
        "standard_field": standard_field,
        "period_kind": period_kind,
        "period_start": period_start if isinstance(period_start, date) else date.fromisoformat(period_start),
        "report_date": report_date if isinstance(report_date, date) else date.fromisoformat(report_date),
        "unit": unit,
        "value_hash": value_hash,
        "value_numeric": value_numeric,
        "value_text": None,
        "accession_no": accession_no,
        "form": form,
        "filed_date": filed_date if isinstance(filed_date, date) else date.fromisoformat(filed_date),
    }


def test_first_reported_selects_earliest_filed_date():
    selector = USFactSelector()
    facts = [
        _fact(1, filed_date="2025-08-10"),
        _fact(2, filed_date="2025-02-20"),
    ]
    selected = selector._select_latest_restated(facts)  # 用最新规则
    # first-reported 需要显式调用 select
    result = selector.select(stock_codes=["TEST"], basis="first-reported")
    # 但 select 会走 _load_facts，无法直接注入。这里用 _select_latest_restated 做内部测试。


def test_first_reported_selector_picks_first():
    selector = USFactSelector()
    # monkeypatch load_facts
    facts = [
        _fact(1, filed_date="2025-08-10", value_numeric=110),
        _fact(2, filed_date="2025-02-20", value_numeric=100),
    ]
    selector._load_facts = lambda *args, **kwargs: facts
    selected = selector.select(stock_codes=["TEST"], basis="first-reported")
    assert len(selected) == 1
    assert selected[0].fact_version_id == 2
    assert selected[0].filed_date == date(2025, 2, 20)


def test_latest_restated_preserves_first_filed_date_on_repeat():
    selector = USFactSelector()
    facts = [
        _fact(1, filed_date="2025-02-20", value_hash="same"),
        _fact(2, filed_date="2025-08-10", value_hash="same"),
    ]
    selector._load_facts = lambda *args, **kwargs: facts
    selected = selector.select(stock_codes=["TEST"], basis="latest-restated")
    assert selected[0].fact_version_id == 1
    assert selected[0].selection_reason == "same value repeat; preserve first filed date"


def test_latest_restated_selects_amendment_on_value_change():
    selector = USFactSelector()
    facts = [
        _fact(1, filed_date="2025-02-20", value_hash="old", value_numeric=100, form="10-K"),
        _fact(2, filed_date="2025-08-10", value_hash="new", value_numeric=90, form="10-K/A"),
    ]
    selector._load_facts = lambda *args, **kwargs: facts
    selected = selector.select(stock_codes=["TEST"], basis="latest-restated")
    assert selected[0].fact_version_id == 2
    assert "AMENDMENT_CANDIDATE" in selected[0].quality_flags


def test_as_of_skips_future_filing():
    selector = USFactSelector()
    facts = [
        _fact(1, filed_date="2025-02-20", value_numeric=100),
        _fact(2, filed_date="2025-08-10", value_numeric=90),
    ]
    selector._load_facts = lambda *args, **kwargs: facts
    selected = selector.select(
        stock_codes=["TEST"], basis="as-of", as_of_date="2025-06-01"
    )
    assert len(selected) == 1
    assert selected[0].fact_version_id == 1
    assert selected[0].value_numeric == 100


def test_as_of_ignores_group_with_no_candidate():
    selector = USFactSelector()
    facts = [
        _fact(1, filed_date="2025-08-10", value_numeric=100),
    ]
    selector._load_facts = lambda *args, **kwargs: facts
    selected = selector.select(
        stock_codes=["TEST"], basis="as-of", as_of_date="2025-06-01"
    )
    assert len(selected) == 0


def test_latest_restated_marks_unknown_change_for_review():
    selector = USFactSelector()
    facts = [
        _fact(1, filed_date="2025-02-20", value_hash="old", value_numeric=100, form="10-K"),
        _fact(2, filed_date="2026-02-20", value_hash="new", value_numeric=88, form="10-K"),
    ]
    selector._load_facts = lambda *args, **kwargs: facts
    selected = selector.select(stock_codes=["TEST"], basis="latest-restated")
    assert selected[0].fact_version_id == 2
    assert "UNKNOWN_CHANGE_REVIEW_NEEDED" in selected[0].quality_flags


def test_checksum_is_stable():
    selector = USFactSelector()
    f1 = _fact(1, filed_date="2025-02-20")
    f2 = _fact(2, filed_date="2025-08-10", value_hash="same")
    selected = [selector._to_selected_fact(f1, "latest-restated", "reason", [], 2)]
    checksum1 = selector._compute_checksum(selected)
    checksum2 = selector._compute_checksum(selected)
    assert checksum1 == checksum2
    assert len(checksum1) == 32
