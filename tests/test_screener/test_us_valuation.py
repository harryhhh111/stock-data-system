"""美股筛选 PE/PB 自算测试：不使用腾讯 daily_quote.pe_ttm/pb。"""

from unittest.mock import patch

import pandas as pd
import pytest

from quant.screener.query import get_us_universe


class _DummyConn:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TestUSValuation:
    def test_get_us_universe_computes_pe_pb_from_fundamentals(self, monkeypatch):
        # 本用例针对 legacy 路径；Phase B2 开关（.env 可能启用）必须显式关闭
        monkeypatch.delenv("US_SCREENER_SNAPSHOT_CURRENT", raising=False)
        raw = pd.DataFrame({
            "stock_code": ["TDC", "VZ", "LOSS"],
            "market_cap": [2.759953e9, 182.805969e9, 1.0e9],
            "parent_equity": [229.268e6, 105.741e9, 1.0e9],
            "net_profit_ttm": [130.0e6, 17.174e9, -1.0e6],
        })

        with patch("quant.screener.query.Connection", return_value=_DummyConn()), \
             patch("quant.screener.query.pd.read_sql", return_value=raw.copy()) as read_sql:
            out = get_us_universe()

        sql = read_sql.call_args[0][0]
        assert "q.pe_ttm" not in sql
        assert "q.pb" not in sql

        by_code = out.set_index("stock_code")
        assert by_code.loc["TDC", "pe_ttm"] == pytest.approx(21.2304, rel=1e-3)
        assert by_code.loc["VZ", "pb"] == pytest.approx(1.7288, rel=1e-3)
        assert pd.isna(by_code.loc["LOSS", "pe_ttm"])
