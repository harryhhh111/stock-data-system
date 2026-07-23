"""统一 PE/PB/ROE 计算模块测试。"""

import pytest

from quant.metrics import compute_pb, compute_pe, compute_roe_annual


class TestComputePB:
    def test_pb_from_market_cap_and_equity(self):
        assert compute_pb(182.805969e9, 105.741e9) == pytest.approx(1.7288, rel=1e-3)

    def test_pb_requires_positive_equity(self):
        assert compute_pb(100.0, 0.0) is None
        assert compute_pb(100.0, -5.0) is None

    def test_pb_requires_positive_market_cap(self):
        assert compute_pb(None, 100.0) is None
        assert compute_pb(0.0, 100.0) is None


class TestComputePE:
    def test_pe_from_market_cap_and_ttm_earnings(self):
        assert compute_pe(2.759953e9, 130e6) == pytest.approx(21.2304, rel=1e-3)

    def test_pe_not_shown_for_losses(self):
        assert compute_pe(100.0, 0.0) is None
        assert compute_pe(100.0, -10.0) is None

    def test_pe_requires_valid_inputs(self):
        assert compute_pe(None, 10.0) is None
        assert compute_pe(100.0, None) is None


class TestComputeROEAnnual:
    def test_annual_roe_uses_ending_equity(self):
        assert compute_roe_annual(17.174e9, 105.741e9) == pytest.approx(0.1624, rel=1e-3)

    def test_roe_returns_none_for_missing_or_zero_equity(self):
        assert compute_roe_annual(1.0, None) is None
        assert compute_roe_annual(1.0, 0.0) is None
