from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import pytest
import psycopg2.errors

spec=importlib.util.spec_from_file_location('news_fixtures', Path('tests/test_web/test_news_api.py'))
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
cluster, env, client=m.cluster,m.env,m.client
UTC=timezone.utc


def test_official_detail_excludes_media(env, client):
    m.seed_company(env,'MU','0000723125')
    sec=m.seed_source(env,'sec','0000723125','sec')
    g=m.seed_source(env,'media','0000723125','google_news_rss')
    now=datetime.now(UTC)
    m.seed_item(env,sec,'sec','MU',title='Official filing',published_at=now,kind_form='8-K')
    m.seed_item(env,g,'g','MU',title='Media story',published_at=now)
    item_id=m.sql(env,"SELECT id FROM watchlist_item WHERE stock_code='MU'")[0][0]
    response=client.get(f'/api/v1/watchlist/{item_id}/announcements')
    assert response.status_code==200,response.text
    data=response.json()['data']
    assert data['total']==1 and all(e['source_kind'] in ('sec','ir') for e in data['items']),data['items']


def test_one_publisher_via_two_channels_not_pinned(env,client):
    """同一出版商经 Google+Finnhub 两个渠道收录，来源数只算 1，
    不得仅凭此进入置顶（v1.1 结构：entries + ai_available）。"""
    m.seed_company(env,'MU','0000723125')
    g=m.seed_source(env,'media','0000723125','google_news_rss')
    f=m.seed_source(env,'media','0000723125','finnhub')
    now=datetime.now(UTC)
    for sid,key,title in [(g,'g','Micron rises - Reuters'),(f,'f','Micron rises')]:
        m.seed_item(env,sid,key,'MU',title=title,published_at=now,media_source='Reuters')
    data=client.get('/api/v1/watchlist/news/pinned').json()['data']
    assert data['entries']==[],data['entries']


@pytest.mark.parametrize('endpoint',['feed','pinned'])
def test_base_table_missing_is_503(env,client,monkeypatch,endpoint):
    real=m.news_service.ann._read_rows
    def fail(sql,params=()):
        if 'FROM watchlist_item' in sql:
            raise psycopg2.errors.UndefinedTable('watchlist_item missing')
        return real(sql,params)
    monkeypatch.setattr(m.news_service.ann,'_read_rows',fail)
    res=client.get('/api/v1/watchlist/news/'+endpoint)
    assert res.status_code==503,res.json()


def test_official_source_failure_visible_on_official_feed(env,client):
    m.seed_company(env,'MU','0000723125')
    sid=m.seed_source(env,'sec','0000723125','sec')
    m.sql(env,"""INSERT INTO watchlist_announcement_check_state
        (market, issuer_identifier, source_id, effective_interval_minutes,
         last_attempt_at, last_attempt_status, last_error_class)
        VALUES ('US','0000723125',%s,60,NOW(),'failed','http_429')""",(sid,))
    data=client.get('/api/v1/watchlist/news/feed?kind=official&stock=MU').json()['data']
    assert any(s['kind']=='sec' and s['state']=='failed' for s in data['sources']),data


def test_title_clause_is_not_publisher_suffix():
    rows=[dict(title='Micron guidance - raised',published_at=datetime(2026,10,23,tzinfo=UTC)),
          dict(title='Micron guidance - cut',published_at=datetime(2026,10,23,tzinfo=UTC))]
    assert len(m.fold_media(rows))==2


def test_same_story_keeps_both_companies(env,client):
    m.seed_company(env,'MU','0000723125');m.seed_company(env,'WDC','0000106040')
    g=m.seed_source(env,'media','0000723125','google_news_rss')
    m.seed_item(env,g,'shared','MU',title='Micron and Western Digital shares rise',published_at=datetime.now(UTC),media_source='Reuters')
    m.sql(env,"""INSERT INTO watchlist_announcement_security(announcement_id,market,stock_code)
        SELECT id,'US','WDC' FROM watchlist_announcement WHERE source_id=%s""",(g,))
    data=client.get('/api/v1/watchlist/news/feed').json()['data']
    assert len(data['entries'])==1
    e=data['entries'][0]
    assert e['fold_count']==1,e
