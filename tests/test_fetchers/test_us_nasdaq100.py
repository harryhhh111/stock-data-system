"""
tests/test_fetchers/test_us_nasdaq100.py — NASDAQ 100 成分股获取测试
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest


# ── fetch_nasdaq100_constituents 单元测试 ──────────────────────


class TestFetchNasdaq100Constituents:
    """测试 fetch_nasdaq100_constituents 核心逻辑。"""

    @pytest.fixture
    def fetcher(self):
        """创建 USFinancialFetcher 实例。"""
        from core.fetchers.us_financial import USFinancialFetcher
        return USFinancialFetcher()

    @pytest.fixture
    def mock_wikipedia_df(self):
        """模拟 Wikipedia 返回的 DataFrame（通过 pd.read_html 解析后）。

        提供 101 只 ticker 以满足 >=80 的阈值检查。
        """
        # 生成足够多的 ticker 以通过 len(tickers) >= 80 的检查
        base_tickers = [
            "AAPL", "MSFT", "BRK.B", "NVDA", "GOOG", "AMZN", "META",
            "TSLA", "AVGO", "AMD", "ADBE", "NFLX", "INTC", "PYPL",
        ]
        # 补充到 101 只
        for i in range(101 - len(base_tickers)):
            base_tickers.append(f"TST{i:03d}")

        return [
            # 第一张表：无关表格
            pd.DataFrame({"Col1": ["a", "b"], "Col2": ["c", "d"]}),
            # 第二张表：包含 Symbol 列的 NASDAQ-100 成分股表
            pd.DataFrame({
                "Company": [f"Company_{t}" for t in base_tickers],
                "Symbol": base_tickers,
                "Sector": ["Technology"] * len(base_tickers),
            }),
        ]

    @patch("core.fetchers.us_financial.pd.read_html")
    @patch("core.fetchers.us_financial.requests.get")
    def test_wikipedia_success(self, mock_get, mock_read_html, fetcher, mock_wikipedia_df):
        """测试从 Wikipedia 成功获取 NASDAQ 100 成分股。"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "<html>mock</html>"
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp
        mock_read_html.return_value = mock_wikipedia_df

        with patch.object(fetcher, "_load_cache", return_value=False):
            with patch.object(fetcher, "_save_cache"):
                tickers = fetcher.fetch_nasdaq100_constituents()

        assert len(tickers) == 101
        # BRK.B 应该被替换为 BRK-B（与 SEC company_tickers.json 一致）
        assert "BRK-B" in tickers
        assert "BRK.B" not in tickers
        assert "AAPL" in tickers
        assert "MSFT" in tickers
        assert "NVDA" in tickers

    def test_fallback_json_file(self, fetcher):
        """测试 Wikipedia 失败且无缓存时，从内置 fallback JSON 加载。"""
        with patch.object(fetcher, "_load_cache", return_value=False):
            with patch("core.fetchers.us_financial.requests.get", side_effect=Exception("network error")):
                # fallback 文件存在，应该能加载
                tickers = fetcher.fetch_nasdaq100_constituents()
                assert isinstance(tickers, list)
                assert len(tickers) >= 80

    def test_fallback_file_not_found_raises(self, fetcher):
        """测试所有数据源都失败时抛出 RuntimeError。"""
        with patch.object(fetcher, "_load_cache", return_value=False):
            with patch("core.fetchers.us_financial.requests.get", side_effect=Exception("network error")):
                # Patch Path.exists to simulate missing fallback file
                real_exists = Path.exists
                def patched_exists(self_path):
                    if "nasdaq100_tickers.json" in str(self_path):
                        return False
                    return real_exists(self_path)
                with patch.object(Path, "exists", patched_exists):
                    with pytest.raises(RuntimeError, match="所有数据源均失败"):
                        fetcher.fetch_nasdaq100_constituents()

    def test_cache_hit(self, fetcher, tmp_path):
        """测试缓存命中时直接返回缓存数据。"""
        cached_tickers = ["AAPL", "MSFT", "GOOG"]
        cache_file = tmp_path / "nasdaq100_tickers.json"
        cache_file.write_text(json.dumps(cached_tickers))

        with patch("core.fetchers.us_financial.CACHE_DIR", tmp_path):
            with patch.object(fetcher, "_load_cache", return_value=True):
                # _load_cache 返回 True 后，函数直接读 cache_file.read_text()
                tickers = fetcher.fetch_nasdaq100_constituents()

        assert tickers == cached_tickers

    @patch("core.fetchers.us_financial.pd.read_html")
    @patch("core.fetchers.us_financial.requests.get")
    def test_dot_to_dash_replacement(self, mock_get, mock_read_html, fetcher):
        """测试 ticker 中的 . 被替换为 -（如 BRK.B → BRK-B）。"""
        # 生成 101 只以确保通过 >=80 阈值
        base = ["BRK.B", "AAPL", "BF-B"]
        for i in range(101 - len(base)):
            base.append(f"TST{i:03d}")

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp
        mock_read_html.return_value = [
            pd.DataFrame({
                "Symbol": base,
            }),
        ]

        with patch.object(fetcher, "_load_cache", return_value=False):
            with patch.object(fetcher, "_save_cache"):
                tickers = fetcher.fetch_nasdaq100_constituents()

        assert "BRK-B" in tickers
        assert "BF-B" in tickers
        assert "BRK.B" not in tickers

    @patch("core.fetchers.us_financial.pd.read_html")
    @patch("core.fetchers.us_financial.requests.get")
    def test_deduplication(self, mock_get, mock_read_html, fetcher):
        """测试重复的 ticker 会被去重。"""
        # 用重复项构建足够多的 ticker
        base = ["AAPL", "AAPL", "MSFT", "MSFT", "GOOG"]
        for i in range(101 - len(base)):
            base.append(f"TST{i:03d}")

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp
        mock_read_html.return_value = [
            pd.DataFrame({
                "Symbol": base,
            }),
        ]

        with patch.object(fetcher, "_load_cache", return_value=False):
            with patch.object(fetcher, "_save_cache"):
                tickers = fetcher.fetch_nasdaq100_constituents()

        # 去重后应有 101 - 2 = 99（去掉了2个重复的 AAPL 和 MSFT）
        assert len(tickers) == 99
        # 验证重复项已去除
        assert tickers.count("AAPL") == 1
        assert tickers.count("MSFT") == 1
        assert tickers.count("GOOG") == 1

    def test_fallback_json_exists_and_readable(self):
        """测试 data/nasdaq100_tickers.json 文件存在且可正确解析。"""
        project_root = Path(__file__).resolve().parent.parent.parent
        fallback_path = project_root / "data" / "nasdaq100_tickers.json"
        assert fallback_path.exists(), f"内置 fallback 文件不存在: {fallback_path}"

        tickers = json.loads(fallback_path.read_text())
        assert isinstance(tickers, list)
        assert len(tickers) >= 80, f"NASDAQ-100 fallback 列表应至少包含 80 只股票，实际 {len(tickers)}"

        # 所有 ticker 应为大写
        for t in tickers:
            assert t == t.upper(), f"ticker 不全大写: {t}"
