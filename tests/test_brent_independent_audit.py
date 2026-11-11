"""Independent audit probes (2026-11-02, against e10d2f3), kept as regression tests.

Supplied by Irakli's independent review. Only the imports are adapted: this repo's tests
folder is a package. The asserted behaviour is unchanged.
"""
from datetime import timedelta
from types import SimpleNamespace

from tests.test_brent_alerts import NewsFake, alerts, make
from tests.test_brent_news import make as make_news
from tradingagents.brent.news import Item, news_view


def opened(tmp_path):
    m, c, send, clock, store = make(tmp_path)
    m.tick()
    clock.advance(25)
    m.tick()
    return m, c, send, clock, store

def test_long_target_requires_bid_reaching_target(tmp_path):
    m,c,send,clock,store = opened(tmp_path)
    target = m.st['signals']['BR-20261103-001']['plan']['t2']
    c.bid, c.ask = target - .01, target + .02
    clock.advance(3)
    m.tick()
    assert m.st['signals']['BR-20261103-001']['paper']['state'] == 'open', 'Long target filled using ask although bid never reached target'

def test_poll_gap_stop_uses_available_price(tmp_path):
    m,c,send,clock,store = opened(tmp_path)
    c.bid,c.ask = 108.0,108.03
    clock.advance(3)
    m.tick()
    pp = m.st['signals']['BR-20261103-001']['paper']
    assert pp['exit'] <= c.bid, f"Stop simulated above available bid: {pp['exit']} vs {c.bid}"

def test_live_window_revoked_when_news_becomes_unavailable(tmp_path):
    m,c,send,clock,store = make(tmp_path)
    m.tick()
    m.news_view = NewsFake({'state':'unavailable', 'why':'new material story not classified', 'items':[]})
    clock.advance(25)
    m.tick()
    # news-policy-v3 (2026-11-11): news becoming unavailable is neutral, so a with-trend alert stays valid and fills.
    assert m.st['signals']['BR-20261103-001']['paper']['state'] == 'open'

def test_failed_chart_refresh_blocks_entry(tmp_path):
    m,c,send,clock,store = make(tmp_path)
    c.bid,c.ask = 110.47,110.50
    m.tick()
    assert not alerts(send)
    clock.advance(16*60)
    c.bid,c.ask = 110.17,110.20
    def fail(*a):
        raise RuntimeError('candle feed unavailable')
    c.candles = fail
    m.tick()
    assert not alerts(send), 'Alert issued despite failed refresh of chart invalidation data'

def test_eia_only_does_not_certify_general_news_coverage(tmp_path):
    col,clock = make_news(tmp_path, fail=('news.google.com',))
    col.poll()
    v = news_view(tmp_path,clock())
    assert v['state'] == 'unavailable', f"All news searches failed but got {v['state']} with {v['health']}"

def test_changed_eia_file_registers_new_version(tmp_path):
    col,clock = make_news(tmp_path)
    a = Item(feed='eia:summary', title='EIA file version OLD', link='https://ir.eia.gov/wpsr/summary.txt')
    b = Item(feed='eia:summary', title='EIA file version NEW', link=a.link)
    assert col._register(a)
    clock.t += timedelta(hours=1)
    assert col._register(b), 'Updated EIA content discarded because URL is unchanged'

def test_pending_analysis_retried_without_another_new_story(tmp_path, monkeypatch):
    from tradingagents.brent import __main__ as cli, news
    from tradingagents.brent.config import BrentConfig
    args = SimpleNamespace(analyze=True, once=False, minutes=999)
    calls = []
    class Collector:
        n = 0
        def poll(self):
            self.n += 1
            if self.n == 1:
                return ['pending item']
            args.once = True
            return []
        def feed_health(self):
            return {'fresh_check':True}
    col = Collector()
    monkeypatch.setattr(news, 'NewsCollector', lambda *a: col)
    monkeypatch.setattr(news, 'default_model', lambda *a: object())
    def analyze(*a, **kw):
        calls.append(1)
        return {'refused':'quiet window'} if len(calls) == 1 else {'state':{'state':'bullish'}}
    monkeypatch.setattr(news, 'analyze', analyze)
    monkeypatch.setattr(cli.time, 'sleep', lambda s: None)
    cli.cmd_news_check(args, BrentConfig(journal_dir=str(tmp_path)))
    assert len(calls) == 2, 'Pending classification only retries when a different new article arrives'
