"""
tests/test_validate.py — validate.py 的单元测试

使用 mock 避免依赖真实数据库，测试校验逻辑的正确性。
"""
import pytest
from datetime import date
from unittest.mock import patch, MagicMock
from decimal import Decimal


# ── helpers ──────────────────────────────────────────────

def _make_validation_issue(**overrides):
    """创建 ValidationIssue 的快捷方法。"""
    from core.validate import ValidationIssue
    defaults = dict(
        stock_code="000001",
        market="CN_A",
        report_date="2024-12-31",
        check_name="test_check",
        severity="warning",
        field_name="test_field",
        actual_value="100",
        expected_value="< 100",
        message="test message",
        suggestion="test suggestion",
    )
    defaults.update(overrides)
    return ValidationIssue(**defaults)


# ── ValidationIssue / ValidationReport ──────────────────

class TestValidationReport:
    def test_counts(self):
        from core.validate import ValidationReport, ValidationIssue
        report = ValidationReport(started_at="2026-01-01T00:00:00")
        report.issues = [
            ValidationIssue("A", "CN_A", "2024-01-01", "c1", "error", "f1"),
            ValidationIssue("B", "CN_A", "2024-01-01", "c2", "warning", "f2"),
            ValidationIssue("C", "CN_A", "2024-01-01", "c3", "info", "f3"),
            ValidationIssue("D", "CN_A", "2024-01-01", "c4", "error", "f4"),
        ]
        assert report.error_count == 2
        assert report.warning_count == 1
        assert report.info_count == 1

    def test_empty_report(self):
        from core.validate import ValidationReport
        report = ValidationReport(started_at="2026-01-01T00:00:00")
        assert report.error_count == 0
        assert report.warning_count == 0
        assert report.info_count == 0


# ── _d helper ───────────────────────────────────────────

class TestDHelper:
    def test_none(self):
        from core.validate import _d
        assert _d(None) is None

    def test_decimal(self):
        from core.validate import _d
        assert _d(Decimal("123.45")) == 123.45

    def test_float(self):
        from core.validate import _d
        assert _d(42.0) == 42.0

    def test_string(self):
        from core.validate import _d
        assert _d("abc") is None

    def test_int(self):
        from core.validate import _d
        assert _d(100) == 100.0


# ── Anomaly Detection: CN/HK ───────────────────────────

