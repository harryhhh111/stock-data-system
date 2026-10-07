"""别名/单位/归一化纯函数测试（对照调研 TRUTH 与单位陷阱案例）。"""
import pytest

from core.parsers.hk_capex_aliases import (
    detect_currency,
    detect_magnitude,
    match_alias,
    normalize_label,
    parse_num,
    t2s,
    unit_scale,
)


class TestNormalize:
    def test_t2s_basic(self):
        assert t2s("購置物業、廠房及設備") == "购置物业、厂房及设备"

    def test_normalize_strips_connectors(self):
        assert normalize_label("Purchase of Property, Plant and Equipment") == "purchaseofpropertyplantequipment"
        assert normalize_label("購置物業、廠房及設備") == "购置物业厂房设备"
        assert normalize_label("已付資本性開支(13)") == "已付资本性开支"  # 尾部角注/括号剔除


class TestMatchAlias:
    def test_hits(self):
        for label in (
            "已付資本性開支",
            "資本開支",
            "購置物業、廠房及設備",
            "投資物業以及物業、廠房及設備付款",  # 不含排除词"投資"
            "purchase of property, plant and equipment",
            "購建固定資產、無形資產和其他長期資產支付的現金",
        ):
            m = match_alias(label)
            assert m is not None and m[1] is False, label

    def test_combined_investment_property_ok(self):
        # 合并行白名单：含"投資物業"但整体是 capex 性质
        m = match_alias("購置物業、廠房及設備以及投資物業")
        assert m is not None and m[1] is False

    def test_excludes_disposal(self):
        for label in (
            "出售固定資產所得款項",
            "出售物業、廠房及設備",
            "處置物業所得款項",
            "proceeds from disposal of property, plant and equipment",
            "折旧及摊销",
            "收回定期存款",
        ):
            assert match_alias(label) is None, label

    def test_weak_match(self):
        m = match_alias("購買機器及設備")  # 别名表未列的写法 → 弱匹配
        assert m is not None and m[1] is True

    def test_non_capex_none(self):
        assert match_alias("投資活動之現金流出淨額") is None
        assert match_alias("經營活動產生現金淨額") is None


class TestParseNum:
    def test_basic(self):
        assert parse_num("(2,275)") == -2275.0
        assert parse_num("44,703,066") == 44703066.0
        assert parse_num("-") == 0.0
        assert parse_num("1,237.9") == 1237.9

    def test_garbage(self):
        assert parse_num("十一") is None
        assert parse_num("25(a)") is None
        assert parse_num("abc") is None


class TestUnitScale:
    def test_currency_magnitude_split(self):
        # 00388 陷阱：页头"以港元為單位"+列头"百萬元" → HKD_mn（原型误判 HKD_unspec）
        text = "簡明綜合現金流動表（未經審核）（財務數字以港元為單位）\n截至2026年6月30日止六個月\n百萬元"
        assert unit_scale(text) == (1e6, "HKD_mn")

    def test_standalone_units(self):
        assert unit_scale("人民幣百萬元") == (1e6, "RMB_mn")
        assert unit_scale("人民幣千元") == (1e3, "RMB_thousand")
        assert unit_scale("百萬美元") == (1e6, "USD_mn")
        assert unit_scale("港幣億元") == (1e8, "HKD_100m")
        assert unit_scale("in millions of US$") == (1e6, "USD_mn")

    def test_magnitude_only(self):
        assert unit_scale("百萬元") == (1e6, "mn")

    def test_unspecified(self):
        assert unit_scale("以港元為單位") == (None, "HKD_unspec")

    def test_detectors(self):
        assert detect_currency("人民幣百萬元") == "RMB"
        assert detect_currency("百萬美元") == "USD"
        assert detect_currency("millions of Renminbi") == "RMB"  # CNOOC 英文版
        assert detect_currency("主要業務活動之現金流量") is None
        assert detect_magnitude("人民幣千元") == (1e3, "thousand")
        assert detect_magnitude("現金流量表") == (None, None)

    def test_en_thousand_requires_currency_prefix(self):
        # 数据数字 "1,000" 不得误命中 thousand（汇丰案例）
        assert unit_scale("Impairment of interest in associate 1,000") == (None, None)
        assert unit_scale("US$'000") == (1e3, "USD_thousand")
        assert unit_scale("Half-year to 30 Jun 2026 $m $m") == (1e6, "mn")
