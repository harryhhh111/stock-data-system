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


# ── Integration: run_validation ─────────────────────────

class TestRunValidation:
    @patch("core.validate.save_results")
    @patch("core.validate.check_cross_source")
    @patch("core.validate.check_logic_us")
    @patch("core.validate.check_logic_cn_hk")
    @patch("core.validate.check_anomalies_us")
    @patch("core.validate.check_anomalies_cn_hk")
    @patch("core.validate.ensure_table")
    def test_run_validation_market_a(self, mock_ensure, mock_anomalies, mock_anomalies_us,
                                      mock_logic, mock_logic_us, mock_cross, mock_save):
        from core.validate import run_validation, ValidationIssue

        mock_anomalies.return_value = 100
        mock_logic.return_value = 100
        mock_cross.return_value = 0
        mock_save.return_value = 0

        report = run_validation(market="A")
        assert report.market == "A"
        assert report.total_rows_scanned == 200  # 100 + 100
        mock_anomalies.assert_called_once()
        mock_logic.assert_called_once()
        mock_anomalies_us.assert_not_called()

    @patch("core.validate.save_results")
    @patch("core.validate.check_cross_source")
    @patch("core.validate.check_logic_us")
    @patch("core.validate.check_anomalies_us")
    @patch("core.validate.ensure_table")
    def test_run_validation_market_us(self, mock_ensure, mock_anomalies_us, mock_logic_us,
                                       mock_cross, mock_save):
        from core.validate import run_validation

        mock_anomalies_us.return_value = 50
        mock_logic_us.return_value = 50
        mock_cross.return_value = 0
        mock_save.return_value = 0

        report = run_validation(market="US")
        assert report.market == "US"
        assert report.total_rows_scanned == 100


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
