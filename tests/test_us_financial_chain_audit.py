from datetime import date

from core.us_financial_chain_audit import _source_horizon


def test_source_horizon_only_uses_official_periodic_filings():
    raw = {
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            {
                                "end": "2025-12-31",
                                "filed": "2026-02-01",
                                "form": "10-K",
                            },
                            {
                                "end": "2026-03-31",
                                "filed": "2026-05-01",
                                "form": "10-Q",
                            },
                            {
                                "end": "2026-06-30",
                                "filed": "2026-07-20",
                                "form": "8-K",
                            },
                        ]
                    }
                }
            }
        }
    }

    assert _source_horizon(raw) == (
        date(2026, 3, 31),
        date(2026, 5, 1),
    )


def test_source_horizon_handles_empty_or_invalid_dates():
    assert _source_horizon({}) == (None, None)
    raw = {
        "facts": {
            "us-gaap": {
                "Assets": {
                    "units": {
                        "USD": [{
                            "end": "not-a-date",
                            "filed": None,
                            "form": "10-Q",
                        }]
                    }
                }
            }
        }
    }
    assert _source_horizon(raw) == (None, None)
