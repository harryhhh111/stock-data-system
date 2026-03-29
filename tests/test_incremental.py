"""
tests/test_incremental.py — 增量同步逻辑测试

测试 incremental.py 的核心函数：
- get_stocks_max_report_date: 批量查询最大报告期
- determine_stocks_to_sync: 增量判断
- update_last_report_date: 更新同步进度
"""
import pytest
from datetime import date
from unittest.mock import patch, MagicMock, call


class TestDetermineStocksToSync:
    """增量同步判断核心逻辑测试。"""

    def test_force_mode_returns_all(self):
        """force=True 时返回全部股票。"""
        from incremental import determine_stocks_to_sync

        stocks = [("000001", "CN_A"), ("000002", "CN_A"), ("00700", "CN_HK")]
        pending, skipped = determine_stocks_to_sync(stocks, force=True)

        assert len(pending) == 3
        assert skipped == 0

    @patch("incremental.get_sync_progress_report_dates")
    @patch("incremental.get_stocks_max_report_date")
    def test_all_stocks_have_same_dates_are_skipped(self, mock_db_max, mock_progress):
        """DB 中最新报告期 = sync_progress 记录 → 跳过。"""
        from incremental import determine_stocks_to_sync

        mock_db_max.return_value = {
            "000001": date(2024, 9, 30),
            "000002": date(2024, 9, 30),
        }
        mock_progress.return_value = {
            "000001": date(2024, 9, 30),
            "000002": date(2024, 9, 30),
        }

        stocks = [("000001", "CN_A"), ("000002", "CN_A")]
        pending, skipped = determine_stocks_to_sync(stocks, force=False)

        assert len(pending) == 0
        assert skipped == 2

    @patch("incremental.get_sync_progress_report_dates")
    @patch("incremental.get_stocks_max_report_date")
    def test_new_report_date_triggers_sync(self, mock_db_max, mock_progress):
        """DB 中有更新的报告期 → 需要同步。"""
        from incremental import determine_stocks_to_sync

        mock_db_max.return_value = {
            "000001": date(2024, 12, 31),
            "000002": date(2024, 9, 30),
        }
        mock_progress.return_value = {
            "000001": date(2024, 9, 30),  # DB 有更新
            "000002": date(2024, 9, 30),  # 相同，跳过
        }

        stocks = [("000001", "CN_A"), ("000002", "CN_A")]
        pending, skipped = determine_stocks_to_sync(stocks, force=False)

        assert len(pending) == 1
        assert pending[0] == ("000001", "CN_A")
        assert skipped == 1

    @patch("incremental.get_sync_progress_report_dates")
    @patch("incremental.get_stocks_max_report_date")
    def test_no_db_data_means_new_stock(self, mock_db_max, mock_progress):
        """财务表中无数据 → 新股票，必须同步。"""
        from incremental import determine_stocks_to_sync

        mock_db_max.return_value = {}  # 无数据
        mock_progress.return_value = {}

        stocks = [("688001", "CN_A")]
        pending, skipped = determine_stocks_to_sync(stocks, force=False)

        assert len(pending) == 1
        assert skipped == 0

    @patch("incremental.get_sync_progress_report_dates")
    @patch("incremental.get_stocks_max_report_date")
    def test_no_progress_record_means_first_sync(self, mock_db_max, mock_progress):
        """sync_progress 无记录 → 首次同步。"""
        from incremental import determine_stocks_to_sync

        mock_db_max.return_value = {"000001": date(2024, 9, 30)}
        mock_progress.return_value = {}  # 无记录

        stocks = [("000001", "CN_A")]
        pending, skipped = determine_stocks_to_sync(stocks, force=False)

        assert len(pending) == 1
        assert skipped == 0

    @patch("incremental.get_sync_progress_report_dates")
    @patch("incremental.get_stocks_max_report_date")
    def test_multi_market(self, mock_db_max, mock_progress):
        """多市场混合判断。"""
        from incremental import determine_stocks_to_sync

        def db_max_side_effect(market):
            if market == "CN_A":
                return {
                    "000001": date(2024, 9, 30),
                    "000002": date(2024, 6, 30),
                }
            elif market == "CN_HK":
                return {
                    "00700": date(2024, 6, 30),
                }
            return {}

        def progress_side_effect(market):
            if market == "CN_A":
                return {
                    "000001": date(2024, 9, 30),  # 相同，跳过
                    "000002": date(2024, 3, 31),  # DB 更新，同步
                }
            elif market == "CN_HK":
                return {
                    "00700": date(2024, 6, 30),  # 相同，跳过
                }
            return {}

        mock_db_max.side_effect = db_max_side_effect
        mock_progress.side_effect = progress_side_effect

        stocks = [
            ("000001", "CN_A"),
            ("000002", "CN_A"),
            ("00700", "CN_HK"),
        ]
        pending, skipped = determine_stocks_to_sync(stocks, force=False)

        assert len(pending) == 1
        assert pending[0] == ("000002", "CN_A")
        assert skipped == 2

    @patch("incremental.get_sync_progress_report_dates")
    @patch("incremental.get_stocks_max_report_date")
    def test_us_market(self, mock_db_max, mock_progress):
        """美股增量判断。"""
        from incremental import determine_stocks_to_sync

        mock_db_max.return_value = {
            "AAPL": date(2024, 9, 30),
            "MSFT": date(2024, 6, 30),
            "GOOGL": date(2024, 6, 30),
        }
        mock_progress.return_value = {
            "AAPL": date(2024, 9, 30),  # 相同
            "MSFT": date(2024, 6, 30),  # 相同
        }

        stocks = [("AAPL", "US"), ("MSFT", "US"), ("GOOGL", "US")]
        pending, skipped = determine_stocks_to_sync(stocks, force=False)

        # GOOGL 没有 progress 记录 → 首次同步
        assert len(pending) == 1
        assert pending[0] == ("GOOGL", "US")
        assert skipped == 2