class TestCheckAnomaliesCNHK:
    """测试 A 股/港股异常值检测逻辑。

    通过 mock db.execute 返回特定数据行，验证各检查规则。
    """

    def _mock_row(self, **overrides):
        """构建 check_anomalies_cn_hk 期望的行格式。"""
        defaults = (
            "000001", "CN_A", date(2024, 12, 31), "annual",  # stock_code, market, report_date, report_type
            Decimal("10000000000"),  # total_revenue
            Decimal("9000000000"),   # operating_revenue
            Decimal("1000000000"),   # net_profit
            Decimal("900000000"),    # parent_net_profit
            Decimal("50000000000"),  # total_assets
            Decimal("30000000000"),  # total_liab
            Decimal("20000000000"),  # total_equity
            Decimal("15000000000"),  # current_assets
            Decimal("5000000000"),   # cash_equivalents
            Decimal("800000000"),    # cfo_net
        )
        row = list(defaults)
        keys = [
            "stock_code", "market", "report_date", "report_type",
            "total_revenue", "operating_revenue", "net_profit", "parent_net_profit",
            "total_assets", "total_liab", "total_equity",
            "current_assets", "cash_equivalents", "cfo_net",
        ]
        for k, v in overrides.items():
            if k in keys:
                idx = keys.index(k)
                row[idx] = v
        return tuple(row)

    @patch("core.validate.db.execute")
    def test_negative_assets(self, mock_exec):
        from core.validate import check_anomalies_cn_hk, ValidationIssue
        mock_exec.return_value = [self._mock_row(total_assets=Decimal("-1000"))]
        issues = []
        check_anomalies_cn_hk("A", issues)
        assert len(issues) == 1
        assert issues[0].check_name == "negative_total_assets"
        assert issues[0].severity == "error"

    @patch("core.validate.db.execute")
    def test_high_debt_ratio(self, mock_exec):
        from core.validate import check_anomalies_cn_hk
        # liab=300亿, assets=100亿 → ratio=300%
        mock_exec.return_value = [self._mock_row(
            total_assets=Decimal("10000000000"),
            total_liab=Decimal("30000000000"),
        )]
        issues = []
        check_anomalies_cn_hk("A", issues)
        names = [i.check_name for i in issues]
        assert "debt_ratio_extreme" in names

    @patch("core.validate.db.execute")
    def test_net_profit_exceeds_revenue(self, mock_exec):
        from core.validate import check_anomalies_cn_hk
        mock_exec.return_value = [self._mock_row(
            total_revenue=Decimal("1000000000"),
            net_profit=Decimal("2000000000"),
        )]
        issues = []
        check_anomalies_cn_hk("A", issues)
        names = [i.check_name for i in issues]
        assert "net_profit_exceeds_revenue" in names

    @patch("core.validate.db.execute")
    def test_cfo_negative_profit_positive(self, mock_exec):
        from core.validate import check_anomalies_cn_hk
        mock_exec.return_value = [self._mock_row(
            parent_net_profit=Decimal("1000000000"),
            cfo_net=Decimal("-500000000"),
        )]
        issues = []
        check_anomalies_cn_hk("A", issues)
        names = [i.check_name for i in issues]
        assert "cfo_negative_profit_positive" in names

    @patch("core.validate.db.execute")
    def test_zero_revenue_annual(self, mock_exec):
        from core.validate import check_anomalies_cn_hk
        mock_exec.return_value = [self._mock_row(
            total_revenue=Decimal("0"),
        )]
        issues = []
        check_anomalies_cn_hk("A", issues)
        names = [i.check_name for i in issues]
        assert "zero_revenue_annual" in names

    @patch("core.validate.db.execute")
    def test_normal_data_no_issues(self, mock_exec):
        from core.validate import check_anomalies_cn_hk
        # 正常数据，不应产生异常
        mock_exec.return_value = [self._mock_row()]
        issues = []
        check_anomalies_cn_hk("A", issues)
        assert len(issues) == 0

    @patch("core.validate.db.execute")
    def test_null_values_handled(self, mock_exec):
        from core.validate import check_anomalies_cn_hk
        # 很多字段为 NULL
        row = (
            "000001", "CN_A", date(2024, 12, 31), "annual",
            None, None, None, None,  # revenue, op_revenue, net_profit, parent_net_profit
            None, None, None,  # assets, liab, equity
            None, None, None,  # current_assets, cash, cfo
        )
        mock_exec.return_value = [row]
        issues = []
        scanned = check_anomalies_cn_hk("A", issues)
        assert scanned == 1
        assert len(issues) == 0  # NULL 不应触发异常


# ── Anomaly Detection: US ──────────────────────────────

class TestCheckAnomaliesUS:
    def _mock_row(self, **overrides):
        defaults = (
            "AAPL", date(2024, 9, 30), "quarterly",
            Decimal("94928000000"),   # revenues
            Decimal("23636000000"),   # net_income
            Decimal("350000000000"),  # total_assets
            Decimal("290000000000"),  # total_liabilities
            Decimal("60000000000"),   # total_equity
            Decimal("100000000000"),  # total_current_assets
            Decimal("30000000000"),   # cash_and_equivalents
            Decimal("25000000000"),   # net_cash_from_operations
        )
        row = list(defaults)
        keys = [
            "stock_code", "report_date", "report_type",
            "revenues", "net_income",
            "total_assets", "total_liabilities", "total_equity",
            "total_current_assets", "cash_and_equivalents",
            "net_cash_from_operations",
        ]
        for k, v in overrides.items():
            if k in keys:
                idx = keys.index(k)
                row[idx] = v
        return tuple(row)

    @patch("core.validate.db.execute")
    def test_negative_assets_us(self, mock_exec):
        from core.validate import check_anomalies_us
        mock_exec.return_value = [self._mock_row(total_assets=Decimal("-1000"))]
        issues = []
        check_anomalies_us(issues)
        assert any(i.check_name == "negative_total_assets" for i in issues)

    @patch("core.validate.db.execute")
    def test_normal_us_data(self, mock_exec):
        from core.validate import check_anomalies_us
        mock_exec.return_value = [self._mock_row()]
        issues = []
        check_anomalies_us(issues)
        assert len(issues) == 0


