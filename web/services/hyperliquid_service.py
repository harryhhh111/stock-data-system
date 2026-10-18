"""Hyperliquid 只读看板：调用本机 hl-trader status --json 并校验契约。"""
from __future__ import annotations

import json
import os
import selectors
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
    """快照不可用：配置缺失、进程失败或契约校验不通过。

    message 供服务端日志；public（若非 None）为可对外展示的安全简短原因。
    """

    def __init__(self, message: str, public: str | None = None):
        super().__init__(message)
        self.public = public


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
        message = f"未配置 {', '.join(missing)}"
        raise HyperliquidUnavailable(message, public=message)
    return bin_path, config_path, expected


def _reap_process(proc: subprocess.Popen) -> None:
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _communicate_bounded(proc: subprocess.Popen, timeout: int) -> tuple[str, str]:
    """二进制非阻塞双管道读取：selector 就绪后用 os.read 按字节累计限额。

    读取与等待退出共用同一个 monotonic deadline；UTF-8 在完整收集后解码，
    不会分块误切多字节字符。超时/超限/读异常均终止并回收子进程。
    """
    sel = selectors.DefaultSelector()
    pipes = []
    try:
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                os.set_blocking(pipe.fileno(), False)
                sel.register(pipe, selectors.EVENT_READ)
                pipes.append(pipe)
        buffers: dict = {pipe: bytearray() for pipe in pipes}
        deadline = time.monotonic() + timeout
        while sel.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _reap_process(proc)
                raise HyperliquidUnavailable(f"hl-trader 超时（>{timeout}s）", public="hl-trader 超时")
            for key, _ in sel.select(remaining):
                pipe = key.fileobj
                try:
                    chunk = os.read(pipe.fileno(), 65536)
                except BlockingIOError:
                    continue
                except OSError as exc:
                    sel.unregister(pipe)
                    continue
                if not chunk:
                    sel.unregister(pipe)
                    continue
                buffers[pipe] += chunk
                if len(buffers[pipe]) > STDOUT_LIMIT_BYTES:
                    which = "stdout" if pipe is proc.stdout else "stderr"
                    _reap_process(proc)
                    raise HyperliquidUnavailable(
                        f"hl-trader {which} 超过 1 MiB 上限", public="hl-trader 输出超限",
                    )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _reap_process(proc)
            raise HyperliquidUnavailable(f"hl-trader 超时（>{timeout}s）", public="hl-trader 超时")
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            _reap_process(proc)
            raise HyperliquidUnavailable(f"hl-trader 超时（>{timeout}s）", public="hl-trader 超时") from exc
        return (bytes(buffers.get(proc.stdout, b"")).decode("utf-8", "replace"),
                bytes(buffers.get(proc.stderr, b"")).decode("utf-8", "replace"))
    finally:
        sel.close()
        for pipe in pipes:
            try:
                pipe.close()
            except OSError:
                pass


def _run_hl_trader(bin_path: str, config_path: str) -> dict:
    argv = [bin_path, "--config", config_path, "status", "--json"]
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        raise HyperliquidUnavailable(f"无法执行 hl-trader: {exc}") from exc
    stdout, _stderr = _communicate_bounded(proc, PROC_TIMEOUT_SECONDS)
    if proc.returncode != 0:
        raise HyperliquidUnavailable(
            f"hl-trader 退出码 {proc.returncode}: {_stderr.strip()[:200]}"
        )
    try:
        snapshot = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise HyperliquidUnavailable(
            f"hl-trader 输出不是合法 JSON: {exc}", public="hl-trader 快照不是合法 JSON"
        ) from exc
    if not isinstance(snapshot, dict):
        raise HyperliquidUnavailable(
            "hl-trader 输出不是 JSON 对象", public="hl-trader 快照不是合法 JSON"
        )
    return snapshot


def _validate(snapshot: dict, expected_account: str) -> dict:
    if snapshot.get("schema_version") != 1:
        message = f"schema_version 不支持: {snapshot.get('schema_version')!r}"
        raise HyperliquidUnavailable(message, public=message)
    if snapshot.get("network") != "testnet":
        message = f"network 不是 testnet: {snapshot.get('network')!r}"
        raise HyperliquidUnavailable(message, public=message)
    address = str(snapshot.get("account_address") or "").strip().lower()
    if address != expected_account:
        message = "account_address 与 STOCK_HL_EXPECTED_ACCOUNT 不一致"
        raise HyperliquidUnavailable(message, public=message)
    if not isinstance(snapshot.get("queried_at"), str) or not snapshot["queried_at"]:
        message = "快照缺少 queried_at"
        raise HyperliquidUnavailable(message, public=message)
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
