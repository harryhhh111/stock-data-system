"""tests/test_audit_us_cogs_consolidated_selection.py

COGS 合并行选择证据审计(#7)测试。

覆盖规格 §7 的验收点:
1. CAT 型同 accession 双 tag 冲突完整输出,不按最大金额自动选择;
2. 同值重复识别为 DUPLICATE,不误报冲突;
3. 不同 dimensions 不混成同一经济键;
4. 不同期间/单位/accession 不进入同一 tie-break 组;
5. 原生 GP 交叉验证三态,命中不自动形成结论;
6. 有原生 GP → NATIVE_GROSS_PROFIT_NO_MARGIN_EFFECT;
7. 派生毛利率行 → DERIVED_MARGIN_AT_RISK;
8. 查询失败/空输入抛带上下文错误;
9. 同一输入重复运行产物稳定。
"""
from __future__ import annotations

import csv
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.relations.us_financial import compute_economic_key_hash  # noqa: E402
from core.selectors.us_financial import SelectedFact  # noqa: E402
from scripts.audit_us_cogs_consolidated_selection import (  # noqa: E402
    AuditError,
    fetch_cogs_facts,
    run_audit,
)
import scripts.audit_us_cogs_consolidated_selection as audit_mod  # noqa: E402

RUN_TS = datetime(2026, 8, 5, 0, 0, tzinfo=timezone.utc)


def _fact(
    fid: int,
    stock: str = "TEST",
    tag: str = "CostOfRevenue",
    value: Decimal | int = Decimal("44752000000"),
    accession: str = "acc-1",
    form: str = "10-K",
    filed: date = date(2026, 2, 13),
    ps: date | None = date(2025, 1, 1),
    rd: date = date(2025, 12, 31),
    unit: str = "USD",
    dims: dict | None = None,
    period_kind: str = "duration",
    excluded: bool = False,
    field: str = "cost_of_goods_sold",
) -> dict:
    return {
        "fact_version_id": fid,
        "stock_code": stock,
        "statement": "income",
        "standard_field": field,
        "period_kind": period_kind,
        "period_start": ps,
        "report_date": rd,
        "fiscal_period_raw": "FY",
        "frame": "CY2025",
        "form": form,
        "filed_date": filed,
        "accession_no": accession,
        "unit": unit,
        "value_numeric": Decimal(str(value)),
        "value_text": None,
        "value_hash": f"h{fid}",
        "dimensions": dims or {},
        "sec_tag": tag,
        "context_hash": f"ctx{fid:064d}"[:64],
        "excluded": excluded,
    }


def _selected(
    fact: dict,
    reason: str = "no subsequent revision; first filed date preserved",
    candidate_count: int = 1,
) -> SelectedFact:
    return SelectedFact(
        fact_version_id=fact["fact_version_id"],
        stock_code=fact["stock_code"],
        statement=fact["statement"],
        standard_field=fact["standard_field"],
        period_kind=fact["period_kind"],
        period_start=fact["period_start"],
        report_date=fact["report_date"],
        value_numeric=fact["value_numeric"],
        value_text=None,
        unit=fact["unit"],
        accession_no=fact["accession_no"],
        filed_date=fact["filed_date"],
        sec_tag=fact["sec_tag"],
        context_hash=fact["context_hash"],
        dimensions=fact["dimensions"],
        economic_key_hash=compute_economic_key_hash(fact),
        selection_basis="latest-restated",
        selection_reason=reason,
        quality_flags=[],
        candidate_count=candidate_count,
        form=fact["form"],
        fiscal_period_raw=fact.get("fiscal_period_raw"),
    )


def _snap(
    stock: str,
    rd: date,
    revenues: Decimal,
    margin: Decimal | None,
    flags: list[str],
    accession: str = "acc-1",
) -> dict:
    return {
        "stock_code": stock,
        "report_date": rd,
        "filed_date": date(2026, 2, 13),
        "accession_no": accession,
        "form": "10-K",
        "revenues": revenues,
        "gross_margin": margin,
        "quality_flags": flags,
    }


