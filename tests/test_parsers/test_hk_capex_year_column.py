"""阿里型「先列上年列」年份列陷阱测试（合成 PDF，不依赖样本）。"""
import pytest

pymupdf = pytest.importorskip("pymupdf")

from core.parsers.hk_capex_pdf import extract_capex


def _build_prior_first_pdf(path):
    """模拟阿里中报 CF 页：列序 = 2024 | 2025 | 美元折算列。"""
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((40, 50), "Unaudited Condensed Consolidated Statement of Cash Flows", fontsize=9)
    page.insert_text((40, 70), "Six months ended September 30", fontsize=9)
    page.insert_text((40, 88), "(RMB in millions)", fontsize=9)
    page.insert_text((260, 110), "2024", fontsize=9)
    page.insert_text((360, 110), "2025", fontsize=9)
    page.insert_text((460, 110), "US$", fontsize=9)
    page.insert_text((40, 140), "Purchases of property and equipment", fontsize=9)
    page.insert_text((250, 140), "(29,585)", fontsize=9)
    page.insert_text((350, 140), "(70,177)", fontsize=9)
    page.insert_text((455, 140), "(9,858)", fontsize=9)
    doc.save(path)
    doc.close()
    return path


class TestPriorYearFirstColumn:
    def test_target_year_column_wins(self, tmp_path):
        pdf = _build_prior_first_pdf(tmp_path / "prior_first.pdf")
        r = extract_capex(pdf, target_period="2025-09-30", target_months=6)
        assert r["status"] == "ok"
        assert r["value_raw"] == pytest.approx(-70177.0)  # 目标年(2025)列，而非第一个数值
        assert r["prior_raw"] == pytest.approx(-29585.0)
        assert r["multiplier"] == pytest.approx(1e6)
        assert r["currency"] == "RMB"

    def test_wrong_target_year_rejected(self, tmp_path):
        pdf = _build_prior_first_pdf(tmp_path / "prior_first2.pdf")
        r = extract_capex(pdf, target_period="2026-09-30", target_months=6)
        assert r["status"] == "period_mismatch"
