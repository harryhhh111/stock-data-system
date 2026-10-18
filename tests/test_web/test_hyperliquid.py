"""Hyperliquid dashboard contract; mock runner, no live hl-trader or exchange."""
import json
import os
import subprocess
import time
import sys
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from web.routes import hyperliquid
from web.services import hyperliquid_service as service

ACCOUNT = "0xabcdef0123456789abcdef0123456789abcdef01"
BIN = "/fixed/hl-trader"
CFG = "/fixed/hl-trader-dashboard.toml"


def full_snapshot(**overrides):
    snapshot = {
        "schema_version": 1,
        "network": "testnet",
        "account_address": ACCOUNT,
        "queried_at": "2026-10-18T08:00:00Z",
        "blocks": {
            "positions": {"status": "ok", "queried_at": "2026-10-18T08:00:00Z",
                          "equity": "1234.56", "positions": [], "notional_ratio": None,
                          "ratio_drift": None, "action_state": "pair_closed",
                          "open_orders": 0, "diagnostics": []},
            "funding": {"status": "ok", "queried_at": "2026-10-18T08:00:00Z",
                        "hourly_rates": {"HYPE": "0.00001", "BTC": "0.00001"},
                        "annualized": {"HYPE": "8.76", "BTC": "8.76"},
                        "daily_estimate": None,
                        "window_start": "2026-10-16T08:00:00Z",
                        "window_end": "2026-10-18T08:00:00Z",
                        "window_complete": True, "actual_48h_net": "0",
                        "recent_payments": [], "diagnostics": []},
            "ratio": {"status": "ok", "queried_at": "2026-10-18T08:00:00Z",
                      "current": "0.0005", "ma30": "0.00049", "vs_ma30_pct": "2.04",
                      "as_of_date": "2026-10-17", "diagnostics": []},
            "unlocks": {"status": "ok", "queried_at": "2026-10-18T08:00:00Z",
                        "anchors": [], "note": "日历预告不等于实际申领已发生",
                        "diagnostics": []},
            "announcements": {"status": "partial", "queried_at": "2026-10-18T08:00:00Z",
                              "items": [], "diagnostics": ["API 频道预览返回 302，暂不可用"]},
        },
    }
    snapshot.update(overrides)
    return snapshot


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv('STOCK_HL_TRADER_BIN', BIN)
    monkeypatch.setenv('STOCK_HL_TRADER_CONFIG', CFG)
    monkeypatch.setenv('STOCK_HL_EXPECTED_ACCOUNT', ACCOUNT)
    service.clear_cache()
    app = FastAPI()
    app.include_router(hyperliquid.router, prefix='/api/v1')
    yield TestClient(app)
    service.clear_cache()


def mock_runner(monkeypatch, snapshot=None, error=None):
    runner = MagicMock()
    if error is not None:
        runner.side_effect = error
    else:
        runner.return_value = snapshot
    monkeypatch.setattr(service, '_run_hl_trader', runner)
    return runner


def test_happy_path_returns_normalized_snapshot(client, monkeypatch):
    runner = mock_runner(monkeypatch, full_snapshot())
    response = client.get('/api/v1/hyperliquid/dashboard')
    assert response.status_code == 200
    body = response.json()
    assert body['ok'] is True
    data = body['data']
    assert data['network'] == 'testnet'
    assert data['account_address'] == ACCOUNT
    assert set(data['blocks']) == set(service.BLOCKS)
    assert data['blocks']['funding']['window_complete'] is True
    runner.assert_called_once_with(BIN, CFG)


def test_account_match_is_case_insensitive(client, monkeypatch):
    snap = full_snapshot(account_address=ACCOUNT.upper().replace('0X', '0x'))
    mock_runner(monkeypatch, snap)
    assert client.get('/api/v1/hyperliquid/dashboard').status_code == 200


