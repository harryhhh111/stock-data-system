import importlib.util
from pathlib import Path
from datetime import datetime,timedelta,timezone
spec=importlib.util.spec_from_file_location('media_fixtures',Path('tests/test_announcements_media.py'))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
cluster,env=m.cluster,m.env

def test_finnhub_downtime_is_not_reported_as_complete(env,monkeypatch):
    dsn,_=env
    m.seed_mu(dsn);m.run();m.enable_media(dsn)
    monkeypatch.setattr(m.media_source,'fetch_and_parse',lambda *a,**k: [])
    m.run()
    old=datetime.now(timezone.utc)-timedelta(days=10)
    m.sql(dsn,"UPDATE watchlist_announcement_check_state SET checked_through=%s,last_success_at=%s WHERE source_id IN (SELECT source_id FROM watchlist_announcement_source WHERE source_kind='media')",(old,old))
    result=m.run()
    f=next(s for s in result['sources'] if s.get('platform')=='finnhub')
    assert f['status']=='partial',f
