"""core/us_financial_field_overrides.py — 发行人受限字段映射 override registry。

规格:docs/core/US_JD_PROFIT_LOSS_MAPPING_TASK.md §2.1

通用 tag→field 映射保持不变;仅当 (stock_code, taxonomy, sec_tag) 在此登记时,
fact 的标准字段按 registry 改写。每条必须有 20-F/10-K 级证据与登记日期。
新增条目必须附带该发行人的审计结论,禁止把单发行人结论泛化为全局 remap。
"""
from __future__ import annotations

# (stock_code, taxonomy, sec_tag) → override 说明
FIELD_OVERRIDES: dict[tuple[str, str, str], dict] = {
    # JD.com 20-F(accession 0001193125-26-157870,filed 2026-04-16):
    # 税前利润 3.621bn − 所得税 0.312bn = ProfitLoss 3.309bn 精确成立,
    # 且与 common 口径 2.807bn 相差 0.502bn(NCI),故 ProfitLoss 是
    # consolidated 税后净利润,不是营业利润(该行 OperatingIncomeLoss=0.397bn)。
    ("JD", "us-gaap", "ProfitLoss"): {
        "standard_field": "net_income",
        "statement": "income",
        "reason": "JD FY2025 20-F verified consolidated post-tax income",
        "evidence": "docs/core/US_JD_PROFIT_LOSS_MAPPING_TASK.md",
        "registered_at": "2026-08-13",
    },
}

# registry 允许的字段白名单(防止非法 field 静默写库)
_ALLOWED_FIELDS = frozenset({
    "revenues", "cost_of_goods_sold", "gross_profit", "operating_expenses",
    "selling_general_admin", "research_and_development", "depreciation_amortization",
    "operating_income", "interest_expense", "interest_income",
    "other_income_expense", "income_before_tax", "income_tax_expense",
    "net_income", "net_income_common",
})


def validate_registry() -> list[str]:
    """registry 结构校验(启动/测试用)。返回问题列表,空 = 通过。"""
    problems: list[str] = []
    for (stock, taxonomy, tag), meta in FIELD_OVERRIDES.items():
        if not stock or not taxonomy or not tag:
            problems.append(f"空键: {(stock, taxonomy, tag)}")
        field = meta.get("standard_field")
        if field not in _ALLOWED_FIELDS:
            problems.append(f"{stock}/{tag}: 非法 standard_field {field!r}")
        if not meta.get("reason") or not meta.get("evidence"):
            problems.append(f"{stock}/{tag}: 缺 reason/evidence")
    return problems


def override_field(stock_code: str, taxonomy: str, sec_tag: str) -> str | None:
    """命中 registry 时返回 override 的 standard_field,否则 None(走通用映射)。"""
    meta = FIELD_OVERRIDES.get((stock_code.upper(), taxonomy, sec_tag))
    return meta["standard_field"] if meta else None
