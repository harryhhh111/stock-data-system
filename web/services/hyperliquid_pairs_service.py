"""v2 多 pair 看板：服务端 JSON 配置校验、目录与配置修订号。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from pathlib import Path

CONFIG_VERSION = 1
SCHEMA_VERSION = 2
MAX_ID_LEN = 64
PAIR_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
ZERO_ADDRESS = "0x" + "0" * 40
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

TOP_KEYS = {"config_version", "pairs"}
PAIR_KEYS = {"id", "label", "enabled", "network", "dex", "coins",
             "account_address", "unlock_anchors"}
NETWORKS = ("mainnet", "testnet")
ANCHOR_KEYS = {"date_utc", "label", "source_url", "verified_utc"}

_cache: dict = {"revision": None, "pairs": None}
_lock = threading.Lock()


class PairsConfigError(RuntimeError):
    """配置不可用：未配置环境变量、文件不可读或校验失败。

    message 供服务端日志；public（若非 None）为可对外展示的安全简短原因。
    """

    def __init__(self, message: str, public: str | None = None):
        super().__init__(message)
        self.public = public


def _reject_dup_keys(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError(f"重复 JSON 键: {key}")
        result[key] = value
    return result


def _fail(message: str) -> None:
    raise PairsConfigError(message)


def _require_str(value, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{what} 必须是非空字符串")
    return value


def _validate_anchor(item) -> None:
    if not isinstance(item, dict):
        _fail("unlock_anchors 条目必须是对象")
    if set(item) != ANCHOR_KEYS:
        _fail(f"unlock_anchors 条目字段必须为 {sorted(ANCHOR_KEYS)}")
    for key in ANCHOR_KEYS:
        _require_str(item[key], f"unlock_anchors.{key}")
    for key in ("date_utc", "verified_utc"):
        if not DATE_RE.fullmatch(item[key]):
            _fail(f"unlock_anchors.{key} 必须是 YYYY-MM-DD")


def _validate_pair(entry, seen_ids: set) -> tuple[dict, bool]:
    if not isinstance(entry, dict):
        _fail("pairs 条目必须是对象")
    keys = set(entry)
    if keys - PAIR_KEYS:
        _fail(f"pair 含未知字段: {sorted(keys - PAIR_KEYS)}")
    if PAIR_KEYS - keys:
        _fail(f"pair 缺少字段: {sorted(PAIR_KEYS - keys)}")

    pid = entry["id"]
    if not isinstance(pid, str) or len(pid) > MAX_ID_LEN or not PAIR_ID_RE.fullmatch(pid):
        _fail(f"pair id 非法: {pid!r}")
    if pid in seen_ids:
        _fail(f"重复 pair id: {pid}")
    seen_ids.add(pid)

    label = _require_str(entry["label"], "label")
    if type(entry["enabled"]) is not bool:
        _fail("enabled 必须是布尔值")
    network = entry["network"]
    if network not in NETWORKS:
        _fail(f"network 只接受 mainnet|testnet: {network!r}")
    dex = entry["dex"]
    if not isinstance(dex, str):
        _fail("dex 必须是字符串")

    coins = entry["coins"]
    if (not isinstance(coins, list) or len(coins) != 2
            or not all(isinstance(c, str) and c for c in coins)):
        _fail("coins 必须是恰好两个非空字符串")
    if coins[0] == coins[1]:
        _fail("coins 两条腿必须不同")
    for coin in coins:
        if dex:
            prefix = f"{dex}:"
            if not coin.startswith(prefix) or ":" in coin[len(prefix):]:
                _fail(f"coin {coin!r} 必须是 {prefix}<name> 形式")
        elif ":" in coin:
            _fail(f"原生 DEX 的 coin 不能含冒号: {coin!r}")

    address = entry["account_address"]
    if address is not None:
        if not isinstance(address, str) or not ADDRESS_RE.fullmatch(address):
            _fail(f"account_address 非法: {address!r}")
        address = address.lower()
        if address == ZERO_ADDRESS:
            _fail("account_address 不允许零地址")

    anchors = entry["unlock_anchors"]
    if anchors is not None:
        if not isinstance(anchors, list):
            _fail("unlock_anchors 必须是数组或 null")
        for item in anchors:
            _validate_anchor(item)

    descriptor = {
        "id": pid,
        "label": label,
        "network": network,
        "dex": dex,
        "coins": list(coins),
        "account_address": address,
        "unlocks_enabled": anchors is not None,
    }
    return descriptor, entry["enabled"]


def _validate(raw: bytes) -> list[tuple[dict, bool]]:
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_dup_keys)
    except (UnicodeDecodeError, ValueError) as exc:
        _fail(f"pair 配置不是合法 JSON: {exc}")
    if not isinstance(data, dict):
        _fail("pair 配置顶层必须是对象")
    keys = set(data)
    if keys - TOP_KEYS:
        _fail(f"顶层含未知字段: {sorted(keys - TOP_KEYS)}")
    if TOP_KEYS - keys:
        _fail(f"顶层缺少字段: {sorted(TOP_KEYS - keys)}")
    if data["config_version"] != CONFIG_VERSION:
        _fail(f"config_version 不支持: {data['config_version']!r}")
    if not isinstance(data["pairs"], list):
        _fail("pairs 必须是数组")
    seen_ids: set = set()
    return [_validate_pair(entry, seen_ids) for entry in data["pairs"]]


def load_pairs() -> tuple[str, list[dict]]:
    """返回 (config_revision, 启用 pair 目录)。配置按内容 SHA-256 缓存。"""
    path = os.environ.get("STOCK_HL_PAIRS_CONFIG", "").strip()
    if not path:
        message = "未配置 STOCK_HL_PAIRS_CONFIG"
        raise PairsConfigError(message, public=message)
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise PairsConfigError(f"读取 pair 配置失败: {exc}") from exc
    revision = hashlib.sha256(raw).hexdigest()
    with _lock:
        if _cache["revision"] == revision:
            return revision, _cache["pairs"]
        validated = _validate(raw)
        pairs = [descriptor for descriptor, enabled in validated if enabled]
        _cache["revision"] = revision
        _cache["pairs"] = pairs
        return revision, pairs


def clear_cache() -> None:
    with _lock:
        _cache["revision"] = None
        _cache["pairs"] = None
