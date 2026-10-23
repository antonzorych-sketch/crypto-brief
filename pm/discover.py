"""pm/discover: что есть на Polymarket US по крипте — слаги, поля рынка, правила, стакан. Плюс проверка источников цены."""
import json, urllib.request, urllib.parse
G = 'https://gateway.polymarket.us'
def get(u):
    try:
        r = urllib.request.urlopen(urllib.request.Request(u, headers={'User-Agent': 'pm-discover'}), timeout=20)
        return json.loads(r.read())
    except Exception as e: return {'_error': repr(e)[:300]}
out = {}
for name, u in [('binance_vision', 'https://data-api.binance.vision/api/v3/ticker/price?symbol=BTCUSDT'),
                ('binance_com', 'https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT'),
                ('coinbase', 'https://api.exchange.coinbase.com/products/BTC-USD/ticker'),
                ('deribit_dvol', 'https://www.deribit.com/api/v2/public/get_index_price?index_name=btc_usd')]:
    out[name] = get(u); print(name, json.dumps(out[name])[:200])
mk = []
for off in range(0, 3000, 100):
    r = get(f'{G}/v1/markets?' + urllib.parse.urlencode({'limit': 100, 'offset': off, 'active': 'true', 'closed': 'false'}))
    if off == 0: print('raw keys', list(r)[:10] if isinstance(r, dict) else type(r)); out['raw_first'] = r if isinstance(r, dict) and '_error' in r else None
    items = r.get('markets', r.get('data', [])) if isinstance(r, dict) else r
    if not items: break
    mk += items
print('активных рынков', len(mk))
def txt(m): return json.dumps(m).lower()
cr = [m for m in mk if any(k in txt(m) for k in ('bitcoin', 'btc', 'ethereum', 'solana', 'xrp'))]
print('крипто рынков', len(cr))
for m in cr[:400]: print(m.get('slug'), '|', m.get('question'), '|', m.get('endDate'), '|', m.get('bestBid'), m.get('bestAsk'))
out['crypto_samples'] = cr[:6]
ud = [m for m in cr if 'up or down' in txt(m) or 'updown' in txt(m)]
print('up/down', len(ud))
out['updown_samples'] = ud[:6]
s = (ud or cr or [{}])[0].get('slug')
if s:
    out['bbo'] = get(f'{G}/v1/markets/{s}/bbo'); out['book'] = get(f'{G}/v1/markets/{s}/book'); out['by_slug'] = get(f'{G}/v1/market/slug/{s}')
    print('bbo', json.dumps(out['bbo'])[:600])
out['search'] = get(f'{G}/v1/search?' + urllib.parse.urlencode({'query': 'bitcoin up or down'}))
out['events'] = get(f'{G}/v1/events?' + urllib.parse.urlencode({'limit': 50, 'active': 'true', 'closed': 'false', 'categories': 'crypto'}))
json.dump(out, open('pm/discover.json', 'w'), indent=1, default=str)