@pytest.mark.parametrize('field,value', [
    ('schema_version', 2),
    ('network', 'mainnet'),
    ('account_address', '0x9999' + ACCOUNT[6:]),
])
def test_contract_mismatch_is_503(field, value, client, monkeypatch):
    mock_runner(monkeypatch, full_snapshot(**{field: value}))
    response = client.get('/api/v1/hyperliquid/dashboard')
    assert response.status_code == 503
    assert 'detail' in response.json()


@pytest.mark.parametrize('missing', list(service.BLOCKS))
def test_missing_block_becomes_unavailable(missing, client, monkeypatch):
    snap = full_snapshot()
    del snap['blocks'][missing]
    mock_runner(monkeypatch, snap)
    response = client.get('/api/v1/hyperliquid/dashboard')
    assert response.status_code == 200
    block = response.json()['data']['blocks'][missing]
    assert block['status'] == 'unavailable'
    assert block['diagnostics']


def test_invalid_block_status_becomes_unavailable(client, monkeypatch):
    snap = full_snapshot()
    snap['blocks']['ratio']['status'] = 'weird'
    mock_runner(monkeypatch, snap)
    block = client.get('/api/v1/hyperliquid/dashboard').json()['data']['blocks']['ratio']
    assert block['status'] == 'unavailable'
    assert block['diagnostics']


def test_runner_failure_is_503(client, monkeypatch):
    mock_runner(monkeypatch, error=service.HyperliquidUnavailable('hl-trader 超时（>30s）', public='hl-trader 超时'))
    response = client.get('/api/v1/hyperliquid/dashboard')
    assert response.status_code == 503
    assert '超时' in response.json()['detail']


def test_internal_error_detail_not_exposed(client, monkeypatch):
    secret = 'internal /home/vinci/secret-path detail'
    mock_runner(monkeypatch, error=service.HyperliquidUnavailable(secret))
    response = client.get('/api/v1/hyperliquid/dashboard')
    assert response.status_code == 503
    detail = response.json()['detail']
    assert secret not in detail
    assert detail == 'Hyperliquid 看板服务暂不可用'


def test_public_reason_exposed_when_marked(client, monkeypatch):
    mock_runner(monkeypatch, error=service.HyperliquidUnavailable('internal noise', public='hl-trader 快照不是合法 JSON'))
    response = client.get('/api/v1/hyperliquid/dashboard')
    assert response.json()['detail'] == 'hl-trader 快照不是合法 JSON'


def test_missing_env_config_is_503(client, monkeypatch):
    monkeypatch.delenv('STOCK_HL_EXPECTED_ACCOUNT')
    response = client.get('/api/v1/hyperliquid/dashboard')
    assert response.status_code == 503
    assert 'STOCK_HL_EXPECTED_ACCOUNT' in response.json()['detail']


def test_cache_serves_second_request_without_runner(client, monkeypatch):
    runner = mock_runner(monkeypatch, full_snapshot())
    assert client.get('/api/v1/hyperliquid/dashboard').status_code == 200
    assert client.get('/api/v1/hyperliquid/dashboard').status_code == 200
    assert runner.call_count == 1


def test_cache_expires(monkeypatch, client):
    runner = mock_runner(monkeypatch, full_snapshot())
    monkeypatch.setattr(service, 'CACHE_TTL_SECONDS', 0)
    assert client.get('/api/v1/hyperliquid/dashboard').status_code == 200
    assert client.get('/api/v1/hyperliquid/dashboard').status_code == 200
    assert runner.call_count == 2


@pytest.fixture
def fake_bin(tmp_path):
    def make(body: str) -> str:
        path = tmp_path / 'hl-fake'
        path.write_text('#!/bin/sh\n' + body + '\n', encoding='utf-8')
        path.chmod(0o755)
        return str(path)
    return make


def test_run_hl_trader_rejects_nonzero_exit(client, fake_bin):
    bin_path = fake_bin('echo "internal /home/vinci/secret-path detail" >&2; exit 3')
    with pytest.raises(service.HyperliquidUnavailable, match='退出码 3') as err:
        service._run_hl_trader(bin_path, CFG)
    assert 'secret-path' in str(err.value)  # 内部日志可含细节


