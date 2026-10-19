"""共享 fixture 契约测试：实读 Rust 生成的 tests/fixtures/hyperliquid/ 快照。

字段破坏测试在同一份真实序列化输出上进行，避免手写 mock 与 Rust 漂移。
"""
import copy
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from web.routes import hyperliquid
from web.services import hyperliquid_pairs_service as pairs
from web.services.hyperliquid_service import HyperliquidUnavailable

FIXTURE_DIR = Path(__file__).resolve().parents[1] / 'fixtures' / 'hyperliquid'
SNAPSHOT_FIXTURES = ['v2_snapshot_hype_btc.json', 'v2_snapshot_nbis_crwv.json']


def load_fixture(name):
    path = FIXTURE_DIR / name
    if not path.exists():
        pytest.skip(f'共享 fixture 尚未生成：{path}')
    return json.loads(path.read_text(encoding='utf-8'))


def descriptor_for(snapshot):
    pair = snapshot['pair']
    return {
        'id': pair['id'],
        'label': pair['label'],
        'network': pair['network'],
        'dex': pair['dex'],
        'coins': list(pair['coins']),
        'account_address': pair['account_address'],
        'unlocks_enabled': snapshot['blocks'].get('unlocks') is not None,
    }


@pytest.mark.parametrize('name', SNAPSHOT_FIXTURES)
def test_rust_fixture_snapshot_passes_contract(name):
    snap = load_fixture(name)
    result = pairs._validate_v2_snapshot(copy.deepcopy(snap), descriptor_for(snap), snap['config_revision'])
    assert result['schema_version'] == 2
    positions = result['blocks']['positions']
    assert positions['items'] is not None or positions['position_state'] == 'unknown'
    funding = result['blocks']['funding']
    assert set(funding['hourly_rates']) == set(snap['pair']['coins'])


def test_rust_fixture_announcements_passes_contract():
    payload = load_fixture('v2_announcements.json')
    result = pairs._validate_announcements(copy.deepcopy(payload), '')
    assert result['block']['status'] in ('ok', 'partial', 'unavailable')


@pytest.mark.parametrize('block_name,rows_key,reason', [
    ('positions', 'items', '持仓条目非法'),
    ('funding', 'recent_payments', 'recent_payments 非法'),
])
def test_bad_coin_degrades_only_affected_block_over_http(
        monkeypatch, block_name, rows_key, reason):
    snap = load_fixture('v2_snapshot_nbis_crwv.json')
    snap['blocks'][block_name][rows_key][0]['coin'] = []
    expected_other = copy.deepcopy({
        name: block for name, block in snap['blocks'].items() if name != block_name
    })
    monkeypatch.setattr(pairs, 'load_pairs', lambda: (snap['config_revision'], [descriptor_for(snap)]))
    monkeypatch.setattr(pairs, '_run_dashboard', lambda _args: copy.deepcopy(snap))
    pairs.clear_runtime_cache()
    app = FastAPI()
    app.include_router(hyperliquid.router, prefix='/api/v1')
    try:
        with TestClient(app) as client:
            response = client.get('/api/v1/hyperliquid/pairs/nbis-crwv/dashboard')
    finally:
        pairs.clear_runtime_cache()
    assert response.status_code == 200, response.text
    body = response.json()['data']
    block = body['blocks'][block_name]
    assert block['status'] == 'unavailable'
    assert set(pairs.BLOCK_DATA_KEYS[block_name]) <= block.keys()
    assert set(block['source_queries']) == set(pairs.BLOCK_SOURCE_KEYS[block_name])
    assert block['queried_at'] == snap['queried_at']
    assert block['diagnostics'] == [reason]
    assert {name: body['blocks'][name] for name in expected_other} == expected_other


def set_deep(obj, path, value):
    target = obj
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value


@pytest.mark.parametrize('name', SNAPSHOT_FIXTURES)
@pytest.mark.parametrize('path,reason_part', [
    (('blocks', 'account', 'equity'), '十进制'),
    (('blocks', 'ratio', 'latest_close'), '十进制'),
    (('blocks', 'positions', 'position_state'), '枚举'),
    (('blocks', 'funding', 'window_complete'), '布尔'),
])
def test_fixture_field_destruction_degrades_block(name, path, reason_part):
    snap = load_fixture(name)
    set_deep(snap, path, 'NaN' if path[-1] in ('equity', 'latest_close')
             else ('weird' if path[-1] == 'position_state' else 'yes'))
    result = pairs._validate_v2_snapshot(snap, descriptor_for(snap), snap['config_revision'])
    block = result['blocks'][path[1]]
    assert block['status'] == 'unavailable'
    assert any(reason_part in d for d in block['diagnostics'])


@pytest.mark.parametrize('name', SNAPSHOT_FIXTURES)
def test_fixture_identity_tamper_rejects_whole_snapshot(name):
    snap = load_fixture(name)
    descriptor = descriptor_for(snap)  # 用原始身份做预期
    snap['pair']['account_address'] = '0x' + '9' * 40
    with pytest.raises(HyperliquidUnavailable, match='身份'):
        pairs._validate_v2_snapshot(snap, descriptor, snap['config_revision'])


@pytest.mark.parametrize('name', SNAPSHOT_FIXTURES)
def test_fixture_wrong_revision_rejects(name):
    snap = load_fixture(name)
    with pytest.raises(HyperliquidUnavailable, match='config_revision'):
        pairs._validate_v2_snapshot(snap, descriptor_for(snap), '0' * 64)


def test_fixture_announcements_destruction_rejects():
    payload = load_fixture('v2_announcements.json')
    payload['block']['items'] = {}
    with pytest.raises(HyperliquidUnavailable, match='数组或 null'):
        pairs._validate_announcements(payload, '')
