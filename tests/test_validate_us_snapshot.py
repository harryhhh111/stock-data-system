"""tests/test_validate_us_snapshot.py — Phase B3b 美股校验版本层路径单元测试。

覆盖规格 §6：
1. 开关分发（关闭走 legacy、开启走版本层，CN 不受影响）；
2. 新路径静态/运行时不读六个旧对象；fcf_roe_check 新分支不读旧视图/供应商估值；
3. pivot 粒度正确性（不跨 period_start 合并、撞键取无维度）；
4. standalone 重建（分类、差异检出、Q4 排除、缺侧跳过计数、歧义计数）；
5. 缺失语义与旧路径一致（NULL 跳过不报问题）；
7. 影子脚本对齐逻辑、空集、显式错误路径。

实库样本（CAT/AA、PR/FANG/PDD、PLTR）见
tests/test_validate_us_snapshot_integration.py（us_integration 标记）。
"""

from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import pytest

from core import validate_us_snapshot as vus
from core.validate import ValidationIssue

FLAG = "US_VALIDATION_SNAPSHOT_CURRENT"


# ── helpers ──────────────────────────────────────────────


def _fact(**kw):
    """构造 _pivot_facts 需要的 SelectedFact 形命名空间。"""
    defaults = dict(
        stock_code="TEST",
        period_kind="duration",
        period_start=date(2024, 1, 1),
        report_date=date(2024, 3, 31),
        unit="USD",
        form="10-Q",
        fiscal_period_raw="Q1",
        value_numeric=Decimal("100"),
        standard_field="revenues",
        dimensions={},
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _pivot_row(**kw):
    defaults = dict(
        stock_code="TEST",
        period_kind="instant",
        period_start=None,
        report_date=date(2024, 12, 31),
        unit="USD",
        forms={"10-K"},
        fiscal_periods={"FY"},
    )
    defaults.update(kw)
    return defaults


def _candidate(**kw):
    """构造 standalone 校验的原始候选事实（_load 返回的行格式）。"""
    defaults = dict(
        fact_version_id=1,
        stock_code="TEST",
        accession_no="0001-24-000001",
        sec_tag="Revenues",
        period_start=date(2024, 1, 1),
        report_date=date(2024, 3, 31),
        form="10-Q",
        filed_date=date(2024, 5, 1),
        value_numeric=Decimal("100"),
    )
    defaults.update(kw)
    return defaults


def _quarter_set(q2_cumulative=Decimal("200"), q2_accn="0001-24-000002"):
    """一组自洽的 FY2024（12 月财年末）季度候选：Q1/Q2 standalone + H1 cumulative + 年度。"""
    return [
        _candidate(  # 年度（用于推导财年末月份）
            fact_version_id=10,
            accession_no="0001-25-000010",
            period_start=date(2024, 1, 1),
            report_date=date(2024, 12, 31),
            form="10-K",
            filed_date=date(2025, 2, 1),
            value_numeric=Decimal("400"),
        ),
        _candidate(  # Q1 standalone
            fact_version_id=11,
            accession_no="0001-24-000001",
            period_start=date(2024, 1, 1),
            report_date=date(2024, 3, 31),
            filed_date=date(2024, 5, 1),
            value_numeric=Decimal("100"),
        ),
        _candidate(  # Q2 standalone（与 H1 cumulative 同 accession）
            fact_version_id=12,
            accession_no=q2_accn,
            period_start=date(2024, 4, 1),
            report_date=date(2024, 6, 30),
            filed_date=date(2024, 8, 1),
            value_numeric=Decimal("100"),
        ),
        _candidate(  # H1 cumulative
            fact_version_id=13,
            accession_no="0001-24-000002",
            period_start=date(2024, 1, 1),
            report_date=date(2024, 6, 30),
            filed_date=date(2024, 8, 1),
            value_numeric=q2_cumulative,
        ),
    ]


# ── 开关 ────────────────────────────────────────────────


class TestFlag:
    def test_default_off(self, monkeypatch):
        monkeypatch.delenv(FLAG, raising=False)
        assert vus.us_validation_snapshot_enabled() is False

    @pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE"])
    def test_on_values(self, monkeypatch, val):
        monkeypatch.setenv(FLAG, val)
        assert vus.us_validation_snapshot_enabled() is True

    def test_off_value(self, monkeypatch):
        monkeypatch.setenv(FLAG, "0")
        assert vus.us_validation_snapshot_enabled() is False


# ── pivot（规格 §3.3：粒度不提前合并）─────────────────────


class TestPivot:
    def test_no_merge_across_period_start(self):
        facts = [
            _fact(standard_field="revenues", period_start=date(2024, 1, 1),
                  report_date=date(2024, 6, 30), value_numeric=Decimal("200")),
            _fact(standard_field="revenues", period_start=date(2024, 4, 1),
                  report_date=date(2024, 6, 30), value_numeric=Decimal("100")),
        ]
        rows = vus._pivot_facts(facts)
        assert len(rows) == 2  # 同报告日累计与单季不得混成一行
        assert {r["period_start"] for r in rows} == {date(2024, 1, 1), date(2024, 4, 1)}

    def test_instant_and_duration_separate(self):
        facts = [
            _fact(standard_field="revenues", period_kind="duration",
                  period_start=date(2024, 1, 1), report_date=date(2024, 12, 31)),
            _fact(standard_field="total_assets", period_kind="instant",
                  period_start=None, report_date=date(2024, 12, 31)),
        ]
        rows = vus._pivot_facts(facts)
        assert len(rows) == 2
        kinds = {r["period_kind"] for r in rows}
        assert kinds == {"duration", "instant"}

    def test_fields_pivot_into_same_period_row(self):
        facts = [
            _fact(standard_field="revenues", value_numeric=Decimal("100")),
            _fact(standard_field="net_income", value_numeric=Decimal("20")),
        ]
        rows = vus._pivot_facts(facts)
        assert len(rows) == 1
        assert rows[0]["revenues"] == 100.0
        assert rows[0]["net_income"] == 20.0

    def test_collision_prefers_dimensionless_and_counts(self):
        stats = {}
        facts = [
            _fact(value_numeric=Decimal("100"), dimensions={"segment": "x"}),
            _fact(value_numeric=Decimal("200"), dimensions={}),
        ]
        rows = vus._pivot_facts(facts, stats)
        assert len(rows) == 1
        assert rows[0]["revenues"] == 200.0
        assert stats["pivot_field_collision"] == 1

    def test_non_usd_skipped_and_counted(self):
        stats = {}
        rows = vus._pivot_facts([_fact(unit="EUR")], stats)
        assert rows == []
        assert stats["non_usd_skipped"] == 1


# ── 异常值检测（新路径）──────────────────────────────────


class TestAnomaliesSnapshot:
    def test_negative_total_assets_on_instant_row(self):
        issues = []
        scanned = vus.check_anomalies_us_snapshot(
            [_pivot_row(total_assets=-1000.0)], issues
        )
        assert scanned == 1
        assert any(i.check_name == "negative_total_assets" and i.severity == "error"
                   for i in issues)

    def test_debt_ratio_extreme(self):
        issues = []
        vus.check_anomalies_us_snapshot(
            [_pivot_row(total_assets=100.0, total_liabilities=250.0)], issues
        )
        assert any(i.check_name == "debt_ratio_extreme" for i in issues)

    def test_net_income_exceeds_revenue_on_duration_row(self):
        row = _pivot_row(period_kind="duration", period_start=date(2024, 1, 1),
                         revenues=100.0, net_income=200.0)
        issues = []
        vus.check_anomalies_us_snapshot([row], issues)
        assert any(i.check_name == "net_income_exceeds_revenue" for i in issues)

    def test_cfo_negative_income_positive(self):
        row = _pivot_row(period_kind="duration", period_start=date(2024, 1, 1),
                         net_income=100.0, net_cash_from_operations=-50.0)
        issues = []
        vus.check_anomalies_us_snapshot([row], issues)
        assert any(i.check_name == "cfo_negative_income_positive" for i in issues)

    def test_period_rows_validated_independently(self):
        """同一报告日的累计行与单季行各占一行、各自校验（不合并）。"""
        rows = [
            _pivot_row(period_kind="duration", period_start=date(2024, 1, 1),
                       report_date=date(2024, 6, 30),
                       revenues=200.0, net_income=400.0),  # 累计：触发
            _pivot_row(period_kind="duration", period_start=date(2024, 4, 1),
                       report_date=date(2024, 6, 30),
                       revenues=100.0, net_income=50.0),   # 单季：不触发
        ]
        issues = []
        scanned = vus.check_anomalies_us_snapshot(rows, issues)
        assert scanned == 2
        matched = [i for i in issues if i.check_name == "net_income_exceeds_revenue"]
        assert len(matched) == 1
        assert "400" in matched[0].actual_value

    def test_missing_fields_skip_silently_like_legacy(self):
        """缺失语义：字段为 NULL 时对应检查跳过不报问题（与 legacy 一致）。"""
        issues = []
        scanned = vus.check_anomalies_us_snapshot([_pivot_row()], issues)
        assert scanned == 0  # 无任何字段值的 instant 行不计入扫描
        assert issues == []
        # duration 行只有 revenues：net_income/cfo 相关检查跳过，不报问题
        row = _pivot_row(period_kind="duration", period_start=date(2024, 1, 1),
                         revenues=100.0)
        issues2 = []
        scanned2 = vus.check_anomalies_us_snapshot([row], issues2)
        assert scanned2 == 1
        assert issues2 == []


# ── 会计等式（新路径）────────────────────────────────────


class TestLogicSnapshot:
    def test_equation_ok(self):
        issues = []
        scanned = vus.check_logic_us_snapshot(
            [_pivot_row(total_assets=350.0, total_liabilities=290.0,
                        total_equity=60.0)],
            issues,
        )
        assert scanned == 1
        assert not any(i.check_name == "balance_equation" for i in issues)

    def test_equation_broken(self):
        issues = []
        vus.check_logic_us_snapshot(
            [_pivot_row(total_assets=350.0, total_liabilities=290.0,
                        total_equity=50.0)],
            issues,
        )
        broken = [i for i in issues if i.check_name == "balance_equation"]
        assert len(broken) == 1
        assert broken[0].severity == "error"

    def test_nci_fallback(self):
        issues = []
        vus.check_logic_us_snapshot(
            [_pivot_row(total_assets=350.0, total_liabilities=290.0,
                        total_equity=58.0, total_equity_including_nci=60.0)],
            issues,
        )
        assert not any(i.check_name == "balance_equation" for i in issues)

    def test_cash_exceeds_current_assets(self):
        issues = []
        vus.check_logic_us_snapshot(
            [_pivot_row(total_assets=350.0, total_liabilities=290.0,
                        total_equity=60.0, total_current_assets=100.0,
                        cash_and_equivalents=200.0)],
            issues,
        )
        assert any(i.check_name == "cash_exceeds_current_assets" for i in issues)

    def test_missing_equation_fields_not_scanned(self):
        """缺失语义与 legacy WHERE 一致：三字段缺一不计扫描、不报问题。"""
        issues = []
        scanned = vus.check_logic_us_snapshot(
            [_pivot_row(total_assets=350.0, total_liabilities=290.0)],  # 缺 equity
            issues,
        )
        assert scanned == 0
        assert issues == []

    def test_no_merge_across_period_start(self):
        """规格 §3.3.3：跨 period_start 的事实不得合并互补。"""
        rows = [
            # instant 行只有 assets
            _pivot_row(period_kind="instant", period_start=None,
                       total_assets=350.0),
            # 另一期间行有 liab/equity——不得被并入 instant 行
            _pivot_row(period_kind="duration", period_start=date(2024, 1, 1),
                       total_liabilities=290.0, total_equity=60.0),
        ]
        merged = vus._merge_same_period_rows(rows)
        assert len(merged) == 2
        issues = []
        scanned = vus.check_logic_us_snapshot(rows, issues)
        assert scanned == 0  # 两行都凑不齐三字段，不得跨期间互补
        assert issues == []


# ── standalone 跨季重建（规格 §3.4）───────────────────────


class TestStandaloneRebuild:
    def _run(self, candidates):
        issues, stats = [], {}
        scanned = vus.check_standalone_cross_validation_us_snapshot(
            issues, stats=stats, candidates=candidates
        )
        return issues, stats, scanned

    def test_consistent_quarters_no_issue(self):
        issues, stats, scanned = self._run(_quarter_set())
        assert not any(i.check_name == "standalone_cross_quarter_sum" for i in issues)
        assert scanned == 4
        assert stats.get("missing_standalone", 0) == 0
        assert stats.get("missing_cumulative", 0) == 0

    def test_discrepancy_detected(self):
        # H1 cumulative 报 250，但 Q1+Q2 standalone 只有 200，diff=50 > max(2.5, 10M)?
        # 50 不大于 10M 阈值——用更大数值构造
        candidates = [
            _candidate(fact_version_id=10, accession_no="A-10K",
                       period_start=date(2024, 1, 1), report_date=date(2024, 12, 31),
                       form="10-K", filed_date=date(2025, 2, 1),
                       value_numeric=Decimal("4000000000")),
            _candidate(fact_version_id=11, accession_no="A-Q1",
                       period_start=date(2024, 1, 1), report_date=date(2024, 3, 31),
                       filed_date=date(2024, 5, 1), value_numeric=Decimal("1000000000")),
            _candidate(fact_version_id=12, accession_no="A-Q2",
                       period_start=date(2024, 4, 1), report_date=date(2024, 6, 30),
                       filed_date=date(2024, 8, 1), value_numeric=Decimal("1000000000")),
            _candidate(fact_version_id=13, accession_no="A-Q2",
                       period_start=date(2024, 1, 1), report_date=date(2024, 6, 30),
                       filed_date=date(2024, 8, 1), value_numeric=Decimal("2100000000")),
        ]
        issues, stats, _ = self._run(candidates)
        matched = [i for i in issues if i.check_name == "standalone_cross_quarter_sum"]
        assert len(matched) == 1
        assert matched[0].severity == "error"
        assert "FY2024 Q2" in matched[0].message

    def test_q4_excluded(self):
        # 财年末（12 月）的 cumulative（7/1→12/31，183 天）应被排除并计数
        candidates = _quarter_set() + [
            _candidate(fact_version_id=14, accession_no="A-Q4",
                       period_start=date(2024, 7, 1), report_date=date(2024, 12, 31),
                       form="10-K", filed_date=date(2025, 2, 1),
                       value_numeric=Decimal("999")),
        ]
        issues, stats, _ = self._run(candidates)
        assert stats.get("q4_excluded", 0) == 1
        assert not any(i.check_name == "standalone_cross_quarter_sum" for i in issues)

    def test_missing_standalone_counted(self):
        # 有 H1 cumulative 但缺 Q1 standalone → 缺侧计数，不报问题
        candidates = [c for c in _quarter_set() if c["fact_version_id"] != 11]
        issues, stats, _ = self._run(candidates)
        assert stats.get("missing_standalone", 0) == 1
        assert not any(i.check_name == "standalone_cross_quarter_sum" for i in issues)

    def test_missing_cumulative_counted(self):
        # 有 Q1+Q2 standalone 但无 H1 cumulative → 缺侧计数
        candidates = [c for c in _quarter_set() if c["fact_version_id"] != 13]
        issues, stats, _ = self._run(candidates)
        assert stats.get("missing_cumulative", 0) == 1
        assert not any(i.check_name == "standalone_cross_quarter_sum" for i in issues)

    def test_ambiguous_candidates_counted(self):
        # 同 accession 同期间两个不同值（同 tag、不同 filed_date）→ 歧义计数
        candidates = _quarter_set() + [
            _candidate(fact_version_id=15, accession_no="0001-24-000001",
                       period_start=date(2024, 1, 1), report_date=date(2024, 3, 31),
                       filed_date=date(2024, 5, 2), value_numeric=Decimal("111")),
        ]
        issues, stats, _ = self._run(candidates)
        assert stats.get("ambiguous_candidates", 0) == 1

    def test_undeterminable_fiscal_year_counted(self):
        # 无年度事实 → 财年无法推导，cumulative 候选按原因计数
        candidates = [c for c in _quarter_set() if c["fact_version_id"] != 10]
        issues, stats, _ = self._run(candidates)
        assert stats.get("undeterminable_fiscal_year", 0) == 1
        assert not any(i.check_name == "standalone_cross_quarter_sum" for i in issues)

    def test_pairing_requires_same_accession(self):
        # 末段 standalone 与 cumulative 不同 accession → 视为缺 standalone
        candidates = _quarter_set(q2_accn="0001-24-000099")
        issues, stats, _ = self._run(candidates)
        assert stats.get("missing_standalone", 0) == 1
        assert not any(i.check_name == "standalone_cross_quarter_sum" for i in issues)

    def test_negative_standalone_and_cumulative_warnings(self):
        candidates = _quarter_set()
        candidates[1]["value_numeric"] = Decimal("-100")  # Q1 standalone 为负
        candidates.append(
            _candidate(fact_version_id=16, accession_no="A-Q3",
                       period_start=date(2024, 1, 1), report_date=date(2024, 9, 30),
                       filed_date=date(2024, 11, 1), value_numeric=Decimal("-5"))
        )
        issues, _, _ = self._run(candidates)
        assert any(i.check_name == "negative_standalone_revenue"
                   and i.severity == "warning" for i in issues)
        assert any(i.check_name == "negative_cumulative_revenue"
                   and i.severity == "warning" for i in issues)

    def test_canonical_tag_priority_reused(self):
        # 同 accession 内 SalesRevenueNet 与 Revenues 并存、同 filed_date 时，
        # 归一后只留一个候选（不歧义、不重复计 scanned）
        candidates = _quarter_set() + [
            _candidate(fact_version_id=17, accession_no="0001-24-000001",
                       sec_tag="SalesRevenueNet",
                       period_start=date(2024, 1, 1), report_date=date(2024, 3, 31),
                       filed_date=date(2024, 5, 1), value_numeric=Decimal("100")),
        ]
        issues, stats, scanned = self._run(candidates)
        assert stats.get("ambiguous_candidates", 0) == 0
        # 同值别名展开：Q1 的 SalesRevenueNet 与 Revenues 各成一条 (期间, tag) 条目
        assert scanned == 5

    def test_cross_tag_mixing_not_compared(self):
        """AEP 场景：canonical largest-abs 规则对不同期间可能选不同 tag，
        跨 tag 的链与 cumulative 不得混比（规格 §3.4 防假阳性），按缺侧计数。"""
        candidates = [
            _candidate(fact_version_id=10, accession_no="A-10K",
                       period_start=date(2024, 1, 1), report_date=date(2024, 12, 31),
                       form="10-K", filed_date=date(2025, 2, 1),
                       value_numeric=Decimal("400")),
            # Q1 standalone 只有 RFC tag（largest 规则在 Q1 选了它）
            _candidate(fact_version_id=11, accession_no="A-Q1",
                       sec_tag="RevenueFromContractWithCustomerExcludingAssessedTax",
                       period_start=date(2024, 1, 1), report_date=date(2024, 3, 31),
                       filed_date=date(2024, 5, 1), value_numeric=Decimal("5631600000")),
            # Q2 standalone 与 H1 cumulative 只有 Revenues tag
            _candidate(fact_version_id=12, accession_no="A-Q2",
                       period_start=date(2024, 4, 1), report_date=date(2024, 6, 30),
                       filed_date=date(2024, 8, 1), value_numeric=Decimal("5086900000")),
            _candidate(fact_version_id=13, accession_no="A-Q2",
                       period_start=date(2024, 1, 1), report_date=date(2024, 6, 30),
                       filed_date=date(2024, 8, 1), value_numeric=Decimal("10550300000")),
        ]
        issues, stats, _ = self._run(candidates)
        # Revenues tag 的链缺 Q1 → 缺侧计数，不得用 RFC 的 Q1 混比报假阳性
        assert stats.get("missing_standalone", 0) == 1
        assert not any(i.check_name == "standalone_cross_quarter_sum" for i in issues)

    def test_vintage_aligned_chain(self):
        """AWI 场景：Q1 有后续重述版本，但与 2017 年 cumulative 配对时必须用
        cumulative filed_date 之前的版本（315.4+330.8==646.2），不得拿 2018
        重述值（219.8）混进 2017 vintage 的链。"""
        candidates = [
            _candidate(fact_version_id=10, accession_no="A-10K",
                       period_start=date(2017, 1, 1), report_date=date(2017, 12, 31),
                       form="10-K", filed_date=date(2018, 2, 1),
                       value_numeric=Decimal("1300000000")),
            # Q1 原报 315.4M（2017），2018 重述为 219.8M
            _candidate(fact_version_id=11, accession_no="A-Q1-17",
                       period_start=date(2017, 1, 1), report_date=date(2017, 3, 31),
                       filed_date=date(2017, 5, 1), value_numeric=Decimal("315400000")),
            _candidate(fact_version_id=12, accession_no="A-Q1-18",
                       period_start=date(2017, 1, 1), report_date=date(2017, 3, 31),
                       filed_date=date(2018, 4, 30), value_numeric=Decimal("219800000")),
            # Q2 standalone 与 H1 cumulative 仅 2017 vintage
            _candidate(fact_version_id=13, accession_no="A-Q2-17",
                       period_start=date(2017, 4, 1), report_date=date(2017, 6, 30),
                       filed_date=date(2017, 7, 31), value_numeric=Decimal("330800000")),
            _candidate(fact_version_id=14, accession_no="A-Q2-17",
                       period_start=date(2017, 1, 1), report_date=date(2017, 6, 30),
                       filed_date=date(2017, 7, 31), value_numeric=Decimal("646200000")),
        ]
        issues, stats, _ = self._run(candidates)
        assert not any(i.check_name == "standalone_cross_quarter_sum" for i in issues)
        assert stats.get("missing_standalone", 0) == 0

    def test_equal_value_tag_alias_interchangeable(self):
        """CBRE 场景：同 accession 内两个 tag 同值时互为别名，链匹配可互换，
        不得因 canonical 归一只保留一个 tag 而误报。"""
        candidates = [
            _candidate(fact_version_id=10, accession_no="A-10K",
                       period_start=date(2024, 1, 1), report_date=date(2024, 12, 31),
                       form="10-K", filed_date=date(2025, 2, 1),
                       value_numeric=Decimal("400")),
            # Q1：重述 accession 中 RFC 与 Revenues 同值（100），canonical 保留 RFC
            _candidate(fact_version_id=11, accession_no="A-Q1R",
                       sec_tag="RevenueFromContractWithCustomerExcludingAssessedTax",
                       period_start=date(2024, 1, 1), report_date=date(2024, 3, 31),
                       filed_date=date(2024, 7, 15), value_numeric=Decimal("100")),
            _candidate(fact_version_id=12, accession_no="A-Q1R",
                       sec_tag="Revenues",
                       period_start=date(2024, 1, 1), report_date=date(2024, 3, 31),
                       filed_date=date(2024, 7, 15), value_numeric=Decimal("100")),
            # Q2 standalone 与 H1 cumulative 只有 Revenues
            _candidate(fact_version_id=13, accession_no="A-Q2",
                       period_start=date(2024, 4, 1), report_date=date(2024, 6, 30),
                       filed_date=date(2024, 8, 1), value_numeric=Decimal("100")),
            _candidate(fact_version_id=14, accession_no="A-Q2",
                       period_start=date(2024, 1, 1), report_date=date(2024, 6, 30),
                       filed_date=date(2024, 8, 1), value_numeric=Decimal("200")),
        ]
        issues, stats, scanned = self._run(candidates)
        # Q1 的 Revenues 别名参与配对：链 100+100 == 累计 200，不报问题
        assert not any(i.check_name == "standalone_cross_quarter_sum" for i in issues)
        assert stats.get("missing_standalone", 0) == 0
        # 别名展开为 (期间, tag) 条目：Q1 的 RFC 与 Revenues 各一条
        assert scanned == 5


# ── 新路径不读六个旧对象（静态 + 运行时）──────────────────


_LEGACY_FORBIDDEN = (
    "mv_us_fcf_yield",
    "mv_us_indicator_ttm",
    "mv_us_financial_indicator",
    "us_income_statement",
    "us_balance_sheet",
    "us_cash_flow_statement",
)


class TestNoLegacyObjects:
    def test_validate_us_snapshot_source_clean(self):
        import inspect

        src = inspect.getsource(vus)
        for name in _LEGACY_FORBIDDEN:
            assert name not in src, f"validate_us_snapshot 引用了旧对象 {name}"

    def test_fcf_roe_check_snapshot_branch_source_clean(self):
        import inspect

        from quant.checks import fcf_roe_check

        for fn in (
            fcf_roe_check._get_fcf_screen_us_snapshot,
            fcf_roe_check._get_roe_history_us_snapshot,
        ):
            src = inspect.getsource(fn)
            for name in _LEGACY_FORBIDDEN:
                assert name not in src
        sql = fcf_roe_check._SQL_US_ROE_HISTORY_SNAPSHOT
        assert "us_financial_current_annual" in sql
        for name in _LEGACY_FORBIDDEN:
            assert name not in sql
        # 不读供应商 PE/PB：snapshot 分支 SQL 不含 pe_ttm/pb 列引用
        assert "pe_ttm" not in sql

    def test_standalone_candidate_sql_runtime_clean(self):
        """运行时：standalone 候选 SQL 只读版本事实表，不读旧对象。"""
        recorded = []

        def spy_execute(sql, params=None, **kwargs):
            recorded.append(sql)
            return []

        issues, stats = [], {}
        with patch.object(vus.db, "execute", side_effect=spy_execute):
            vus.check_standalone_cross_validation_us_snapshot(issues, stats=stats)
        assert recorded, "应至少执行一次候选查询"
        for sql in recorded:
            assert "us_financial_fact_version" in sql
            for name in _LEGACY_FORBIDDEN:
                assert name not in sql


# ── 开关分发（run_validation / fcf_roe_check）──────────────


class TestSwitchDispatch:
    def _patch_legacy_us_checks(self):
        return (
            patch("core.validate.check_anomalies_us", return_value=10),
            patch("core.validate.check_logic_us", return_value=20),
            patch("core.validate.check_standalone_cross_validation_us", return_value=30),
            patch("core.validate.check_market_cap_jump", return_value=0),
            patch("core.validate.check_cross_source", return_value=0),
            patch("core.validate.save_results", return_value=0),
            patch("core.validate.ensure_table"),
        )

    def test_flag_off_runs_legacy(self, monkeypatch):
        monkeypatch.delenv(FLAG, raising=False)
        p = self._patch_legacy_us_checks()
        with p[0] as m_anom, p[1] as m_logic, p[2] as m_std, p[3], p[4], p[5], p[6]:
            with patch.object(vus, "run_us_snapshot_checks") as m_new:
                from core.validate import run_validation

                report = run_validation(market="US")
        m_anom.assert_called_once()
        m_logic.assert_called_once()
        m_std.assert_called_once()
        m_new.assert_not_called()
        assert report.total_rows_scanned == 60

    def test_flag_on_runs_snapshot(self, monkeypatch):
        monkeypatch.setenv(FLAG, "1")
        p = self._patch_legacy_us_checks()
        with p[0] as m_anom, p[1], p[2], p[3], p[4], p[5], p[6]:
            with patch.object(
                vus,
                "run_us_snapshot_checks",
                return_value={"anomalies": 1, "logic": 2, "standalone": 3},
            ) as m_new:
                from core.validate import run_validation

                report = run_validation(market="US")
        m_anom.assert_not_called()
        m_new.assert_called_once()
        assert report.total_rows_scanned == 6

    def test_cn_unaffected_by_flag(self, monkeypatch):
        monkeypatch.setenv(FLAG, "1")
        with (
            patch("core.validate.check_anomalies_cn_hk", return_value=5) as m_cn,
            patch("core.validate.check_logic_cn_hk", return_value=5),
            patch("core.validate.check_market_cap_jump", return_value=0),
            patch("core.validate.check_cross_source", return_value=0),
            patch("core.validate.save_results", return_value=0),
            patch("core.validate.ensure_table"),
            patch.object(vus, "run_us_snapshot_checks") as m_new,
        ):
            from core.validate import run_validation

            report = run_validation(market="A")
        m_cn.assert_called_once()
        m_new.assert_not_called()
        assert report.total_rows_scanned == 10


class TestFcfRoeCheckDispatch:
    def test_flag_off_uses_legacy_view(self, monkeypatch):
        monkeypatch.delenv(FLAG, raising=False)
        from quant.checks import fcf_roe_check

        with patch("quant.checks.fcf_roe_check.pd.read_sql") as m_read:
            m_read.return_value = pd.DataFrame()
            with patch("quant.checks.fcf_roe_check.Connection"):
                fcf_roe_check.get_fcf_screen("US")
        sql = m_read.call_args[0][0]
        assert "mv_us_fcf_yield" in sql

    def test_flag_on_uses_snapshot_universe(self, monkeypatch):
        monkeypatch.setenv(FLAG, "1")
        from quant.checks import fcf_roe_check

        universe = pd.DataFrame([
            # 正常入选
            dict(stock_code="GOOD", stock_name="Good Co", market="US",
                 industry="Software", fcf_ttm=2e9, fcf_yield=0.12,
                 cfo_ttm=2.5e9, market_cap=1.6e10, pe_ttm=20.0, pb=3.0,
                 close=100.0, ttm_report_date=date(2025, 12, 31)),
            # fcf_ttm NULL（含已登记 exception）→ 不得入选
            dict(stock_code="PR", stock_name="Exception Co", market="US",
                 industry="Software", fcf_ttm=None, fcf_yield=None,
                 cfo_ttm=None, market_cap=1e10, pe_ttm=None, pb=None,
                 close=50.0, ttm_report_date=None),
            # 排除行业
            dict(stock_code="BANK", stock_name="Bank", market="US",
                 industry="National Commercial Banks", fcf_ttm=2e9,
                 fcf_yield=0.2, cfo_ttm=2e9, market_cap=1e10, pe_ttm=10.0,
                 pb=1.0, close=30.0, ttm_report_date=date(2025, 12, 31)),
            # 市值不过滤阈值
            dict(stock_code="TINY", stock_name="Tiny", market="US",
                 industry="Software", fcf_ttm=1e6, fcf_yield=0.5,
                 cfo_ttm=1e6, market_cap=2e6, pe_ttm=5.0, pb=1.0,
                 close=3.0, ttm_report_date=date(2025, 12, 31)),
        ])
        with patch(
            "quant.analyzer.query_us.load_us_snapshot_universe",
            return_value=universe,
        ):
            df = fcf_roe_check.get_fcf_screen("US", min_yield=0.10, min_mcap=1e9)
        assert df["stock_code"].tolist() == ["GOOD"]
        row = df.iloc[0]
        assert row["fcf_yield"] == pytest.approx(0.12)
        assert row["market"] == "US"
        # 返回列契约与 legacy 一致
        for col in ("stock_name", "industry", "fcf_ttm", "cfo_ttm", "market_cap",
                    "pe_ttm", "pb", "close", "ttm_report_date"):
            assert col in df.columns

    def test_flag_on_errors_propagate(self, monkeypatch):
        """新路径 DB/数据错误必须显式抛出，不得回退旧表（规格 §2.5）。"""
        monkeypatch.setenv(FLAG, "1")
        from quant.checks import fcf_roe_check

        with patch(
            "quant.analyzer.query_us.load_us_snapshot_universe",
            side_effect=RuntimeError("db down"),
        ):
            with pytest.raises(RuntimeError, match="db down"):
                fcf_roe_check.get_fcf_screen("US")

    def test_roe_history_flag_on_reads_snapshot_table(self, monkeypatch):
        monkeypatch.setenv(FLAG, "1")
        from quant.checks import fcf_roe_check

        with patch("quant.checks.fcf_roe_check.pd.read_sql") as m_read:
            m_read.return_value = pd.DataFrame()
            with patch("quant.checks.fcf_roe_check.Connection"):
                fcf_roe_check.get_roe_history("US", ["AAPL"])
        sql = m_read.call_args[0][0]
        assert "us_financial_current_annual" in sql
        for name in _LEGACY_FORBIDDEN:
            assert name not in sql

    def test_roe_history_flag_off_uses_legacy_view(self, monkeypatch):
        monkeypatch.delenv(FLAG, raising=False)
        from quant.checks import fcf_roe_check

        with patch("quant.checks.fcf_roe_check.pd.read_sql") as m_read:
            m_read.return_value = pd.DataFrame()
            with patch("quant.checks.fcf_roe_check.Connection"):
                fcf_roe_check.get_roe_history("US", ["AAPL"])
        assert "mv_us_financial_indicator" in m_read.call_args[0][0]

    def test_cn_roe_history_unaffected(self, monkeypatch):
        monkeypatch.setenv(FLAG, "1")
        from quant.checks import fcf_roe_check

        with patch("quant.checks.fcf_roe_check.pd.read_sql") as m_read:
            m_read.return_value = pd.DataFrame()
            with patch("quant.checks.fcf_roe_check.Connection"):
                fcf_roe_check.get_roe_history("CN_A", ["000001"])
        assert "mv_financial_indicator" in m_read.call_args[0][0]


# ── 影子对比脚本 ──────────────────────────────────────────


class TestShadowScript:
    def _issue(self, check, code, rd, field, severity="error"):
        return ValidationIssue(
            stock_code=code, market="US", report_date=rd,
            check_name=check, severity=severity, field_name=field,
        )

    def test_diff_categories(self):
        from scripts.compare_us_validation_snapshot_vs_legacy import diff_issues

        legacy = [
            self._issue("balance_equation", "AAA", "2024-12-31", "f1"),
            self._issue("debt_ratio_extreme", "BBB", "2024-12-31", "f2",
                        severity="warning"),
            self._issue("negative_total_assets", "CCC", "2024-12-31", "f3"),
        ]
        new = [
            self._issue("balance_equation", "AAA", "2024-12-31", "f1"),
            self._issue("debt_ratio_extreme", "BBB", "2024-12-31", "f2",
                        severity="error"),  # 严重度不同
            self._issue("net_income_exceeds_revenue", "DDD", "2024-12-31", "f4"),
        ]
        diffs = diff_issues(legacy, new)
        assert len(diffs["both_same"]) == 1
        assert len(diffs["severity_diff"]) == 1
        assert len(diffs["legacy_only"]) == 1
        assert len(diffs["new_only"]) == 1
        assert diffs["legacy_only"][0][1].stock_code == "CCC"
        assert diffs["new_only"][0][1].stock_code == "DDD"

    def test_diff_dedups_same_key(self):
        from scripts.compare_us_validation_snapshot_vs_legacy import diff_issues

        new = [
            self._issue("net_income_exceeds_revenue", "AAA", "2024-06-30", "f"),
            self._issue("net_income_exceeds_revenue", "AAA", "2024-06-30", "f"),
        ]
        diffs = diff_issues([], new)
        assert len(diffs["new_only"]) == 1
        assert diffs["new_dup_keys"] == 1

    def test_diff_empty_sets(self):
        from scripts.compare_us_validation_snapshot_vs_legacy import diff_issues

        diffs = diff_issues([], [])
        assert all(len(diffs[k]) == 0 for k in
                   ("both_same", "severity_diff", "legacy_only", "new_only"))

    def test_explicit_error_path(self, monkeypatch, tmp_path):
        """新路径出错必须显式失败（抛出/非零退出），不得静默回退。"""
        import scripts.compare_us_validation_snapshot_vs_legacy as shadow

        monkeypatch.setattr(
            shadow, "run_legacy_checks", lambda: ([], {"anomalies": 0})
        )

        def _boom(*a, **kw):
            raise RuntimeError("selector failed")

        monkeypatch.setattr(shadow.snapshot_validate, "run_us_snapshot_checks", _boom)
        monkeypatch.setattr(shadow, "OUTPUT_DIR", tmp_path)
        with pytest.raises(RuntimeError, match="selector failed"):
            shadow.main()
