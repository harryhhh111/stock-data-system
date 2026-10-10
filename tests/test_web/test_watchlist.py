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
        watchlist.require_admin('Bearer 中文')
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
