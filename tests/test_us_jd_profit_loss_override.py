"""tests/test_us_jd_profit_loss_override.py

JD ProfitLoss 归属净利润的受限映射(US_JD_PROFIT_LOSS_MAPPING_TASK §3.1.3)。
"""
from __future__ import annotations

import pytest

from core.fetchers.us_financial import USFinancialFetcher
from core.us_financial_field_overrides import (
    FIELD_OVERRIDES,
    override_field,
    validate_registry,
)


def _facts(tag: str, value: float) -> dict:
    return {
        "cik": 1546973,
        "facts": {
            "us-gaap": {
                tag: {
                    "units": {
                        "USD": [{
                            "fp": "FY", "start": "2025-01-01", "end": "2025-12-31",
                            "form": "20-F", "filed": "2026-04-16", "frame": "CY2025",
                            "val": value, "accn": "0001193125-26-157870", "fy": 2025,
                        }]
                    }
                }
            }
        },
    }


class TestJdOverride:
    def test_jd_profit_loss_maps_to_net_income(self):
        f = USFinancialFetcher()
        _, _, fact_records = f._extract_facts(
            _facts("ProfitLoss", 3309000000), f.INCOME_TAGS,
            statement="income", stock_code="JD")
        assert len(fact_records) == 1
        assert fact_records[0]["field"] == "net_income"
        assert fact_records[0]["tag"] == "ProfitLoss"

    def test_jd_operating_income_loss_stays_operating_income(self):
        f = USFinancialFetcher()
        _, _, fact_records = f._extract_facts(
            _facts("OperatingIncomeLoss", 397000000), f.INCOME_TAGS,
            statement="income", stock_code="JD")
        assert fact_records[0]["field"] == "operating_income"

    def test_jd_common_stays_net_income_common(self):
        f = USFinancialFetcher()
        _, _, fact_records = f._extract_facts(
            _facts("NetIncomeLossAvailableToCommonStockholdersBasic", 2807000000),
            f.INCOME_TAGS, statement="income", stock_code="JD")
        assert fact_records[0]["field"] == "net_income_common"

    def test_non_jd_profit_loss_unchanged(self):
        """非 JD 的 ProfitLoss 结果在本任务前后完全不变(仍为 operating_income)。"""
        f = USFinancialFetcher()
        for stock in ("AAPL", "BIDU", None):
            _, _, fact_records = f._extract_facts(
                _facts("ProfitLoss", 100.0), f.INCOME_TAGS,
                statement="income", stock_code=stock)
            assert fact_records[0]["field"] == "operating_income", stock

    def test_ingest_path_applies_override(self):
        """version-only ingest 入口(非仅在线 fetch)使用同一 resolver。"""
        f = USFinancialFetcher()
        ctx = type("C", (), {"stock_code": "JD", "cik": "0001546973",
                             "snapshot_id": 1, "content_hash": "h"})()
        facts = _facts("ProfitLoss", 3309000000)
        calls = {}

        def fake_write(fact_records, invalid_records, statement, context):
            calls[statement] = fact_records
            return {"facts_inserted": 0, "facts_repeated": 0,
                    "facts_conflicted": 0, "facts_staged": 0, "run_id": 1}

        f._compute_content_hash = lambda _: "h"
        f._write_version_layer = fake_write
        f._supplement_total_liabilities_records = lambda r, fr, c: (r, fr)
        f.ingest_version_layer(facts, ctx)
        assert calls["income"][0]["field"] == "net_income"


class TestRegistry:
    def test_registry_valid(self):
        assert validate_registry() == []

    def test_unknown_ticker_returns_none(self):
        assert override_field("AAPL", "us-gaap", "ProfitLoss") is None

    def test_registry_hit(self):
        assert override_field("JD", "us-gaap", "ProfitLoss") == "net_income"

    def test_illegal_field_detected(self, monkeypatch):
        bad = dict(FIELD_OVERRIDES)
        bad[("XX", "us-gaap", "ProfitLoss")] = {
            "standard_field": "not_a_field", "statement": "income",
            "reason": "x", "evidence": "y",
        }
        monkeypatch.setattr(
            "core.us_financial_field_overrides.FIELD_OVERRIDES", bad)
        assert any("非法 standard_field" in p for p in validate_registry())