def test_run_hl_trader_rejects_non_json_stdout(client, fake_bin):
    bin_path = fake_bin('echo not-json')
    with pytest.raises(service.HyperliquidUnavailable, match='不是合法 JSON'):
        service._run_hl_trader(bin_path, CFG)


def test_run_hl_trader_rejects_oversized_stdout(client, fake_bin):
    bin_path = fake_bin('head -c 2097152 /dev/zero | tr "\\0" x; echo')
    with pytest.raises(service.HyperliquidUnavailable, match='1 MiB'):
        service._run_hl_trader(bin_path, CFG)


def test_run_hl_trader_passes_snapshot_through(client, fake_bin, tmp_path):
    snap_file = tmp_path / 'snap.json'
    snap_file.write_text(json.dumps(full_snapshot(), ensure_ascii=False), encoding='utf-8')
    bin_path = fake_bin(f'cat {snap_file}')
    result = service._run_hl_trader(bin_path, CFG)
    assert result['schema_version'] == 1
    assert result['blocks']['funding']['window_complete'] is True


def test_run_hl_trader_uses_argv_without_shell(client, fake_bin, tmp_path):
    argv_log = tmp_path / 'argv.log'
    bin_path = fake_bin(f'printf \'%s\\n\' "$@" > {argv_log}\necho "{{}}"')
    result = service._run_hl_trader(bin_path, CFG)
    assert result == {}
    assert argv_log.read_text().splitlines() == ['--config', CFG, 'status', '--json']


# ── 整改 B2（R5）：真实子进程的非阻塞双管道读取 ──────────────