class TestGetStocksMaxReportDate:
    """get_stocks_max_report_date 测试。"""

    @patch("incremental.execute")
    def test_returns_dict_of_dates(self, mock_execute):
        """正确解析查询结果为 {code: date} 字典。"""
        from incremental import get_stocks_max_report_date

        mock_execute.return_value = [
            ("000001", date(2024, 9, 30)),
            ("000002", date(2024, 6, 30)),
        ]

        result = get_stocks_max_report_date("CN_A")

        assert result == {
            "000001": date(2024, 9, 30),
            "000002": date(2024, 6, 30),
        }

    @patch("incremental.execute")
    def test_empty_result(self, mock_execute):
        """空查询结果返回空字典。"""
        from incremental import get_stocks_max_report_date

        mock_execute.return_value = []
        result = get_stocks_max_report_date("CN_A")
        assert result == {}

    @patch("incremental.execute")
    def test_unknown_market(self, mock_execute):
        """未知市场返回空字典。"""
        from incremental import get_stocks_max_report_date

        result = get_stocks_max_report_date("UNKNOWN")
        assert result == {}
        mock_execute.assert_not_called()


class TestGetSyncProgressReportDates:
    """get_sync_progress_report_dates 测试。"""

    @patch("incremental.execute")
    def test_returns_dict(self, mock_execute):
        """正确返回 progress 记录。"""
        from incremental import get_sync_progress_report_dates

        mock_execute.return_value = [
            ("000001", date(2024, 9, 30)),
            ("000002", date(2024, 6, 30)),
        ]

        result = get_sync_progress_report_dates("CN_A")
        assert result == {
            "000001": date(2024, 9, 30),
            "000002": date(2024, 6, 30),
        }

    @patch("incremental.execute")
    def test_empty_result(self, mock_execute):
        """空结果返回空字典。"""
        from incremental import get_sync_progress_report_dates

        mock_execute.return_value = []
        result = get_sync_progress_report_dates("CN_A")
        assert result == {}


class TestUpdateLastReportDate:
    """update_last_report_date 测试。"""

    @patch("incremental.execute")
    def test_updates_with_max_date(self, mock_execute):
        """从财务表中取最大日期并更新。"""
        from incremental import update_last_report_date

        # 第一次调用: UNION ALL 查询
        # 第二次调用: UPDATE sync_progress
        mock_execute.side_effect = [
            [("2024-09-30",), ("2024-06-30",)],  # 查询结果
            None,  # UPDATE 结果
        ]

        result = update_last_report_date("000001", ["income_statement", "balance_sheet"])
        assert result == date(2024, 9, 30)
        assert mock_execute.call_count == 2

    @patch("incremental.execute")
    def test_empty_tables_returns_none(self, mock_execute):
        """无表则返回 None。"""
        from incremental import update_last_report_date

        result = update_last_report_date("000001", [])
        assert result is None
        mock_execute.assert_not_called()

    @patch("incremental.execute")
    def test_no_data_returns_none(self, mock_execute):
        """查询无结果返回 None。"""
        from incremental import update_last_report_date

        mock_execute.return_value = [(None,)]
        result = update_last_report_date("000001", ["income_statement"])
        assert result is None


class TestEnsureLastReportDateColumn:
    """ensure_last_report_date_column 测试。"""

    @patch("incremental.execute")
    def test_executes_alter_and_index(self, mock_execute):
        """执行 ALTER TABLE 和 CREATE INDEX。"""
        from incremental import ensure_last_report_date_column

        ensure_last_report_date_column()

        assert mock_execute.call_count == 2
        calls = mock_execute.call_args_list
        assert "ALTER TABLE" in calls[0][0][0]
        assert "CREATE INDEX" in calls[1][0][0]
