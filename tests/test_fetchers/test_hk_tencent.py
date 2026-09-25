"""腾讯港股 fetcher 测试（HTTP 全部 mock，不发外网）。"""
import json
from pathlib import Path

import pytest

import core.fetchers.hk_tencent_financial as hk_tencent
from core.fetchers.hk_tencent_financial import TencentHkFinancialFetcher

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "tencent_hk"


def _load(code: str) -> dict:
    with open(FIXTURES / f"{code}.json", encoding="utf-8") as f:
        return json.load(f)


class _FakeResp:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """限流 sleep 归零，避免拖慢测试；间隔约束另由实现保证。"""
    monkeypatch.setattr(hk_tencent.time, "sleep", lambda *_a, **_k: None)


class TestFetchCashflow:
    def test_request_params_and_symbol(self, monkeypatch):
        calls = {}

        def fake_get(url, params=None, headers=None, timeout=None):
            calls["url"] = url
            calls["params"] = params
            calls["headers"] = headers
            return _FakeResp(_load("02020"))

        monkeypatch.setattr(hk_tencent.requests, "get", fake_get)
        fetcher = TencentHkFinancialFetcher()
        payload = fetcher.fetch_cashflow("02020")

        assert "hkcwbb/detail" in calls["url"]
        assert calls["params"] == {
            "num": 12, "type": "xjll", "rttype": "all", "symbol": "hk02020",
        }
        assert "Mozilla/5.0" in calls["headers"]["User-Agent"]
        assert payload["code"] == 0

    def test_save_snapshot_even_when_skip_snapshot(self, monkeypatch):
        """方案要求腾讯快照全量存：skip_snapshot=True 也必须落快照。"""
        saved = {}
        monkeypatch.setattr(
            hk_tencent.requests, "get", lambda *a, **k: _FakeResp(_load("02020"))
        )

        fetcher = TencentHkFinancialFetcher()
        fetcher.skip_snapshot = True

        def spy_save(**kwargs):
            saved.update(kwargs)

        monkeypatch.setattr(fetcher, "save_raw_snapshot", spy_save)
        fetcher.fetch_cashflow("02020")

        assert saved["stock_code"] == "02020"
        assert saved["data_type"] == "cashflow"
        assert saved["source"] == "tencent_hk"
        assert saved["api_params"]["symbol"] == "hk02020"
        assert saved["raw_data"]["code"] == 0
        assert fetcher.skip_snapshot is True  # 调用后恢复原值

    def test_non_zero_code_raises(self, monkeypatch):
        monkeypatch.setattr(
            hk_tencent.requests, "get",
            lambda *a, **k: _FakeResp({"code": 100, "msg": "no such stock"}),
        )
        fetcher = TencentHkFinancialFetcher()
        with pytest.raises(RuntimeError, match="100"):
            fetcher.fetch_cashflow("99999")

    def test_request_failure_records_and_raises(self, monkeypatch):
        from tenacity import RetryError

        def boom(*a, **k):
            raise ConnectionError("boom")

        monkeypatch.setattr(hk_tencent.requests, "get", boom)
        fetcher = TencentHkFinancialFetcher()
        # retry_with_backoff 重试耗尽后抛 RetryError，内嵌原始 ConnectionError
        with pytest.raises(RetryError) as exc_info:
            fetcher.fetch_cashflow("02020")
        assert isinstance(exc_info.value.last_attempt.exception(), ConnectionError)

    def test_min_interval_between_requests(self, monkeypatch):
        """请求间隔 ≥0.5s：专用限流器 base_delay=0.5，且每次请求前必过限流。"""
        assert hk_tencent._tencent_rate_limiter._base_delay >= 0.5

        order = []
        monkeypatch.setattr(
            hk_tencent.requests, "get",
            lambda *a, **k: (order.append("get"), _FakeResp(_load("02020")))[1],
        )
        real_wait = hk_tencent.AdaptiveRateLimiter.wait
        monkeypatch.setattr(
            hk_tencent.AdaptiveRateLimiter, "wait",
            lambda self: (order.append("wait"), real_wait(self))[1],
        )
        monkeypatch.setattr(
            hk_tencent.TencentHkFinancialFetcher, "save_raw_snapshot", lambda *a, **k: None
        )

        fetcher = TencentHkFinancialFetcher()
        fetcher.fetch_cashflow("02020")
        fetcher.fetch_cashflow("02020")
        assert order == ["wait", "get", "wait", "get"]
