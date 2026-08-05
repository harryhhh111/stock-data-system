#!/usr/bin/env python3
"""生成/更新 CapEx 映射影响审计 CSV。

用法:
  python scripts/audit_capex_mapping_impact.py

输出:
  build/financial_comparison/phaseA_snapshot/capex_mapping_impact_audit.csv
"""
from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.selectors.us_financial import USFactSelector, _CANONICAL_TAG_PRIORITY
from db import execute

OUTPUT_PATH = Path("build/financial_comparison/phaseA_snapshot/capex_mapping_impact_audit.csv")
CANONICAL_CAPEX_TAG = "PaymentsToAcquirePropertyPlantAndEquipment"
# Phase A 映射修复中新增的行业特定 cash CapEx tag（原 6 个，现移除 PaymentsToAcquireOilAndGasProperty）。
NEW_CAPEX_TAGS = {
    "PaymentsForCapitalImprovements",
    "PaymentsToAcquireBuildings",
    "PaymentsToAcquireWasteWaterSystems",
    "PaymentsToAcquireOilAndGasPropertyAndEquipment",
    "PaymentsToAcquireOilAndGasProperty",
    "PaymentsToAcquireOtherPropertyPlantAndEquipment",
}


def load_tag_descriptions(stock_code: str) -> dict[str, str]:
    path = Path(f"data/sec_cache/{stock_code}.json")
    if not path.exists():
        return {}
    try:
        with path.open() as f:
            data = json.load(f)
    except Exception:
        return {}
    descriptions = {}
    for taxonomy in data.get("facts", {}).values():
        for tag, info in taxonomy.items():
            desc = info.get("description", "")
            label = info.get("label", "")
            if desc or label:
                descriptions[tag] = desc or label
    return descriptions


def main() -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    selector = USFactSelector()
    selected = selector.select(fields=["capital_expenditures"], basis="latest-restated")

    # 按股票分组，取 filed_date 最新的年度（>=330 天）事实
    by_stock: dict[str, dict] = {}
    for fact in selected:
        if fact.period_kind == "duration" and fact.period_start and fact.report_date:
            if (fact.report_date - fact.period_start).days < 330:
                continue
        key = fact.stock_code
        if key not in by_stock or (fact.filed_date or "") > (by_stock[key].filed_date or ""):
            by_stock[key] = fact

    # 批量获取所有相关 accession 的候选 tag
    accession_list = [f.accession_no for f in by_stock.values() if f.accession_no]
    accession_candidates: dict[str, set[str]] = defaultdict(set)
    if accession_list:
        placeholders = ",".join(["%s"] * len(accession_list))
        rows = execute(
            f"""
            SELECT DISTINCT accession_no, sec_tag
            FROM us_financial_fact_version
            WHERE standard_field = 'capital_expenditures'
              AND accession_no IN ({placeholders})
            """,
            tuple(accession_list),
            fetch=True,
        )
        for accession_no, sec_tag in rows or []:
            if sec_tag:
                accession_candidates[accession_no].add(sec_tag)

    # 只给需要 description 的股票加载 JSON（新 tag 选中或无选中的情况）
    stocks_needing_desc = {
        fact.stock_code for fact in by_stock.values()
        if (fact.sec_tag in NEW_CAPEX_TAGS) or not fact.sec_tag
    }
    desc_cache = {stock: load_tag_descriptions(stock) for stock in stocks_needing_desc}

    with OUTPUT_PATH.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "stock_code", "report_date", "filed_date", "accession_no",
            "candidate_tags", "selected_tag", "selected_value", "is_new_tag_selected",
            "canonical_tag_present", "conflict_risk", "candidate_count", "tag_description",
        ])

        for key in sorted(by_stock.keys()):
            fact = by_stock[key]
            accession = fact.accession_no
            candidates = sorted(accession_candidates.get(accession, set()))
            selected_tag = fact.sec_tag or ""
            selected_value = fact.value_numeric
            is_new = selected_tag in NEW_CAPEX_TAGS
            canonical_present = CANONICAL_CAPEX_TAG in candidates
            conflict_risk = is_new and canonical_present
            candidate_count = len(candidates)

            description = desc_cache.get(fact.stock_code, {}).get(selected_tag, "")

            writer.writerow([
                fact.stock_code,
                fact.report_date,
                fact.filed_date,
                accession,
                ";".join(candidates),
                selected_tag,
                selected_value,
                is_new,
                canonical_present,
                conflict_risk,
                candidate_count,
                description,
            ])

    print(f"Wrote {len(by_stock)} rows to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
