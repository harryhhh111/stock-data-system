from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from core.sync.us_market import _filter_pending_us_tickers


def _run(progress_rows, db_rows, filing_rows=None):
    with patch(
        "core.sync.us_market.execute",
        side_effect=[progress_rows, db_rows, filing_rows or []],
    ):
        return _filter_pending_us_tickers(["AAPL", "TDC"], force=False)


def test_force_sync_returns_all_without_querying_database():
    with patch("core.sync.us_market.execute") as execute:
        pending, skipped = _filter_pending_us_tickers(["AAPL", "TDC"], force=True)

    assert pending == ["AAPL", "TDC"]
    assert skipped == 0
    execute.assert_not_called()


def test_recent_successful_sync_is_skipped():
    recent = datetime.now(timezone.utc) - timedelta(days=2)

    pending, skipped = _run(
        [("AAPL", recent), ("TDC", recent)],
        [("AAPL", datetime(2025, 12, 31)), ("TDC", datetime(2025, 12, 31))],
    )

    assert pending == []
    assert skipped == 2


def test_sync_older_than_seven_days_is_rechecked():
    recent = datetime.now(timezone.utc) - timedelta(days=2)
    stale = datetime.now(timezone.utc) - timedelta(days=8)

    pending, skipped = _run(
        [("AAPL", recent), ("TDC", stale)],
        [("AAPL", datetime(2025, 12, 31)), ("TDC", datetime(2025, 12, 31))],
    )

    assert pending == ["TDC"]
    assert skipped == 1


def test_filing_younger_than_sixty_days_is_not_rechecked():
    stale = datetime.now(timezone.utc) - timedelta(days=30)
    recent_filing = datetime.now(timezone.utc).date() - timedelta(days=45)

    pending, skipped = _run(
        [("AAPL", stale), ("TDC", stale)],
        [("AAPL", datetime(2025, 12, 31)), ("TDC", datetime(2025, 12, 31))],
        [("AAPL", recent_filing), ("TDC", recent_filing)],
    )

    assert pending == []
    assert skipped == 2


def test_filing_between_sixty_and_seventy_five_days_uses_fourteen_days():
    twelve_days_ago = datetime.now(timezone.utc) - timedelta(days=12)
    fifteen_days_ago = datetime.now(timezone.utc) - timedelta(days=15)
    mid_cycle_filing = datetime.now(timezone.utc).date() - timedelta(days=65)

    pending, skipped = _run(
        [("AAPL", twelve_days_ago), ("TDC", fifteen_days_ago)],
        [("AAPL", datetime(2025, 12, 31)), ("TDC", datetime(2025, 12, 31))],
        [("AAPL", mid_cycle_filing), ("TDC", mid_cycle_filing)],
    )

    assert pending == ["TDC"]
    assert skipped == 1


def test_filing_older_than_seventy_five_days_uses_seven_days():
    six_days_ago = datetime.now(timezone.utc) - timedelta(days=6)
    eight_days_ago = datetime.now(timezone.utc) - timedelta(days=8)
    old_filing = datetime.now(timezone.utc).date() - timedelta(days=76)

    pending, skipped = _run(
        [("AAPL", six_days_ago), ("TDC", eight_days_ago)],
        [("AAPL", datetime(2025, 12, 31)), ("TDC", datetime(2025, 12, 31))],
        [("AAPL", old_filing), ("TDC", old_filing)],
    )

    assert pending == ["TDC"]
    assert skipped == 1


def test_missing_statement_or_progress_forces_sync():
    recent = datetime.now(timezone.utc) - timedelta(days=2)

    pending, skipped = _run(
        [("AAPL", recent)],
        [("TDC", datetime(2025, 12, 31))],
    )

    assert pending == ["AAPL", "TDC"]
    assert skipped == 0