def _read_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _run(tmp_path: Path, facts, selected, snaps, overrides=None, name="out"):
    return run_audit(
        facts=facts,
        selected=selected,
        snapshot_rows=snaps,
        output_dir=tmp_path / name,
        basis="latest-restated",
        review_overrides=overrides or {},
        run_started_at=RUN_TS,
    )


# ── §7.1 CAT 型:双 tag/金额完整输出,不按最大金额自动选择 ──────


def test_cat_type_conflict_fully_output_no_auto_max_selection(tmp_path):
    small = _fact(392473, stock="CAT", tag="CostOfGoodsAndServicesSold", value=49000000)
    large = _fact(392595, stock="CAT", tag="CostOfRevenue", value=44752000000)
    # 当前 selector 选中了较小的子项值(与线上行为一致)
    selected = [_selected(small, candidate_count=2)]
    stats = _run(tmp_path, [small, large], selected, [])

    out = tmp_path / "out"
    all_rows = _read_csv(out / "cogs_all_candidate_groups.csv")
    assert {r["fact_version_id"] for r in all_rows} == {"392473", "392595"}
    sel_flags = {r["fact_version_id"]: r["current_selector_selected"] for r in all_rows}
    assert sel_flags == {"392473": "1", "392595": "0"}

    conflicts = _read_csv(out / "cogs_conflicting_candidate_groups.csv")
    assert conflicts, "同 accession 双 tag 异值必须进入冲突清单"
    ekey_conf = [r for r in conflicts if r["grouping_kind"] == "same_economic_key"]
    obs_conf = [r for r in conflicts if r["grouping_kind"] == "same_accession"]
    assert len(ekey_conf) == 1 and len(obs_conf) == 1
    row = ekey_conf[0]
    assert row["conflict_subtype"] == "SAME_ACCESSION_DISTINCT_VALUES"
    assert set(row["candidate_fact_ids"].split("|")) == {"392473", "392595"}
    # 审计如实记录当前选择(较小值),不得替换成最大金额
    assert row["selected_fact_id"] == "392473"
    assert row["selected_value"] == "49000000"

    # 台账默认 EVIDENCE_INSUFFICIENT:命中/金额都不自动形成结论
    ledger = _read_csv(out / "cogs_manual_evidence_ledger.csv")
    assert all(r["disposition"] == "EVIDENCE_INSUFFICIENT" for r in ledger)
    assert stats["conflict_subtype_counts"]["SAME_ACCESSION_DISTINCT_VALUES"] >= 1


# ── §7.2 同值重复:DUPLICATE,不误报冲突 ────────────────────────


def test_same_value_duplicate_not_a_conflict(tmp_path):
    f1 = _fact(1, accession="acc-1", filed=date(2025, 2, 14))
    f2 = _fact(2, accession="acc-2", filed=date(2026, 2, 13))
    stats = _run(tmp_path, [f1, f2], [_selected(f1, candidate_count=2)], [])

    conflicts = _read_csv(tmp_path / "out" / "cogs_conflicting_candidate_groups.csv")
    assert conflicts == []
    assert stats["duplicate_group_count"] >= 1


# ── §7.3 不同 dimensions:不混成同一经济键,观察组暴露范围冲突 ──


def test_different_dimensions_not_merged_into_one_economic_key(tmp_path):
    plain = _fact(1, tag="CostOfRevenue", value=44752000000)
    dimmed = _fact(
        2,
        tag="CostOfGoodsAndServicesSold",
        value=49000000,
        dims={"srt:ProductOrServiceAxis": "cat:ProductAMember"},
    )
    _run(tmp_path, [plain, dimmed], [_selected(plain), _selected(dimmed)], [])

    out = tmp_path / "out"
    all_rows = _read_csv(out / "cogs_all_candidate_groups.csv")
    assert len({r["ekey_group_id"] for r in all_rows}) == 2, "不同 dimensions 必须分属不同经济键"
    assert len({r["obs_group_id"] for r in all_rows}) == 1, "同 accession 同期间仍在同一观察组"

    conflicts = _read_csv(out / "cogs_conflicting_candidate_groups.csv")
    ekey_conf = [r for r in conflicts if r["grouping_kind"] == "same_economic_key"]
    obs_conf = [r for r in conflicts if r["grouping_kind"] == "same_accession"]
    assert ekey_conf == [], "不同 dimensions 的候选不得混成同一经济键冲突组"
    assert len(obs_conf) == 1
    dims = obs_conf[0]["candidate_dimensions"]
    assert "ProductAMember" in dims and "{}" in dims


