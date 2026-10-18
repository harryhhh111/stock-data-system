"""Hyperliquid dashboard contract; mock runner, no live hl-trader or exchange."""
import json
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
    mock_runner(monkeypatch, error=service.HyperliquidUnavailable('hl-trader 超时（>30s）'))
    response = client.get('/api/v1/hyperliquid/dashboard')
    assert response.status_code == 503
    assert '超时' in response.json()['detail']


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


def test_run_hl_trader_rejects_nonzero_exit(client, monkeypatch):
    proc = MagicMock(returncode=3, stderr='boom', stdout='{}')
    monkeypatch.setattr(service.subprocess, 'run', MagicMock(return_value=proc))
    with pytest.raises(service.HyperliquidUnavailable, match='退出码 3'):
        service._run_hl_trader(BIN, CFG)


def test_run_hl_trader_rejects_non_json_stdout(client, monkeypatch):
    proc = MagicMock(returncode=0, stderr='', stdout='<html>not json</html>')
    monkeypatch.setattr(service.subprocess, 'run', MagicMock(return_value=proc))
    with pytest.raises(service.HyperliquidUnavailable, match='不是合法 JSON'):
        service._run_hl_trader(BIN, CFG)


def test_run_hl_trader_rejects_oversized_stdout(client, monkeypatch):
    proc = MagicMock(returncode=0, stderr='', stdout='{"pad":"' + 'x' * service.STDOUT_LIMIT_BYTES + '"}')
    monkeypatch.setattr(service.subprocess, 'run', MagicMock(return_value=proc))
    with pytest.raises(service.HyperliquidUnavailable, match='1 MiB'):
        service._run_hl_trader(BIN, CFG)


def test_run_hl_trader_passes_args_without_shell(client, monkeypatch):
    run = MagicMock(return_value=MagicMock(returncode=0, stderr='', stdout=json.dumps(full_snapshot())))
    monkeypatch.setattr(service.subprocess, 'run', run)
    snapshot = service._run_hl_trader(BIN, CFG)
    args, kwargs = run.call_args
    assert args[0] == [BIN, '--config', CFG, 'status', '--json']
    assert 'shell' not in kwargs
    assert snapshot['schema_version'] == 1
