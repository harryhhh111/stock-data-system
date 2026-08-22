"""可复现回测 baseline 的纯证据工具。"""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable


def normalize(value: Any) -> Any:
    """将数据库/回测值转为跨进程稳定的 JSON 标量。"""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, float):
        return format(value, ".17g")
    if isinstance(value, tuple):
        return [normalize(v) for v in value]
    if isinstance(value, list):
        return [normalize(v) for v in value]
    if isinstance(value, dict):
        return {str(k): normalize(v) for k, v in sorted(value.items())}
    return value


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        normalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256_value(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def sha256_rows(rows: Iterable[tuple[Any, ...]]) -> tuple[str, int]:
    """按调用方 SQL ORDER BY 的稳定行序计算 SHA-256。"""
    digest = hashlib.sha256()
    count = 0
    for row in rows:
        digest.update(canonical_json(row))
        digest.update(b"\n")
        count += 1
    return digest.hexdigest(), count


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def comparison_key(parameters: dict[str, Any], inputs: dict[str, Any]) -> str:
    """只有相同实验参数与输入指纹的结果才允许直接横向比较。"""
    return sha256_value({"parameters": parameters, "inputs": inputs})


def rebalance_records(results: list[tuple[float, Any]]) -> list[dict[str, Any]]:
    """将 BacktestResult 的调仓快照转成稳定、可审计的行。"""
    records: list[dict[str, Any]] = []
    for bps, result in results:
        for index, snapshot in enumerate(result.rebalance_history, start=1):
            holdings = {
                code: snapshot.holdings[code]
                for code in sorted(snapshot.holdings)
            }
            records.append({
                "strategy": result.preset_name,
                "single_side_cost_bps": bps,
                "rebalance_index": index,
                "rebalance_date": snapshot.date.isoformat(),
                "total_value": snapshot.total_value,
                "cash": snapshot.cash,
                "turnover": snapshot.turnover,
                "cumulative_costs": result.total_costs,
                "holdings_json": json.dumps(
                    normalize(holdings), ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"),
                ),
                "holdings_sha256": sha256_value(holdings),
            })
    return records


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        raise ValueError(f"cannot write empty CSV: {path}")
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        for row in rows:
            writer.writerow({key: normalize(value) for key, value in row.items()})


def write_sha256sums(path: Path, paths: list[Path]) -> None:
    lines = [f"{sha256_file(item)}  {item.name}" for item in sorted(paths)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
