"""pm/kalshi: общий код для paper-ботов на Kalshi — публичные данные, стакан, комиссия, журнал сделок.
Ключ не нужен: рыночные данные Kalshi публичные. Сделки виртуальные."""
import asyncio, json, math, os, time, urllib.parse, urllib.request
from datetime import datetime, timezone

K = 'https://api.elections.kalshi.com/trade-api/v2'
STAT = {'req': 0, 'err': 0}


def log(*a):
    print(datetime.now(timezone.utc).strftime('%H:%M:%S'), *a, flush=True)


def http(u, headers=None):
    r = urllib.request.urlopen(urllib.request.Request(u, headers=headers or {'User-Agent': 'pm-paper (antonzorych@gmail.com)', 'Accept': 'application/json'}), timeout=15)
    return json.loads(r.read())


async def aget(u, headers=None):
    STAT['req'] += 1
    try:
        return await asyncio.to_thread(http, u, headers)
    except Exception as e:
        STAT['err'] += 1
        return {'_error': repr(e)[:150]}


async def markets(**q):
    """Все рынки по фильтру (series_ticker / event_ticker / status), с пагинацией."""
    out, cur = [], None
    for _ in range(10):
        p = {'limit': 1000, **q}
        if cur: p['cursor'] = cur
        r = await aget(f'{K}/markets?' + urllib.parse.urlencode(p))
        if '_error' in r: break
        out += r.get('markets', []); cur = r.get('cursor')
        if not cur: break
    return out


def top(m):
    """Лучшие цены из списка рынков (без объёмов): yes_bid, yes_ask, no_bid, no_ask в долларах."""
    yb, ya = float(m.get('yes_bid_dollars') or 0), float(m.get('yes_ask_dollars') or 0)
    return {'yes_bid': yb or None, 'yes_ask': ya if 0 < ya < 1 else None,
            'no_ask': round(1 - yb, 4) if yb else None, 'no_bid': round(1 - ya, 4) if 0 < ya < 1 else None}


async def book(ticker):
    """Полный стакан: (цена, кол-во) лучших уровней для покупки YES и NO."""
    ob = await aget(f'{K}/markets/{ticker}/orderbook')
    if '_error' in ob: return None
    o = ob.get('orderbook_fp') or {}
    lv = lambda key: max(((float(p), float(q)) for p, q in (o.get(key) or [])), default=None)
    yb, nb = lv('yes_dollars'), lv('no_dollars')
    return {'yes_bid': yb, 'no_bid': nb, 'yes_ask': (round(1 - nb[0], 4), nb[1]) if nb else None, 'no_ask': (round(1 - yb[0], 4), yb[1]) if yb else None}


def fee(p, coef=0.07): return coef * p * (1 - p)
def fee_order(p, c, coef=0.07): return math.ceil(round(coef * c * p * (1 - p) * 100, 6)) / 100


def wins(m, x):
    """Выигрывает ли YES при значении x по описанию страйка рынка Kalshi."""
    st, lo, hi = m.get('strike_type'), m.get('floor_strike'), m.get('cap_strike')
    if st == 'greater': return x > lo
    if st == 'greater_or_equal': return x >= lo
    if st == 'less': return x < hi
    if st == 'less_or_equal': return x <= hi
    if st == 'between': return lo <= x <= hi
    return None


class Journal:
    """Файлы прогона: <out>/<run>/<prefix>_trades.jsonl. Расчёт по официальным результатам — в ksettle.py."""
    def __init__(s, prefix, out='pm/paper'):
        run = os.environ.get('RUN_ID') or datetime.now(timezone.utc).strftime('%Y%m%d-%H%M')
        s.dir = os.path.join(out, run); os.makedirs(s.dir, exist_ok=True); s.prefix = prefix

    def w(s, name, obj):
        with open(os.path.join(s.dir, f'{s.prefix}_{name}.jsonl'), 'a') as f: f.write(json.dumps(obj, default=str) + '\n')


async def paper_buy(j, m, side, fair, edge_min, stake, meta, coef=0.07, latency=1.0):
    """Сигнал -> через latency с берём свежий полный стакан и «покупаем», если перевес сохранился."""
    await asyncio.sleep(latency)
    b = await book(m['ticker'])
    q = b and (b['yes_ask'] if side == 'YES' else b['no_ask'])
    p = fair if side == 'YES' else 1 - fair
    rec = {'t': time.time(), 'ticker': m['ticker'], 'event': m.get('event_ticker'), 'side': side, 'fair': round(fair, 4), 'close': m.get('close_time'),
           'strike_type': m.get('strike_type'), 'floor': m.get('floor_strike'), 'cap': m.get('cap_strike'), **meta}
    if not q:
        rec['status'] = 'no_book'
    else:
        px, avail = q; n = min(stake / px, avail)
        e = p - px - fee(px, coef)
        if e < edge_min * 0.5 or n * px < 1:
            rec.update(status='gone', px=px, edge_now=round(e, 4))
        else:
            rec.update(status='filled', px=px, qty=round(n, 2), fee=fee_order(px, n, coef), edge=round(e, 4))
    j.w('trades', rec)
    log(j.prefix, m['ticker'], side, rec['status'], 'цена', rec.get('px'), 'fair %.3f' % p)
    return rec
