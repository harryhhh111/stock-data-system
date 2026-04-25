import pytest
from datetime import date, datetime
import numpy as np
import pandas as pd
from core.transformers.base import parse_report_date, transform_report_type, REPORT_TYPE_MAP


class TestParseReportDate:
    """parse_report_date 的边界测试。"""

    def test_pandas_timestamp(self):
        ts = pd.Timestamp("2024-03-31")
        assert parse_report_date(ts) == date(2024, 3, 31)

    def test_numpy_datetime64(self):
        dt = np.datetime64("2024-06-30")
        assert parse_report_date(dt) == date(2024, 6, 30)

    def test_python_date(self):
        assert parse_report_date(date(2024, 12, 31)) == date(2024, 12, 31)

    def test_python_datetime(self):
        assert parse_report_date(datetime(2024, 3, 31, 15, 30)) == date(2024, 3, 31)

    def test_string_formats(self):
        assert parse_report_date("2024-03-31") == date(2024, 3, 31)
        assert parse_report_date("2024-03-31 00:00:00") == date(2024, 3, 31)
        assert parse_report_date("20240331") == date(2024, 3, 31)

    def test_none_returns_none(self):
        assert parse_report_date(None) is None

    def test_nan_returns_none(self):
        assert parse_report_date(float("nan")) is None
        assert parse_report_date(pd.NaT) is None

    def test_invalid_string_returns_none(self):
        assert parse_report_date("invalid") is None

    def test_empty_string_returns_none(self):
        assert parse_report_date("") is None
        assert parse_report_date("  ") is None


class TestTransformReportType:
    """transform_report_type 测试。"""

    def test_known_types(self):
        assert transform_report_type("年报") == "annual"
        assert transform_report_type("中报") == "semi"
        assert transform_report_type("一季报") == "quarterly"
        assert transform_report_type("三季报") == "quarterly"

    def test_unknown_type(self):
        assert transform_report_type("unknown") is None
        assert transform_report_type("") is None
