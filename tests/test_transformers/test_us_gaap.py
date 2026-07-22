import numpy as np
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

    def test_transform_balance_equity_fallback_calculated(self):
        """total_equity = total_assets - total_liabilities 作为第三层 fallback。"""
        transformer = USGAAPTransformer()
        df = pd.DataFrame([{
            "end": "2025-12-31",
            "fp": "FY",
            "filed": "2026-02-15",
            "accn": "0000320193-26-000001",
            "total_assets": 100000,
            "total_liabilities": 60000,
        }])
        records = transformer.transform_balance(df, stock_code="TEST", cik="0000000001")
        assert len(records) == 1
        assert records[0]["total_equity"] == 40000

    def test_transform_balance_nci_fallback_over_calculated(self):
        """含 NCI 的权益 fallback 优先于计算值。"""
        transformer = USGAAPTransformer()
        df = pd.DataFrame([{
            "end": "2025-12-31",
            "fp": "FY",
            "filed": "2026-02-15",
            "accn": "0000320193-26-000001",
            "total_assets": 100000,
            "total_liabilities": 60000,
            "total_equity_including_nci": 42000,
        }])
        records = transformer.transform_balance(df, stock_code="TEST", cik="0000000001")
        assert len(records) == 1
        assert records[0]["total_equity"] == 42000  # NCI takes priority

    def test_transform_equity_direct_tag_over_fallbacks(self):
        """StockholdersEquity 直接标签优先于所有 fallback。"""
        transformer = USGAAPTransformer()
        df = pd.DataFrame([{
            "end": "2025-12-31",
            "fp": "FY",
            "filed": "2026-02-15",
            "accn": "0000320193-26-000001",
            "total_equity": 38000,
            "total_equity_including_nci": 42000,
            "total_assets": 100000,
            "total_liabilities": 60000,
        }])
        records = transformer.transform_balance(df, stock_code="TEST", cik="0000000001")
        assert len(records) == 1
        assert records[0]["total_equity"] == 38000  # direct tag wins

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


class TestUnknownFormFpFilter:
    """unknown form/fp 的记录不允许进入正式宽表。"""

    def test_unknown_report_type_filtered_out(self):
        """report_type=unknown 的记录被 _filter_unknown_records 过滤。"""
        transformer = USGAAPTransformer()
        records = [
            {"stock_code": "TEST", "report_type": "annual", "report_date": "2025-12-31",
             "accession_no": "0001"},
            {"stock_code": "TEST", "report_type": "unknown", "report_date": "2025-12-31",
             "accession_no": "0002"},
            {"stock_code": "TEST", "report_type": "quarterly", "report_date": "2025-09-30",
             "accession_no": "0003"},
        ]
        filtered = transformer._filter_unknown_records(records, "TEST", "income")
        assert len(filtered) == 2, f"应过滤 1 条 unknown, 实际保留 {len(filtered)} 条"
        report_types = {r["report_type"] for r in filtered}
        assert "unknown" not in report_types, "unknown 不应出现在过滤后记录中"

    def test_all_unknown_filtered_returns_empty(self):
        """全部是 unknown 时返回空列表。"""
        transformer = USGAAPTransformer()
        records = [
            {"stock_code": "TEST", "report_type": "unknown", "report_date": "2025-12-31",
             "accession_no": "0001"},
            {"stock_code": "TEST", "report_type": "unknown", "report_date": "2025-09-30",
             "accession_no": "0002"},
        ]
        filtered = transformer._filter_unknown_records(records, "TEST", "balance")
        assert filtered == [], f"全部 unknown 应返回空列表, 实际: {filtered}"

    def test_all_valid_passes_through(self):
        """全部是有效 report_type 时原样返回。"""
        transformer = USGAAPTransformer()
        records = [
            {"stock_code": "TEST", "report_type": "annual", "report_date": "2025-12-31"},
            {"stock_code": "TEST", "report_type": "quarterly", "report_date": "2025-09-30"},
        ]
        filtered = transformer._filter_unknown_records(records, "TEST", "cashflow")
        assert len(filtered) == 2
        assert filtered == records