# ── §7.4 不同期间/单位/accession 不进入同一 tie-break 组 ──────


def test_different_period_unit_accession_not_same_tiebreak_group(tmp_path):
    base = _fact(1, accession="acc-1", value=100)
    other_period = _fact(2, accession="acc-2", ps=date(2024, 1, 1), rd=date(2024, 12, 31), value=200)
    other_accession = _fact(3, accession="acc-3", value=300)
    stats = _run(
        tmp_path,
        [base, other_period, other_accession],
        [_selected(base), _selected(other_period)],
        [],
    )
    out = tmp_path / "out"
    all_rows = _read_csv(out / "cogs_all_candidate_groups.csv")
    by_id = {r["fact_version_id"]: r for r in all_rows}
    # 不同期间:两种分组都必须分开
    assert by_id["1"]["ekey_group_id"] != by_id["2"]["ekey_group_id"]
    assert by_id["1"]["obs_group_id"] != by_id["2"]["obs_group_id"]
    # 不同 accession 同期间:同一经济键(版本候选),不同观察组
    assert by_id["1"]["ekey_group_id"] == by_id["3"]["ekey_group_id"]
    assert by_id["1"]["obs_group_id"] != by_id["3"]["obs_group_id"]

    # 该经济键冲突只是跨 accession 版本差异,不是同 filing tie-break
    conflicts = _read_csv(out / "cogs_conflicting_candidate_groups.csv")
    ekey_conf = [r for r in conflicts if r["grouping_kind"] == "same_economic_key"]
    assert len(ekey_conf) == 1
    assert ekey_conf[0]["conflict_subtype"] == "CROSS_ACCESSION_ONLY"


def test_out_of_scope_unit_not_audited_as_candidate(tmp_path):
    usd = _fact(1)
    eur = _fact(2, unit="EUR", accession="acc-2")
    stats = _run(tmp_path, [usd, eur], [_selected(usd)], [])
    assert stats["in_scope_candidates"] == 1
    assert stats["out_of_scope_facts"] == 1
    all_rows = _read_csv(tmp_path / "out" / "cogs_all_candidate_groups.csv")
    assert {r["fact_version_id"] for r in all_rows} == {"1"}


# ── §7.5 原生 GP 交叉验证三态;命中不自动形成结论 ──────────────


def _gp_crosscheck_fixture(implied: Decimal, candidate_values: list[Decimal]):
    rd = date(2025, 12, 31)
    ps = date(2025, 1, 1)
    cogs_facts = [
        _fact(10 + i, tag=tag, value=v, accession="acc-1")
        for i, (tag, v) in enumerate(
            zip(["CostOfGoodsAndServicesSold", "CostOfRevenue"], candidate_values)
        )
    ]
    rev_fact = _fact(20, field="revenues", tag="Revenues", value=Decimal("1000"))
    gp_fact = _fact(21, field="gross_profit", tag="GrossProfit", value=Decimal("1000") - implied)
    facts = cogs_facts + [rev_fact, gp_fact]
    selected = [_selected(cogs_facts[0], candidate_count=2), _selected(rev_fact), _selected(gp_fact)]
    return facts, selected


def test_native_gp_crosscheck_exact_match_does_not_auto_conclude(tmp_path):
    facts, selected = _gp_crosscheck_fixture(Decimal("400"), [Decimal("49"), Decimal("400")])
    _run(tmp_path, facts, selected, [])
    xc = _read_csv(tmp_path / "out" / "cogs_native_gp_crosscheck.csv")
    assert xc and all(r["match_status"] == "EXACT_MATCH" for r in xc)
    assert all(r["matched_candidate_fact_id"] == "11" for r in xc)
    # 命中只是强证据:台账仍须人工 disposition,不得自动 PROVEN
    ledger = _read_csv(tmp_path / "out" / "cogs_manual_evidence_ledger.csv")
    assert all(r["disposition"] == "EVIDENCE_INSUFFICIENT" for r in ledger)


