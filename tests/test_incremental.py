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


# 辅助：构造 get_sync_progress_report_dates 的返回值
def _progress(stock_code, last_report_date, tables_synced=None):
    """构造 {stock_code: (last_report_date, tables_synced)} 格式。"""
    if tables_synced is None:
        tables_synced = ["income_statement", "balance_sheet", "cash_flow_statement"]
    return {stock_code: (last_report_date, tables_synced)}


def _progress_multi(*args):
    """构造多只股票的 progress 字典。args 每项为 (code, date, tables)。"""
    result = {}
    for code, d, tables in args:
        result[code] = (d, tables if tables is not None else
                        ["income_statement", "balance_sheet", "cash_flow_statement"])
    return result


class TestDetermineStocksToSync:
    """增量同步判断核心逻辑测试。"""

    def test_force_mode_returns_all(self):
        """force=True 时返回全部股票。"""
        from core.incremental import determine_stocks_to_sync

        stocks = [("000001", "CN_A"), ("000002", "CN_A"), ("00700", "CN_HK")]
        pending, skipped = determine_stocks_to_sync(stocks, force=True)

        assert len(pending) == 3
        assert skipped == 0

    RECENT = date(2026, 3, 31)       # Q1 2026，下一期 Q2(6/30) 截止 8/31，尚未到 → 不触发
    Q3_2025 = date(2025, 9, 30)      # Q3 2025，下一期年报(12/31) 截止 4/30 → 已过 → 触发
    Q4_2025 = date(2025, 12, 31)     # Q4 2025，下一期 Q1(3/31) 截止 4/30 → 已过 → 触发
    ALL_TABLES = ["income_statement", "balance_sheet", "cash_flow_statement"]

    def _p(self, report_date, tables=None):
        if tables is None:
            tables = self.ALL_TABLES
        return (report_date, tables)

    @patch("core.incremental.get_sync_progress_report_dates")
    @patch("core.incremental.get_stocks_max_report_date")
    def test_all_stocks_have_same_dates_are_skipped(self, mock_db_max, mock_progress):
        """Q1 报告期 + 下一期截止日未到 → 跳过。"""
        from core.incremental import determine_stocks_to_sync

        mock_db_max.return_value = {"000001": self.RECENT, "000002": self.RECENT}
        mock_progress.return_value = {"000001": self._p(self.RECENT), "000002": self._p(self.RECENT)}

        stocks = [("000001", "CN_A"), ("000002", "CN_A")]
        pending, skipped = determine_stocks_to_sync(stocks, force=False)

        assert len(pending) == 0
        assert skipped == 2

    @patch("core.incremental.get_sync_progress_report_dates")
    @patch("core.incremental.get_stocks_max_report_date")
    def test_new_report_date_triggers_sync(self, mock_db_max, mock_progress):
        """DB 中有更新的报告期 → 需要同步。"""
        from core.incremental import determine_stocks_to_sync

        mock_db_max.return_value = {"000001": self.RECENT, "000002": self.RECENT}
        mock_progress.return_value = {
            "000001": self._p(self.Q4_2025),  # DB 更新 → 同步
            "000002": self._p(self.RECENT),    # 相同 → 跳过
        }

        stocks = [("000001", "CN_A"), ("000002", "CN_A")]
        pending, skipped = determine_stocks_to_sync(stocks, force=False)

        assert len(pending) == 1
        assert pending[0] == ("000001", "CN_A")
        assert skipped == 1

    @patch("core.incremental.get_sync_progress_report_dates")
    @patch("core.incremental.get_stocks_max_report_date")
    def test_incomplete_tables_triggers_sync(self, mock_db_max, mock_progress):
        """即使日期相同，三表不完整也需补同步。"""
        from core.incremental import determine_stocks_to_sync

        mock_db_max.return_value = {"000001": self.RECENT}
        mock_progress.return_value = {"000001": self._p(self.RECENT, tables=["income_statement"])}

        stocks = [("000001", "CN_A")]
        pending, skipped = determine_stocks_to_sync(stocks, force=False)

        assert len(pending) == 1
        assert skipped == 0

    @patch("core.incremental.get_sync_progress_report_dates")
    @patch("core.incremental.get_stocks_max_report_date")
    def test_q3_next_deadline_passed_triggers_recheck(self, mock_db_max, mock_progress):
        """Q3 报告(9/30)，年报截止(4/30)已过 → 应有年报数据，触发重检。"""
        from core.incremental import determine_stocks_to_sync

        mock_db_max.return_value = {"000001": self.Q3_2025}
        mock_progress.return_value = {"000001": self._p(self.Q3_2025)}

        stocks = [("000001", "CN_A")]
        pending, skipped = determine_stocks_to_sync(stocks, force=False)

        assert len(pending) == 1
        assert skipped == 0

    @patch("core.incremental.get_sync_progress_report_dates")
    @patch("core.incremental.get_stocks_max_report_date")
    def test_q4_next_deadline_passed_triggers_recheck(self, mock_db_max, mock_progress):
        """Q4 年报(12/31)，Q1 截止(4/30)已过 → 应有 Q1 数据，触发重检。"""
        from core.incremental import determine_stocks_to_sync

        mock_db_max.return_value = {"000001": self.Q4_2025}
        mock_progress.return_value = {"000001": self._p(self.Q4_2025)}

        stocks = [("000001", "CN_A")]
        pending, skipped = determine_stocks_to_sync(stocks, force=False)

        assert len(pending) == 1
        assert skipped == 0

    @patch("core.incremental.get_sync_progress_report_dates")
    @patch("core.incremental.get_stocks_max_report_date")
    def test_no_db_data_means_new_stock(self, mock_db_max, mock_progress):
        """财务表中无数据 → 新股票，必须同步。"""
        from core.incremental import determine_stocks_to_sync

        mock_db_max.return_value = {}  # 无数据
        mock_progress.return_value = {}

        stocks = [("688001", "CN_A")]
        pending, skipped = determine_stocks_to_sync(stocks, force=False)

        assert len(pending) == 1
        assert skipped == 0

    @patch("core.incremental.get_sync_progress_report_dates")
    @patch("core.incremental.get_stocks_max_report_date")
    def test_no_progress_record_means_first_sync(self, mock_db_max, mock_progress):
        """sync_progress 无记录 → 首次同步。"""
        from core.incremental import determine_stocks_to_sync

        mock_db_max.return_value = {"000001": date(2024, 9, 30)}
        mock_progress.return_value = {}  # 无记录

        stocks = [("000001", "CN_A")]
        pending, skipped = determine_stocks_to_sync(stocks, force=False)

        assert len(pending) == 1
        assert skipped == 0

    @patch("core.incremental.get_sync_progress_report_dates")
    @patch("core.incremental.get_stocks_max_report_date")
    def test_multi_market(self, mock_db_max, mock_progress):
        """多市场混合判断。"""
        from core.incremental import determine_stocks_to_sync

        def db_max_side_effect(market):
            if market == "CN_A":
                return {"000001": self.RECENT, "000002": self.Q3_2025}
            elif market == "CN_HK":
                return {"00700": self.RECENT}
            return {}

        def progress_side_effect(market):
            if market == "CN_A":
                return {
                    "000001": (self.RECENT, self.ALL_TABLES),    # Q1，下一期未到 → 跳过
                    "000002": (self.Q3_2025, self.ALL_TABLES),   # Q3，年报截止已过 → 重检
                }
            elif market == "CN_HK":
                return {"00700": (self.RECENT, self.ALL_TABLES)}  # Q1 → 跳过
            return {}

        mock_db_max.side_effect = db_max_side_effect
        mock_progress.side_effect = progress_side_effect

        stocks = [("000001", "CN_A"), ("000002", "CN_A"), ("00700", "CN_HK")]
        pending, skipped = determine_stocks_to_sync(stocks, force=False)

        assert len(pending) == 1
        assert pending[0] == ("000002", "CN_A")
        assert skipped == 2

    @patch("core.incremental._get_us_annual_report_dates")
    @patch("core.incremental.get_sync_progress_report_dates")
    @patch("core.incremental.get_stocks_max_report_date")
    def test_us_market(self, mock_db_max, mock_progress, mock_annual):
        """美股无 annual 数据时不触发重检。"""
        from core.incremental import determine_stocks_to_sync

        mock_db_max.return_value = {
            "AAPL": date(2025, 9, 30),
            "MSFT": date(2025, 6, 30),
            "GOOGL": date(2025, 9, 30),
        }
        mock_progress.return_value = {
            "AAPL": (date(2025, 9, 30), ["us_income_statement", "us_balance_sheet", "us_cash_flow_statement"]),
            "MSFT": (date(2025, 6, 30), ["us_income_statement", "us_balance_sheet", "us_cash_flow_statement"]),
        }
        mock_annual.return_value = {}  # 无 annual 数据

        stocks = [("AAPL", "US"), ("MSFT", "US"), ("GOOGL", "US")]
        pending, skipped = determine_stocks_to_sync(stocks, force=False)

        # GOOGL 无 progress → 首次; AAPL/MSFT 美股不触发重检 → 跳过
        assert len(pending) == 1
        assert pending[0] == ("GOOGL", "US")
        assert skipped == 2

    @patch("core.incremental._get_us_annual_report_dates")
    @patch("core.incremental.get_sync_progress_report_dates")
    @patch("core.incremental.get_stocks_max_report_date")
    def test_us_annual_stale_triggers_recheck(self, mock_db_max, mock_progress, mock_annual):
        """美股 annual 报告超过 470 天（365+105）→ 触发重检。"""
        from core.incremental import determine_stocks_to_sync

        old_annual = date(2024, 1, 31)  # 超过 470 天 → 应触发
        recent_annual = date(2025, 9, 30)  # 未超过 → 不触发

        mock_db_max.return_value = {
            "AAPL": recent_annual,
            "MSFT": old_annual,
        }
        mock_progress.return_value = {
            "AAPL": (recent_annual, ["us_income_statement", "us_balance_sheet", "us_cash_flow_statement"]),
            "MSFT": (old_annual, ["us_income_statement", "us_balance_sheet", "us_cash_flow_statement"]),
        }
        mock_annual.return_value = {
            "AAPL": recent_annual,
            "MSFT": old_annual,
        }

        stocks = [("AAPL", "US"), ("MSFT", "US")]
        pending, skipped = determine_stocks_to_sync(stocks, force=False)

        # MSFT annual 超期 → 重检; AAPL 未超期 → 跳过
        assert len(pending) == 1
        assert pending[0] == ("MSFT", "US")
        assert skipped == 1


