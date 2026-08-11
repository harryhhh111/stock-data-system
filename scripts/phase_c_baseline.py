#!/usr/bin/env python3
"""Phase C1 切换前基线与切换后零写入检查。

规格:docs/core/US_PHASE_C_SYNC_CUTOVER_TASK.md §3.5

  # 切换前(停旧写入之前)记录基线
  venv/bin/python scripts/phase_c_baseline.py record

  # 在线同步后检查:相对基线任何写入即非零退出并输出对象名与变化量
  venv/bin/python scripts/phase_c_baseline.py check

产物:build/financial_comparison/phaseC_sync/baseline.json
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from db import execute  # noqa: E402

OUT = Path("build/financial_comparison/phaseC_sync/baseline.json")

RETIRING_OBJECTS = [
    "us_income_statement",
    "us_balance_sheet",
    "us_cash_flow_statement",
    "mv_us_financial_indicator",
    "mv_us_indicator_ttm",
    "mv_us_fcf_yield",
]

# 采样列(确定性 checksum 用):主键列 + 金额列过多,改为行数 + 聚合 hash
_PROBE_COLUMNS = {
    "us_income_statement": "stock_code, report_date, report_type, revenues, net_income",
    "us_balance_sheet": "stock_code, report_date, report_type, total_assets, total_equity",
    "us_cash_flow_statement": "stock_code, report_date, report_type, net_cash_from_operations",
    "mv_us_financial_indicator": "stock_code, report_date, report_type, roe, fcf",
    "mv_us_indicator_ttm": "stock_code, report_date, revenue_ttm, net_income_ttm",
    "mv_us_fcf_yield": "stock_code, trade_date, ttm_report_date, fcf_yield",
}


def _object_stats(obj: str) -> dict:
    cols = _PROBE_COLUMNS[obj]
    rows = execute(f"SELECT COUNT(*), md5(string_agg(md5(t::text), '' ORDER BY t::text)) "
                   f"FROM (SELECT {cols} FROM {obj}) t", fetch=True)
    count, agg = rows[0]
    updated_col = None
    for col in ("updated_at", "sync_time", "created_at"):
        try:
            r = execute(f"SELECT MAX({col}) FROM {obj}", fetch=True)
            updated_col = (col, r[0][0].isoformat() if r and r[0][0] else None)
            break
        except Exception:
            continue
    return {
        "row_count": int(count),
        "content_md5": agg,
        "max_updated_column": updated_col[0] if updated_col else None,
        "max_updated_at": updated_col[1] if updated_col else None,
    }


def record() -> int:
    stats = {obj: _object_stats(obj) for obj in RETIRING_OBJECTS}
    payload = {
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
        "purpose": "Phase C1 停旧写入前基线(§3.5.1);Phase D 零写入与回退对比锚点",
        "objects": stats,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def check() -> int:
    if not OUT.exists():
        print(f"基线不存在: {OUT} — 先运行 record", file=sys.stderr)
        return 2
    baseline = json.loads(OUT.read_text())["objects"]
    violations = []
    for obj in RETIRING_OBJECTS:
        now = _object_stats(obj)
        base = baseline[obj]
        if now["row_count"] != base["row_count"] or now["content_md5"] != base["content_md5"]:
            violations.append({
                "object": obj,
                "baseline_rows": base["row_count"], "current_rows": now["row_count"],
                "row_delta": now["row_count"] - base["row_count"],
                "baseline_md5": base["content_md5"], "current_md5": now["content_md5"],
            })
    if violations:
        print(json.dumps({"zero_write_violations": violations}, indent=2, ensure_ascii=False))
        return 1
    print("零写入检查通过: 六个对象相对基线无变化")
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "record":
        sys.exit(record())
    if cmd == "check":
        sys.exit(check())
    print(__doc__)
    sys.exit(2)
