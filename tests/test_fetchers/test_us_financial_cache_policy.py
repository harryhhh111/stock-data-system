import json

import core.fetchers.us_financial as us_financial
from core.fetchers.us_financial import USFinancialFetcher


class _Response:
    status_code = 200
    headers = {"Last-Modified": "Wed, 29 Jul 2026 00:00:00 GMT"}

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def test_network_recheck_bypasses_valid_company_facts_cache(tmp_path, monkeypatch):
    cached = {"cik": "1", "facts": {"us-gaap": {"Old": {}}}}
    fresh = {"cik": "1", "facts": {"us-gaap": {"Fresh": {}}}}
    (tmp_path / "TEST.json").write_text(json.dumps(cached))

    fetcher = USFinancialFetcher()
    fetcher._company_list_loaded = True
    fetcher._ticker_to_cik = {"TEST": "0000000001"}
    monkeypatch.setattr(us_financial, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(fetcher._rate_limiter, "wait", lambda: None)
    monkeypatch.setattr(fetcher, "_request_sec", lambda _url: _Response(fresh))
    monkeypatch.setattr(us_financial, "get_or_create_raw_snapshot_version", lambda **_kwargs: 1)
    monkeypatch.setattr(us_financial, "save_raw_snapshot_observation", lambda **_kwargs: None)

    result, _context = fetcher.fetch_company_facts_with_context(
        "TEST",
        allow_cache=False,
    )

    assert result == fresh
    assert json.loads((tmp_path / "TEST.json").read_text()) == fresh
