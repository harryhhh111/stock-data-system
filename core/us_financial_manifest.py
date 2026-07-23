"""core/us_financial_manifest.py — Phase 2 batch manifest 规范化与 hash。

manifest 分为 deterministic_payload（参与 hash）与 runtime_metadata（不参与 hash）。
本模块保证跨 PYTHONHASHSEED、跨运行环境的 hash 稳定性。
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any


MANIFEST_SCHEMA_VERSION = "us_financial_phase2_v1"


def _canonical_value(obj: Any) -> Any:
    """将对象转成可 JSON 序列化的规范形式。"""
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): _canonical_value(v) for k, v in sorted(obj.items())}
    if isinstance(obj, (list, tuple)):
        return [_canonical_value(v) for v in obj]
    if isinstance(obj, (int, float, str, bool, type(None))):
        return obj
    return str(obj)


def canonical_json(obj: Any) -> str:
    """返回对象的规范化 JSON 字符串，用于 hash 计算。"""
    return json.dumps(
        _canonical_value(obj),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def compute_manifest_hash(deterministic_payload: dict[str, Any]) -> str:
    """计算 manifest 确定性部分的 SHA-256 hash。"""
    canonical = canonical_json(deterministic_payload)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _sort_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """对 source 列表按稳定键排序。"""
    return sorted(
        sources,
        key=lambda s: (
            s.get("stock_code", ""),
            s.get("source_kind", ""),
            s.get("source_content_hash", ""),
            s.get("source_locator", "") or "",
        ),
    )


def _sort_stock_scope(stock_scope: list[str]) -> list[str]:
    """对股票范围列表按字符串排序。"""
    return sorted(set(stock_scope))


def compute_stock_scope_hash(stock_scope: list[str]) -> str:
    """计算股票范围列表的 hash。"""
    return hashlib.sha256(
        canonical_json(_sort_stock_scope(stock_scope)).encode("utf-8")
    ).hexdigest()


def build_manifest(
    batch_id: str,
    environment: str,
    mode: str,
    stock_scope: list[str],
    source_policy_version: str,
    parser_git_sha: str,
    sources: list[dict[str, Any]],
    mapping_version: str | None = None,
    selector_version: str | None = None,
    expected_counts: dict[str, Any] | None = None,
    source_counts: dict[str, Any] | None = None,
    quality_counts: dict[str, Any] | None = None,
    failed_items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """构造完整 manifest dict，包含 deterministic 与 runtime 两部分。"""
    deterministic_payload = {
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "batch_id": batch_id,
        "environment": environment,
        "mode": mode,
        "stock_scope": _sort_stock_scope(stock_scope),
        "stock_scope_hash": compute_stock_scope_hash(stock_scope),
        "source_policy_version": source_policy_version,
        "sources": _sort_sources(sources),
        "parser_git_sha": parser_git_sha,
        "mapping_version": mapping_version,
        "selector_version": selector_version,
        "checksum_schema_version": "v2",
        "source_counts": source_counts or {},
        "expected_counts": expected_counts or {},
        "quality_counts": quality_counts or {},
        "failed_items": failed_items or [],
    }

    manifest_hash = compute_manifest_hash(deterministic_payload)

    runtime_metadata = {
        "created_at": datetime.now().isoformat(),
        "canonicalization_version": "v1",
    }

    return {
        **deterministic_payload,
        "manifest_hash": manifest_hash,
        "runtime_metadata": runtime_metadata,
    }


def extract_deterministic_payload(manifest: dict[str, Any]) -> dict[str, Any]:
    """从完整 manifest 中提取确定性部分（用于 apply 时重新计算 hash）。"""
    deterministic_keys = {
        "manifest_schema_version",
        "batch_id",
        "environment",
        "mode",
        "stock_scope",
        "stock_scope_hash",
        "source_policy_version",
        "sources",
        "parser_git_sha",
        "mapping_version",
        "selector_version",
        "checksum_schema_version",
        "source_counts",
        "expected_counts",
        "quality_counts",
        "failed_items",
    }
    return {k: manifest[k] for k in deterministic_keys if k in manifest}


def verify_manifest_hash(manifest: dict[str, Any]) -> bool:
    """校验 manifest 的 hash 是否与 deterministic payload 一致。"""
    stored_hash = manifest.get("manifest_hash")
    if not stored_hash:
        return False
    payload = extract_deterministic_payload(manifest)
    return compute_manifest_hash(payload) == stored_hash
