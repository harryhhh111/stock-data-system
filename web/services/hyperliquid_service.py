"""Hyperliquid 只读看板：调用本机 hl-trader status --json 并校验契约。"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time

CACHE_TTL_SECONDS = 30
PROC_TIMEOUT_SECONDS = 30
STDOUT_LIMIT_BYTES = 1_048_576  # 1 MiB

BLOCKS = ("positions", "funding", "ratio", "unlocks", "announcements")

_cache: dict = {"snapshot": None, "expires_at": 0.0}
_cache_lock = threading.Lock()


class HyperliquidUnavailable(RuntimeError):
    """快照不可用：配置缺失、进程失败或契约校验不通过。"""


def _config() -> tuple[str, str, str]:
    bin_path = os.environ.get("STOCK_HL_TRADER_BIN", "").strip()
    config_path = os.environ.get("STOCK_HL_TRADER_CONFIG", "").strip()
    expected = os.environ.get("STOCK_HL_EXPECTED_ACCOUNT", "").strip().lower()
    missing = [name for name, value in (
        ("STOCK_HL_TRADER_BIN", bin_path),
        ("STOCK_HL_TRADER_CONFIG", config_path),
        ("STOCK_HL_EXPECTED_ACCOUNT", expected),
    ) if not value]
    if missing:
        raise HyperliquidUnavailable(f"未配置 {', '.join(missing)}")
    return bin_path, config_path, expected


def _run_hl_trader(bin_path: str, config_path: str) -> dict:
    try:
        proc = subprocess.run(
            [bin_path, "--config", config_path, "status", "--json"],
            capture_output=True, text=True, timeout=PROC_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise HyperliquidUnavailable(f"hl-trader 超时（>{PROC_TIMEOUT_SECONDS}s）") from exc
    except OSError as exc:
        raise HyperliquidUnavailable(f"无法执行 hl-trader: {exc}") from exc
    if proc.returncode != 0:
        raise HyperliquidUnavailable(
            f"hl-trader 退出码 {proc.returncode}: {proc.stderr.strip()[:200]}"
        )
    if len(proc.stdout.encode("utf-8", "ignore")) > STDOUT_LIMIT_BYTES:
        raise HyperliquidUnavailable("hl-trader 输出超过 1 MiB 上限")
    try:
        snapshot = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise HyperliquidUnavailable(f"hl-trader 输出不是合法 JSON: {exc}") from exc
    if not isinstance(snapshot, dict):
        raise HyperliquidUnavailable("hl-trader 输出不是 JSON 对象")
    return snapshot


def _validate(snapshot: dict, expected_account: str) -> dict:
    if snapshot.get("schema_version") != 1:
        raise HyperliquidUnavailable(f"schema_version 不支持: {snapshot.get('schema_version')!r}")
    if snapshot.get("network") != "testnet":
        raise HyperliquidUnavailable(f"network 不是 testnet: {snapshot.get('network')!r}")
    address = str(snapshot.get("account_address") or "").strip().lower()
    if address != expected_account:
        raise HyperliquidUnavailable("account_address 与 STOCK_HL_EXPECTED_ACCOUNT 不一致")
    if not isinstance(snapshot.get("queried_at"), str) or not snapshot["queried_at"]:
        raise HyperliquidUnavailable("快照缺少 queried_at")
    return snapshot


def _normalize_blocks(snapshot: dict) -> dict:
    blocks = snapshot.get("blocks")
    if not isinstance(blocks, dict):
        blocks = {}
    for name in BLOCKS:
        block = blocks.get(name)
        if not isinstance(block, dict):
            block = {"status": "unavailable", "diagnostics": [f"快照缺少 {name} 区块"]}
        elif block.get("status") not in ("ok", "partial", "unavailable"):
            block["status"] = "unavailable"
            block["diagnostics"] = [f"{name} 区块 status 非法"]
        diagnostics = block.get("diagnostics")
        if not isinstance(diagnostics, list):
            block["diagnostics"] = [str(diagnostics)] if diagnostics else []
        block.setdefault("queried_at", snapshot.get("queried_at", ""))
        blocks[name] = block
    snapshot["blocks"] = blocks
    return snapshot


def get_dashboard() -> dict:
    """返回通过校验的快照；30 秒缓存并合并并发请求。"""
    with _cache_lock:
        if _cache["snapshot"] is not None and time.monotonic() < _cache["expires_at"]:
            return _cache["snapshot"]
        bin_path, config_path, expected = _config()
        snapshot = _normalize_blocks(_validate(_run_hl_trader(bin_path, config_path), expected))
        _cache["snapshot"] = snapshot
        _cache["expires_at"] = time.monotonic() + CACHE_TTL_SECONDS
        return snapshot


def clear_cache() -> None:
    with _cache_lock:
        _cache["snapshot"] = None
        _cache["expires_at"] = 0.0
