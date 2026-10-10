"""Opt-in real DB verification; cleans only its own uniquely named test item."""
import os
from uuid import uuid4

import pytest

from web.services import watchlist_service as service


pytestmark = pytest.mark.skipif(os.getenv('STOCK_TEST_WATCHLIST_DB') != '1',
                              reason='Set STOCK_TEST_WATCHLIST_DB=1 to test migrated US DB')


def test_committed_crud_survives_new_connections():
    code = 'MVPTEST-' + uuid4().hex[:10].upper()
    data = dict(market='US', stock_code=code, stock_name='MVP temporary test',
                group_name='test', note='original', research_url='https://example.com/research')
    item_id = None
    try:
        item_id = service.save_item(data)['id']
        assert any(r['id'] == item_id and r['note'] == 'original' for r in service.list_items())
        with pytest.raises(ValueError, match='已经'):
            service.save_item(data)
        service.save_item({**data, 'note': 'updated'}, item_id)
        assert next(r for r in service.list_items() if r['id'] == item_id)['note'] == 'updated'
    finally:
        if item_id is not None:
            service.delete_item(item_id)
    assert all(r['id'] != item_id for r in service.list_items())


def test_real_quote_matches_latest_two_rows():
    for code in ['ORCL', 'INTC']:
        rows = service.read_rows('''SELECT trade_date,close FROM daily_quote
            WHERE market=%s AND stock_code=%s ORDER BY trade_date DESC LIMIT 2''', ('US', code))
        assert len(rows) == 2
        result = service.quote_batch('US', [code])['quotes'][0]
        assert result['close'] == float(rows[0]['close'])
        assert result['quote_date'] == rows[0]['trade_date'].isoformat()
        assert result['change_pct'] == pytest.approx((float(rows[0]['close']) / float(rows[1]['close']) - 1) * 100)
