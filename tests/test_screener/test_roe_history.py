"""ROE 历史连续性测试：缺失年份不能被更老年份顶替。"""

from datetime import date

import pandas as pd

from quant.screener.filters import filter_consecutive_roe, pivot_roe_history


class TestRoeHistoryContinuity:
    def test_null_roe_year_fails_instead_of_using_old_year(self):
        """VZ case：2023 ROE 缺失时，2014 的高 ROE 不得顶成“前年”。"""
        df = pd.DataFrame({
            "stock_code": ["VZ"],
            "roe": [0.1624],
        })
        roe_hist = pd.DataFrame({
            "stock_code": ["VZ", "VZ", "VZ", "VZ"],
            "report_date": [
                date(2025, 12, 31),
                date(2024, 12, 31),
                date(2023, 12, 31),
                date(2014, 12, 31),
            ],
            "roe": [0.1624, 0.1741, None, 0.7038],
        })

        filtered, _, n_after = filter_consecutive_roe(df, roe_hist, 3, 0.12)
        assert filtered.empty
        assert n_after == 0

        shown = pivot_roe_history(df, roe_hist, 3)
        assert shown.loc[0, "roe_1y_ago"] == 0.1741
        assert pd.isna(shown.loc[0, "roe_2y_ago"])

    def test_unsorted_history_is_sorted_before_pivot(self):
        """即使输入顺序乱，也应按 report_date 倒序取上年/前年。"""
        df = pd.DataFrame({"stock_code": ["AAA"], "roe": [0.20]})
        roe_hist = pd.DataFrame({
            "stock_code": ["AAA", "AAA", "AAA"],
            "report_date": [
                date(2023, 12, 31),
                date(2025, 12, 31),
                date(2024, 12, 31),
            ],
            "roe": [0.12, 0.20, 0.15],
        })

        shown = pivot_roe_history(df, roe_hist, 3)
        assert shown.loc[0, "roe_1y_ago"] == 0.15
        assert shown.loc[0, "roe_2y_ago"] == 0.12