# ── Logic Consistency: CN/HK ───────────────────────────

class TestCheckLogicCNHK:
    def _mock_row(self, **overrides):
        defaults = (
            "000001", "CN_A", date(2024, 12, 31),
            Decimal("100000000"),  # total_assets
            Decimal("60000000"),   # total_liab
            Decimal("40000000"),   # total_equity
            Decimal("50000000"),   # current_assets
            Decimal("10000000"),   # cash_equivalents
            Decimal("0"),          # minority_equity
        )
        row = list(defaults)
        keys = [
            "stock_code", "market", "report_date",
            "total_assets", "total_liab", "total_equity",
            "current_assets", "cash_equivalents", "minority_equity",
        ]
        for k, v in overrides.items():
            if k in keys:
                idx = keys.index(k)
                row[idx] = v
        return tuple(row)

    @patch("core.validate.db.execute")
    def test_balance_equation_ok(self, mock_exec):
        from core.validate import check_logic_cn_hk
        # 100 = 60 + 40 ✓
        mock_exec.return_value = [self._mock_row()]
        issues = []
        check_logic_cn_hk("A", issues)
        assert not any(i.check_name == "balance_equation" for i in issues)

    @patch("core.validate.db.execute")
    def test_balance_equation_broken(self, mock_exec):
        from core.validate import check_logic_cn_hk
        # assets=100, liab=60, equity=50 → 60+50=110 ≠ 100
        mock_exec.return_value = [self._mock_row(
            total_assets=Decimal("100000000"),
            total_liab=Decimal("60000000"),
            total_equity=Decimal("50000000"),
        )]
        issues = []
        check_logic_cn_hk("A", issues)
        balance_issues = [i for i in issues if i.check_name == "balance_equation"]
        assert len(balance_issues) == 1
        assert balance_issues[0].severity == "error"

    @patch("core.validate.db.execute")
    def test_balance_equation_minor_tolerance(self, mock_exec):
        from core.validate import check_logic_cn_hk
        # 100 = 60 + 39.5 → 0.5% deviation, within 1% tolerance
        mock_exec.return_value = [self._mock_row(
            total_assets=Decimal("100000000"),
            total_liab=Decimal("60000000"),
            total_equity=Decimal("39500000"),
        )]
        issues = []
        check_logic_cn_hk("A", issues)
        assert not any(i.check_name == "balance_equation" for i in issues)

    @patch("core.validate.db.execute")
    def test_cash_exceeds_current_assets(self, mock_exec):
        from core.validate import check_logic_cn_hk
        mock_exec.return_value = [self._mock_row(
            current_assets=Decimal("10000000"),
            cash_equivalents=Decimal("20000000"),
        )]
        issues = []
        check_logic_cn_hk("A", issues)
        cash_issues = [i for i in issues if i.check_name == "cash_exceeds_current_assets"]
        assert len(cash_issues) == 1
        assert cash_issues[0].severity == "error"


# ── Logic Consistency: US ──────────────────────────────

