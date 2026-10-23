"""pm/discover: почему стакан нового окна отдаёт 404 первые минуты — опрашиваем book/bbo/market каждые 5 с с начала окна."""
import json, time, urllib.request
from datetime import datetime, timezone
G = 'https://gateway.polymarket.us'
def code(u):
    try:
        r = urllib.request.urlopen(urllib.request.Request(u, headers={'User-Agent': 'pm-discover'}), timeout=10); b = json.loads(r.read())
        md = b.get('marketData', b.get('market', b))
        return 200, {k: md.get(k) for k in ('state', 'status', 'ep3Status', 'active', 'closed', 'bestBid', 'bestAsk') if k in md} | ({'nb': len(md.get('bids') or []), 'no': len(md.get('offers') or [])} if 'bids' in md or 'offers' in md else {})
    except urllib.error.HTTPError as e: return e.code, e.read()[:150]
    except Exception as e: return 'ERR', repr(e)[:100]
t = time.time(); st = int(t) // 300 * 300 + 300
print('ждём начала окна', datetime.fromtimestamp(st, timezone.utc)); time.sleep(max(0, st - time.time() - 20))
s0 = datetime.fromtimestamp(st, timezone.utc).strftime('%Y-%m-%d-%H%Mz')
sl = {'btc5': f'cpc-btc-updown-5m-{s0}', 'eth5': f'cpceth-eth-updown-5m-{s0}'}
if (st // 300) % 3 == 0: sl['btc15'] = f'cpc-btc-updown-15m-{s0}'
while time.time() < st + 240:
    for k, s in sl.items():
        print(f'{time.time() - st:+6.0f}s', k, 'book', *code(f'{G}/v1/markets/{s}/book'), '| bbo', *code(f'{G}/v1/markets/{s}/bbo'), '| mkt', *code(f'{G}/v1/market/slug/{s}'), flush=True)
    time.sleep(5)
