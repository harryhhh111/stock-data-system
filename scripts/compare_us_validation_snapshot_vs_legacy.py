#!/usr/bin/env python3
"""Phase B3b 影子对比：美股校验 版本事实层路径 vs legacy 宽表路径。

只读脚本：不改 .env、不写数据库（直接调用检查函数，不经 run_validation /
save_results）。对同一批次分别用新旧路径运行三个美股校验，按
(check_name, stock_code, report_date, field_name) 对齐问题清单。

用法:
  venv/bin/python scripts/compare_us_validation_snapshot_vs_legacy.py

产物:
  build/financial_comparison/phaseB3b_validation/
  ├── summary.md
  └── issue_diffs.csv
"""

from __future__ import annotations

import csv
import logging
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import db  # noqa: E402
from core import validate as legacy_validate  # noqa: E402
from core import validate_us_snapshot as snapshot_validate  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("compare_us_validation")

OUTPUT_DIR = Path("build/financial_comparison/phaseB3b_validation")

# 对齐键（规格 §5）
KEY_FIELDS = ("check_name", "stock_code", "report_date", "field_name")

_SKIP_STAT_KEYS = (
    "missing_standalone",
    "missing_cumulative",
    "ambiguous_candidates",
    "undeterminable_fiscal_year",
    "q4_excluded",
    "pivot_field_collision",
    "non_usd_skipped",
)


def _issue_key(issue) -> tuple:
    return tuple(getattr(issue, f) for f in KEY_FIELDS)


def run_legacy_checks() -> tuple[list, dict[str, int]]:
    """legacy 宽表路径：三个美股校验。返回 (issues, scanned)。"""
    issues: list = []
    scanned = {
        "anomalies": legacy_validate.check_anomalies_us(issues),
        "logic": legacy_validate.check_logic_us(issues),
        "standalone": legacy_validate.check_standalone_cross_validation_us(issues),
    }
    return issues, scanned


def run_snapshot_checks() -> tuple[list, dict[str, int], dict]:
    """版本事实层路径：三个美股校验。返回 (issues, scanned, skip_stats)。

    任何 DB/selector 错误直接向上抛（规格：新路径不得回退旧表）。
    """
    issues: list = []
    stats: dict = {}
    scanned = snapshot_validate.run_us_snapshot_checks(issues, stats=stats)
    return issues, scanned, stats


def diff_issues(
    legacy_issues: list,
    snapshot_issues: list,
) -> dict[str, list]:
    """按对齐键比较两路径问题清单。

    Returns:
        {"both_same": [...], "severity_diff": [...],
         "legacy_only": [...], "new_only": [...]}
        both_same/severity_diff 元素为 (key, legacy_issue, new_issue)；
        legacy_only/new_only 元素为 (key, issue)。
        同键多条（新路径同一报告日的多个期间行触发同一检查）先去重并计数。
    """
    def _dedup(issues: list) -> tuple[dict, int]:
        seen: dict = {}
        dup = 0
        for i in issues:
            k = _issue_key(i)
            if k in seen:
                dup += 1
                continue
            seen[k] = i
        return seen, dup

    legacy_map, legacy_dups = _dedup(legacy_issues)
    new_map, new_dups = _dedup(snapshot_issues)

    both_same, severity_diff, legacy_only, new_only = [], [], [], []
    for key, li in legacy_map.items():
        ni = new_map.get(key)
        if ni is None:
            legacy_only.append((key, li))
        elif ni.severity != li.severity:
            severity_diff.append((key, li, ni))
        else:
            both_same.append((key, li, ni))
    for key, ni in new_map.items():
        if key not in legacy_map:
            new_only.append((key, ni))

    return {
        "both_same": both_same,
        "severity_diff": severity_diff,
        "legacy_only": legacy_only,
        "new_only": new_only,
        "legacy_dup_keys": legacy_dups,
        "new_dup_keys": new_dups,
    }


def write_csv(diffs: dict, path: Path) -> None:
    fieldnames = [
        "category",
        *KEY_FIELDS,
        "legacy_severity",
        "new_severity",
        "legacy_actual_value",
        "new_actual_value",
        "legacy_message",
        "new_message",
    ]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        def _row(category, key, li=None, ni=None):
            row = {"category": category}
            row.update(dict(zip(KEY_FIELDS, key)))
            row["legacy_severity"] = li.severity if li else ""
            row["new_severity"] = ni.severity if ni else ""
            row["legacy_actual_value"] = (li.actual_value or "") if li else ""
            row["new_actual_value"] = (ni.actual_value or "") if ni else ""
            row["legacy_message"] = li.message if li else ""
            row["new_message"] = ni.message if ni else ""
            writer.writerow(row)

        for key, li, ni in diffs["severity_diff"]:
            _row("severity_diff", key, li, ni)
        for key, li in diffs["legacy_only"]:
            _row("legacy_only", key, li=li)
        for key, ni in diffs["new_only"]:
            _row("new_only", key, ni=ni)


