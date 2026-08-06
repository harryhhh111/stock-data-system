"""tests/test_backtest/test_us_pit_dataset.py

Phase B4a:USPITDataset 构建器单元测试。
"""
from __future__ import annotations

from datetime import date

import pytest

from core.selectors.us_financial import USFactSelector
from quant.backtest import us_pit_dataset as ds_mod
from quant.backtest.us_pit_dataset import build_us_pit_dataset


@pytest.fixture(autouse=True)
def _no_db_reviews(monkeypatch):
    monkeypatch.setattr(
        USFactSelector, "_load_restatement_reviews", lambda self, ids: {}
    )


def _fact(
    fid, stock="X", field="revenues", value=100.0, tag="Revenues",
    form="10-K", filed="2025-02-20", rd="2024-12-31", ps="2024-01-01",
    pk="duration", accession="accn-1", unit="USD",
):
    return {
        "fact_version_id": fid, "stock_code": stock, "statement": "income",
        "standard_field": field, "period_kind": pk,
        "period_start": date.fromisoformat(ps) if ps else None,
        "report_date": date.fromisoformat(rd), "unit": unit,
        "value_hash": f"h{fid}", "value_numeric": value, "value_text": None,
        "accession_no": accession, "form": form,
        "filed_date": date.fromisoformat(filed), "dimensions": {},
        "sec_tag": tag, "context_hash": f"ctx{fid}", "fiscal_period_raw": "FY",
    }


def _annual_set(stock="X", rd="2024-12-31", ps="2024-01-01", filed="2025-02-20", base=0, ni=100.0, rev=1000.0):
    return [
        _fact(base + 1, stock, "revenues", rev, filed=filed, rd=rd, ps=ps),
        _fact(base + 2, stock, "net_income", ni, tag="NetIncomeLoss", filed=filed, rd=rd, ps=ps),
        _fact(base + 3, stock, "total_equity", 500.0, tag="StockholdersEquity", filed=filed, rd=rd, ps=None, pk="instant"),
        _fact(base + 4, stock, "total_assets", 2000.0, tag="Assets", filed=filed, rd=rd, ps=None, pk="instant"),
        _fact(base + 5, stock, "total_liabilities", 1500.0, tag="Liabilities", filed=filed, rd=rd, ps=None, pk="instant"),
        _fact(base + 6, stock, "net_cash_from_operations", 150.0, tag="NetCashProvidedByUsedInOperatingActivities", filed=filed, rd=rd, ps=ps),
        _fact(base + 7, stock, "capital_expenditures", 50.0, tag="PaymentsToAcquirePropertyPlantAndEquipment", filed=filed, rd=rd, ps=ps),
    ]


class TestFutureFilingExclusion:
    def test_filing_after_as_of_not_in_dataset(self):
        facts = _annual_set()
        before = build_us_pit_dataset(date(2025, 2, 19), facts=facts, exclusions=[], persist_audit=False)
        after = build_us_pit_dataset(date(2025, 2, 20), facts=facts, exclusions=[], persist_audit=False)
        assert before.annual.empty or "X" not in set(before.annual.get("stock_code", []))
        assert "X" in set(after.annual["stock_code"])


class TestChecksumStability:
    def test_same_inputs_same_checksums(self):
        facts = _annual_set()
        d1 = build_us_pit_dataset(date(2025, 3, 1), facts=facts, exclusions=[], persist_audit=False)
        d2 = build_us_pit_dataset(date(2025, 3, 1), facts=facts, exclusions=[], persist_audit=False)
        assert d1.manifest["annual_checksum"] == d2.manifest["annual_checksum"]
        assert d1.manifest["ttm_checksum"] == d2.manifest["ttm_checksum"]
        assert d1.checksum == d2.checksum

    def test_different_inputs_different_checksums(self):
        facts_a = _annual_set()
        facts_b = _annual_set(ni=200.0)
        d1 = build_us_pit_dataset(date(2025, 3, 1), facts=facts_a, exclusions=[], persist_audit=False)
        d2 = build_us_pit_dataset(date(2025, 3, 1), facts=facts_b, exclusions=[], persist_audit=False)
        assert d1.checksum != d2.checksum


class TestManifestContract:
    def test_manifest_required_keys(self):
        facts = _annual_set()
        d = build_us_pit_dataset(date(2025, 3, 1), facts=facts, exclusions=[], persist_audit=False)
        m = d.manifest
        for key in ("dataset_schema_version", "as_of_date", "selection_basis",
                    "selector_version", "selection_run_id", "stock_count",
                    "annual_checksum", "ttm_checksum"):
            assert key in m
        assert m["selection_basis"] == "as-of"
        assert d.selection_run_id


class TestStaticScan:
    def test_no_legacy_objects_or_current_snapshot(self):
        import inspect
        src = inspect.getsource(ds_mod)
        for obj in ("us_income_statement", "us_balance_sheet", "us_cash_flow_statement",
                    "mv_us_financial_indicator", "mv_us_indicator_ttm", "mv_us_fcf_yield",
                    "us_financial_current_annual", "us_financial_current_ttm",
                    "pe_ttm"):
            assert obj not in src


class TestTtmComponentsProvenance:
    def test_components_recorded(self):
        facts = _annual_set()
        # 补 Q1'24 与 Q1'25 让 TTM 可算
        facts.append(_fact(20, field="revenues", value=280.0, form="10-Q", filed="2024-05-01", rd="2024-03-31", ps="2024-01-01", accession="accn-q124"))
        facts.append(_fact(21, field="revenues", value=300.0, form="10-Q", filed="2025-05-01", rd="2025-03-31", ps="2025-01-01", accession="accn-q125"))
        d = build_us_pit_dataset(date(2025, 6, 1), facts=facts, exclusions=[], persist_audit=False)
        info = d.ttm_components.get(("X", "revenues"))
        assert info is not None
        assert float(info["value"]) == 1020.0
        comps = info["components"]
        assert comps["latest"]["accession_no"] == "accn-q125"
        assert comps["prior_year"]["accession_no"] == "accn-q124"
