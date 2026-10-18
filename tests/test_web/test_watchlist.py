"""Watchlist contracts; isolated from live PostgreSQL and external markets."""
from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from web.routes import watchlist
from web.services import watchlist_service as service


def quotes(*prices):
    return [{'trade_date': date(2026, 10, 1) + timedelta(days=i), 'close': p}
            for i, p in enumerate(prices)]


def test_daily_change_and_series():
    result = service.summarize_quote('ORCL', quotes(100, 102), date(2026, 10, 2))
    assert result['change_pct'] == pytest.approx(2)
    assert result['close'] == 102
    assert result['quote_date'] == '2026-10-02'
    assert result['previous_quote_date'] == '2026-10-01'
    assert result['price_basis'] == 'raw_unadjusted'
    assert len(result['trend']) == 2
    assert not result['stale']


@pytest.mark.parametrize('invalid', [None, 0, -1, float('nan'), float('inf')])
def test_invalid_latest_is_never_forward_filled(invalid):
    result = service.summarize_quote('ORCL', quotes(100, invalid), date(2026, 10, 2))
    assert result['close'] is None
    assert result['change_pct'] is None
    assert result['status'] == 'invalid_close'
    assert result['trend'][-1]['close'] is None


def test_missing_previous_does_not_jump_over_gap():
    result = service.summarize_quote('ORCL', quotes(100, None, 102), date(2026, 10, 3))
    assert result['status'] == 'missing_previous'
    assert result['change_pct'] is None
    assert result['previous_quote_date'] == '2026-10-02'


def test_comparison_dates_report_actual_stored_sessions():
    rows = [{'trade_date': date(2026, 10, 1), 'close': 100},
            {'trade_date': date(2026, 10, 8), 'close': 102}]
    result = service.summarize_quote('ORCL', rows, date(2026, 10, 8))
    assert result['previous_quote_date'] == '2026-10-01'
    assert result['quote_date'] == '2026-10-08'
    assert result['change_pct'] == pytest.approx(2)


def test_comparison_date_is_absent_without_previous_record():
    for rows in [[], quotes(100)]:
        result = service.summarize_quote('ORCL', rows, date(2026, 10, 2))
        assert result['previous_quote_date'] is None
        assert result['change_pct'] is None


def test_split_like_jump_is_not_normal_change():
    result = service.summarize_quote('ORCL', quotes(100, 50, 51), date(2026, 10, 3))
    assert result['status'] == 'review_price_basis'
    assert result['change_pct'] is None
    assert result['trend'] == []


def test_empty_short_and_stale_history():
    assert service.summarize_quote('ORCL', [], date.today())['status'] == 'missing'
    single = service.summarize_quote('ORCL', quotes(100), date(2026, 10, 8))
    assert single['status'] == 'missing_previous'
    assert single['stale']
    assert len(single['trend']) == 1


def test_batch_market_boundary_and_missing(monkeypatch):
    monkeypatch.setattr(service.config.scheduler, 'markets', ['US'])
    reader = MagicMock(return_value=[{'stock_code': 'ORCL', **row} for row in quotes(100, 102)])
    monkeypatch.setattr(service, 'read_rows', reader)
    result = service.quote_batch('US', ['ORCL', 'NBIS'])
    assert result['currency'] == 'USD'
    assert result['quotes'][1]['status'] == 'missing'
    assert reader.call_args.args[1] == (['ORCL', 'NBIS'], 'US')
    with pytest.raises(ValueError, match='不支持'):
        service.quote_batch('CN_HK', ['00700'])
    assert reader.call_count == 1


def test_canonical_code_formats():
    c = service.canonical_code
    assert c('US', 'aapl') == 'AAPL'
    assert c('CN_HK', '2020.HK') == '02020'
    assert c('CN_HK', '2020') == '02020'
    assert c('CN_HK', ' 0700 ') == '00700'
    assert c('CN_A', '600519.SH') == '600519'
    assert c('CN_A', '000001.SZ') == '000001'
    # 不匹配市场规则时原样返回（仅大写）
    assert c('CN_HK', 'BRK.B') == 'BRK.B'
    assert c('CN_HK', '20201') == '20201'


def test_quote_batch_normalizes_lookup_but_keys_by_request(monkeypatch):
    monkeypatch.setattr(service.config.scheduler, 'markets', ['CN_HK'])
    seen = {}
    def reader(sql, params=()):
        seen['params'] = params
        return [{'stock_code': '02020', **row} for row in quotes(75.45, 77.95)]
    monkeypatch.setattr(service, 'read_rows', reader)
    result = service.quote_batch('CN_HK', ['2020.HK'])
    # 查询用规范化代码，响应键仍是请求原码（前端按池内代码合并不错位）
    assert seen['params'][0] == ['02020']
    assert result['quotes'][0]['stock_code'] == '2020.HK'
    assert result['quotes'][0]['close'] == 77.95
    assert result['quotes'][0]['status'] == 'ok'


