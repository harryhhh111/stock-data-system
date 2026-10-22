"""每日资讯基准回放：线 A 基准表 v2 的 15 事件离线重放。

合同：docs/WATCHLIST_DAILY_NEWS_MVP_PLAN.md §6.2——Google 源 15/15、
Finnhub 11/15，不达标阻塞本片。

回放走生产路径：media_source.parse_google_rss / parse_finnhub 解析 +
matches_company 过滤（与线 A 归档快照 docs/evidence/watchlist_daily_news/
完全一致，不访问网络）。事件命中关键词取自线 A 证据抽取日志
（evidence_extract.log）与基准表 v2 的标题记录；Finnhub 的 4 个未命中
事件（N-P2~N-P5，NBIS 官方 blog 条目确实不在 Finnhub feed）是线 A
已确认的真实缺口，不是过滤器误杀。
"""
from pathlib import Path

import pytest

from core.announcements import media_source

BASE = Path("docs/evidence/watchlist_daily_news")

GN_FILE = {"MU": "gn_0.xml", "WDC": "gn_1.xml", "NBIS": "gn_2.xml"}

# (事件 ID, 涉及公司, 命中关键词任一)——依据基准表 v2 与 evidence_extract.log
EVENTS = [
    ("M-P1", ["MU"], ["technology webinar"]),
    ("M-N1", ["MU"], ["micron stock dropped", "micron stock’s drop",
                       "micron stock's drop", "micron stock tumbles"]),
    ("W-N1", ["WDC"], ["seagate and western digital",
                        "western digital over seagate", "seagate, western digital"]),
    ("N-N1", ["NBIS"], ["nebius stock fell", "nebius group stock falling",
                         "nebius group declines",
                         "neocloud stocks like coreweave and nebius",
                         "coreweave, nebius"]),
    ("E1",   ["MU", "WDC"], ["cxmt"]),
    ("E2",   ["NBIS"], ["coreweave"]),
    ("N-P2", ["NBIS"], ["run:ai"]),
    ("N-P3", ["NBIS"], ["nvidia dli", "hands-on ai"]),
    ("N-P4", ["NBIS"], ["open-weight models"]),
    ("N-P5", ["NBIS"], ["ai builder program"]),
    ("W-N3", ["WDC"], ["mark long"]),
    ("M-N2", ["MU"], ["buyback", "$35"]),
    ("M-N3", ["MU"], ["union", "strike"]),
    ("N-N2", ["NBIS"], ["rosenblatt"]),
    ("E5",   ["NBIS"], ["gartner"]),
]

# 线 A 终审：Finnhub 对 N-P2~N-P5（NBIS 官方 blog）确实无命中
FINNHUB_KNOWN_MISS = {"N-P2", "N-P3", "N-P4", "N-P5"}


@pytest.fixture(scope="module")
def parsed():
    gn = {code: media_source.parse_google_rss((BASE / fname).read_bytes())
          for code, fname in GN_FILE.items()}
    fh = {s: media_source.parse_finnhub((BASE / f"fh_{s}.json").read_bytes(), s)
          for s in ("MU", "WDC", "NBIS")}
    return gn, fh


def _hits(items, keys, terms):
    out = []
    for it in items:
        text = (it.title + " " + it.summary).lower()
        if any(k in text for k in keys) and media_source.matches_company(
                it.title, it.summary, terms):
            out.append(it)
    return out


def test_baseline_replay(parsed):
    gn, fh = parsed
    results = {}
    for event_id, codes, keys in EVENTS:
        g_hits, f_hits = [], []
        for code in codes:
            terms = media_source.MEDIA_SEEDS[code]["match_terms"]
            g_hits.extend(_hits(gn[code], keys, terms))
            f_hits.extend(_hits(fh[code], keys, terms))
        results[event_id] = (g_hits, f_hits)

    google_ok = sum(1 for g, _ in results.values() if g)
    finnhub_ok = sum(1 for _, f in results.values() if f)
    detail = {ev: (len(g), len(f)) for ev, (g, f) in results.items()}
    assert google_ok == 15, f"Google 源命中 {google_ok}/15: {detail}"
    assert finnhub_ok == 11, f"Finnhub 命中 {finnhub_ok}/15: {detail}"
    # 已知缺口逐事件核对：不多不少
    missed = {ev for ev, (_, f) in results.items() if not f}
    assert missed == FINNHUB_KNOWN_MISS