class TestCheckLogicUS:
    def _mock_row(self, **overrides):
        defaults = (
            "AAPL", date(2024, 9, 30),
            Decimal("350000000000"),   # total_assets
            Decimal("290000000000"),   # total_liabilities
            Decimal("60000000000"),    # total_equity
            Decimal("62000000000"),    # total_equity_including_nci
            Decimal("100000000000"),   # total_current_assets
            Decimal("30000000000"),    # cash_and_equivalents
        )
        row = list(defaults)
        keys = [
            "stock_code", "report_date",
            "total_assets", "total_liabilities", "total_equity",
            "total_equity_including_nci", "total_current_assets",
            "cash_and_equivalents",
        ]
        for k, v in overrides.items():
            if k in keys:
                idx = keys.index(k)
                row[idx] = v
        return tuple(row)

    @patch("core.validate.db.execute")
    def test_balance_equation_us_ok(self, mock_exec):
        from core.validate import check_logic_us
        # 350 = 290 + 60 ✓
        mock_exec.return_value = [self._mock_row()]
        issues = []
        check_logic_us(issues)
        assert not any(i.check_name == "balance_equation" for i in issues)

    @patch("core.validate.db.execute")
    def test_balance_equation_us_nci_fix(self, mock_exec):
        from core.validate import check_logic_us
        # 350 = 290 + 58, but 350 = 290 + 60(NCI) → should pass
        mock_exec.return_value = [self._mock_row(
            total_equity=Decimal("58000000000"),
            total_equity_including_nci=Decimal("60000000000"),
        )]
        issues = []
        check_logic_us(issues)
        assert not any(i.check_name == "balance_equation" for i in issues)


# ── Cross Source ────────────────────────────────────────

class TestCrossSource:
    def test_records_limitation(self):
        from core.validate import check_cross_source, ValidationIssue
        issues = []
        check_cross_source("A", issues)
        assert any(i.check_name == "single_source_limitation" for i in issues)

    def test_us_limitation(self):
        from core.validate import check_cross_source
        issues = []
        check_cross_source("US", issues)
        assert any(i.check_name == "single_source_limitation" and i.market == "US" for i in issues)


# ── save_results ─────────────────────────────────────────

class TestSaveResults:
    @patch("core.validate.db._check_db_encoding", return_value="UTF8")
    @patch("core.validate.db.Connection")
    def test_preserves_chinese_characters(self, mock_conn_cls, mock_enc):
        """中文消息不应被替换成问号（UTF8 库，如 CN 服务器）。"""
        from core.validate import save_results, ValidationReport, ValidationIssue

        report = ValidationReport(started_at="2026-01-01T00:00:00")
        report.issues.append(
            ValidationIssue(
                stock_code="00006",
                market="CN_HK",
                report_date="2024-12-31",
                check_name="net_profit_exceeds_revenue",
                severity="warning",
                field_name="net_profit/total_revenue",
                actual_value="净利润=2,728,138,820, 营收=610,350,760",
                expected_value="净利润通常不超过营收的 1.5 倍",
                message="净利润(2,728,138,820)远超营收(610,350,760)",
                suggestion="可能有大额投资收益/营业外收入，需核实利润构成",
            )
        )

        mock_cur = MagicMock()
        mock_cur.rowcount = 1
        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_conn_cls.return_value.__enter__.return_value = mock_conn

        save_results(report, "20260101_120000")

        assert mock_cur.executemany.called
        # 取出 executemany 的第二个参数（参数列表）
        params_list = mock_cur.executemany.call_args[0][1]
        assert len(params_list) == 1
        row = params_list[0]
        assert "净利润" in row["message"]
        assert "营收" in row["message"]
        assert "?" not in row["message"]
        assert "?" not in row["actual_value"]
        assert "?" not in row["expected_value"]
        assert "?" not in row["suggestion"]

    @patch("core.validate.db._check_db_encoding", return_value="SQL_ASCII")
    @patch("core.validate.db.Connection")
    def test_sanitizes_non_ascii_on_sql_ascii_db(self, mock_conn_cls, mock_enc):
        """SQL_ASCII 库（US 服务器）：非 ASCII 字符（如 →）替换为 ?，避免写入失败。"""
        from core.validate import save_results, ValidationReport, ValidationIssue

        report = ValidationReport(started_at="2026-01-01T00:00:00")
        report.issues.append(
            ValidationIssue(
                stock_code="AAPL",
                market="US",
                report_date="2024-12-31",
                check_name="market_cap_jump",
                severity="warning",
                field_name="market_cap",
                message="close 9.90→10.00, mcap $0.50B→$1.00B",
            )
        )

        mock_cur = MagicMock()
        mock_cur.rowcount = 1
        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_conn_cls.return_value.__enter__.return_value = mock_conn

        save_results(report, "20260101_120000")

        params_list = mock_cur.executemany.call_args[0][1]
        row = params_list[0]
        assert "→" not in row["message"]
        assert "?" in row["message"]


