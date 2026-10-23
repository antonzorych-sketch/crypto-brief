"""pm/discover: Kalshi 15m/часовые крипто-рынки (поля, стакан), лестницы цен на Polymarket US, Deribit DVOL."""
import json, urllib.request, urllib.parse
G = 'https://gateway.polymarket.us'; K = 'https://api.elections.kalshi.com/trade-api/v2'
def get(u):
    try:
        r = urllib.request.urlopen(urllib.request.Request(u, headers={'User-Agent': 'pm-discover'}), timeout=20); return json.loads(r.read())
    except urllib.error.HTTPError as e: return {'_error': f'{e.code} {e.read()[:200]}'}
    except Exception as e: return {'_error': repr(e)[:200]}
print('=== KALSHI')
for ser in ('KXBTC15M', 'KXETH15M', 'KXSOL15M', 'KXXRP15M', 'KXBTCD', 'KXBTC', 'KXETHD'):
    r = get(f'{K}/markets?' + urllib.parse.urlencode({'series_ticker': ser, 'status': 'open', 'limit': 5}))
    ms = r.get('markets', []) if isinstance(r, dict) else []
    print(ser, 'открытых', len(ms), r.get('_error', ''))
    for m in ms[:2]:
        print('  ', json.dumps({k: m.get(k) for k in ('ticker', 'event_ticker', 'title', 'subtitle', 'open_time', 'close_time', 'expected_expiration_time', 'floor_strike', 'cap_strike', 'strike_type',
              'yes_bid', 'yes_ask', 'no_bid', 'no_ask', 'yes_bid_dollars', 'yes_ask_dollars', 'volume', 'volume_24h', 'liquidity', 'open_interest', 'status', 'price_level_structure')})[:900])
    if ms and ser.endswith('15M'):
        t = ms[0]['ticker']
        print('   правила:', (ms[0].get('rules_primary') or '')[:500], '|', (ms[0].get('rules_secondary') or '')[:300])
        print('   стакан:', json.dumps(get(f'{K}/markets/{t}/orderbook?depth=5'))[:600])
s = get(f'{K}/series/KXBTC15M'); print('серия:', json.dumps(s)[:700])
print('\n=== POLYMARKET US лестницы')
seen = {}
for q in ['bitcoin price at', 'BTC price', 'bitcoin above', 'ethereum price at', 'bitcoin price range', 'price at 5pm', 'BTC weekly', 'bitcoin weekly']:
    r = get(f'{G}/v1/search?' + urllib.parse.urlencode({'query': q}))
    for e in r.get('events', []) if isinstance(r, dict) else []:
        if 'updown' in e.get('slug', ''): continue
        seen[e['slug']] = (e.get('title'), e.get('seriesSlug'), e.get('endDate'), e.get('closed'), [(m['slug'], m.get('question'), m.get('title')) for m in e.get('markets', [])])
for k, (ti, ser, end, cl, ms) in seen.items():
    print(k, '|', ti, '|', ser, '|', end, 'closed' if cl else '', '| рынков', len(ms))
    for m in ms[:3]: print('    ', m)
op = [k for k, v in seen.items() if not v[3]]
for k in op[:3]:
    ms = seen[k][4]
    for m in ms[:2]:
        b = get(f'{G}/v1/markets/{m[0]}/book'); print('  book', m[0], json.dumps(b)[:300])
    d = get(f'{G}/v1/market/slug/{ms[0][0]}'); print('  descr', ((d.get('market') or d).get('description') or '')[:500])
print('\n=== DERIBIT')
print(json.dumps(get('https://www.deribit.com/api/v2/public/get_volatility_index_data?currency=BTC&resolution=60&start_timestamp=%d&end_timestamp=%d' % (0, 0)))[:200])
import time; n = int(time.time() * 1000)
for c in ('BTC', 'ETH'):
    r = get(f'https://www.deribit.com/api/v2/public/get_volatility_index_data?currency={c}&resolution=60&start_timestamp={n - 600000}&end_timestamp={n}')
    print(c, 'DVOL', json.dumps(r.get('result', r))[:300])