class TestGetStocksMaxReportDate:
    """get_stocks_max_report_date 测试。"""

    @patch("core.incremental.execute")
    def test_returns_dict_of_dates(self, mock_execute):
        """正确解析查询结果为 {code: date} 字典。"""
        from core.incremental import get_stocks_max_report_date

        mock_execute.return_value = [
            ("000001", date(2024, 9, 30)),
            ("000002", date(2024, 6, 30)),
        ]

        result = get_stocks_max_report_date("CN_A")

        assert result == {
            "000001": date(2024, 9, 30),
            "000002": date(2024, 6, 30),
        }

    @patch("core.incremental.execute")
    def test_empty_result(self, mock_execute):
        """空查询结果返回空字典。"""
        from core.incremental import get_stocks_max_report_date

        mock_execute.return_value = []
        result = get_stocks_max_report_date("CN_A")
        assert result == {}

    @patch("core.incremental.execute")
    def test_unknown_market(self, mock_execute):
        """未知市场返回空字典。"""
        from core.incremental import get_stocks_max_report_date

        result = get_stocks_max_report_date("UNKNOWN")
        assert result == {}
        mock_execute.assert_not_called()


class TestGetSyncProgressReportDates:
    """get_sync_progress_report_dates 测试。"""

    @patch("core.incremental.execute")
    def test_returns_dict(self, mock_execute):
        """正确返回 progress 记录（包含 tables_synced）。"""
        from core.incremental import get_sync_progress_report_dates

        mock_execute.return_value = [
            ("000001", date(2024, 9, 30), ["income_statement", "balance_sheet", "cash_flow_statement"]),
            ("000002", date(2024, 6, 30), ["income_statement", "balance_sheet"]),
        ]

        result = get_sync_progress_report_dates("CN_A")
        assert result == {
            "000001": (date(2024, 9, 30), ["income_statement", "balance_sheet", "cash_flow_statement"]),
            "000002": (date(2024, 6, 30), ["income_statement", "balance_sheet"]),
        }

    @patch("core.incremental.execute")
    def test_null_tables_synced(self, mock_execute):
        """tables_synced 为 NULL 时返回空列表。"""
        from core.incremental import get_sync_progress_report_dates

        mock_execute.return_value = [
            ("000001", date(2024, 9, 30), None),
        ]

        result = get_sync_progress_report_dates("CN_A")
        assert result == {
            "000001": (date(2024, 9, 30), []),
        }

    @patch("core.incremental.execute")
    def test_empty_result(self, mock_execute):
        """空结果返回空字典。"""
        from core.incremental import get_sync_progress_report_dates

        mock_execute.return_value = []
        result = get_sync_progress_report_dates("CN_A")
        assert result == {}


