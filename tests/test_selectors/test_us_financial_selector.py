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
    sec_tag: str = "RevenueFromContractWithCustomerExcludingAssessedTax",
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
        "sec_tag": sec_tag,
    }


def test_first_reported_selects_earliest_filed_date():
    selector = USFactSelector()
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
    assert "first filed date preserved" in selected[0].selection_reason


def test_latest_restated_accepts_same_tag_official_annual_amendment():
    selector = USFactSelector()
    facts = [
        _fact(1, filed_date="2025-02-20", value_hash="old", value_numeric=100, form="10-K"),
        _fact(2, filed_date="2025-08-10", value_hash="new", value_numeric=90, form="10-K/A"),
    ]
    selector._load_facts = lambda *args, **kwargs: facts
    selected = selector.select(stock_codes=["TEST"], basis="latest-restated")
    assert selected[0].fact_version_id == 2
    assert "AUTO_RESTATED_SAME_TAG_ANNUAL" in selected[0].quality_flags


def test_latest_observed_selects_amendment():
    selector = USFactSelector()
    facts = [
        _fact(1, filed_date="2025-02-20", value_hash="old", value_numeric=100, form="10-K"),
        _fact(2, filed_date="2025-08-10", value_hash="new", value_numeric=90, form="10-K/A"),
    ]
    selector._load_facts = lambda *args, **kwargs: facts
    selected = selector.select(stock_codes=["TEST"], basis="latest-observed")
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


def test_latest_restated_accepts_same_tag_later_annual_value():
    selector = USFactSelector()
    facts = [
        _fact(1, filed_date="2025-02-20", value_hash="old", value_numeric=100, form="10-K"),
        _fact(2, filed_date="2026-02-20", value_hash="new", value_numeric=88, form="10-K"),
    ]
    selector._load_facts = lambda *args, **kwargs: facts
    selected = selector.select(stock_codes=["TEST"], basis="latest-restated")
    assert selected[0].fact_version_id == 2
    assert "AUTO_RESTATED_SAME_TAG_ANNUAL" in selected[0].quality_flags


def test_latest_restated_requires_review_for_cross_tag_change():
    selector = USFactSelector()
    facts = [
        _fact(1, filed_date="2025-02-20", value_hash="old", value_numeric=100),
        _fact(
            2,
            filed_date="2026-02-20",
            value_hash="new",
            value_numeric=88,
            accession_no="accn-2",
            sec_tag="Revenues",
        ),
    ]
    selector._load_facts = lambda *args, **kwargs: facts
    selected = selector.select(stock_codes=["TEST"], basis="latest-restated")
    assert selected[0].fact_version_id == 1
    assert "LATEST_RESTATED_APPROVED_ONLY" in selected[0].quality_flags


def test_latest_restated_requires_review_for_non_annual_change():
    selector = USFactSelector()
    facts = [
        _fact(1, filed_date="2025-02-20", value_hash="old", value_numeric=100),
        _fact(
            2,
            filed_date="2025-05-10",
            value_hash="new",
            value_numeric=88,
            form="10-Q",
        ),
    ]
    selector._load_facts = lambda *args, **kwargs: facts
    selected = selector.select(stock_codes=["TEST"], basis="latest-restated")
    assert selected[0].fact_version_id == 1
    assert "LATEST_RESTATED_APPROVED_ONLY" in selected[0].quality_flags


def test_as_of_switches_to_same_tag_restatement_only_after_filed_date():
    selector = USFactSelector()
    facts = [
        _fact(1, filed_date="2017-03-06", value_hash="old", value_numeric=179_632_000),
        _fact(2, filed_date="2019-03-08", value_hash="new", value_numeric=323_000_000),
    ]
    selector._load_facts = lambda *args, **kwargs: facts

    before = selector.select(
        stock_codes=["TEST"], basis="as-of", as_of_date="2019-03-07"
    )
    after = selector.select(
        stock_codes=["TEST"], basis="as-of", as_of_date="2019-03-08"
    )

    assert before[0].fact_version_id == 1
    assert after[0].fact_version_id == 2


def test_same_accession_uses_canonical_revenue_tag():
    selector = USFactSelector()
    facts = [
        _fact(
            1,
            value_hash="generic",
            value_numeric=800_000,
            sec_tag="Revenues",
        ),
        _fact(
            2,
            value_hash="sales",
            value_numeric=3_539_800_000,
            sec_tag="SalesRevenueNet",
        ),
    ]
    selector._load_facts = lambda *args, **kwargs: facts

    selected = selector.select(stock_codes=["TEST"], basis="first-reported")

    assert len(selected) == 1
    assert selected[0].fact_version_id == 2


def test_same_accession_revenue_uses_consolidated_value_not_static_tag_order():
    selector = USFactSelector()
    facts = [
        _fact(
            1,
            value_hash="total",
            value_numeric=936_407_000,
            sec_tag="Revenues",
        ),
        _fact(
            2,
            value_hash="subline",
            value_numeric=611_000,
            sec_tag="RevenueFromContractWithCustomerExcludingAssessedTax",
        ),
    ]
    selector._load_facts = lambda *args, **kwargs: facts

    selected = selector.select(stock_codes=["TEST"], basis="latest-restated")

    assert len(selected) == 1
    assert selected[0].fact_version_id == 1


