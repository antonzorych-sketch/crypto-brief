"""pm/discover: ищем рынки Up or Down по всем монетам на Polymarket US — слаги, окна, источник расчёта."""
import json, urllib.request, urllib.parse
G = 'https://gateway.polymarket.us'
def get(u):
    try:
        r = urllib.request.urlopen(urllib.request.Request(u, headers={'User-Agent': 'pm-discover'}), timeout=20)
        return json.loads(r.read())
    except Exception as e: return {'_error': repr(e)[:300]}
seen = {}
for q in ['bitcoin up or down', 'BTC Up or Down', 'ETH Up or Down', 'ethereum up or down', 'SOL Up or Down', 'solana up or down',
          'XRP Up or Down', 'xrp up or down', 'up or down 5m', 'up or down 15m', 'up or down hourly', 'up or down daily']:
    r = get(f'{G}/v1/search?' + urllib.parse.urlencode({'query': q}))
    if '_error' in r: print(q, r['_error']); continue
    for e in r.get('events', []):
        for m in e.get('markets', []):
            if 'up' in m['slug'] and 'down' in m['slug'] or 'updown' in m['slug']:
                seen[m['slug']] = (e.get('seriesSlug'), e.get('startDate'), e.get('endDate'), (m.get('description') or '')[:400])
for s, (ser, a, b, d) in sorted(seen.items()):
    print(s, '|', ser, '|', a, '->', b)
print('\nисточники расчёта:')
by = {}
for s, v in seen.items(): by.setdefault(v[0], v[3])
for k, v in by.items(): print(k, '::', v, '\n')
for ser in sorted({v[0] for v in seen.values() if v[0]}):
    r = get(f'{G}/v1/events?' + urllib.parse.urlencode({'seriesSlug': ser, 'limit': 3, 'active': 'true', 'closed': 'false'}))
    print('серия', ser, [ (e['slug'], [m['slug'] for m in e.get('markets', [])]) for e in r.get('events', [])][:3] if isinstance(r, dict) else r)
