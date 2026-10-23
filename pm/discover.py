"""pm/discover: ликвидность Up/Down рынков на Polymarket US по всем активам + проверка источников цены золота/серебра."""
import json, time, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta
G = 'https://gateway.polymarket.us'
def get(u, data=None):
    try:
        h = {'User-Agent': 'pm-discover', 'Content-Type': 'application/json'}
        r = urllib.request.urlopen(urllib.request.Request(u, data=json.dumps(data).encode() if data else None, headers=h), timeout=20)
        return json.loads(r.read())
    except Exception as e: return {'_error': repr(e)[:200]}
def book(slug):
    b = get(f'{G}/v1/markets/{slug}/book'); md = b.get('marketData', {})
    if '_error' in b: return b['_error']
    bids = sorted(((float(x['px']['value']), float(x['qty'])) for x in md.get('bids') or []), reverse=True)
    offs = sorted((float(x['px']['value']), float(x['qty'])) for x in md.get('offers') or [])
    st = md.get('stats', {})
    d5b = sum(q * p for p, q in bids if bids and p >= bids[0][0] - 0.05); d5a = sum(q * p for p, q in offs if offs and p <= offs[0][0] + 0.05)
    return {'bid': bids[0] if bids else None, 'ask': offs[0] if offs else None, 'spread': round(offs[0][0] - bids[0][0], 3) if bids and offs else None,
            '$depth5c_bid': round(d5b), '$depth5c_ask': round(d5a), 'traded': st.get('sharesTraded'), 'oi': st.get('openInterest'), 'state': md.get('state')}
seen = {}
for q in ['up or down 5m', 'up or down 15m', 'up or down hourly', 'up or down daily', 'BTC Up or Down', 'ETH Up or Down', 'SOL Up or Down',
          'XRP Up or Down', 'gold up or down', 'silver up or down']:
    r = get(f'{G}/v1/search?' + urllib.parse.urlencode({'query': q}))
    for e in r.get('events', []) if isinstance(r, dict) else []:
        for m in e.get('markets', []):
            if 'updown' in m['slug']: seen[m['slug']] = (e.get('startDate'), e.get('endDate'), e.get('closed'))
now = datetime.now(timezone.utc)
print('сейчас', now)
for s, (a, b, c) in sorted(seen.items()):
    print(s, a, '->', b, 'closed' if c else '', '\n   ', book(s))
print('\n--- предыдущие (закрытые) окна: сколько наторговали за окно ---')
for s in sorted(seen):
    if '-5m-' in s or '-15m-' in s:
        st = datetime.strptime(s[-16:], '%Y-%m-%d-%H%Mz').replace(tzinfo=timezone.utc); n = 300 if '-5m-' in s else 900
        for k in (1, 2, 3):
            p = s[:-16] + (st - timedelta(seconds=n * k)).strftime('%Y-%m-%d-%H%Mz')
            b = get(f'{G}/v1/markets/{p}/book'); print(p, (b.get('marketData', {}).get('stats') or {}).get('sharesTraded') if isinstance(b, dict) else b)
print('\n--- источники цены золота/серебра ---')
hl = get('https://api.hyperliquid.xyz/info', {'type': 'allMids'})
print('hyperliquid main PAXG', hl.get('PAXG') if isinstance(hl, dict) else hl)
for dex in ('xyz', 'flx', 'km', 'cash'):
    r = get('https://api.hyperliquid.xyz/info', {'type': 'allMids', 'dex': dex})
    if isinstance(r, dict): print('hyperliquid', dex, {k: v for k, v in r.items() if any(x in k.upper() for x in ('GOLD', 'SILVER', 'XAU', 'XAG'))} or str(r)[:200])
for name, u in [('coinbase PAXG', 'https://api.exchange.coinbase.com/products/PAXG-USD/ticker'),
                ('kraken PAXG/XAUT', 'https://api.kraken.com/0/public/Ticker?pair=PAXGUSD,XAUTUSD'),
                ('gemini PAXG', 'https://api.gemini.com/v1/pubticker/paxgusd'),
                ('binance vision PAXG', 'https://data-api.binance.vision/api/v3/ticker/price?symbol=PAXGUSDT'),
                ('kraken futures XAU/XAG', 'https://futures.kraken.com/derivatives/api/v3/tickers'),
                ('okx XAU', 'https://www.okx.com/api/v5/market/ticker?instId=XAU-USDT-SWAP'),
                ('bybit XAU', 'https://api.bybit.com/v5/market/tickers?category=linear&symbol=XAUUSDT'),
                ('swissquote XAU', 'https://forex-data-feed.swissquote.com/public-quotes/bboquotes/instrument/XAU/USD'),
                ('swissquote XAG', 'https://forex-data-feed.swissquote.com/public-quotes/bboquotes/instrument/XAG/USD')]:
    r = get(u); t = json.dumps(r)
    if 'kraken futures' in name and isinstance(r, dict) and 'tickers' in r:
        t = json.dumps([x for x in r['tickers'] if any(k in x.get('symbol', '').upper() for k in ('XAU', 'XAG', 'GOLD', 'SILVER'))])[:600]
    print(name, t[:500])