def test_latest_restated_accepts_preferred_total_ocf_tag_migration():
    selector = USFactSelector()
    facts = [
        _fact(
            1,
            standard_field="net_cash_from_operations",
            value_hash="continuing",
            value_numeric=-100,
            sec_tag="NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
            filed_date="2017-01-13",
        ),
        _fact(
            2,
            standard_field="net_cash_from_operations",
            value_hash="total",
            value_numeric=493_800_000,
            sec_tag="NetCashProvidedByUsedInOperatingActivities",
            accession_no="accn-2",
            filed_date="2018-04-02",
        ),
    ]
    selector._load_facts = lambda *args, **kwargs: facts

    selected = selector.select(stock_codes=["TEST"], basis="latest-restated")

    assert selected[0].fact_version_id == 2
    assert "AUTO_RESTATED_SAME_TAG_ANNUAL" in selected[0].quality_flags


def test_ocf_same_value_tag_migration_allows_later_official_restatement():
    selector = USFactSelector()
    facts = [
        _fact(
            1,
            standard_field="net_cash_from_operations",
            value_hash="old",
            value_numeric=65_824_000_000,
            sec_tag="NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
            filed_date="2016-10-26",
        ),
        _fact(
            2,
            standard_field="net_cash_from_operations",
            value_hash="old",
            value_numeric=65_824_000_000,
            sec_tag="NetCashProvidedByUsedInOperatingActivities",
            accession_no="accn-2",
            filed_date="2017-11-03",
        ),
        _fact(
            3,
            standard_field="net_cash_from_operations",
            value_hash="restated",
            value_numeric=66_231_000_000,
            sec_tag="NetCashProvidedByUsedInOperatingActivities",
            accession_no="accn-3",
            filed_date="2018-11-05",
        ),
    ]
    selector._load_facts = lambda *args, **kwargs: facts

    before = selector.select(
        stock_codes=["TEST"], basis="as-of", as_of_date="2018-11-04"
    )
    after = selector.select(
        stock_codes=["TEST"], basis="as-of", as_of_date="2018-11-05"
    )

    assert before[0].fact_version_id == 1
    assert after[0].fact_version_id == 3


def test_later_trusted_annual_version_clears_superseded_pending_candidate():
    selector = USFactSelector()
    facts = [
        _fact(1, value_hash="old", value_numeric=100),
        _fact(
            2,
            value_hash="ambiguous",
            value_numeric=90,
            accession_no="accn-2",
            sec_tag="Revenues",
            filed_date="2026-02-01",
        ),
        _fact(
            3,
            value_hash="trusted",
            value_numeric=110,
            accession_no="accn-3",
            filed_date="2027-02-01",
        ),
    ]
    selector._load_facts = lambda *args, **kwargs: facts

    selected = selector.select(stock_codes=["TEST"], basis="latest-restated")

    assert selected[0].fact_version_id == 3
    assert "LATEST_RESTATED_APPROVED_ONLY" not in selected[0].quality_flags


def test_later_annual_repeat_clears_interim_quarterly_candidate():
    selector = USFactSelector()
    facts = [
        _fact(1, value_hash="annual", value_numeric=5_375_600_000),
        _fact(
            2,
            value_hash="quarterly-anomaly",
            value_numeric=179_000_000,
            accession_no="accn-2",
            form="10-Q",
            filed_date="2025-04-01",
        ),
        _fact(
            3,
            value_hash="annual",
            value_numeric=5_375_600_000,
            accession_no="accn-3",
            filed_date="2026-02-01",
        ),
    ]
    selector._load_facts = lambda *args, **kwargs: facts

    selected = selector.select(stock_codes=["TEST"], basis="latest-restated")

    assert selected[0].fact_version_id == 1
    assert "LATEST_RESTATED_APPROVED_ONLY" not in selected[0].quality_flags


def test_unpaid_capex_tag_is_not_selectable():
    selector = USFactSelector()
    facts = [
        _fact(
            1,
            standard_field="capital_expenditures",
            value_hash="unpaid",
            value_numeric=116_194,
            sec_tag="CapitalExpendituresIncurredButNotYetPaid",
        ),
        _fact(
            2,
            standard_field="capital_expenditures",
            value_hash="cash",
            value_numeric=1_137_089_000,
            sec_tag="PaymentsToAcquireProductiveAssets",
        ),
    ]
    selector._load_facts = lambda *args, **kwargs: facts

    selected = selector.select(stock_codes=["TEST"], basis="first-reported")

    assert len(selected) == 1
    assert selected[0].fact_version_id == 2


def test_tag_priority_does_not_discard_cross_accession_migration():
    selector = USFactSelector()
    facts = [
        _fact(
            1,
            accession_no="old",
            filed_date="2025-02-20",
            value_hash="same",
            sec_tag="SalesRevenueNet",
        ),
        _fact(
            2,
            accession_no="new",
            filed_date="2026-02-20",
            value_hash="same",
            sec_tag="RevenueFromContractWithCustomerExcludingAssessedTax",
        ),
    ]
    selector._load_facts = lambda *args, **kwargs: facts

    selected = selector.select(stock_codes=["TEST"], basis="first-reported")

    assert len(selected) == 1
    assert selected[0].candidate_count == 2


def test_checksum_is_stable():
    selector = USFactSelector()
    f1 = _fact(1, filed_date="2025-02-20")
    selected = [selector._to_selected_fact(f1, "latest-restated", "reason", [], 2)]
    checksum1 = selector._compute_checksum(selected)
    checksum2 = selector._compute_checksum(selected)
    assert checksum1 == checksum2
    assert len(checksum1) == 64