def test_native_gp_crosscheck_no_exact_match(tmp_path):
    facts, selected = _gp_crosscheck_fixture(Decimal("400"), [Decimal("49"), Decimal("410")])
    _run(tmp_path, facts, selected, [])
    xc = _read_csv(tmp_path / "out" / "cogs_native_gp_crosscheck.csv")
    assert xc and all(r["match_status"] == "NO_EXACT_MATCH" for r in xc)


def test_native_gp_crosscheck_not_applicable_without_gp(tmp_path):
    f1 = _fact(1, tag="CostOfGoodsAndServicesSold", value=49)
    f2 = _fact(2, tag="CostOfRevenue", value=400)
    _run(tmp_path, [f1, f2], [_selected(f1, candidate_count=2)], [])
    xc = _read_csv(tmp_path / "out" / "cogs_native_gp_crosscheck.csv")
    assert xc and all(r["match_status"] == "NOT_APPLICABLE" for r in xc)


# ── §7.6/§7.7 snapshot 影响分类 ──────────────────────────────


def test_native_gross_profit_present_no_margin_effect(tmp_path):
    f1 = _fact(1, tag="CostOfGoodsAndServicesSold", value=49)
    f2 = _fact(2, tag="CostOfRevenue", value=400)
    gp_fact = _fact(3, field="gross_profit", tag="GrossProfit", value=600)
    rev = Decimal("1000")
    snap = _snap("TEST", date(2025, 12, 31), rev, Decimal("0.6"), [], accession="acc-1")
    _run(
        tmp_path,
        [f1, f2, gp_fact],
        [_selected(f1, candidate_count=2), _selected(gp_fact)],
        [snap],
    )
    impact = _read_csv(tmp_path / "out" / "cogs_projection_impact.csv")
    assert len(impact) == 1
    assert impact[0]["impact_class"] == "NATIVE_GROSS_PROFIT_NO_MARGIN_EFFECT"
    assert impact[0]["gross_margin_is_cogs_derived"] == "0"
    # 有原生 GP 的冲突组不进人工台账
    ledger = _read_csv(tmp_path / "out" / "cogs_manual_evidence_ledger.csv")
    assert ledger == []


def test_derived_margin_at_risk_classification_and_ledger(tmp_path):
    f1 = _fact(1, tag="CostOfGoodsAndServicesSold", value=49)
    f2 = _fact(2, tag="CostOfRevenue", value=400)
    rev = Decimal("1000")
    margin = (rev - Decimal("49")) / rev
    snap = _snap(
        "TEST",
        date(2025, 12, 31),
        rev,
        margin,
        ["gross_profit_derived_from_cogs"],
        accession="acc-1",
    )
    stats = _run(tmp_path, [f1, f2], [_selected(f1, candidate_count=2)], [snap])
    impact = _read_csv(tmp_path / "out" / "cogs_projection_impact.csv")
    assert len(impact) == 1
    assert impact[0]["impact_class"] == "DERIVED_MARGIN_AT_RISK"
    assert impact[0]["gross_margin_is_cogs_derived"] == "1"
    assert stats["derived_conflict_count"] == 1

    ledger = _read_csv(tmp_path / "out" / "cogs_manual_evidence_ledger.csv")
    assert len(ledger) == 1
    assert ledger[0]["disposition"] == "EVIDENCE_INSUFFICIENT"


def test_derived_flag_but_missing_cogs_goes_unresolved(tmp_path):
    f1 = _fact(1, tag="CostOfGoodsAndServicesSold", value=49)
    f2 = _fact(2, tag="CostOfRevenue", value=400)
    # pivot 里 COGS 来自别的 report_date,snapshot 行却有 derived flag → 不一致必须暴露
    snap = _snap(
        "TEST",
        date(2025, 12, 31),
        Decimal("1000"),
        Decimal("0.5"),
        ["gross_profit_derived_from_cogs"],
        accession="acc-9",
    )
    _run(tmp_path, [f1, f2], [], [snap])
    unresolved = (tmp_path / "out" / "unresolved_groups.txt").read_text(encoding="utf-8")
    assert "DERIVED_FLAG_BUT_DEFINITION_FAILS" in unresolved


# ── §7.8 失败必须显式 ─────────────────────────────────────────


