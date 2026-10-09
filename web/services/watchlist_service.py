"""Watchlist persistence and market-local daily quote batches."""
from __future__ import annotations

import math
from datetime import datetime, timezone

import config
from db import Connection
from psycopg2.errors import UniqueViolation


def read_rows(sql, params=()):
    with Connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            names = [d[0] for d in cur.description]
            rows = [dict(zip(names, row)) for row in cur.fetchall()]
    return rows


def list_items():
    rows = read_rows('SELECT * FROM watchlist_item ORDER BY sort_order, id')
    for row in rows:
        for key in ('created_at', 'updated_at'):
            row[key] = row[key].isoformat()
    return rows


def save_item(data, item_id=None):
    """Explicit transaction: db.execute(fetch=True) does not commit RETURNING."""
    with Connection() as conn:
        with conn.cursor() as cur:
            try:
                if item_id is None:
                    cur.execute(
                        '''INSERT INTO watchlist_item
                        (market,stock_code,stock_name,group_name,note,research_url)
                        VALUES (%s,%s,%s,%s,%s,%s) RETURNING id''',
                        tuple(data[k] for k in ('market','stock_code','stock_name',
                                               'group_name','note','research_url')),
                    )
                else:
                    cur.execute(
                        '''UPDATE watchlist_item SET group_name=%s,note=%s,
                        research_url=%s,updated_at=NOW() WHERE id=%s RETURNING id''',
                        (data['group_name'], data['note'], data['research_url'], item_id),
                    )
                row = cur.fetchone()
                if row is None:
                    raise LookupError('关注项不存在')
                conn.commit()
                return {'id': row[0]}
            except UniqueViolation as exc:
                raise ValueError('这只股票已经在关注列表中') from exc


def delete_item(item_id):
    with Connection() as conn:
        with conn.cursor() as cur:
            cur.execute('DELETE FROM watchlist_item WHERE id=%s RETURNING id', (item_id,))
            if cur.fetchone() is None:
                raise LookupError('关注项不存在')
            conn.commit()


def number(value):
    if value is None:
        return None
    result = float(value)
    return result if math.isfinite(result) and result > 0 else None


def summarize_quote(code, rows, market_date):
    """Keep missing sessions in the series; never jump over an invalid latest close."""
    rows = sorted(rows, key=lambda r: r['trade_date'])
    base = dict(stock_code=code, close=None, change_pct=None, quote_date=None,
                status='missing', warning='没有日线行情', trend=[], stale=False,
                price_basis='raw_unadjusted')
    if not rows:
        return base
    latest = rows[-1]
    close = number(latest['close'])
    previous = number(rows[-2]['close']) if len(rows) > 1 else None
    quote_date = latest['trade_date']
    base.update(close=close, quote_date=quote_date.isoformat(),
                stale=(market_date - quote_date).days > 4,
                trend=[{'date': r['trade_date'].isoformat(), 'close': number(r['close'])}
                       for r in rows[-20:]])
    if close is None:
        base.update(status='invalid_close', warning='最近交易日收盘价缺失或无效')
    elif previous is None:
        base.update(status='missing_previous', warning='缺少有效前收价，无法计算涨跌幅')
    else:
        change = (close / previous - 1) * 100
        jumps = any(
            abs(number(b['close']) / number(a['close']) - 1) > .35
            for a, b in zip(rows, rows[1:])
            if number(a['close']) is not None and number(b['close']) is not None
        )
        if jumps:
            base.update(status='review_price_basis', trend=[],
                        warning='窗口内价格跳变超过35%，需核对拆股/除权或真实异动')
        else:
            base.update(status='ok', change_pct=change,
                        warning='原始日线价格，未校验拆股及除权')
    return base


def quote_batch(market, codes):
    if market not in config.scheduler.markets:
        raise ValueError(f'本服务器不支持 {market} 行情')
    rows = read_rows('''SELECT requested.stock_code, q.trade_date, q.close
        FROM unnest(%s::text[]) AS requested(stock_code)
        CROSS JOIN LATERAL (
            SELECT trade_date,close FROM daily_quote
            WHERE stock_code=requested.stock_code AND market=%s
            ORDER BY trade_date DESC LIMIT 20
        ) q''', (codes, market))
    from zoneinfo import ZoneInfo
    zones = {'US': 'America/New_York', 'CN_HK': 'Asia/Hong_Kong', 'CN_A': 'Asia/Shanghai'}
    today = datetime.now(ZoneInfo(zones[market])).date()
    groups = {code: [] for code in codes}
    for row in rows:
        groups[row['stock_code']].append(row)
    return {'market': market, 'currency': {'US':'USD','CN_HK':'HKD','CN_A':'CNY'}[market],
            'fetched_at': datetime.now(timezone.utc).isoformat(),
            'quotes': [summarize_quote(code, groups[code], today) for code in codes]}
