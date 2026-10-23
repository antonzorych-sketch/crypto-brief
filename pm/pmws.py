"""pm/pmws: общий доступ к Polymarket US — подпись Ed25519, стакан по WebSocket, разбор стакана."""
import asyncio, base64, json, os, time
from datetime import datetime, timezone

import websockets

G = 'https://gateway.polymarket.us'
KEY_ID, SECRET = os.environ.get('PM_KEY_ID', '').strip(), os.environ.get('PM_SECRET', '').strip()
WS_ON = bool(KEY_ID and SECRET)


def log(*a):
    print(datetime.now(timezone.utc).strftime('%H:%M:%S'), *a, flush=True)


def headers(path, method='GET'):
    from cryptography.hazmat.primitives.asymmetric import ed25519
    key = ed25519.Ed25519PrivateKey.from_private_bytes(base64.b64decode(SECRET)[:32])
    ts = str(int(time.time() * 1000))
    sig = base64.b64encode(key.sign(f'{ts}{method}{path}'.encode())).decode()
    return {'X-PM-Access-Key': KEY_ID, 'X-PM-Timestamp': ts, 'X-PM-Signature': sig}


def parse_book(md):
    """-> dict: yes_bid, yes_ask (цена, кол-во) лучших уровней long-стороны; no_ask = 1 - yes_bid; no_bid = 1 - yes_ask."""
    md = md.get('marketData', md)
    bids = sorted(((float(x['px']['value']), float(x['qty'])) for x in md.get('bids') or []), reverse=True)
    offs = sorted((float(x['px']['value']), float(x['qty'])) for x in md.get('offers') or [])
    yb, ya = (bids[0] if bids else None), (offs[0] if offs else None)
    return {'yes_bid': yb, 'yes_ask': ya,
            'no_ask': (round(1 - yb[0], 4), yb[1]) if yb else None,
            'no_bid': (round(1 - ya[0], 4), ya[1]) if ya else None,
            'state': md.get('state')}


class Feed:
    """Подписка на полные стаканы. want — множество слагов; book[slug] = (время, parse_book)."""
    def __init__(s, tag='pm'):
        s.tag = tag; s.want = set(); s.book = {}; s.stat = {'msgs': 0, 'books': 0, 'errors': 0}; s.raw = 0

    def get(s, slug, max_age=120):
        b = s.book.get(slug)
        return b[1] if b and time.time() - b[0] < max_age else None

    async def run(s):
        n = 0
        while True:
            try:
                async with websockets.connect('wss://api.polymarket.us/v1/ws/markets', additional_headers=headers('/v1/ws/markets'),
                                              ping_interval=20, max_size=2**24) as w:
                    log(s.tag, 'ws Polymarket подключён'); subbed = set()

                    async def sub_loop():
                        nonlocal n
                        while True:
                            new = sorted(s.want - subbed)
                            for i in range(0, len(new), 90):
                                n += 1; part = new[i:i + 90]
                                await w.send(json.dumps({'subscribe': {'requestId': f'{s.tag}-{n}', 'subscriptionType': 'SUBSCRIPTION_TYPE_MARKET_DATA', 'marketSlugs': part}}))
                                subbed.update(part)
                            if len(subbed - s.want) > 300: await w.close(); return
                            await asyncio.sleep(0.5)
                    t = asyncio.create_task(sub_loop())
                    try:
                        async for m in w:
                            d = json.loads(m); s.stat['msgs'] += 1
                            if 'error' in d:
                                s.stat['errors'] += 1; log(s.tag, 'ws ошибка', json.dumps(d)[:300]); continue
                            md = d.get('marketData')
                            if md and md.get('marketSlug'):
                                s.book[md['marketSlug']] = (time.time(), parse_book(md)); s.stat['books'] += 1
                            elif s.raw < 3 and 'heartbeat' not in d:
                                s.raw += 1; log(s.tag, 'ws сообщение:', json.dumps(d)[:300])
                    finally:
                        t.cancel()
            except Exception as e:
                log(s.tag, 'ws Polymarket reconnect', repr(e)[:200]); await asyncio.sleep(3)
