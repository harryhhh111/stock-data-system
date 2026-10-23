"""Russell 1000 成分来源的受控 fallback 链回归。

链：fresh cache(7d) → live Wikipedia(带 sanity 校验) → 有界 stale cache(30d)
→ 内置策展快照 → 显式失败。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pandas as pd
import pytest

import core.fetchers.us_financial as us_financial


def _write_cache(path: Path, *, age_days: int, count: int = 1000) -> None:
    path.write_text(json.dumps([f"T{i}" for i in range(count)]))
    then = time.time() - age_days * 86400
    os.utime(path, (then, then))


def _write_builtin(path: Path, *, count: int = 1000) -> None:
    path.write_text(json.dumps([f"B{i}" for i in range(count)]))


def _reject_live(*args, **kwargs):
    raise RuntimeError("Wikipedia unavailable")


class _StubResp:
    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self):
        pass


def _live_html(tickers: list[str]) -> str:
    return pd.DataFrame({"Symbol": tickers}).to_html(index=False)


@pytest.fixture
def env(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    builtin = tmp_path / "builtin.json"
    _write_builtin(builtin)
    monkeypatch.setattr(us_financial, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(us_financial, "RUSSELL1000_BUILTIN_PATH", builtin)
    return cache_dir, builtin


def test_fresh_cache_is_normal_source_without_network(env, monkeypatch):
    cache_dir, _ = env
    _write_cache(cache_dir / "russell1000_tickers.json", age_days=1)
    monkeypatch.setattr(us_financial.requests, "get", _reject_live)

    fetcher = us_financial.USFinancialFetcher()
    assert len(fetcher.fetch_russell1000_constituents()) == 1000
    assert fetcher.get_index_source_status("RUSSELL1000")["mode"] == "fresh_cache"


def test_live_failure_uses_bounded_stale_cache_and_marks_degraded(env, monkeypatch):
    cache_dir, _ = env
    _write_cache(cache_dir / "russell1000_tickers.json", age_days=8)
    monkeypatch.setattr(us_financial.requests, "get", _reject_live)
    monkeypatch.setattr(us_financial.config.sec, "russell1000_stale_cache_max_days", 30)

    fetcher = us_financial.USFinancialFetcher()
    assert len(fetcher.fetch_russell1000_constituents()) == 1000
    status = fetcher.get_index_source_status("RUSSELL1000")
    assert status["mode"] == "stale_cache_fallback"
    assert status["max_stale_cache_days"] == 30
    assert 7 < status["cache_age_days"] < 9


def test_live_failure_with_expired_cache_falls_back_to_builtin_snapshot(
    env, monkeypatch
):
    cache_dir, builtin = env
    _write_cache(cache_dir / "russell1000_tickers.json", age_days=31)
    monkeypatch.setattr(us_financial.requests, "get", _reject_live)
    monkeypatch.setattr(us_financial.config.sec, "russell1000_stale_cache_max_days", 30)

    fetcher = us_financial.USFinancialFetcher()
    assert len(fetcher.fetch_russell1000_constituents()) == 1000
    status = fetcher.get_index_source_status("RUSSELL1000")
    assert status["mode"] == "builtin_snapshot"
    assert status["ticker_count"] == 1000


def test_expired_cache_without_builtin_still_blocks(env, monkeypatch):
    cache_dir, builtin = env
    builtin.unlink()  # 无内置快照时维持显式失败
    _write_cache(cache_dir / "russell1000_tickers.json", age_days=31)
    monkeypatch.setattr(us_financial.requests, "get", _reject_live)
    monkeypatch.setattr(us_financial.config.sec, "russell1000_stale_cache_max_days", 30)

    fetcher = us_financial.USFinancialFetcher()
    with pytest.raises(RuntimeError, match="no usable stale cache"):
        fetcher.fetch_russell1000_constituents()
    assert fetcher.get_index_source_status("RUSSELL1000")["mode"] == "no_usable_source"


def test_live_failure_with_invalid_cache_uses_builtin(env, monkeypatch):
    cache_dir, _ = env
    _write_cache(cache_dir / "russell1000_tickers.json", age_days=8, count=3)
    monkeypatch.setattr(us_financial.requests, "get", _reject_live)

    fetcher = us_financial.USFinancialFetcher()
    assert len(fetcher.fetch_russell1000_constituents()) == 1000
    assert fetcher.get_index_source_status("RUSSELL1000")["mode"] == "builtin_snapshot"


def test_live_valid_list_is_accepted(env, monkeypatch):
    cache_dir, builtin = env
    builtin_list = sorted({f"B{i}" for i in range(1000)})
    _write_builtin(builtin)
    live = sorted(set(builtin_list) | {"NEW1", "NEW2"})
    monkeypatch.setattr(
        us_financial.requests, "get", lambda *a, **k: _StubResp(_live_html(live))
    )

    fetcher = us_financial.USFinancialFetcher()
    tickers = fetcher.fetch_russell1000_constituents()
    assert len(tickers) == 1002
    assert tickers[:2] == ["B0", "B1"]
    status = fetcher.get_index_source_status("RUSSELL1000")
    assert status["mode"] == "live_wikipedia"
    # 通过的 live 名单写回缓存
    assert (cache_dir / "russell1000_tickers.json").exists()


def test_live_list_with_low_overlap_is_rejected_and_falls_back(env, monkeypatch):
    cache_dir, builtin = env
    _write_cache(cache_dir / "russell1000_tickers.json", age_days=8)
    _write_builtin(builtin)
    live = [f"X{i}" for i in range(1000)]  # 数量合法但与快照零重叠
    monkeypatch.setattr(
        us_financial.requests, "get", lambda *a, **k: _StubResp(_live_html(live))
    )
    monkeypatch.setattr(us_financial.config.sec, "russell1000_stale_cache_max_days", 30)

    fetcher = us_financial.USFinancialFetcher()
    # 拒绝 live 名单 → 回退到 8 天旧缓存
    assert len(fetcher.fetch_russell1000_constituents()) == 1000
    status = fetcher.get_index_source_status("RUSSELL1000")
    assert status["mode"] == "stale_cache_fallback"
    assert status["ticker_count"] == 1000


def test_live_list_with_absurd_count_is_rejected_and_falls_back(env, monkeypatch):
    cache_dir, builtin = env
    _write_cache(cache_dir / "russell1000_tickers.json", age_days=8)
    _write_builtin(builtin)
    live = [f"B{i}" for i in range(50)]  # 远低于正常成分数
    monkeypatch.setattr(
        us_financial.requests, "get", lambda *a, **k: _StubResp(_live_html(live))
    )
    monkeypatch.setattr(us_financial.config.sec, "russell1000_stale_cache_max_days", 30)

    fetcher = us_financial.USFinancialFetcher()
    assert len(fetcher.fetch_russell1000_constituents()) == 1000
    assert fetcher.get_index_source_status("RUSSELL1000")["mode"] == "stale_cache_fallback"


def test_stale_builtin_snapshot_warns_even_on_fresh_cache(env, monkeypatch, caplog):
    """内置快照超过 90 天未更新时提醒——即使本次走鲜缓存不经过兜底路径。"""
    cache_dir, builtin = env
    _write_cache(cache_dir / "russell1000_tickers.json", age_days=1)
    _write_builtin(builtin)
    old = time.time() - 100 * 86400
    os.utime(builtin, (old, old))
    monkeypatch.setattr(us_financial.requests, "get", _reject_live)

    fetcher = us_financial.USFinancialFetcher()
    with caplog.at_level("WARNING", logger=us_financial.logger.name):
        fetcher.fetch_russell1000_constituents()
    assert any("内置快照已" in r.message and "未更新" in r.message
               for r in caplog.records)


def test_recent_builtin_snapshot_does_not_warn(env, monkeypatch, caplog):
    cache_dir, _ = env
    _write_cache(cache_dir / "russell1000_tickers.json", age_days=1)
    monkeypatch.setattr(us_financial.requests, "get", _reject_live)

    fetcher = us_financial.USFinancialFetcher()
    with caplog.at_level("WARNING", logger=us_financial.logger.name):
        fetcher.fetch_russell1000_constituents()
    assert not any("内置快照" in r.message for r in caplog.records)


def test_as_of_date_wins_over_fresh_mtime(env, monkeypatch, caplog):
    """as-of 日期优先于文件 mtime：重新检出/复制会重置 mtime，
    快照年龄必须按随快照保存的实际更新日期计算。"""
    cache_dir, builtin = env
    _write_cache(cache_dir / "russell1000_tickers.json", age_days=1)
    # 新格式快照：as_of 120 天前，但文件刚写入（mtime 极新）
    builtin.write_text(json.dumps({
        "as_of": "2026-06-23",
        "tickers": [f"B{i}" for i in range(1000)],
    }))
    monkeypatch.setattr(us_financial.requests, "get", _reject_live)

    fetcher = us_financial.USFinancialFetcher()
    with caplog.at_level("WARNING", logger=us_financial.logger.name):
        fetcher.fetch_russell1000_constituents()
    assert any("内置快照已" in r.message and "未更新" in r.message
               for r in caplog.records)


def test_as_of_format_loads_and_legacy_format_compatible(tmp_path):
    """新 {"as_of", "tickers"} 与旧纯数组格式均可加载；年龄口径：
    新格式按 as_of，旧格式退回 mtime。"""
    from datetime import date
    new_file = tmp_path / "new.json"
    new_file.write_text(json.dumps({
        "as_of": date.today().isoformat(), "tickers": [f"T{i}" for i in range(900)]}))
    tickers = us_financial.USFinancialFetcher._load_russell1000_cache(new_file)
    assert len(tickers) == 900
    assert us_financial.USFinancialFetcher._snapshot_as_of(new_file) == date.today()

    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps([f"T{i}" for i in range(900)]))
    assert len(us_financial.USFinancialFetcher._load_russell1000_cache(legacy)) == 900
    assert us_financial.USFinancialFetcher._snapshot_as_of(legacy) is None
    assert us_financial.USFinancialFetcher._snapshot_age_days(legacy) is not None