# ── Integration: run_validation ─────────────────────────

class TestRunValidation:
    @patch("core.validate.save_results")
    @patch("core.validate.check_market_cap_jump")
    @patch("core.validate.check_standalone_cross_validation_us")
    @patch("core.validate.check_cross_source")
    @patch("core.validate.check_logic_us")
    @patch("core.validate.check_logic_cn_hk")
    @patch("core.validate.check_anomalies_us")
    @patch("core.validate.check_anomalies_cn_hk")
    @patch("core.validate.ensure_table")
    def test_run_validation_market_a(self, mock_ensure, mock_anomalies, mock_anomalies_us,
                                      mock_logic, mock_logic_us, mock_cross,
                                      mock_standalone_us, mock_mcap_jump, mock_save):
        from core.validate import run_validation, ValidationIssue

        mock_anomalies.return_value = 100
        mock_logic.return_value = 100
        mock_cross.return_value = 0
        mock_save.return_value = 0
        mock_standalone_us.return_value = 0
        mock_mcap_jump.return_value = 0

        report = run_validation(market="A")
        assert report.market == "A"
        assert report.total_rows_scanned == 200  # 100 + 100
        mock_anomalies.assert_called_once()
        mock_logic.assert_called_once()
        mock_anomalies_us.assert_not_called()
        mock_standalone_us.assert_not_called()
        mock_mcap_jump.assert_called_once()
        assert mock_mcap_jump.call_args[1]["market"] == "A"

    @patch("core.validate.save_results")
    @patch("core.validate.check_market_cap_jump")
    @patch("core.validate.check_cross_source")
    @patch("core.validate.check_standalone_cross_validation_us")
    @patch("core.validate.check_logic_us")
    @patch("core.validate.check_anomalies_us")
    @patch("core.validate.ensure_table")
    def test_run_validation_market_us(self, mock_ensure, mock_anomalies_us, mock_logic_us,
                                       mock_standalone_us, mock_cross, mock_mcap_jump, mock_save,
                                       monkeypatch):
        from core.validate import run_validation

        # Phase B3b：与 US_VALIDATION_SNAPSHOT_CURRENT 环境隔离，固定走 legacy 分支
        monkeypatch.delenv("US_VALIDATION_SNAPSHOT_CURRENT", raising=False)

        mock_anomalies_us.return_value = 50
        mock_logic_us.return_value = 50
        mock_cross.return_value = 0
        mock_save.return_value = 0
        mock_standalone_us.return_value = 0
        mock_mcap_jump.return_value = 0

        report = run_validation(market="US")
        assert report.market == "US"
        assert report.total_rows_scanned == 100  # 50 + 50
        mock_anomalies_us.assert_called_once()
        mock_logic_us.assert_called_once()
        mock_standalone_us.assert_called_once()
        mock_mcap_jump.assert_called_once()
        assert mock_mcap_jump.call_args[1]["market"] == "US"


# ── Market Cap Jump ──────────────────────────────────────


