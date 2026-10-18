"""Hyperliquid 只读看板：调用本机 hl-trader status --json 并校验契约。"""
from __future__ import annotations

import json
import os
import selectors
import subprocess
import threading
import time
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

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
                    _reap_process(proc)
                    raise HyperliquidUnavailable(
                        "读取 hl-trader 输出失败", public="hl-trader 输出读取失败",
                    ) from exc
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
        if not isinstance(diagnostics, list) or not all(isinstance(d, str) for d in diagnostics):
            block["diagnostics"] = [str(d) for d in diagnostics] if diagnostics else []
        block.setdefault("queried_at", snapshot.get("queried_at", ""))
        reason = _validate_v1_block(name, block)
        if reason:
            block = _degraded_v1_block(name, reason, block.get("queried_at") or snapshot["queried_at"])
        blocks[name] = block
    snapshot["blocks"] = blocks
    return snapshot


# ── v1 块级严格校验（复验整改）：旧接口仍注册且公开，非法数据不得标 ok ──

def _dec(value) -> bool:
    """有限十进制字符串（拒绝 NaN/Infinity/空串/JSON number）。"""
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        return Decimal(value).is_finite()
    except InvalidOperation:
        return False


def _nonneg_dec(value) -> bool:
    return _dec(value) and Decimal(value) >= 0


def _ts(value) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return True


def _date(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


V1_SOURCE_COINS = ("HYPE", "BTC")


def _degraded_v1_block(name: str, reason: str, fallback_queried_at: str) -> dict:
    null_coins = {coin: None for coin in V1_SOURCE_COINS}
    data = {
        "positions": {"equity": None, "positions": None, "notional_ratio": None,
                      "ratio_drift": None, "action_state": None, "open_orders": None},
        "funding": {"hourly_rates": dict(null_coins), "annualized_pct": dict(null_coins),
                    "daily_estimate": None, "window_start": None, "window_end": None,
                    "window_complete": False, "actual_48h_net": None, "recent_payments": None},
        "ratio": {"current": None, "ma30": None, "vs_ma30_pct": None, "as_of_date": None},
        "unlocks": {"anchors": [], "note": "日历预告不等于实际申领已发生"},
        "announcements": {"items": None, "queried_at": None},
    }[name]
    block = {"status": "unavailable", "queried_at": fallback_queried_at,
             "diagnostics": [reason]}
    block.update(data)
    return block


def _validate_v1_block(name: str, block: dict) -> str | None:
    """校验 v1 区块数据；返回降级原因，None 表示通过。"""
    if not _ts(block.get("queried_at")):
        return f"{name} 区块 queried_at 非法"
    if block["status"] == "ok" and block["diagnostics"]:
        return f"{name} 区块 ok 时不允许携带诊断"
    if name == "positions":
        items = block.get("positions")
        if items is not None:
            if not isinstance(items, list):
                return "positions 必须是数组或 null"
            for item in items:
                if (not isinstance(item, dict)
                        or not isinstance(item.get("coin"), str)
                        or item.get("side") not in ("long", "short")
                        or not _nonneg_dec(item.get("size"))
                        or (item.get("mark_px") is not None and not _dec(item["mark_px"]))
                        or (item.get("unrealized_pnl") is not None and not _dec(item["unrealized_pnl"]))
                        or (item.get("notional") is not None and not _nonneg_dec(item["notional"]))):
                    return "持仓条目非法"
        for key in ("equity", "notional_ratio", "ratio_drift"):
            if block.get(key) is not None and not _dec(block[key]):
                return f"{key} 不是有限十进制字符串"
        if block.get("action_state") is not None and not isinstance(block["action_state"], str):
            return "action_state 类型错误"
        if block.get("open_orders") is not None and (
                type(block["open_orders"]) is not int or block["open_orders"] < 0):
            return "open_orders 非法"
    elif name == "funding":
        for key in ("hourly_rates", "annualized"):
            rates = block.get(key)
            if (not isinstance(rates, dict) or set(rates) != set(V1_SOURCE_COINS)
                    or any(v is not None and not _dec(v) for v in rates.values())):
                return f"{key} 必须恰好覆盖 HYPE/BTC 且为有限十进制或 null"
        estimate = block.get("daily_estimate")
        if estimate is not None and (
                not isinstance(estimate, dict)
                or estimate.get("direction") not in ("receive", "pay")
                or not _nonneg_dec(estimate.get("amount"))):
            return "daily_estimate 非法"
        for key in ("window_start", "window_end"):
            if block.get(key) is not None and not _ts(block[key]):
                return f"{key} 不是合法 UTC 时间"
        if type(block.get("window_complete")) is not bool:
            return "window_complete 必须是布尔值"
        if block.get("actual_48h_net") is not None and not _dec(block["actual_48h_net"]):
            return "actual_48h_net 不是有限十进制字符串"
        payments = block.get("recent_payments")
        if payments is not None:
            if not isinstance(payments, list) or len(payments) > 3:
                return "recent_payments 非法"
            for p in payments:
                if (not isinstance(p, dict)
                        or not _ts(p.get("time"))
                        or not isinstance(p.get("coin"), str)
                        or not _dec(p.get("usdc"))
                        or not _dec(p.get("rate"))):
                    return "recent_payments 条目非法"
    elif name == "ratio":
        for key in ("current", "ma30", "vs_ma30_pct"):
            if block.get(key) is not None and not _dec(block[key]):
                return f"{key} 不是有限十进制字符串"
        if block.get("as_of_date") is not None and not _date(block["as_of_date"]):
            return "as_of_date 不是合法日期"
    elif name == "unlocks":
        anchors = block.get("anchors")
        if not isinstance(anchors, list):
            return "anchors 必须是数组"
        for anchor in anchors:
            if (not isinstance(anchor, dict)
                    or not _date(anchor.get("date"))
                    or type(anchor.get("days_ahead")) is not int or anchor["days_ahead"] < 0
                    or not isinstance(anchor.get("label"), str)
                    or not isinstance(anchor.get("source_url"), str)
                    or not _date(anchor.get("verified_utc"))):
                return "解锁锚点非法"
        if not isinstance(block.get("note"), str):
            return "note 必须是字符串"
    elif name == "announcements":
        items = block.get("items")
        if items is not None:
            if not isinstance(items, list):
                return "公告条目必须是数组或 null"
            for item in items:
                if (not isinstance(item, dict)
                        or item.get("kind") not in ("general", "api")
                        or not isinstance(item.get("text"), str)
                        or (item.get("published_at") is not None
                            and not isinstance(item["published_at"], str))
                        or not isinstance(item.get("url"), str)
                        or not isinstance(item.get("source"), str)):
                    return "公告条目非法"
        if block.get("queried_at") is not None and not _ts(block["queried_at"]):
            return "公告 queried_at 非法"
    return None


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
