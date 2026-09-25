"""腾讯港股现金流量表转换器测试（fixtures 为已存档的真实响应）。"""
import json
from datetime import date
from pathlib import Path

import pytest

from core.transformers.tencent_hk import _parse_amount, transform_cashflow

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "tencent_hk"


def _load(code: str) -> dict:
    with open(FIXTURES / f"{code}.json", encoding="utf-8") as f:
        return json.load(f)


class TestReportPeriodMapping:
    """期数归期：reportype 映射、非 12 月财年。"""

    def test_02020_annual_and_semi(self):
        records = transform_cashflow(_load("02020"), "02020")
        assert len(records) == 4
        by_date = {r["report_date"]: r for r in records}
        assert by_date[date(2025, 12, 31)]["report_type"] == "annual"
        assert by_date[date(2024, 12, 31)]["report_type"] == "annual"
        assert by_date[date(2026, 6, 30)]["report_type"] == "semi"
        assert by_date[date(2025, 6, 30)]["report_type"] == "semi"

    def test_09988_march_fiscal_year(self):
        """阿里 3 月止财年：20260331 是年报（reportype=0），不能按 1231 后缀归期。"""
        records = transform_cashflow(_load("09988"), "09988")
        by_date = {r["report_date"]: r for r in records}
        assert by_date[date(2026, 3, 31)]["report_type"] == "annual"
        assert by_date[date(2026, 6, 30)]["report_type"] == "quarterly"
        assert by_date[date(2025, 12, 31)]["report_type"] == "quarterly"
        assert by_date[date(2025, 9, 30)]["report_type"] == "semi"

    def test_00224_march_fiscal_annual(self):
        """非目标形态：3 月财年公司的年报落在 0331。"""
        records = transform_cashflow(_load("00224"), "00224")
        by_date = {r["report_date"]: r for r in records}
        assert by_date[date(2026, 3, 31)]["report_type"] == "annual"
        assert by_date[date(2025, 3, 31)]["report_type"] == "annual"
        assert by_date[date(2025, 9, 30)]["report_type"] == "semi"

    def test_stock_code_echoed(self):
        records = transform_cashflow(_load("02020"), "02020")
        assert all(r["stock_code"] == "02020" for r in records)


class TestValueParsing:
    """值解析：亿/万/元/千分位/--。"""

    def test_parse_amount_units(self):
        assert _parse_amount("13.37亿元") == pytest.approx(1.337e9)
        assert _parse_amount("2.40万元") == pytest.approx(2.4e4)
        assert _parse_amount("9,000.00元") == pytest.approx(9e3)
        assert _parse_amount("1,427.75亿元") == pytest.approx(1.42775e11)
        assert _parse_amount("--") is None
        assert _parse_amount("") is None
        assert _parse_amount(None) is None
        assert _parse_amount("垃圾") is None

    def test_02020_annual_capex(self):
        """年报有值、中报 '--'（缺失不设 capex 键）。"""
        records = transform_cashflow(_load("02020"), "02020")
        by_date = {r["report_date"]: r for r in records}
        annual = by_date[date(2025, 12, 31)]
        assert annual["capex"] == pytest.approx(1.337e9)
        assert annual["capex_raw"] == "13.37亿元"
        semi = by_date[date(2026, 6, 30)]
        assert "capex" not in semi
        assert semi["capex_raw"] == "--"

    def test_00224_wan_unit(self):
        records = transform_cashflow(_load("00224"), "00224")
        by_date = {r["report_date"]: r for r in records}
        assert by_date[date(2025, 3, 31)]["capex"] == pytest.approx(2.4e4)

    def test_09988_thousands_separator(self):
        records = transform_cashflow(_load("09988"), "09988")
        by_date = {r["report_date"]: r for r in records}
        assert by_date[date(2026, 3, 31)]["capex"] == pytest.approx(1.42775e11)


class TestCapexRowMatching:
    """capex 行命中与排除（出售/处置/折旧）。"""

    def test_02020_picks_purchase_not_disposal(self):
        """02020 同时存在 '出售固定资产' 与 '购买固定资产' 行，必须选后者。"""
        records = transform_cashflow(_load("02020"), "02020")
        by_date = {r["report_date"]: r for r in records}
        assert by_date[date(2025, 12, 31)]["capex"] == pytest.approx(1.337e9)

    def test_00008_disposal_row_with_value_excluded(self):
        """00008 的 '出售固定资产' 有值（200.00万元），不能被当作 capex。"""
        records = transform_cashflow(_load("00008"), "00008")
        by_date = {r["report_date"]: r for r in records}
        annual = by_date[date(2025, 12, 31)]
        assert annual["capex"] == pytest.approx(2.068e9)
        assert annual["capex_raw"] == "20.68亿元"

    def test_no_capex_row_keeps_record_without_capex(self):
        """整张表无 capex 行时 record 仍产出，只是无 capex 键。"""
        payload = {"code": 0, "data": {"data": [[
            [["", ""], ["20251231", {"reportype": "0", "fiscalYear": "2025-12-31 00:00:00"}]],
            [["经营活动现金流量", {"font": 1}], ["", {"font": 1}]],
            [["除税前利润", {"font": 4}], ["1.00亿元", {"font": 4}, "0.1"]],
        ]]}}
        records = transform_cashflow(payload, "99999")
        assert len(records) == 1
        assert records[0]["report_type"] == "annual"
        assert "capex" not in records[0]
        assert records[0]["capex_raw"] is None


class TestStringifiedCells:
    """cell 可能是字符串化 list（防御处理，先试 ast.literal_eval）。"""

    def _stringify_cells(self, payload: dict) -> dict:
        import copy

        cloned = copy.deepcopy(payload)
        for table in cloned["data"]["data"]:
            for i, row in enumerate(table):
                if isinstance(row, list):
                    table[i] = [str(c) for c in row]
        return cloned

    def test_stringified_rows_parse_identically(self):
        payload = _load("02020")
        normal = transform_cashflow(payload, "02020")
        stringified = transform_cashflow(self._stringify_cells(payload), "02020")
        assert normal == stringified