class TestCheckMarketCapJump:
    """测试 check_market_cap_jump 的市场参数化及 LATERAL JOIN 去重。"""

    def _mock_jump_row(self, **overrides):
        """构建 check_market_cap_jump 期望的 jumps 行。"""
        defaults = (
            "000001",       # stock_code
            "CN_A",         # market
            "2024-12-31",   # trade_date (text from ::text cast)
            10.0,           # close
            1000.0,         # market_cap
            500.0,          # prev_mcap (one-day prior, normal mcap)
            9.9,            # prev_close
            100.0,          # total_shares (from LATERAL join)
        )
        row = list(defaults)
        keys = [
            "stock_code", "market", "trade_date",
            "close", "market_cap", "prev_mcap", "prev_close", "total_shares",
        ]
        for k, v in overrides.items():
            if k in keys:
                idx = keys.index(k)
                row[idx] = v
        return tuple(row)

    @patch("core.validate.db.execute")
    def test_market_a_only_scans_cn_a(self, mock_exec):
        """传入 market="A" 时，应仅扫描 CN_A 市场。"""
        from core.validate import check_market_cap_jump
        mock_exec.return_value = []  # 无异常行
        issues = []
        scanned = check_market_cap_jump(issues, market="A")
        assert scanned == 0
        # 验证 db.execute 收到正确的市场参数
        call_args = mock_exec.call_args[0]
        assert "CN_A" in call_args[1]  # params 中含 CN_A
        assert "CN_HK" not in call_args[1]  # 不应含 HK
        assert "US" not in call_args[1]      # 不应含 US

    @patch("core.validate.db.execute")
    def test_market_all_scans_all(self, mock_exec):
        """传入 market="" 时，应扫描全部三个市场。"""
        from core.validate import check_market_cap_jump
        mock_exec.return_value = []
        issues = []
        scanned = check_market_cap_jump(issues, market="")
        assert scanned == 0
        call_args = mock_exec.call_args[0]
        assert "CN_A" in call_args[1]
        assert "CN_HK" in call_args[1]
        assert "US" in call_args[1]

    @patch("core.validate.db.execute")
    def test_lateral_join_prevents_duplicate_anomalies(self, mock_exec):
        """多期股本不会重复生成异常——每个 db 行对应恰好一个 issue。

        场景：stock_share 有 3 条股本记录（不同日期），
        但 LEFT JOIN LATERAL ... LIMIT 1 确保 SQL 只返回 1 行，
        因此只产生 1 个 issue。
        """
        from core.validate import check_market_cap_jump
        # 单行数据：prev_mcap=500, mcap=1000 → 跳变 100% > 50%
        # close 10.0, prev_close 9.9 → 变化 1% < 10%
        mock_exec.return_value = [self._mock_jump_row()]
        issues = []
        scanned = check_market_cap_jump(issues, market="A")
        assert scanned == 1
        assert len(issues) == 1  # 1 行 → 1 个 issue，不会因多期股本而重复

    @patch("core.validate.db.execute")
    def test_no_jumps_returns_zero(self, mock_exec):
        """无跳变异常时返回 0。"""
        from core.validate import check_market_cap_jump
        mock_exec.return_value = []
        issues = []
        scanned = check_market_cap_jump(issues, market="US")
        assert scanned == 0
        assert len(issues) == 0

    @patch("core.validate.db.execute")
    def test_market_hk_maps_to_cn_hk(self, mock_exec):
        """market="HK" 应映射为 CN_HK。"""
        from core.validate import check_market_cap_jump
        mock_exec.return_value = []
        issues = []
        check_market_cap_jump(issues, market="HK")
        call_args = mock_exec.call_args[0]
        assert "CN_HK" in call_args[1]
        assert "CN_A" not in call_args[1]


# ── Output: JSON / CSV ─────────────────────────────────

class TestOutput:
    def test_output_json(self, tmp_path):
        from core.validate import ValidationReport, ValidationIssue, output_json
        report = ValidationReport(started_at="2026-01-01")
        report.issues = [ValidationIssue("000001", "CN_A", "2024-12-31",
                                          "test", "error", "field")]
        filepath = str(tmp_path / "test.json")
        output_json(report, filepath)

        import json
        with open(filepath) as f:
            data = json.load(f)
        assert data["summary"]["errors"] == 1
        assert len(data["issues"]) == 1

    def test_output_csv(self, tmp_path):
        from core.validate import ValidationReport, ValidationIssue, output_csv
        report = ValidationReport(started_at="2026-01-01")
        report.issues = [ValidationIssue("000001", "CN_A", "2024-12-31",
                                          "test", "warning", "field")]
        filepath = str(tmp_path / "test.csv")
        output_csv(report, filepath)

        with open(filepath) as f:
            lines = f.readlines()
        assert len(lines) == 2  # header + 1 row
        assert "test" in lines[1]
