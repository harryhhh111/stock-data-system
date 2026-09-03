"""PaperPreloader 的 US PIT 切换回归测试。"""

from __future__ import annotations

from datetime import date
from inspect import getsource
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from quant.paper import preloader as paper_preloader


AS_OF = date(2026, 8, 28)


def test_us_uses_one_streaming_pit_preloader_for_universe_and_roe(monkeypatch):
    """US 不能触碰 legacy SQL；同一实例的两类请求复用一个流式 loader。"""
    monkeypatch.setenv("US_BACKTEST_PIT_VERSION", "1")
    streamed = MagicMock()
    streamed.get_universe.return_value = pd.DataFrame({
        "stock_code": ["AAA"],
        "net_profit_ttm": ["12.5"],
        "roe": ["0.2"],
        "report_date": [AS_OF],
    })
    streamed.get_roe_history.return_value = pd.DataFrame({
        "stock_code": ["AAA"], "report_date": [AS_OF], "roe": ["0.2"],
    })
    factory = MagicMock(return_value=streamed)
    monkeypatch.setattr("quant.backtest.preloader.PITPreloader", factory)
    legacy_universe = MagicMock(side_effect=AssertionError("不得读取 legacy universe SQL"))
    legacy_roe = MagicMock(side_effect=AssertionError("不得读取 legacy ROE SQL"))
    monkeypatch.setattr(paper_preloader, "get_point_in_time_universe", legacy_universe)
    monkeypatch.setattr(paper_preloader, "get_roe_history_as_of", legacy_roe)

    loader = paper_preloader.PaperPreloader("US")
    universe = loader.get_universe(AS_OF)
    roe_history = loader.get_roe_history(AS_OF, 3)

    factory.assert_called_once_with("US", pit_streaming=True)
    streamed.load.assert_called_once_with()
    streamed.get_universe.assert_called_once_with(AS_OF)
    streamed.get_roe_history.assert_called_once_with(AS_OF, 3)
    assert universe["net_profit_ttm"].dtype == np.dtype("float64")
    assert universe["roe"].dtype == np.dtype("float64")
    assert pd.isna(universe.loc[0, "cfo_ttm"])
    assert roe_history["roe"].dtype == np.dtype("float64")
    legacy_universe.assert_not_called()
    legacy_roe.assert_not_called()


def test_us_without_versioned_pit_fails_before_legacy_sql(monkeypatch):
    """开关关闭时必须给迁移说明，不能延迟到 UndefinedTable。"""
    monkeypatch.delenv("US_BACKTEST_PIT_VERSION", raising=False)
    legacy = MagicMock(side_effect=AssertionError("不应调用 legacy SQL"))
    monkeypatch.setattr(paper_preloader, "get_point_in_time_universe", legacy)

    with pytest.raises(RuntimeError, match="US_BACKTEST_PIT_VERSION=1"):
        paper_preloader.PaperPreloader("US").get_universe(AS_OF)
    legacy.assert_not_called()


def test_cn_path_remains_legacy_sql_driven(monkeypatch):
    """本修复只影响 US；CN 继续使用原有轻量 SQL 路径。"""
    expected = pd.DataFrame({"stock_code": ["000001"], "roe": ["0.1"]})
    universe = MagicMock(return_value=expected)
    roe = MagicMock(return_value=pd.DataFrame({
        "stock_code": ["000001"], "report_date": [AS_OF], "roe": ["0.1"],
    }))
    monkeypatch.setattr(paper_preloader, "get_point_in_time_universe", universe)
    monkeypatch.setattr(paper_preloader, "get_roe_history_as_of", roe)

    loader = paper_preloader.PaperPreloader("CN_A")
    result = loader.get_universe(AS_OF)
    hist = loader.get_roe_history(AS_OF, 3)

    universe.assert_called_once_with(AS_OF, market="CN_A")
    roe.assert_called_once_with(AS_OF, "CN_A", 3)
    assert result.loc[0, "roe"] == pytest.approx(0.1)
    assert hist.loc[0, "roe"] == pytest.approx(0.1)
    assert loader._us_preloader is None


def test_normalise_preserves_real_values_and_uses_nan_for_missing_columns():
    result = paper_preloader.PaperPreloader._normalise_universe(pd.DataFrame({
        "stock_code": ["AAA"], "total_shares": ["100"], "fcf": [None],
        "annual_fcf": ["20.5"],
    }))

    assert result.loc[0, "total_shares"] == pytest.approx(100.0)
    assert result.loc[0, "annual_fcf"] == pytest.approx(20.5)
    assert pd.isna(result.loc[0, "fcf"])
    assert pd.isna(result.loc[0, "net_profit_ttm"])


def test_paper_preloader_contains_no_retired_us_financial_objects():
    source = getsource(paper_preloader)
    for retired_object in (
        "mv_us_financial_indicator",
        "us_income_statement",
        "us_cash_flow_statement",
    ):
        assert retired_object not in source
