"""标准回测可复用预加载数据的回归测试。"""

from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd

from quant.backtest.engine import run_backtest
from quant.backtest import us_pit_source
from quant.backtest.us_pit_source import _CompactFactRow


def test_run_backtest_reuses_supplied_preload_data():
    """成本情景批跑不应为每个场景重新加载 PIT 或调仓行情。"""
    supplied_preloader = MagicMock()
    supplied_preloader.get_universe.return_value = pd.DataFrame()
    supplied_quotes = {date(2024, 1, 31): pd.DataFrame()}

    with (
        patch("quant.backtest.engine.PITPreloader", side_effect=AssertionError),
        patch("quant.backtest.engine.batch_query_quote", side_effect=AssertionError),
        patch("quant.backtest.engine.build_universe", return_value=pd.DataFrame()),
        patch(
            "quant.backtest.engine.apply_hard_filters",
            return_value=(pd.DataFrame(), 0, {}),
        ),
    ):
        result = run_backtest(
            preset_name="fcf_roe_value",
            start=date(2024, 1, 1),
            end=date(2024, 1, 31),
            market="US",
            benchmark="",
            rebalance_dates=[date(2024, 1, 31)],
            preloader=supplied_preloader,
            quote_by_date=supplied_quotes,
        )

    supplied_preloader.get_universe.assert_called_once_with(date(2024, 1, 31))
    assert result.metrics.num_rebalances == 0


def test_compact_fact_row_preserves_selector_mapping_contract():
    row = _CompactFactRow(tuple(range(18)))
    assert row["stock_code"] == 1
    assert row.get("filed_date") == 13
    assert row.get("not_a_column", "fallback") == "fallback"


def test_streaming_selector_uses_bounded_chunks(monkeypatch):
    facts_by_stock = {
        "A": [_CompactFactRow(tuple([1, "A", "income", "revenues", "duration", None, date(2024, 12, 31), "USD", "h", 1, None, "a", "10-K", date(2025, 2, 1), {}, "Revenues", "ctx", None]))],
        "B": [_CompactFactRow(tuple([2, "B", "income", "revenues", "duration", None, date(2024, 12, 31), "USD", "h", 2, None, "b", "10-K", date(2025, 2, 1), {}, "Revenues", "ctx", None]))],
    }

    class Cursor:
        def execute(self, *_args):
            pass

        def fetchall(self):
            return [("A",), ("B",)]

        def close(self):
            pass

    class Conn:
        def cursor(self):
            return Cursor()

    monkeypatch.setattr(us_pit_source, "Connection", lambda: MagicMock(__enter__=lambda _: Conn(), __exit__=lambda *_: None))
    monkeypatch.setattr(
        us_pit_source,
        "_fetch_fact_rows_for_stocks_as_of",
        lambda stocks, *_args: [fact for stock in stocks for fact in facts_by_stock[stock]],
    )

    selected = us_pit_source.select_as_of_from_db(date(2025, 3, 1), chunk_size=1)
    assert [fact.stock_code for fact in selected] == ["A", "B"]