def test_save_item_normalizes_stock_code(monkeypatch):
    captured = {}
    class FakeCursor:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def execute(self, sql, params):
            captured['params'] = params
        def fetchone(self):
            return (42,)
    class FakeConn:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def cursor(self):
            return FakeCursor()
        def commit(self):
            pass
    monkeypatch.setattr(service, 'Connection', FakeConn)
    data = {'market': 'CN_HK', 'stock_code': '2020.HK', 'stock_name': '安踏体育',
            'group_name': '', 'note': '', 'research_url': ''}
    assert service.save_item(data) == {'id': 42}
    assert captured['params'][1] == '02020'
    # 调用方的 dict 不被就地改动
    assert data['stock_code'] == '2020.HK'


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv('STOCK_WATCHLIST_ADMIN_TOKEN', 'test-token-for-watchlist-123456')
    app = FastAPI()
    app.include_router(watchlist.router, prefix='/api/v1')
    return TestClient(app)


def test_read_and_write_auth(client, monkeypatch):
    reader = MagicMock(return_value=[])
    saver = MagicMock(return_value={'id': 1})
    monkeypatch.setattr(service, 'list_items', reader)
    monkeypatch.setattr(service, 'save_item', saver)
    assert client.get('/api/v1/watchlist').json() == {'ok': True, 'data': []}
    payload = {'market': 'US', 'stock_code': 'orcl', 'stock_name': 'Oracle'}
    for auth in [None, 'Bearer wrong']:
        headers = {'Authorization': auth} if auth else {}
        assert client.post('/api/v1/watchlist', json=payload, headers=headers).status_code == 401
    assert not saver.called
    response = client.post('/api/v1/watchlist', json=payload,
                           headers={'Authorization': 'Bearer test-token-for-watchlist-123456'})
    assert response.status_code == 200
    assert saver.call_args.args[0]['stock_code'] == 'ORCL'


def test_unicode_wrong_token_is_rejected():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        watchlist.require_admin('Bearer 中文', None)
    assert exc.value.status_code in (401, 503)


def test_no_token_fails_closed(client, monkeypatch):
    monkeypatch.delenv('STOCK_WATCHLIST_ADMIN_TOKEN')
    response = client.delete('/api/v1/watchlist/1')
    assert response.status_code == 503


def test_url_validation_and_quote_limit(client):
    headers = {'Authorization': 'Bearer test-token-for-watchlist-123456'}
    assert client.post('/api/v1/watchlist', json={'market': 'US', 'stock_code': 'NBIS', 'stock_name': ' '}, headers=headers).status_code == 422
    for url in ['file:///Users/test/a.md', 'javascript:alert(1)', 'https://user:password@example.com']:
        assert client.patch('/api/v1/watchlist/1', json={'research_url': url}, headers=headers).status_code == 422
    for codes in [[], ['ORCL'] * 201, ['bad/sql']]:
        assert client.post('/api/v1/watchlist/quotes', json={'market': 'US', 'codes': codes}).status_code == 422


def test_errors_are_explicit(client, monkeypatch):
    monkeypatch.setattr(service, 'list_items', MagicMock(side_effect=RuntimeError('offline')))
    assert client.get('/api/v1/watchlist').status_code == 503
    monkeypatch.setattr(service, 'save_item', MagicMock(side_effect=ValueError('duplicate')))
    headers = {'Authorization': 'Bearer test-token-for-watchlist-123456'}
    assert client.post('/api/v1/watchlist', json={'market': 'US', 'stock_code': 'ORCL', 'stock_name': 'Oracle'}, headers=headers).status_code == 409
    monkeypatch.setattr(service, 'delete_item', MagicMock(side_effect=LookupError('missing')))
    assert client.delete('/api/v1/watchlist/1', headers=headers).status_code == 404


def test_mutations_commit_returning_rows(monkeypatch):
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = (42,)
    context = MagicMock()
    context.__enter__.return_value = connection
    monkeypatch.setattr(service, 'Connection', lambda: context)
    payload = dict(market='US', stock_code='ORCL', stock_name='Oracle', group_name='AI', note='reason', research_url='')
    assert service.save_item(payload) == {'id': 42}
    assert service.save_item(payload, 42) == {'id': 42}
    service.delete_item(42)
    assert connection.commit.call_count == 3
    cursor.fetchone.return_value = None
    with pytest.raises(LookupError):
        service.delete_item(42)
    assert connection.commit.call_count == 3


SUGGEST_HEADERS = {'Authorization': 'Bearer test-token-for-watchlist-123456'}
SUGGEST_PAYLOAD = {'market': 'US', 'stock_code': 'ORCL', 'stock_name': 'Oracle'}


def test_suggest_requires_admin(client):
    assert client.post('/api/v1/watchlist/suggest', json=SUGGEST_PAYLOAD).status_code == 401
    assert client.post('/api/v1/watchlist/suggest', json=SUGGEST_PAYLOAD,
                       headers={'Authorization': 'Bearer wrong'}).status_code == 401


