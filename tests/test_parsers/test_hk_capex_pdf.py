"""extract_capex / locate_cf_pages / parse_period_header 测试。

断言对照调研 TRUTH（/tmp/hkex_samples/analyze.py，人工核验黄金标准）：
  02020 已付資本性開支 -1453/-1223 RMB百万（上年經重列）
  00388 購置固定資產及無形資產所支付款項 -1006 HKD百万（表头"以港元為單位"实为百万）
  00001 購入固定資產 -6271 HKD百万（行首(804)为美元列，须取港币报告列）
  01299 投資物業以及物業、廠房及設備付款 -53 USD百万（角注13在无形资产行剔除；PPE行优先于纯无形行）
  03306 購買不動產、廠房及設備 -49637 RMB千元（财政年度6月止，列=止2025-12-31六個月）
"""
import pytest

from core.parsers.hk_capex_pdf import (
    extract_capex,
    locate_cf_pages,
    parse_period_header,
)


class TestParsePeriodHeader:
    def test_chinese_numeral_date(self):
        end, months = parse_period_header("截至二零二六年六月三十日止六個月 — 未經審核")
        assert end is not None and end.isoformat() == "2026-06-30"
        assert months == 6

    def test_arabic_date(self):
        end, months = parse_period_header("簡明綜合現金流量表\n截至 2026 年 6 月 30 日止六個月")
        assert end is not None and end.isoformat() == "2026-06-30"
        assert months == 6

    def test_dec31_chinese(self):
        end, months = parse_period_header("截至二零二五年十二月三十一日止六個月")
        assert end is not None and end.isoformat() == "2025-12-31"
        assert months == 6

    def test_three_months(self):
        _, months = parse_period_header("資本開支\n未經審核\n截至以下日期止三個月")
        assert months == 3

    def test_year_ended(self):
        end, months = parse_period_header("綜合現金流量表 截至2025年12月31日止年度")
        assert months == 12
        assert end is not None and end.isoformat() == "2025-12-31"

    def test_english(self):
        end, months = parse_period_header("CONDENSED CONSOLIDATED STATEMENT OF CASH FLOWS\nsix months ended 30 June 2026")
        assert months == 6
        assert end is not None and end.isoformat() == "2026-06-30"

    def test_half_year_abbreviated_month(self):
        # 汇丰英文版："Half-year to 30 Jun 2026"
        end, months = parse_period_header("Consolidated statement of cash flows\nHalf-year to\n30 Jun 2026\n30 Jun 2025\n$m")
        assert months == 6
        assert end is not None and end.isoformat() == "2026-06-30"

    def test_garbage(self):
        assert parse_period_header("投資活動之現金流量") == (None, None)


class TestLocateCfPages:
    def test_cf_page_found(self, cf_page_pdf):
        r = locate_cf_pages(cf_page_pdf["anta_cf"])
        assert r["cf_pages"] == [1]

    def test_title_page_no_cf(self, cf_page_pdf):
        r = locate_cf_pages(cf_page_pdf["li_title"])
        assert r["cf_pages"] == []
        assert r["scan_pages"] == []


class TestExtractAnta:
    """基准：安踏 H1'26。"""

    def test_value_and_unit(self, cf_page_pdf):
        r = extract_capex(cf_page_pdf["anta_cf"], target_period="2026-06-30", target_months=6)
        assert r["status"] == "ok"
        assert r["grade"] == "A"
        assert r["value_raw"] == pytest.approx(-1453.0)
        assert r["prior_raw"] == pytest.approx(-1223.0)
        assert r["multiplier"] == pytest.approx(1e6)
        assert r["currency"] == "RMB"
        assert r["value_abs"] == pytest.approx(1453 * 1e6)
        assert r["label_raw"] == "已付資本性開支"
        assert r["period_end"] == "2026-06-30"
        assert r["period_months"] == 6
        assert r["restated"] is True  # 上年列（經重列）

    def test_wrong_period_rejected(self, cf_page_pdf):
        r = extract_capex(cf_page_pdf["anta_cf"], target_period="2026-06-30", target_months=3)
        assert r["status"] == "period_mismatch"


class TestExtractHkex:
    """单位陷阱 + 总部物业行排除。"""

    def test_unit_trap_resolved(self, cf_page_pdf):
        r = extract_capex(cf_page_pdf["hkex_cf"], target_period="2026-06-30", target_months=6)
        assert r["status"] == "ok"
        assert r["value_raw"] == pytest.approx(-1006.0)
        assert r["multiplier"] == pytest.approx(1e6)
        assert r["currency"] == "HKD"
        assert r["unit_label"] == "HKD_mn"  # 不是 HKD_unspec

    def test_hq_property_row_excluded(self, cf_page_pdf):
        r = extract_capex(cf_page_pdf["hkex_cf"], target_period="2026-06-30", target_months=6)
        assert r["label_raw"] == "購置固定資產及無形資產所支付款項"
        reasons = [e["reason"] for e in r["excluded"]]
        assert "hq_property_row" in reasons


class TestExtractCkh:
    """双币种：美元列在前，须取港币报告列。"""

    def test_reporting_currency_column(self, cf_page_pdf):
        r = extract_capex(cf_page_pdf["ckh_cf"], target_period="2026-06-30", target_months=6)
        assert r["status"] == "ok"
        assert r["value_raw"] == pytest.approx(-6271.0)  # 不是行首美元列 (804)
        assert r["prior_raw"] == pytest.approx(-7719.0)
        assert r["currency"] == "HKD"
        assert r["multiplier"] == pytest.approx(1e6)


class TestExtractAia:
    """角注13剔除 + PPE合并行优先于纯无形资产行。"""

    def test_footnote_and_row_priority(self, cf_page_pdf):
        r = extract_capex(cf_page_pdf["aia_cf"], target_period="2026-06-30", target_months=6)
        assert r["status"] == "ok"
        assert r["value_raw"] == pytest.approx(-53.0)  # 不是角注13，也不是无形资产行 -99
        assert r["currency"] == "USD"
        assert "投資物業以及" in r["label_raw"]


class TestExtractJnby:
    """6月财年：期末日 2025-12-31。"""

    def test_june_fiscal_year(self, cf_page_pdf):
        r = extract_capex(cf_page_pdf["jnby_cf"], target_period="2025-12-31", target_months=6)
        assert r["status"] == "ok"
        assert r["value_raw"] == pytest.approx(-49637.0)
        assert r["multiplier"] == pytest.approx(1e3)
        assert r["currency"] == "RMB"
        assert r["period_end"] == "2025-12-31"


class TestXiaomiMd:
    """MD&A 季度资本开支表：止三個月 ≠ 止六個月，必须拒止（原型此处假阳性）。"""

    def test_quarterly_table_rejected(self, cf_page_pdf):
        r = extract_capex(cf_page_pdf["xiaomi_md"], target_period="2026-06-30", target_months=6)
        assert r["status"] == "period_mismatch"
        assert r["value"] is None if "value" in r else True


class TestNoCfPage:
    def test_no_cf_page(self, cf_page_pdf):
        r = extract_capex(cf_page_pdf["li_title"], target_period="2026-06-30", target_months=6)
        assert r["status"] == "no_cf_page"