class TestUpdateLastReportDate:
    """update_last_report_date 测试。"""

    @patch("core.incremental.execute")
    def test_updates_with_min_date(self, mock_execute):
        """从财务表中取最小日期（MIN of MAXs）并更新。"""
        from core.incremental import update_last_report_date

        mock_execute.side_effect = [
            [("2024-09-30",), ("2024-06-30",)],  # UNION ALL 查询
            None,  # UPDATE 结果
        ]

        result = update_last_report_date("000001", ["income_statement", "balance_sheet"])
        # MIN of (2024-09-30, 2024-06-30) = 2024-06-30
        assert result == date(2024, 6, 30)
        assert mock_execute.call_count == 2

    @patch("core.incremental.execute")
    def test_all_same_date(self, mock_execute):
        """所有表同一日期 → 返回该日期。"""
        from core.incremental import update_last_report_date

        mock_execute.side_effect = [
            [("2024-12-31",), ("2024-12-31",), ("2024-12-31",)],
            None,
        ]

        result = update_last_report_date("000001",
                                          ["income_statement", "balance_sheet", "cash_flow_statement"])
        assert result == date(2024, 12, 31)

    @patch("core.incremental.execute")
    def test_empty_tables_returns_none(self, mock_execute):
        """无表则返回 None。"""
        from core.incremental import update_last_report_date

        result = update_last_report_date("000001", [])
        assert result is None
        mock_execute.assert_not_called()

    @patch("core.incremental.execute")
    def test_no_data_returns_none(self, mock_execute):
        """查询无结果返回 None。"""
        from core.incremental import update_last_report_date

        mock_execute.return_value = [(None,)]
        result = update_last_report_date("000001", ["income_statement"])
        assert result is None


class TestEnsureLastReportDateColumn:
    """ensure_last_report_date_column 测试。"""

    @patch("core.incremental.execute")
    def test_executes_alter_and_index(self, mock_execute):
        """执行 ALTER TABLE 和 CREATE INDEX。"""
        from core.incremental import ensure_last_report_date_column

        ensure_last_report_date_column()

        assert mock_execute.call_count == 2
        calls = mock_execute.call_args_list
        assert "ALTER TABLE" in calls[0][0][0]
        assert "CREATE INDEX" in calls[1][0][0]
