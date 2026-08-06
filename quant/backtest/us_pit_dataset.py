"""US PIT 数据集构建器(Phase B4a)— 带 selector audit 与 manifest 的可复现数据集。

与 us_pit_source(回测引擎热路径,非持久化)的关系:
- 本模块用于正式数据集构建与验收:每次构建持久化一个 us_fact_selection_run,
  输出 manifest + 溯源产物,checksum 可复现;
- 引擎热路径(preloader)出于性能不打 audit,两者使用完全相同的
  select_as_of / build_annual / TTM 纯函数,结果一致由测试保证。
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from core.selectors.us_financial import USFactSelector
from db import Connection

from quant.backtest import us_pit_source as pit

logger = logging.getLogger(__name__)

ARTIFACT_BASE = Path("build/financial_comparison/phaseB4a_pit")

ALLOWLIST_PATH = Path("docs/core/US_TTM_52_53_WEEK_ALLOWLIST.csv")

DATASET_SCHEMA_VERSION = "us_pit_dataset_v1"


@dataclass
class USPITDataset:
    as_of_date: date
    annual: pd.DataFrame                 # 全历史年度(含派生指标与 quality_flags)
    ttm_components: dict                 # {(stock, field): {value, flags, components}}
    roe_history_df: pd.DataFrame         # 全历史年度 ROE(调用者自取年数)
    selection_run_id: str
    selector_version: str
    manifest: dict = field(default_factory=dict)
    checksum: str = ""


def _df_checksum(df: pd.DataFrame, exclude_cols: tuple[str, ...] = ()) -> str:
    """稳定排序后的内容 checksum(排除构建时戳等非业务列)。"""
    if df.empty:
        return hashlib.sha256(b"").hexdigest()
    cols = [c for c in df.columns if c not in exclude_cols]
    d = df[cols].copy()
    for c in cols:
        d[c] = d[c].astype(str)
    d = d.sort_values(by=list(d.columns)).reset_index(drop=True)
    return hashlib.sha256(pd.util.hash_pandas_object(d, index=False).values.tobytes()).hexdigest()


def _git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except Exception:
        return "unknown"


def _file_sha(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ttm_checksum(components: dict) -> str:
    parts = []
    for (stock, field_name), info in sorted(components.items()):
        parts.append(f"{stock}|{field_name}|{info.get('value')}|{sorted(info.get('quality_flags', []))}")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def build_us_pit_dataset(
    as_of_date: date,
    stock_codes: list[str] | None = None,
    *,
    persist_audit: bool = True,
    facts: list[dict[str, Any]] | None = None,
    exclusions: list[dict[str, Any]] | None = None,
) -> USPITDataset:
    """构建一个 as-of 日期的全市场(或指定范围)PIT 数据集。

    persist_audit=True 时持久化一个 us_fact_selection_run(本数据集精确对应一个
    run_id);selector audit 失败或构建失败整体抛错,不产生部分成功数据集。
    """
    if facts is None:
        facts = pit.load_fact_rows()
    if exclusions is None:
        exclusions = pit.load_exclusions()

    visible = pit._apply_exclusions(facts, exclusions, as_of_date)
    if stock_codes:
        wanted = {s.upper() for s in stock_codes}
        visible = [f for f in visible if str(f.get("stock_code") or "").upper() in wanted]
    selector = USFactSelector()
    selector._load_facts = lambda *args, **kwargs: visible

    if persist_audit:
        run_id, selected = selector.select_and_audit(
            stock_codes=stock_codes,
            basis="as-of",
            as_of_date=as_of_date,
            fields=pit.PIT_FIELDS,
            persist=True,
        )
    else:
        run_id = uuid.uuid4()
        selected = selector.select(
            stock_codes=stock_codes,
            basis="as-of",
            as_of_date=as_of_date,
            fields=pit.PIT_FIELDS,
        )

    run_id_str = str(run_id)
    annual = pit._build_annual_df(selected, run_id_str)
    ttm_components = pit._snap.build_ttm_component_index(selected)
    roe_hist = pit.build_roe_history(selected, years=10**9, run_id=run_id_str, annual_df=annual)

    annual_checksum = _df_checksum(
        annual, exclude_cols=("projection_run_id", "generated_at")
    ) if not annual.empty else _df_checksum(annual)
    ttm_checksum = _ttm_checksum(ttm_components)

    flag_count = 0
    if not annual.empty and "quality_flags" in annual.columns:
        flag_count = int(annual["quality_flags"].apply(len).sum())
    ttm_flag_count = sum(len(i.get("quality_flags", [])) for i in ttm_components.values())

    manifest = {
        "dataset_schema_version": DATASET_SCHEMA_VERSION,
        "as_of_date": as_of_date.isoformat(),
        "built_at": datetime.now().isoformat(),
        "git_sha": _git_sha(),
        "selection_basis": "as-of",
        "selector_version": USFactSelector.VERSION,
        "selection_run_id": run_id_str,
        "stock_scope": "full_market" if stock_codes is None else f"{len(stock_codes)}_stocks",
        "stock_count": len({f.stock_code for f in selected}),
        "selected_fact_count": len(selected),
        "annual_rows": int(len(annual)),
        "ttm_stock_count": len({k[0] for k in ttm_components}),
        "annual_checksum": annual_checksum,
        "ttm_checksum": ttm_checksum,
        "allowlist_path": str(ALLOWLIST_PATH),
        "allowlist_checksum": _file_sha(ALLOWLIST_PATH),
        "annual_quality_flag_count": flag_count,
        "ttm_quality_flag_count": ttm_flag_count,
    }

    return USPITDataset(
        as_of_date=as_of_date,
        annual=annual,
        ttm_components=ttm_components,
        roe_history_df=roe_hist,
        selection_run_id=run_id_str,
        selector_version=USFactSelector.VERSION,
        manifest=manifest,
        checksum=hashlib.sha256(
            (annual_checksum + ttm_checksum).encode()
        ).hexdigest(),
    )


def write_artifacts(ds: USPITDataset, base: Path = ARTIFACT_BASE) -> Path:
    """按 §4 契约输出 manifest 与验收产物。"""
    out_dir = base / ds.as_of_date.isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)

    with open(out_dir / "manifest.json", "w") as f:
        json.dump(ds.manifest, f, indent=2, ensure_ascii=False)

    if not ds.annual.empty:
        ds.annual.to_csv(out_dir / "annual.csv", index=False)

    ttm_rows = []
    for (stock, field_name), info in sorted(ds.ttm_components.items()):
        comps = info.get("components") or {}
        row = {"stock_code": stock, "field": field_name, "value": info.get("value"),
               "quality_flags": ",".join(sorted(info.get("quality_flags", [])))}
        for comp_name in ("latest", "last_annual", "prior_year"):
            comp = comps.get(comp_name) or {}
            row[f"{comp_name}_report_date"] = comp.get("report_date")
            row[f"{comp_name}_filed_date"] = comp.get("filed_date")
            row[f"{comp_name}_accession"] = comp.get("accession_no")
            row[f"{comp_name}_value"] = comp.get("value")
        ttm_rows.append(row)
    pd.DataFrame(ttm_rows).to_csv(out_dir / "ttm_components.csv", index=False)

    with open(out_dir / "summary.md", "w") as f:
        f.write(f"# US PIT dataset {ds.as_of_date}\n\n")
        for k, v in ds.manifest.items():
            f.write(f"- {k}: {v}\n")

    logger.info("Artifacts written to %s", out_dir)
    return out_dir