def run_child(script: str, timeout: int):
    proc = subprocess.Popen(
        [sys.executable, '-c', script], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    started = time.monotonic()
    try:
        out, err = service._communicate_bounded(proc, timeout)
        return proc, out, err, time.monotonic() - started
    except Exception:
        proc.wait()
        raise


def test_both_pipes_drained_under_pressure():
    """双管道交替大输出：阻塞式读取会死锁至超时，非阻塞必须快速完成。"""
    script = (
        "import sys,time\n"
        "for i in range(200):\n"
        "    sys.stdout.write('o'*2048); sys.stdout.flush()\n"
        "    sys.stderr.write('e'*2048); sys.stderr.flush()\n"
    )
    proc, out, err, elapsed = run_child(script, 15)
    assert proc.returncode == 0
    assert len(out) == 200 * 2048 and len(err) == 200 * 2048
    assert elapsed < 10


def test_multibyte_chars_split_across_chunks():
    script = (
        "import sys,time\n"
        "data='比价中文字符'*2000\n"
        "for i in range(0,len(data),7):\n"
        "    sys.stdout.write(data[i:i+7]); sys.stdout.flush(); time.sleep(0.001)\n"
    )
    proc, out, _err, _elapsed = run_child(script, 20)
    assert proc.returncode == 0
    assert '比价中文字符' in out and len(out) == len('比价中文字符' * 2000)


def test_no_output_child():
    proc, out, err, _e = run_child("pass", 10)
    assert proc.returncode == 0 and out == '' and err == ''


def test_oversized_output_kills_child(tmp_path):
    pid_file = tmp_path / 'child.pid'
    script = (
        f"import os,pathlib; pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        "print('x' * (2 * 1024 * 1024))\n"
    )
    with pytest.raises(service.HyperliquidUnavailable, match='1 MiB'):
        run_child(script, 15)
    pid = int(pid_file.read_text())
    with pytest.raises(OSError):
        os.kill(pid, 0)  # 进程已被回收


def test_child_closing_output_but_not_exiting_is_killed(tmp_path, monkeypatch):
    monkeypatch.setattr(service, 'PROC_TIMEOUT_SECONDS', 1)
    pid_file = tmp_path / 'child2.pid'
    script = (
        f"import os,pathlib; pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        "os.close(1); os.close(2)\n"
        "import time; time.sleep(60)\n"
    )
    with pytest.raises(service.HyperliquidUnavailable, match='超时'):
        run_child(script, 1)
    pid = int(pid_file.read_text())
    with pytest.raises(OSError):
        os.kill(pid, 0)


def test_silent_child_timeout_reaps(tmp_path):
    pid_file = tmp_path / 'child3.pid'
    script = (
        f"import os,pathlib; pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        "import time; time.sleep(60)\n"
    )
    started = time.monotonic()
    with pytest.raises(service.HyperliquidUnavailable, match='超时'):
        run_child(script, 1)
    assert time.monotonic() - started < 5
    pid = int(pid_file.read_text())
    with pytest.raises(OSError):
        os.kill(pid, 0)


# ── 复验整改：v1 块级严格校验（旧接口仍注册，非法数据不得标 ok） ──

def test_v1_nan_notional_degrades_positions(client, monkeypatch):
    snap = full_snapshot()
    snap['blocks']['positions']['positions'] = [
        {"coin": "HYPE", "side": "long", "size": "1", "mark_px": "30", "unrealized_pnl": "NaN",
         "notional": "30"},
    ]
    mock_runner(monkeypatch, snap)
    body = client.get('/api/v1/hyperliquid/dashboard').json()['data']
    block = body['blocks']['positions']
    assert block['status'] == 'unavailable'
    assert block['positions'] is None and block['equity'] is None
    assert body['blocks']['ratio']['status'] == 'ok'  # 其他块不受影响


@pytest.mark.parametrize('mutate,block_name,reason_part', [
    (lambda s: s['blocks']['funding'].__setitem__('window_start', 'not-a-time'), 'funding', 'UTC 时间'),
    (lambda s: s['blocks']['funding'].__setitem__('hourly_rates', {"HYPE": "0.1"}), 'funding', 'HYPE/BTC'),
    (lambda s: s['blocks']['ratio'].__setitem__('vs_ma30_pct', 12), 'ratio', '十进制字符串'),
    (lambda s: s['blocks']['unlocks'].__setitem__('anchors', {}), 'unlocks', '数组'),
    (lambda s: s['blocks']['announcements'].__setitem__('items', {}), 'announcements', '数组或 null'),
    (lambda s: s['blocks']['positions'].__setitem__('diagnostics', ['源失败']), 'positions', '不允许携带诊断'),
    (lambda s: s['blocks']['positions']['positions'].__setitem__(0, {"coin": "HYPE", "side": "long",
                                                                      "size": "-1"}), 'positions', '持仓条目非法'),
])
def test_v1_invalid_block_data_degraded(client, monkeypatch, mutate, block_name, reason_part):
    snap = full_snapshot()
    snap['blocks']['positions']['positions'] = [
        {"coin": "HYPE", "side": "long", "size": "1", "mark_px": "30", "unrealized_pnl": "0",
         "notional": "30"},
    ]
    mutate(snap)
    mock_runner(monkeypatch, snap)
    body = client.get('/api/v1/hyperliquid/dashboard').json()['data']
    block = body['blocks'][block_name]
    assert block['status'] == 'unavailable'
    assert any(reason_part in d for d in block['diagnostics'])


def test_v1_valid_snapshot_still_ok(client, monkeypatch):
    mock_runner(monkeypatch, full_snapshot())
    body = client.get('/api/v1/hyperliquid/dashboard').json()['data']
    assert all(body['blocks'][n]['status'] == 'ok' for n in ('positions', 'funding', 'ratio', 'unlocks'))


def test_flushed_output_then_sleep_obeys_deadline(tmp_path):
    """已有少量可读输出时也不能卡在定长读取，整体 deadline 仍然有效。"""
    pid_file = tmp_path / 'flushed-child.pid'
    script = (
        f"import os,pathlib; pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        "import time; print('partial-output', flush=True); time.sleep(60)\n"
    )
    started = time.monotonic()
    with pytest.raises(service.HyperliquidUnavailable, match='超时'):
        run_child(script, 0.3)
    assert time.monotonic() - started < 3
    pid = int(pid_file.read_text())
    with pytest.raises(OSError):
        os.kill(pid, 0)
