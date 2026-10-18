"""v2 多 pair 目录接口：配置校验与边界，无外部进程、无交易所查询。"""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from web.routes import hyperliquid
from web.services import hyperliquid_pairs_service as pairs

ADDRESS = "0x7F2120F612B78D579197762196B8A202AB654096"
ADDRESS_LOWER = ADDRESS.lower()

VALID = {
    "config_version": 1,
    "pairs": [
        {"id": "hype-btc", "label": "HYPE / BTC", "enabled": True, "network": "mainnet",
         "dex": "", "coins": ["HYPE", "BTC"], "account_address": None, "unlock_anchors": []},
        {"id": "nbis-crwv", "label": "NBIS / CRWV", "enabled": True, "network": "mainnet",
         "dex": "xyz", "coins": ["xyz:NBIS", "xyz:CRWV"], "account_address": ADDRESS,
         "unlock_anchors": None},
    ],
}


@pytest.fixture
def client(monkeypatch, tmp_path):
    config_file = tmp_path / 'hl-pairs.json'
    monkeypatch.setenv('STOCK_HL_PAIRS_CONFIG', str(config_file))
    pairs.clear_cache()
    app = FastAPI()
    app.include_router(hyperliquid.router, prefix='/api/v1')
    yield TestClient(app), config_file
    pairs.clear_cache()


def write_config(client, data=None, raw=None):
    _, config_file = client
    if raw is not None:
        config_file.write_text(raw, encoding='utf-8')
    else:
        config_file.write_text(json.dumps(data or VALID), encoding='utf-8')
    pairs.clear_cache()


def test_directory_returns_enabled_pairs_with_revision(client):
    write_config(client)
    response = client[0].get('/api/v1/hyperliquid/pairs')
    assert response.status_code == 200
    body = response.json()
    assert body['ok'] is True
    data = body['data']
    assert data['schema_version'] == 2
    assert len(data['config_revision']) == 64
    hype, nbis = data['pairs']
    assert hype['id'] == 'hype-btc' and hype['account_address'] is None
    assert hype['unlocks_enabled'] is True
    assert nbis['account_address'] == ADDRESS_LOWER  # 大写输入归一化
    assert nbis['unlocks_enabled'] is False


def test_disabled_pair_excluded_from_directory(client):
    data = json.loads(json.dumps(VALID))
    data['pairs'][0]['enabled'] = False
    write_config(client, data)
    body = client[0].get('/api/v1/hyperliquid/pairs').json()['data']
    assert [p['id'] for p in body['pairs']] == ['nbis-crwv']


def test_revision_tracks_config_content(client):
    write_config(client)
    first = client[0].get('/api/v1/hyperliquid/pairs').json()['data']['config_revision']
    write_config(client)  # 同内容再写：revision 相同
    again = client[0].get('/api/v1/hyperliquid/pairs').json()['data']['config_revision']
    assert first == again
    data = json.loads(json.dumps(VALID))
    data['pairs'][1]['account_address'] = None
    write_config(client, data)
    changed = client[0].get('/api/v1/hyperliquid/pairs').json()['data']['config_revision']
    assert changed != first


@pytest.mark.parametrize('raw', [
    '{"config_version": 1, "config_version": 1, "pairs": []}',          # 重复 JSON 键
])
def test_duplicate_json_key_is_503(client, raw):
    write_config(client, raw=raw)
    response = client[0].get('/api/v1/hyperliquid/pairs')
    assert response.status_code == 503
    assert response.json()['detail'] == 'Hyperliquid 多 pair 配置不可用'


@pytest.mark.parametrize('mutate,match', [
    (lambda d: d.update(config_version=2), 'config_version'),
    (lambda d: d.pop('pairs'), '缺少字段'),
    (lambda d: d.update(extra=1), '未知字段'),
    (lambda d: d['pairs'][0].update(id='HYPE/BTC'), 'id 非法'),
    (lambda d: d['pairs'][0].update(id='nbis-crwv'), '重复 pair id'),
    (lambda d: d['pairs'][0].pop('enabled'), '缺少字段'),
    (lambda d: d['pairs'][0].update(enabled='yes'), '布尔'),
    (lambda d: d['pairs'][0].update(network='devnet'), 'network'),
    (lambda d: d['pairs'][1]['coins'].__setitem__(0, 'NBIS'), '形式'),
    (lambda d: d['pairs'][1]['coins'].__setitem__(1, 'xyz:NBIS'), '必须不同'),
    (lambda d: d['pairs'][1]['coins'].append('xyz:HYPE'), '恰好两个'),
    (lambda d: d['pairs'][0]['coins'].__setitem__(0, 'xyz:HYPE'), '冒号'),
    (lambda d: d['pairs'][0].update(account_address=''), 'account_address 非法'),
    (lambda d: d['pairs'][0].update(account_address='0xZZZ'), 'account_address 非法'),
    (lambda d: d['pairs'][0].update(account_address='0x' + '0' * 40), '零地址'),
    (lambda d: d['pairs'][0].update(unlock_anchors=[{'date_utc': '2026-10-20'}]), '字段'),
    (lambda d: d['pairs'][0].update(unlock_anchors=[{'date_utc': '10/20', 'label': 'x',
                                                      'source_url': 'https://a', 'verified_utc': '2026-10-18'}]), 'YYYY'),
])
def test_invalid_config_is_503_generic_detail(client, mutate, match):
    import copy
    data = copy.deepcopy(VALID)
    mutate(data)
    write_config(client, data)
    response = client[0].get('/api/v1/hyperliquid/pairs')
    assert response.status_code == 503
    assert response.json()['detail'] == 'Hyperliquid 多 pair 配置不可用'


def test_missing_env_var_is_specific_503(client, monkeypatch):
    monkeypatch.delenv('STOCK_HL_PAIRS_CONFIG')
    response = client[0].get('/api/v1/hyperliquid/pairs')
    assert response.status_code == 503
    assert 'STOCK_HL_PAIRS_CONFIG' in response.json()['detail']


def test_valid_anchor_passes(client):
    import copy
    data = copy.deepcopy(VALID)
    data['pairs'][0]['unlock_anchors'] = [
        {'date_utc': '2026-10-20', 'label': '团队份额解锁',
         'source_url': 'https://example.com', 'verified_utc': '2026-10-18'},
    ]
    write_config(client, data)
    body = client[0].get('/api/v1/hyperliquid/pairs').json()['data']
    assert body['pairs'][0]['unlocks_enabled'] is True


def test_v1_dashboard_untouched_by_pairs_config(client, monkeypatch):
    write_config(client)
    from web.services import hyperliquid_service as v1
    v1.clear_cache()
    runner_called = []

    def fake_runner(bin_path, config_path):
        runner_called.append((bin_path, config_path))
        return {"schema_version": 1, "network": "testnet",
                "account_address": "0xabcdef0123456789abcdef0123456789abcdef01",
                "queried_at": "2026-10-18T10:00:00Z", "blocks": {}}

    monkeypatch.setattr(v1, '_run_hl_trader', fake_runner)
    monkeypatch.setenv('STOCK_HL_TRADER_BIN', '/bin/hl')
    monkeypatch.setenv('STOCK_HL_TRADER_CONFIG', '/etc/hl.toml')
    monkeypatch.setenv('STOCK_HL_EXPECTED_ACCOUNT', '0xabcdef0123456789abcdef0123456789abcdef01')
    response = client[0].get('/api/v1/hyperliquid/dashboard')
    assert response.status_code == 200
    assert runner_called == [('/bin/hl', '/etc/hl.toml')]
