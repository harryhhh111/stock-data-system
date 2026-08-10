"""tests/test_audit_adt_consolidated_cogs.py

ADT 合并 Cost of Revenue 证据审计(USQ-001)单元测试。
全部使用最小化 inline XBRL fixture,不依赖 SEC 网络与数据库。
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from scripts.audit_adt_consolidated_cogs import (
    collect_cost_candidates,
    compute_gross_margin,
    find_dimensionless_revenue,
    parse_inline_xbrl,
    parse_numeric,
    select_consolidated_total,
)

TARGET = "adt:CostofRevenueExcludingDepreciationDepletionandAmortization"
REV = "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"


def _ix_doc(facts: str, contexts: str) -> str:
    """最小化 inline XBRL 文档。"""
    return f"""<?xml version="1.0"?>
<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
      xmlns:xbrli="http://www.xbrl.org/2003/instance"
      xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
      xmlns:adt="http://www.adt.com/20251231"
      xmlns:us-gaap="http://fasb.org/us-gaap/2025"
      xmlns:srt="http://fasb.org/srt/2025">
<body>
<ix:hidden>{contexts}</ix:hidden>
{facts}
</body>
</html>"""


def _ctx(ctx_id: str, start: str, end: str, member: str | None = None) -> str:
    segment = ""
    if member:
        segment = (
            f'<xbrli:segment><xbrldi:explicitMember '
            f'dimension="srt:ProductOrServiceAxis">{member}</xbrldi:explicitMember>'
            f"</xbrli:segment>"
        )
    return (
        f'<xbrli:context id="{ctx_id}"><xbrli:entity>'
        f'<xbrli:identifier scheme="x">0001703056</xbrli:identifier>{segment}'
        f"</xbrli:entity><xbrli:period>"
        f"<xbrli:startDate>{start}</xbrli:startDate>"
        f"<xbrli:endDate>{end}</xbrli:endDate>"
        f"</xbrli:period></xbrli:context>"
    )


def _fact(tag: str, ctx: str, text: str, scale: int = 3) -> str:
    return (f'<ix:nonFraction name="{tag}" contextRef="{ctx}" unitRef="usd" '
            f'decimals="-3" scale="{scale}">{text}</ix:nonFraction>')


FY2025_CONTEXTS = (
    _ctx("c-1", "2025-01-01", "2025-12-31")
    + _ctx("c-2", "2025-01-01", "2025-12-31", "adt:MonitoringAndRelatedServicesMember")
    + _ctx("c-3", "2025-01-01", "2025-12-31", "adt:ProductsAndInstallationMember")
)

FY2025_FACTS = (
    _fact(REV, "c-1", "5,128,607")
    + _fact(TARGET, "c-1", "982,972")
    + _fact(TARGET, "c-2", "642,270")
    + _fact(TARGET, "c-3", "340,702")
)


@pytest.fixture
def fy2025():
    return parse_inline_xbrl(_ix_doc(FY2025_FACTS, FY2025_CONTEXTS))


# ── 1. 无维度总额与 ProductOrServiceAxis 子项均完整导出 ──────

class TestCandidateExport:
    def test_total_and_components_all_exported(self, fy2025):
        facts, contexts = fy2025
        cand = collect_cost_candidates(facts, contexts, 2025, "accn", "doc.htm")
        assert len(cand) == 3
        dimensionless = [c for c in cand if c.is_dimensionless is True]
        components = [c for c in cand if c.is_dimensionless is False]
        assert len(dimensionless) == 1
        assert dimensionless[0].value_numeric == Decimal("982972000")
        assert {c.value_numeric for c in components} == {
            Decimal("642270000"), Decimal("340702000")}
        for c in components:
            assert "srt:ProductOrServiceAxis" in c.dimensions


# ── 2. 仅显式无维度才标 true;context 缺失不得误标 ───────────

class TestDimensionlessFlag:
    def test_missing_context_not_dimensionless(self):
        doc = _ix_doc(_fact(TARGET, "c-unknown", "982,972"), FY2025_CONTEXTS)
        facts, contexts = parse_inline_xbrl(doc)
        cand = collect_cost_candidates(facts, contexts, 2025, "accn", "doc.htm")
        assert len(cand) == 1
        assert cand[0].is_dimensionless is None  # 不是 True,也不是 False

    def test_context_without_segment_is_dimensionless(self, fy2025):
        facts, contexts = fy2025
        cand = collect_cost_candidates(facts, contexts, 2025, "accn", "doc.htm")
        by_ctx = {c.context_id: c for c in cand}
        assert by_ctx["c-1"].is_dimensionless is True
        assert by_ctx["c-2"].is_dimensionless is False


# ── 3. FY2025 精确毛利率 ─────────────────────────────────────

class TestGrossMargin:
    def test_fy2025_exact(self):
        gm = compute_gross_margin(Decimal("5128607000"), Decimal("982972000"))
        assert gm.quantize(Decimal("0.0001")) == Decimal("0.8083")

    def test_parse_numeric_scale_sign(self):
        assert parse_numeric("982,972", scale=3) == Decimal("982972000")
        assert parse_numeric("(100)", scale=0) == Decimal("-100")
        assert parse_numeric("50", scale=0, sign_negative=True) == Decimal("-50")

    def test_nil_placeholder_raises_nil_not_value_error(self):
        from scripts.audit_adt_consolidated_cogs import NilValueError
        for placeholder in ("—", "", "-", " $ "):
            with pytest.raises(NilValueError):
                parse_numeric(placeholder)


# ── 4. 子项求和只作交叉验证,不影响总额选取 ───────────────────

class TestComponentsNotUsedForSelection:
    def test_components_sum_mismatch_still_selects_dimensionless(self):
        # 子项和 != 总额时,选取结果仍是无维度总额,不是子项和
        contexts = FY2025_CONTEXTS
        facts_text = (
            _fact(TARGET, "c-1", "900,000")      # 总额 900m
            + _fact(TARGET, "c-2", "642,270")    # 子项和 = 982,972m ≠ 900m
            + _fact(TARGET, "c-3", "340,702")
        )
        facts, ctx = parse_inline_xbrl(_ix_doc(facts_text, contexts))
        cand = collect_cost_candidates(facts, ctx, 2025, "accn", "doc.htm")
        total, _ = select_consolidated_total(cand)
        assert total is not None
        assert total.value_numeric == Decimal("900000000")

    def test_max_abs_rule_not_applied(self):
        # 子项比总额大时也不得按最大绝对值选子项
        contexts = FY2025_CONTEXTS
        facts_text = (
            _fact(TARGET, "c-1", "100,000")
            + _fact(TARGET, "c-2", "642,270")
        )
        facts, ctx = parse_inline_xbrl(_ix_doc(facts_text, contexts))
        cand = collect_cost_candidates(facts, ctx, 2025, "accn", "doc.htm")
        total, _ = select_consolidated_total(cand)
        assert total is not None
        assert total.value_numeric == Decimal("100000000")


# ── 5. 无合并总额 → COMPONENT_ONLY / EVIDENCE_INSUFFICIENT ──

class TestNoTotal:
    def test_component_only(self):
        facts_text = _fact(TARGET, "c-2", "642,270") + _fact(TARGET, "c-3", "340,702")
        facts, ctx = parse_inline_xbrl(_ix_doc(facts_text, FY2025_CONTEXTS))
        cand = collect_cost_candidates(facts, ctx, 2025, "accn", "doc.htm")
        total, note = select_consolidated_total(cand)
        assert total is None
        assert "子项" in note

    def test_conflicting_dimensionless_totals_rejected(self):
        contexts = _ctx("c-a", "2025-01-01", "2025-12-31") + _ctx("c-b", "2025-01-01", "2025-12-31")
        facts_text = _fact(TARGET, "c-a", "982,972") + _fact(TARGET, "c-b", "900,000")
        facts, ctx = parse_inline_xbrl(_ix_doc(facts_text, contexts))
        cand = collect_cost_candidates(facts, ctx, 2025, "accn", "doc.htm")
        total, note = select_consolidated_total(cand)
        assert total is None
        assert "不一致" in note

    def test_missing_context_blocks_selection(self):
        facts_text = _fact(TARGET, "c-unknown", "982,972")
        facts, ctx = parse_inline_xbrl(_ix_doc(facts_text, FY2025_CONTEXTS))
        cand = collect_cost_candidates(facts, ctx, 2025, "accn", "doc.htm")
        total, note = select_consolidated_total(cand)
        assert total is None
        assert "context" in note


# ── 6. 失败语义:解析失败抛错,不伪装成功 ─────────────────────

class TestFailureSemantics:
    def test_unparseable_target_fact_raises(self):
        bad = ('<ix:nonFraction name="us-gaap:CostOfRevenue" contextRef="c-1" '
               'unitRef="usd">not-a-number</ix:nonFraction>')
        with pytest.raises(ValueError, match="数值解析失败"):
            parse_inline_xbrl(_ix_doc(bad, FY2025_CONTEXTS))

    def test_unparseable_unrelated_fact_skipped(self):
        """非目标 fact 的空值/占位符(如 '—')跳过,不阻断审计。"""
        noise = ('<ix:nonFraction name="us-gaap:CommitmentsAndContingencies" '
                 'contextRef="c-1" unitRef="usd">—</ix:nonFraction>')
        facts, ctx = parse_inline_xbrl(_ix_doc(noise + FY2025_FACTS, FY2025_CONTEXTS))
        cand = collect_cost_candidates(facts, ctx, 2025, "accn", "doc.htm")
        assert len(cand) == 3

    def test_no_revenue_means_no_margin(self):
        # 只有成本总额、无收入事实 → 找不到收入,不得算毛利率
        facts_text = _fact(TARGET, "c-1", "982,972")
        facts, ctx = parse_inline_xbrl(_ix_doc(facts_text, FY2025_CONTEXTS))
        assert find_dimensionless_revenue(facts, ctx, 2025) is None


# ── 7. 行序稳定 ─────────────────────────────────────────────

class TestDeterminism:
    def test_candidate_order_stable(self, fy2025):
        facts, contexts = fy2025
        a = collect_cost_candidates(facts, contexts, 2025, "accn", "doc.htm")
        b = collect_cost_candidates(facts, contexts, 2025, "accn", "doc.htm")
        ka = [(c.sec_tag, c.context_id, str(c.value_numeric)) for c in a]
        kb = [(c.sec_tag, c.context_id, str(c.value_numeric)) for c in b]
        assert ka == kb

    def test_revenue_found(self, fy2025):
        facts, contexts = fy2025
        rev = find_dimensionless_revenue(facts, contexts, 2025)
        assert rev is not None
        assert rev.value_numeric == Decimal("5128607000")

    def test_annual_period_boundary(self):
        # 期间不匹配的同年 fact 不进入候选
        q_ctx = _ctx("c-q", "2025-10-01", "2025-12-31")
        facts_text = _fact(TARGET, "c-q", "250,000")
        facts, ctx = parse_inline_xbrl(_ix_doc(facts_text, q_ctx))
        assert collect_cost_candidates(facts, ctx, 2025, "accn", "doc.htm") == []


# ── 报表交叉验证(量级因子 / R 文件解析)─────────────────────

class TestStatementCrossCheck:
    def test_match_statement_value_scales(self):
        from scripts.audit_adt_consolidated_cogs import (
            StatementRow, match_statement_value)
        row = StatementRow("S", "Total cost of revenue",
                           [Decimal("982972"), Decimal("900000")])
        # xbrl 美元值 = 报表(千美元)× 1e3
        assert match_statement_value(Decimal("982972000"), row) == (
            Decimal("982972000"), 1000)
        # 无匹配返回 None,不得近似匹配
        assert match_statement_value(Decimal("983000000"), row) is None

    def test_parse_r_file_table(self):
        from scripts.audit_adt_consolidated_cogs import parse_r_file_table
        htm = """<html><body><table>
        <tr><th></th><th>2025</th><th>2024</th></tr>
        <tr><td>Revenue</td><td>$ 5,128,607</td><td>4,890,000</td></tr>
        <tr><td>Total cost of revenue</td><td>982,972</td><td>—</td></tr>
        </table></body></html>"""
        rows = parse_r_file_table(htm)
        cost = [r for r in rows if "cost of revenue" in r.label.lower()]
        assert len(cost) == 1
        assert cost[0].values == [Decimal("982972")]  # '—' 跳过