class TestUnknownFormFpEndToEnd:
    """端到端：raw DataFrame → _build_record → transform → unknown 被过滤。"""

    def _make_row(self, **overrides):
        """构造模拟 extract_table 输出的宽表行。"""
        defaults = {
            "end": "2025-12-31",
            "fp": "FY",
            "filed": "2026-02-15",
            "accn": "0000320193-26-000001",
            "frame": "CY2025Q4I",
            "form": "10-K",
            "_period_kind": "instant",
            "_quality_flag": None,
            "_frame_has_q": True,
            "_frame_is_instant": True,
            "revenues": 100e9,
            "net_income": 20e9,
        }
        defaults.update(overrides)
        return pd.Series(defaults)

    def test_unknown_fp_and_unknown_form_yields_unknown(self, caplog):
        """fp=XX + form=8-K + instant → _build_record → report_type=unknown。"""
        transformer = USGAAPTransformer()
        row = self._make_row(fp="XX", form="8-K", _period_kind="instant")
        record = transformer._build_record(row, "TEST", "0000000001")
        assert record is not None, "unknown fp+form 不应返回 None（应保留并标记 unknown）"
        assert record["report_type"] == "unknown"

    def test_unknown_fp_10k_duration_blocked_from_form_fallback(self, caplog):
        """fp=XX + form=10-K + duration → 不适用 form fallback → unknown。"""
        transformer = USGAAPTransformer()
        row = self._make_row(fp="XX", form="10-K", _period_kind="duration",
                             _frame_is_instant=False)
        record = transformer._build_record(row, "TEST", "0000000001")
        assert record is not None
        assert record["report_type"] == "unknown", (
            f"duration+10-K 不应自动判为 annual, got {record['report_type']}"
        )

    def test_unknown_fp_10q_instant_passes_form_fallback(self):
        """fp=XX + form=10-Q + instant → form fallback 允许 → quarterly。"""
        transformer = USGAAPTransformer()
        row = self._make_row(fp="XX", form="10-Q", _period_kind="instant")
        record = transformer._build_record(row, "TEST", "0000000001")
        assert record is not None
        assert record["report_type"] == "quarterly", (
            f"instant+10-Q 应通过 form fallback 判为 quarterly, got {record['report_type']}"
        )

    def test_unknown_fp_10k_instant_passes_form_fallback(self):
        """fp=XX + form=10-K + instant → form fallback 允许 → annual。"""
        transformer = USGAAPTransformer()
        row = self._make_row(fp="XX", form="10-K", _period_kind="instant")
        record = transformer._build_record(row, "TEST", "0000000001")
        assert record is not None
        assert record["report_type"] == "annual", (
            f"instant+10-K 应通过 form fallback 判为 annual, got {record['report_type']}"
        )

    def test_transform_income_filters_unknown(self):
        """transform_income 出口不含 unknown 记录。"""
        transformer = USGAAPTransformer()
        # 构造两条：一条正常 annual，一条 unknown
        row1 = self._make_row(fp="FY")
        row2 = self._make_row(fp="XX", form="8-K", end="2025-09-30")
        df = pd.DataFrame([row1, row2])
        records = transformer.transform_income(df, stock_code="TEST", cik="0000000001")
        report_types = {r["report_type"] for r in records}
        assert "unknown" not in report_types, (
            f"transform_income 输出不应含 unknown, got {report_types}"
        )
        assert len(records) == 1, f"应只保留 1 条 annual 记录, got {len(records)}"

    def test_transform_balance_instant_form_fallback_does_not_filter(self):
        """transform_balance: fp=XX + form=10-K + instant → annual（不过滤）。"""
        transformer = USGAAPTransformer()
        row = self._make_row(fp="XX", form="10-K")
        df = pd.DataFrame([row])
        records = transformer.transform_balance(df, stock_code="TEST", cik="0000000001")
        assert len(records) == 1, f"instant+10-K 应通过, got {len(records)}"
        assert records[0]["report_type"] == "annual"

    def test_transform_cashflow_duration_form_blocked(self):
        """transform_cashflow: fp=XX + form=10-K + duration → unknown → 被过滤。"""
        transformer = USGAAPTransformer()
        row = self._make_row(fp="XX", form="10-K", _period_kind="duration",
                             _frame_is_instant=False)
        df = pd.DataFrame([row])
        records = transformer.transform_cashflow(df, stock_code="TEST", cik="0000000001")
        assert len(records) == 0, (
            f"duration+10-K+unknown fp 应被过滤, got {len(records)} 条"
        )
