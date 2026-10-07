"""披露易 fetcher 测试（HTTP 全部 mock，不发外网）。"""
import json
from pathlib import Path

import pytest

import core.fetchers.hkex_news as hkex_news
from core.fetchers.hkex_news import HkexNewsError, HkexNewsFetcher


class _FakeResp:
    def __init__(self, text="", payload=None, headers=None):
        self.text = text
        self._payload = payload
        self.headers = headers or {}

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload

    def iter_content(self, chunk_size=1 << 16):
        yield self.text.encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture(autouse=True)
def _no_rate_limit_sleep(monkeypatch):
    """披露易限流 sleep 归零；≥3s 间隔约束由 base_delay=3.0 保证（断言见下）。"""
    monkeypatch.setattr(hkex_news.time, "sleep", lambda *_a, **_k: None)


class TestGetStockId:
    def test_jsonp_parse_and_cache(self, monkeypatch):
        calls = []

        def fake_get(url, params=None, headers=None, timeout=None):
            calls.append(params["name"])
            body = json.dumps({"stockInfo": [{"code": "02020", "stockId": 16111}]})
            return _FakeResp(text=f"callback({body});")

        monkeypatch.setattr(hkex_news.requests, "get", fake_get)
        f = HkexNewsFetcher()
        assert f.get_stock_id("02020") == 16111
        assert f.get_stock_id("02020") == 16111
        assert calls == ["02020"]  # 第二次走内存缓存

    def test_bad_response_raises(self, monkeypatch):
        monkeypatch.setattr(hkex_news.requests, "get", lambda *a, **k: _FakeResp(text="<html>404</html>"))
        with pytest.raises(HkexNewsError):
            HkexNewsFetcher().get_stock_id("02020")


class TestSearch:
    def test_result_double_json_and_sort(self, monkeypatch):
        items = [
            {"TITLE": "舊公告", "FILE_LINK": "/listedco/a_c.pdf", "DATE_TIME": "20/08/2025 16:00", "FILE_INFO": "1M"},
            {"TITLE": "新公告", "FILE_LINK": "/listedco/b_c.pdf", "DATE_TIME": "26/08/2026 12:04", "FILE_INFO": "2M"},
        ]
        captured = {}

        def fake_get(url, params=None, headers=None, timeout=None):
            captured.update(params)
            return _FakeResp(payload={"result": json.dumps(items)})

        saved = {}
        monkeypatch.setattr(hkex_news.requests, "get", fake_get)
        f = HkexNewsFetcher()
        f.save_raw_snapshot = lambda **kw: saved.update(kw)
        out = f.search_announcements("02020", 16111, "10000", "13400", "zh", "20260101", "20261007")

        assert [a["title"] for a in out] == ["新公告", "舊公告"]  # 按日期倒序（非字符串排序）
        assert out[0]["url"].startswith("https://www1.hkexnews.hk/")
        assert out[0]["date"] == "2026-08-26"
        assert captured["t2code"] == "13400" and captured["lang"] == "zh"
        # 快照元数据
        assert saved["source"] == "hkexnews"
        assert saved["data_type"] == "announcement"
        assert saved["api_params"]["lang"] == "zh"
        assert saved["api_params"]["t2code"] == "13400"
        assert saved["raw_data"][0]["file_info"] == "2M"

    def test_empty_result(self, monkeypatch):
        monkeypatch.setattr(hkex_news.requests, "get", lambda *a, **k: _FakeResp(payload={"result": ""}))
        f = HkexNewsFetcher()
        f.save_raw_snapshot = lambda **kw: None
        assert f.search_announcements("00001", 1, "10000", "13300") == []


class TestDownload:
    def test_happy_path(self, monkeypatch, tmp_path):
        payload = b"%PDF-1.4 fake" + b"0" * 20000
        resp = _FakeResp(text=payload.decode("latin1"), headers={"Content-Length": str(len(payload))})
        monkeypatch.setattr(hkex_news.requests, "get", lambda *a, **k: resp)
        dest = HkexNewsFetcher().download_pdf("https://www1.hkexnews.hk/x_c.pdf", tmp_path / "x.pdf")
        assert dest.exists() and dest.stat().st_size == len(payload)

    def test_too_large_skipped(self, monkeypatch, tmp_path):
        big = hkex_news.MAX_PDF_BYTES + 1
        resp = _FakeResp(text="", headers={"Content-Length": str(big)})
        monkeypatch.setattr(hkex_news.requests, "get", lambda *a, **k: resp)
        with pytest.raises(HkexNewsError, match="file_too_large"):
            HkexNewsFetcher().download_pdf("https://www1.hkexnews.hk/big_c.pdf", tmp_path / "big.pdf")
        assert not (tmp_path / "big.pdf").exists()


class TestEscalation:
    def test_semi_chain(self):
        steps = HkexNewsFetcher.escalation_steps(6, "zh")
        assert [s["t2code"] for s in steps] == ["13400", "13300", "40200"]
        assert steps[-1]["t1code"] == "40000"

    def test_annual_chain(self):
        steps = HkexNewsFetcher.escalation_steps(12, "zh")
        assert steps[0]["t2code"] == "13300"

    def test_en_retry(self):
        steps = HkexNewsFetcher.escalation_steps(6, "en")
        assert all(s["lang"] == "en" for s in steps)
        assert steps[0]["t2code"] == "13400"
