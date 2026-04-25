"""
tests/test_scheduler_market_filter.py — scheduler.py 市场过滤功能的单元测试

测试 STOCK_MARKETS 环境变量驱动的任务过滤逻辑。
"""
import os
import pytest
from unittest.mock import patch, MagicMock


# ── config.py SchedulerConfig.markets ──────────────────

class TestSchedulerConfigMarkets:
    """测试 SchedulerConfig 的 markets 字段从环境变量正确解析。"""

    def test_single_market(self):
        os.environ["STOCK_MARKETS"] = "US"
        try:
            # 重新实例化以读取新环境变量
            from config import SchedulerConfig
            cfg = SchedulerConfig()
            assert cfg.markets == ["US"]
        finally:
            os.environ.pop("STOCK_MARKETS", None)

    def test_multiple_markets(self):
        os.environ["STOCK_MARKETS"] = "CN_A,CN_HK"
        try:
            from config import SchedulerConfig
            cfg = SchedulerConfig()
            assert cfg.markets == ["CN_A", "CN_HK"]
        finally:
            os.environ.pop("STOCK_MARKETS", None)

    def test_markets_with_spaces(self):
        os.environ["STOCK_MARKETS"] = " CN_A , CN_HK , US "
        try:
            from config import SchedulerConfig
            cfg = SchedulerConfig()
            assert cfg.markets == ["CN_A", "CN_HK", "US"]
        finally:
            os.environ.pop("STOCK_MARKETS", None)

    def test_empty_env_var(self):
        os.environ["STOCK_MARKETS"] = ""
        try:
            from config import SchedulerConfig
            cfg = SchedulerConfig()
            assert cfg.markets == []
        finally:
            os.environ.pop("STOCK_MARKETS", None)

    def test_env_not_set(self):
        os.environ.pop("STOCK_MARKETS", None)
        from config import SchedulerConfig
        cfg = SchedulerConfig()
        assert cfg.markets == []


# ── _filter_job_defs ───────────────────────────────────

class TestFilterJobDefs:
    """测试 scheduler._filter_job_defs 过滤逻辑。"""

    def test_filter_us_only(self):
        """test_filter_cn_only covers CN_A; see test_filter_us_only_jobs below for US."""
        # This test is covered by test_filter_us_only_jobs

    def test_filter_cn_only(self):
        """测试只保留 CN_A 市场。"""
        from core.scheduler import _filter_job_defs, JOB_DEFS
        with patch("config.scheduler") as mock_sched:
            mock_sched.markets = ["CN_A"]
            filtered = _filter_job_defs()
            assert all(jd["market"] == "CN_A" for jd in filtered.values())
            assert len(filtered) == 2  # CN_A_daily_quote + CN_A_financial

    def test_filter_cn_hk(self):
        """测试保留 CN_A 和 CN_HK 市场。"""
        from core.scheduler import _filter_job_defs
        with patch("config.scheduler") as mock_sched:
            mock_sched.markets = ["CN_A", "CN_HK"]
            filtered = _filter_job_defs()
            markets_in_result = {jd["market"] for jd in filtered.values()}
            assert markets_in_result == {"CN_A", "CN_HK"}
            assert len(filtered) == 4  # 2 quote + 2 financial

    def test_filter_us_only_jobs(self):
        """测试只保留 US 市场。"""
        from core.scheduler import _filter_job_defs
        with patch("config.scheduler") as mock_sched:
            mock_sched.markets = ["US"]
            filtered = _filter_job_defs()
            markets_in_result = {jd["market"] for jd in filtered.values()}
            assert markets_in_result == {"US"}
            assert len(filtered) == 2  # US_daily_quote + US_financial

    def test_filter_empty_markets(self):
        """测试未配置 markets 时返回空。"""
        from core.scheduler import _filter_job_defs
        with patch("config.scheduler") as mock_sched:
            mock_sched.markets = []
            filtered = _filter_job_defs()
            assert filtered == {}

    def test_filter_all_markets(self):
        """测试所有市场都配置时返回全部任务。"""
        from core.scheduler import _filter_job_defs, JOB_DEFS
        with patch("config.scheduler") as mock_sched:
            mock_sched.markets = ["CN_A", "CN_HK", "US"]
            filtered = _filter_job_defs()
            assert len(filtered) == len(JOB_DEFS)

    def test_filter_unknown_market(self):
        """测试配置了不存在的市场时返回空。"""
        from core.scheduler import _filter_job_defs
        with patch("config.scheduler") as mock_sched:
            mock_sched.markets = ["UNKNOWN"]
            filtered = _filter_job_defs()
            assert filtered == {}


# ── run_scheduler exits on empty markets ───────────────

class TestSchedulerExitOnEmptyMarkets:
    """测试 scheduler 启动时未配置 markets 会警告退出。"""

    @patch("core.scheduler.health_check", return_value=True)
    def test_exit_when_no_markets(self, mock_health):
        """未配置 STOCK_MARKETS 时，scheduler 应 sys.exit(1)。"""
        from core.scheduler import run_scheduler
        with patch("config.scheduler") as mock_sched:
            mock_sched.markets = []
            with pytest.raises(SystemExit) as exc_info:
                run_scheduler(once=False)
            assert exc_info.value.code == 1


# ── dry_run shows market info ──────────────────────────

class TestDryRunMarketDisplay:
    """测试 dry-run 模式显示市场信息。"""

    def test_dry_run_shows_markets(self, capsys):
        from core.scheduler import dry_run
        with patch("config.scheduler") as mock_sched:
            mock_sched.markets = ["US"]
            mock_sched.daily_quote_enabled = True
            mock_sched.cn_a_daily_quote_cron = "37 16 * * 1-5"
            mock_sched.hk_daily_quote_cron = "12 17 * * 1-5"
            mock_sched.us_daily_quote_cron = "37 5 * * 2-6"
            mock_sched.cn_a_cron = "7 17 * * 1-5"
            mock_sched.hk_cron = "37 17 * * 1-5"
            mock_sched.us_cron = "12 6 * * 1-6"
            mock_sched.max_retries = 3
            mock_sched.retry_base_delay = 60
            mock_sched.sync_workers = 4
            mock_sched.force_sync = False
            mock_sched.notify_url = ""
            dry_run()
            output = capsys.readouterr().out
            assert "US" in output
            assert "US_daily_quote" in output
            assert "US_financial" in output
            # CN_A / CN_HK 任务不应出现
            assert "CN_A_daily_quote" not in output
            assert "CN_HK_financial" not in output

    def test_dry_run_no_markets_configured(self, capsys):
        from core.scheduler import dry_run
        with patch("config.scheduler") as mock_sched:
            mock_sched.markets = []
            mock_sched.daily_quote_enabled = True
            mock_sched.cn_a_daily_quote_cron = "37 16 * * 1-5"
            mock_sched.hk_daily_quote_cron = "12 17 * * 1-5"
            mock_sched.us_daily_quote_cron = "37 5 * * 2-6"
            mock_sched.cn_a_cron = "7 17 * * 1-5"
            mock_sched.hk_cron = "37 17 * * 1-5"
            mock_sched.us_cron = "12 6 * * 1-6"
            mock_sched.max_retries = 3
            mock_sched.retry_base_delay = 60
            mock_sched.sync_workers = 4
            mock_sched.force_sync = False
            mock_sched.notify_url = ""
            dry_run()
            output = capsys.readouterr().out
            assert "未配置" in output
