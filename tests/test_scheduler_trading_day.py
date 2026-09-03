"""
tests/test_scheduler_trading_day.py — scheduler.py 交易日判断的单元测试

重点回归：美股交易日必须以美东日期判断。服务器 cron 在北京时间
周二~周六早晨触发（对应美东周一~周五收盘后），其中北京时间周六
早晨 = 美东周五收盘后，不能被判成非交易日（否则周五行情每周缺失）。
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from core.scheduler import _is_china_trading_day, _is_us_trading_day

SHANGHAI = ZoneInfo("Asia/Shanghai")
NEW_YORK = ZoneInfo("America/New_York")


class TestIsUsTradingDay:
    """_is_us_trading_day 以美东时间判断 weekday。"""

    def test_saturday_morning_beijing_is_friday_close(self):
        """北京时间周六 05:37 = 美东周五 17:37（夏令时），必须是交易日。"""
        dt = datetime(2026, 8, 29, 5, 37, tzinfo=SHANGHAI)
        assert _is_us_trading_day(dt) is True

    def test_saturday_morning_beijing_winter_is_friday_close(self):
        """冬令时同理：北京时间周六 05:37 = 美东周五 16:37 EST。"""
        dt = datetime(2026, 1, 10, 5, 37, tzinfo=SHANGHAI)
        assert _is_us_trading_day(dt) is True

    def test_tuesday_morning_beijing_is_monday(self):
        """北京时间周二 05:37 = 美东周一傍晚，交易日。"""
        dt = datetime(2026, 9, 1, 5, 37, tzinfo=SHANGHAI)
        assert _is_us_trading_day(dt) is True

    def test_sunday_morning_beijing_is_saturday(self):
        """北京时间周日早晨 = 美东周六，非交易日。"""
        dt = datetime(2026, 8, 30, 5, 37, tzinfo=SHANGHAI)
        assert _is_us_trading_day(dt) is False

    def test_naive_dt_treated_as_server_local(self):
        """无时区的 dt 按服务器本地时间换算（北京时间周六早晨 → 交易日）。"""
        assert _is_us_trading_day(datetime(2026, 8, 29, 5, 37)) is True

    def test_us_saturday_is_not_trading_day(self):
        """美东周六本身仍是非交易日。"""
        dt = datetime(2026, 8, 29, 12, 0, tzinfo=NEW_YORK)
        assert _is_us_trading_day(dt) is False


class TestIsChinaTradingDay:
    """_is_china_trading_day 保持原有语义（仅排除本地周末）。"""

    def test_weekday(self):
        assert _is_china_trading_day(datetime(2026, 8, 28, 16, 37)) is True

    def test_saturday(self):
        assert _is_china_trading_day(datetime(2026, 8, 29, 16, 37)) is False