def write_summary(
    diffs: dict,
    legacy_scanned: dict[str, int],
    snapshot_scanned: dict[str, int],
    skip_stats: dict,
    legacy_issues: list,
    snapshot_issues: list,
    path: Path,
) -> None:
    lines: list[str] = []
    lines.append("# Phase B3b 影子对比：美股校验 snapshot vs legacy")
    lines.append("")
    lines.append(f"- 运行时间: {datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"- legacy 问题总数: {len(legacy_issues)}")
    lines.append(f"- snapshot 问题总数: {len(snapshot_issues)}")
    lines.append("")

    lines.append("## 扫描行数")
    lines.append("")
    lines.append("| 检查 | legacy | snapshot |")
    lines.append("|---|---|---|")
    for name in ("anomalies", "logic", "standalone"):
        lines.append(
            f"| {name} | {legacy_scanned.get(name, 0)} | {snapshot_scanned.get(name, 0)} |"
        )
    lines.append("")
    lines.append("注：legacy standalone 的 scanned 含 SQL 过滤后的命中行数，"
                 "snapshot 的 scanned 为归一化后的候选事实数，口径不同属预期。")
    lines.append("")

    lines.append("## snapshot 路径跳过计数（不静默跳过）")
    lines.append("")
    lines.append("| 原因 | 计数 |")
    lines.append("|---|---|")
    for k in _SKIP_STAT_KEYS:
        lines.append(f"| {k} | {skip_stats.get(k, 0)} |")
    lines.append("")

    lines.append("## 对齐结果（key = check_name+stock_code+report_date+field_name）")
    lines.append("")
    lines.append("| 类别 | 数量 |")
    lines.append("|---|---|")
    lines.append(f"| 两边都有且严重度相同 | {len(diffs['both_same'])} |")
    lines.append(f"| 两边都有但严重度不同 | {len(diffs['severity_diff'])} |")
    lines.append(f"| 旧有新无 (legacy_only) | {len(diffs['legacy_only'])} |")
    lines.append(f"| 新有旧无 (new_only) | {len(diffs['new_only'])} |")
    lines.append(f"| legacy 同键重复(已去重) | {diffs['legacy_dup_keys']} |")
    lines.append(f"| snapshot 同键重复(已去重) | {diffs['new_dup_keys']} |")
    lines.append("")

    for category, title in (
        ("severity_diff", "两边都有但严重度不同"),
        ("new_only", "新有旧无（必须逐条证明是新路径正确发现）"),
        ("legacy_only", "旧有新无（预期占多数：latest-restated 修复旧表已知错值）"),
    ):
        items = diffs[category]
        lines.append(f"## {title}: {len(items)} 条")
        lines.append("")
        if not items:
            lines.append("（无）")
            lines.append("")
            continue
        counter = Counter(key[0] for key, *_ in items)
        lines.append("| check_name | 条数 |")
        lines.append("|---|---|")
        for name, cnt in counter.most_common():
            lines.append(f"| {name} | {cnt} |")
        lines.append("")
        lines.append("| stock_code | report_date | check_name | legacy | snapshot | 说明 |")
        lines.append("|---|---|---|---|---|---|")
        for entry in items:
            key = entry[0]
            if category == "severity_diff":
                li, ni = entry[1], entry[2]
            elif category == "legacy_only":
                li, ni = entry[1], None
            else:  # new_only
                li, ni = None, entry[1]
            lines.append(
                f"| {key[1]} | {key[2]} | {key[0]} | "
                f"{(li.severity if li else '—')} | {(ni.severity if ni else '—')} | "
                f"{(ni.message if ni else li.message)[:120]} |"
            )
        lines.append("")

    lines.append("明细见 issue_diffs.csv。")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    logger.info("=== 运行 legacy 校验路径 ===")
    legacy_issues, legacy_scanned = run_legacy_checks()
    logger.info("legacy: %d issues, scanned=%s", len(legacy_issues), legacy_scanned)

    logger.info("=== 运行 snapshot 校验路径（全历史选择，可能需要几分钟）===")
    snapshot_issues, snapshot_scanned, skip_stats = run_snapshot_checks()
    logger.info(
        "snapshot: %d issues, scanned=%s, skips=%s",
        len(snapshot_issues),
        snapshot_scanned,
        {k: skip_stats.get(k, 0) for k in _SKIP_STAT_KEYS},
    )

    diffs = diff_issues(legacy_issues, snapshot_issues)
    write_csv(diffs, OUTPUT_DIR / "issue_diffs.csv")
    write_summary(
        diffs,
        legacy_scanned,
        snapshot_scanned,
        skip_stats,
        legacy_issues,
        snapshot_issues,
        OUTPUT_DIR / "summary.md",
    )
    logger.info("产物已写入 %s", OUTPUT_DIR)

    # 新有旧无是 blocker 候选：显式报出，由人工逐条核实
    if diffs["new_only"]:
        logger.warning(
            "new_only=%d 条：必须逐条证明是新路径正确发现，否则视为 blocker",
            len(diffs["new_only"]),
        )
    db.close_pool()
    return 0


if __name__ == "__main__":
    sys.exit(main())
