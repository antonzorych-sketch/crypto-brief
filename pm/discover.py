"""pm/discover: Kalshi — индексы акций и погода: серии, правила, ликвидность; источники данных из Actions."""
import json, time, urllib.request, urllib.parse
K = 'https://api.elections.kalshi.com/trade-api/v2'
def get(u, h=None):
    try:
        r = urllib.request.urlopen(urllib.request.Request(u, headers=h or {'User-Agent': 'pm-discover (antonzorych@gmail.com)', 'Accept': 'application/json'}), timeout=20); return json.loads(r.read())
    except urllib.error.HTTPError as e: return {'_error': f'{e.code} {e.read()[:150]}'}
    except Exception as e: return {'_error': repr(e)[:150]}
# все серии по категориям
for cat in ('Financials', 'Climate and Weather', 'Economics'):
    r = get(f'{K}/series?' + urllib.parse.urlencode({'category': cat}))
    ss = r.get('series', []) if isinstance(r, dict) else []
    print(f'\n=== {cat}: серий {len(ss)}', r.get('_error', ''))
    for s in ss:
        t = s.get('ticker', '')
        if cat == 'Financials' and not any(x in t for x in ('INX', 'NASDAQ', 'SPX', 'NDX', 'DOW', 'WTI', 'EUR', 'USDJPY', 'GOLD')): continue
        if cat == 'Climate and Weather' and 'HIGH' not in t and 'LOW' not in t: continue
        if cat == 'Economics': continue
        print(t, '|', s.get('title'), '|', s.get('frequency'), '| fee', s.get('fee_type'), s.get('fee_multiplier'))
def series_detail(ser, n=40):
    r = get(f'{K}/markets?' + urllib.parse.urlencode({'series_ticker': ser, 'status': 'open', 'limit': 200}))
    ms = r.get('markets', []) if isinstance(r, dict) else []
    if not ms: print(ser, 'нет открытых', r.get('_error', '')); return
    ev = {}
    for m in ms: ev.setdefault(m['event_ticker'], []).append(m)
    print(f'\n--- {ser}: событий {len(ev)}, рынков {len(ms)}')
    for e, mm in list(ev.items())[:2]:
        v = sum(float(m.get('volume_fp') or m.get('volume') or 0) for m in mm); oi = sum(float(m.get('open_interest_fp') or m.get('open_interest') or 0) for m in mm)
        print(' событие', e, 'рынков', len(mm), 'объём', v, 'OI', oi, 'закрытие', mm[0].get('close_time'))
        for m in sorted(mm, key=lambda m: m.get('floor_strike') or m.get('cap_strike') or 0)[:n]:
            print('   ', m['ticker'], '|', m.get('subtitle') or m.get('yes_sub_title'), '| bid/ask', m.get('yes_bid_dollars'), m.get('yes_ask_dollars'), '| vol', m.get('volume_fp') or m.get('volume'), '| liq', m.get('liquidity_dollars'))
        print('  правила:', (mm[0].get('rules_primary') or '')[:700]); print('  доп:', (mm[0].get('rules_secondary') or '')[:500])
for ser in ('KXINX', 'KXINXU', 'KXNASDAQ100', 'KXNASDAQ100U', 'KXHIGHNY', 'KXHIGHCHI', 'KXHIGHMIA', 'KXHIGHAUS', 'KXHIGHLAX', 'KXHIGHDEN', 'KXHIGHPHIL'):
    series_detail(ser, 12)
print('\n=== источники')
for name, u in [('hyperliquid xyz', None), ('yahoo ES=F', 'https://query1.finance.yahoo.com/v8/finance/chart/ES=F?interval=1m&range=1d'),
                ('yahoo ^GSPC', 'https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC?interval=1m&range=1d'),
                ('nws KNYC obs', 'https://api.weather.gov/stations/KNYC/observations/latest'),
                ('nws KNYC 5min list', 'https://api.weather.gov/stations/KNYC/observations?limit=3'),
                ('awc metar', 'https://aviationweather.gov/api/data/metar?ids=KNYC,KMDW,KMIA&format=json&hours=1'),
                ('iem asos 1min', 'https://mesonet.agron.iastate.edu/cgi-bin/request/asos1min.py?station=NYC&vars=tmpf&sts=2026-10-23T12:00Z&ets=2026-10-23T13:00Z&sample=1min&what=view'),
                ('nws CLI NYC', 'https://api.weather.gov/products/types/CLI/locations/NYC'),
                ('nws forecast hourly NYC', 'https://api.weather.gov/gridpoints/OKX/33,37/forecast/hourly')]:
    if u is None:
        try:
            req = urllib.request.Request('https://api.hyperliquid.xyz/info', data=json.dumps({'type': 'allMids', 'dex': 'xyz'}).encode(), headers={'Content-Type': 'application/json'})
            d = json.loads(urllib.request.urlopen(req, timeout=15).read()); print(name, {k: v for k, v in d.items() if any(x in k.upper() for x in ('SP500', 'SPX', 'NDX', 'NASDAQ', 'XYZ100', 'US500', 'NQ', 'ES'))} or list(d)[:30])
        except Exception as e: print(name, repr(e)[:150])
        continue
    d = get(u, {'User-Agent': 'pm-discover (antonzorych@gmail.com)', 'Accept': 'application/geo+json,application/json'}) if 'weather.gov' in u else get(u)
    s = json.dumps(d)
    if 'yahoo' in name and isinstance(d, dict) and 'chart' in d:
        m = d['chart']['result'][0]['meta']; ts = d['chart']['result'][0].get('timestamp') or [0]
        s = f"цена {m.get('regularMarketPrice')} время {m.get('regularMarketTime')} (сейчас {int(time.time())}, отставание {int(time.time()) - (m.get('regularMarketTime') or 0)} с) последняя свеча {ts[-1]}"
    print(name, s[:500])
