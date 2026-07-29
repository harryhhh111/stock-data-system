from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from core.sync.us_market import _filter_pending_us_tickers


def _run(progress_rows, db_rows):
    with patch(
        "core.sync.us_market.execute",
        side_effect=[progress_rows, db_rows],
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


def test_missing_statement_or_progress_forces_sync():
    recent = datetime.now(timezone.utc) - timedelta(days=2)

    pending, skipped = _run(
        [("AAPL", recent)],
        [("TDC", datetime(2025, 12, 31))],
    )

    assert pending == ["AAPL", "TDC"]
    assert skipped == 0
