"""Russell 1000 成分来源的受控 stale-cache fallback 回归。"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

import core.fetchers.us_financial as us_financial


def _write_cache(path: Path, *, age_days: int, count: int = 1000) -> None:
    path.write_text(json.dumps([f"T{i}" for i in range(count)]))
    then = time.time() - age_days * 86400
    os.utime(path, (then, then))


def _reject_live(*args, **kwargs):
    raise RuntimeError("Wikipedia unavailable")


def test_fresh_cache_is_normal_source_without_network(tmp_path, monkeypatch):
    monkeypatch.setattr(us_financial, "CACHE_DIR", tmp_path)
    _write_cache(tmp_path / "russell1000_tickers.json", age_days=1)
    monkeypatch.setattr(us_financial.requests, "get", _reject_live)

    fetcher = us_financial.USFinancialFetcher()
    assert len(fetcher.fetch_russell1000_constituents()) == 1000
    assert fetcher.get_index_source_status("RUSSELL1000")["mode"] == "fresh_cache"


def test_live_failure_uses_bounded_stale_cache_and_marks_degraded(tmp_path, monkeypatch):
    monkeypatch.setattr(us_financial, "CACHE_DIR", tmp_path)
    _write_cache(tmp_path / "russell1000_tickers.json", age_days=8)
    monkeypatch.setattr(us_financial.requests, "get", _reject_live)
    monkeypatch.setattr(us_financial.config.sec, "russell1000_stale_cache_max_days", 30)

    fetcher = us_financial.USFinancialFetcher()
    assert len(fetcher.fetch_russell1000_constituents()) == 1000
    status = fetcher.get_index_source_status("RUSSELL1000")
    assert status["mode"] == "stale_cache_fallback"
    assert status["max_stale_cache_days"] == 30
    assert 7 < status["cache_age_days"] < 9


def test_live_failure_with_expired_cache_still_blocks(tmp_path, monkeypatch):
    monkeypatch.setattr(us_financial, "CACHE_DIR", tmp_path)
    _write_cache(tmp_path / "russell1000_tickers.json", age_days=31)
    monkeypatch.setattr(us_financial.requests, "get", _reject_live)
    monkeypatch.setattr(us_financial.config.sec, "russell1000_stale_cache_max_days", 30)

    fetcher = us_financial.USFinancialFetcher()
    with pytest.raises(RuntimeError, match="no usable stale cache"):
        fetcher.fetch_russell1000_constituents()
    assert fetcher.get_index_source_status("RUSSELL1000")["mode"] == "stale_cache_expired"


def test_live_failure_with_invalid_cache_still_blocks(tmp_path, monkeypatch):
    monkeypatch.setattr(us_financial, "CACHE_DIR", tmp_path)
    _write_cache(tmp_path / "russell1000_tickers.json", age_days=8, count=3)
    monkeypatch.setattr(us_financial.requests, "get", _reject_live)

    fetcher = us_financial.USFinancialFetcher()
    with pytest.raises(RuntimeError, match="no usable stale cache"):
        fetcher.fetch_russell1000_constituents()
    assert fetcher.get_index_source_status("RUSSELL1000")["mode"] == "no_usable_cache"