def test_fetch_failure_raises_with_context(monkeypatch):
    def _boom(sql, params=None, *, fetch=False, commit=True):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(audit_mod, "execute", _boom)
    with pytest.raises(AuditError, match="us_financial_fact_version"):
        fetch_cogs_facts(date(2026, 8, 5))


def test_empty_facts_rejected_not_silent(tmp_path):
    with pytest.raises(AuditError, match="候选"):
        _run(tmp_path, [], [], [])


def test_review_override_validation(tmp_path):
    f1 = _fact(1, tag="CostOfGoodsAndServicesSold", value=49)
    f2 = _fact(2, tag="CostOfRevenue", value=400)
    rev = Decimal("1000")
    snap = _snap(
        "TEST", date(2025, 12, 31), rev, (rev - 49) / rev,
        ["gross_profit_derived_from_cogs"],
    )
    selected = [_selected(f1, candidate_count=2)]

    _run(tmp_path, [f1, f2], selected, [snap], name="first")
    ledger = _read_csv(tmp_path / "first" / "cogs_manual_evidence_ledger.csv")
    gid = ledger[0]["group_id"]

    # PROVEN 但 consolidated_fact_id 不在候选中 → 报错
    bad = {
        gid: {
            "group_id": gid,
            "disposition": "CONSOLIDATED_TOTAL_PROVEN",
            "consolidated_fact_id": "999999",
            "consolidated_tag": "CostOfRevenue",
            "consolidated_value": "400",
            "filing_evidence_ref": "https://example.org/filing",
            "filing_statement_line": "Cost of revenue 400",
            "filing_scope_and_unit": "consolidated, USD",
            "reviewer_note": "x",
        }
    }
    with pytest.raises(AuditError, match="不在该组候选中"):
        _run(tmp_path, [f1, f2], selected, [snap], overrides=bad, name="bad")

    # 与台账无关的 group_id → 报错(不静默忽略);合法条目须能通过校验
    valid = {gid: {**bad[gid], "consolidated_fact_id": "2"}}
    with pytest.raises(AuditError, match="不对应任何台账组"):
        _run(
            tmp_path, [f1, f2], selected, [snap],
            overrides={**valid, "EKEY-deadbeef": bad[gid]}, name="unused",
        )

    # 合法 override 生效
    _run(tmp_path, [f1, f2], selected, [snap], overrides=valid, name="good")
    ledger2 = _read_csv(tmp_path / "good" / "cogs_manual_evidence_ledger.csv")
    assert ledger2[0]["disposition"] == "CONSOLIDATED_TOTAL_PROVEN"
    assert ledger2[0]["consolidated_fact_id"] == "2"


# ── §7.9 重复运行稳定 ─────────────────────────────────────────


def test_repeat_run_byte_identical(tmp_path):
    f1 = _fact(392473, stock="CAT", tag="CostOfGoodsAndServicesSold", value=49000000)
    f2 = _fact(392595, stock="CAT", tag="CostOfRevenue", value=44752000000)
    f3 = _fact(7, stock="TEST", accession="acc-9", filed=date(2025, 3, 1), value=10)
    f4 = _fact(8, stock="TEST", accession="acc-10", value=10)
    rev = Decimal("67589000000")
    margin = (rev - Decimal("49000000")) / rev
    snaps = [
        _snap("CAT", date(2025, 12, 31), rev, margin,
              ["gross_profit_derived_from_cogs"], accession="acc-1")
    ]
    selected = [_selected(f1, candidate_count=2), _selected(f3, candidate_count=2)]
    facts = [f1, f2, f3, f4]

    _run(tmp_path, facts, selected, snaps, name="run1")
    _run(tmp_path, facts, selected, snaps, name="run2")

    names = [
        "cogs_all_candidate_groups.csv",
        "cogs_conflicting_candidate_groups.csv",
        "cogs_native_gp_crosscheck.csv",
        "cogs_projection_impact.csv",
        "cogs_manual_evidence_ledger.csv",
        "unresolved_groups.txt",
        "summary.md",
    ]
    for name in names:
        b1 = (tmp_path / "run1" / name).read_bytes()
        b2 = (tmp_path / "run2" / name).read_bytes()
        assert b1 == b2, f"{name} 两次运行不一致"