def test_suggest_prefers_research_doc_and_falls_back_to_generic(client, monkeypatch, research_repo):
    research_repo.entry()
    sha = research_repo.commit()
    monkeypatch.setattr(service, 'RESEARCH_REPO_PATH', research_repo.root)
    monkeypatch.setattr(service, 'read_rows', MagicMock(return_value=[{'industry': '软件'}]))
    seen_prompts = []

    def fake_mmx(system, user):
        seen_prompts.append(user)
        return '{"group": "AI 云", "note": "OCI 增速与 RPO 是核心变量"}'

    monkeypatch.setattr(service, '_run_mmx', fake_mmx)
    ok = client.post('/api/v1/watchlist/suggest', json=SUGGEST_PAYLOAD, headers=SUGGEST_HEADERS)
    assert ok.status_code == 200
    data = ok.json()['data']
    assert data['source'] == 'research_doc'
    assert data['docs'] == ['ORCL/主文档.md']
    assert data['research']['commit_sha'] == sha
    assert data['research']['missing_sections'] == ['risks', 'metrics', 'open_questions']
    assert 'OCI 增速' in seen_prompts[0] and '软件' in seen_prompts[0]
    assert '简介不是观点' not in seen_prompts[0]
    assert sha in seen_prompts[0]

    generic = client.post('/api/v1/watchlist/suggest',
                          json={'market': 'US', 'stock_code': 'NBIS', 'stock_name': 'Nebius'},
                          headers=SUGGEST_HEADERS)
    assert generic.json()['data']['source'] == 'generic'
    assert generic.json()['data']['note'].startswith('[通用草稿]')
    assert '（无）' in seen_prompts[1]


def test_suggest_parses_fenced_json(client, monkeypatch, research_repo):
    research_repo.commit()
    monkeypatch.setattr(service, 'RESEARCH_REPO_PATH', research_repo.root)
    monkeypatch.setattr(service, 'read_rows', MagicMock(return_value=[]))
    monkeypatch.setattr(service, '_run_mmx',
                        lambda s, u: '```json\n{"group": "AI 云", "note": "[通用草稿] 测试"}\n```')
    resp = client.post('/api/v1/watchlist/suggest', json=SUGGEST_PAYLOAD, headers=SUGGEST_HEADERS)
    assert resp.json()['data']['group'] == 'AI 云'


def test_suggest_failure_maps_to_503(client, monkeypatch, research_repo):
    research_repo.commit()
    monkeypatch.setattr(service, 'RESEARCH_REPO_PATH', research_repo.root)
    monkeypatch.setattr(service, 'read_rows', MagicMock(return_value=[]))
    monkeypatch.setattr(service, '_run_mmx', MagicMock(side_effect=RuntimeError('mmx 调用失败: timeout')))
    assert client.post('/api/v1/watchlist/suggest', json=SUGGEST_PAYLOAD,
                       headers=SUGGEST_HEADERS).status_code == 503


@pytest.mark.parametrize('broken_entry', [False, True])
def test_research_failure_is_visible_and_never_calls_model(client, monkeypatch, research_repo, broken_entry):
    if broken_entry:
        research_repo.write('ORCL/research.yaml', 'schema_version: 999')
        research_repo.commit()
        root = research_repo.root
    else:
        root = research_repo.root / 'missing'
    monkeypatch.setattr(service, 'RESEARCH_REPO_PATH', root)
    model = MagicMock()
    monkeypatch.setattr(service, '_run_mmx', model)
    response = client.post('/api/v1/watchlist/suggest', json=SUGGEST_PAYLOAD, headers=SUGGEST_HEADERS)
    assert response.status_code == 503
    assert '研究读取失败' in response.json()['detail']
    model.assert_not_called()


def test_login_session_logout_cookie_flow(client, monkeypatch):
    assert client.get('/api/v1/watchlist/session').json()['data']['authenticated'] is False
    assert client.post('/api/v1/watchlist/login', json={'token': 'wrong'}).status_code == 401

    resp = client.post('/api/v1/watchlist/login', json={'token': 'test-token-for-watchlist-123456'})
    assert resp.status_code == 200
    assert client.get('/api/v1/watchlist/session').json()['data']['authenticated'] is True

    saver = MagicMock(return_value={'id': 1})
    monkeypatch.setattr(service, 'save_item', saver)
    assert client.post('/api/v1/watchlist', json=SUGGEST_PAYLOAD).status_code == 200  # 免 header
    assert saver.called

    client.cookies.set('watchlist_admin', '999999999.forged-signature')
    assert client.post('/api/v1/watchlist', json=SUGGEST_PAYLOAD).status_code == 401

    assert client.post('/api/v1/watchlist/login', json={'token': 'test-token-for-watchlist-123456'}).status_code == 200
    assert client.post('/api/v1/watchlist/logout').status_code == 200
    assert client.get('/api/v1/watchlist/session').json()['data']['authenticated'] is False
    assert client.post('/api/v1/watchlist', json=SUGGEST_PAYLOAD).status_code == 401
