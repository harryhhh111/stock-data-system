import pytest
import pandas as pd
from core.transformers.us_gaap import USGAAPTransformer, SEC_FP_MAP


class TestSEC_FP_MAP:
    """SEC_FP_MAP 完整性测试。"""

    def test_covers_all_standard_fp(self):
        """所有标准的 SEC 10-K/10-Q 报告类型都应有映射。"""
        expected = {"FY", "Q1", "Q2", "Q3", "Q4", "H1"}
        assert set(SEC_FP_MAP.keys()) == expected

    def test_all_values_are_standard(self):
        """映射值必须是标准 report_type。"""
        valid = {"annual", "semi", "quarterly"}
        for v in SEC_FP_MAP.values():
            assert v in valid, f"{v} 不是标准 report_type"


class TestUSGAAPTransformer:
    """USGAAPTransformer 核心转换逻辑测试。"""

    def test_empty_df_returns_empty(self):
        transformer = USGAAPTransformer()
        result = transformer.transform_income(pd.DataFrame(), stock_code="AAPL")
        assert result == []

    def test_transform_income_basic(self, sample_sec_facts):
        """测试从 Company Facts 提取并转换利润表。"""
        from core.fetchers.us_financial import USFinancialFetcher

        fetcher = USFinancialFetcher()
        transformer = USGAAPTransformer()

        income_df = fetcher.extract_table(sample_sec_facts, fetcher.INCOME_TAGS)
        assert not income_df.empty, "应能从样本数据提取到利润表"

        records = transformer.transform_income(income_df, stock_code="AAPL", cik="0000320193")
        assert len(records) >= 1, "应至少有一条记录"

        # 验证记录结构
        record = records[0]
        assert record["stock_code"] == "AAPL"
        assert record["cik"] == "0000320193"
        assert record["currency"] == "USD"
        assert "revenues" in record or "Revenues" in record

    def test_transform_income_all_keys_equal(self, sample_sec_facts):
        """所有记录应有相同的 key 集合（upsert 要求）。"""
        from core.fetchers.us_financial import USFinancialFetcher

        fetcher = USFinancialFetcher()
        transformer = USGAAPTransformer()

        income_df = fetcher.extract_table(sample_sec_facts, fetcher.INCOME_TAGS)
        records = transformer.transform_income(income_df, stock_code="AAPL", cik="0000320193")

        if len(records) > 1:
            key_sets = [set(r.keys()) for r in records]
            assert all(ks == key_sets[0] for ks in key_sets), "记录间 key 集合不一致"
