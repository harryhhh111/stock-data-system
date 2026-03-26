import pytest
import pandas as pd
from datetime import date, datetime


@pytest.fixture
def sample_income_data():
    """东方财富利润表原始 DataFrame 样本。"""
    return pd.DataFrame({
        "REPORT_DATE": ["2024-09-30", "2024-06-30", "2023-12-31"],
        "REPORT_TYPE_NAME": ["三季报", "中报", "年报"],
        "BASIC_EPS": [2.50, 1.80, 3.00],
        "OPERATE_INCOME": [5000000000, 3500000000, 6000000000],
    })


@pytest.fixture
def sample_sec_facts():
    """SEC Company Facts JSON 样本（精简版 AAPL）。"""
    return {
        "cik": "0000320193",
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {"USD": [
                        {"val": 94928000000, "end": "2024-03-30", "fp": "Q2", "fy": 2024, "filed": "2024-05-03", "accn": "0000320193-24-000010", "frame": "CY2024Q2"},
                        {"val": 90753000000, "end": "2023-12-30", "fp": "Q1", "fy": 2024, "filed": "2024-02-02", "accn": "0000320193-24-000004", "frame": "CY2024Q1"},
                    ]}
                },
                "NetIncomeLoss": {
                    "units": {"USD": [
                        {"val": 23636000000, "end": "2024-03-30", "fp": "Q2", "fy": 2024, "filed": "2024-05-03", "accn": "0000320193-24-000010", "frame": "CY2024Q2"},
                        {"val": 16900000000, "end": "2023-12-30", "fp": "Q1", "fy": 2024, "filed": "2024-02-02", "accn": "0000320193-24-000004", "frame": "CY2024Q1"},
                    ]}
                },
                "GrossProfit": {
                    "units": {"USD": [
                        {"val": 41756000000, "end": "2024-03-30", "fp": "Q2", "fy": 2024, "filed": "2024-05-03", "accn": "0000320193-24-000010", "frame": "CY2024Q2"},
                    ]}
                },
            }
        }
    }
