"""pm/lag: paper-трейдер «лага цены» на Polymarket US (рынки Up or Down 5m/15m/1h/1d).

Идея: рынки рассчитываются по CF Benchmarks BRTI (среднее 60 секундных цен в последнюю минуту до начала и до конца окна).
Мы строим свой прокси индекса CF (BRTI, ETHUSD_RTI, SOLUSD_RTI, XRPUSD_RTI) (медиана мидов Coinbase/Kraken/Bitstamp/Gemini/Crypto.com без выбросов), считаем
справедливую вероятность Up с учётом усреднения и волатильности и сравниваем со стаканом Polymarket US.
Входы трёх типов (поле reason):
  lag   — справедливая цена резко сдвинулась за последние секунды, а стакан Polymarket стоит на месте;
  last  — последняя минута окна: часть финального среднего уже известна, модель здесь точнее всего;
  model — просто сильное расхождение с моделью (высокий порог, для сравнения).
Выход: продаём, когда рынок догнал справедливую цену (иначе держим до расчёта). Для каждой сделки
считаем и P&L «если бы держали» — чтобы видеть, помогает ли выход.
Комиссия тейкера θ·p·(1-p) на вход и на выход.
Исполнение — по следующему снимку стакана (имитация задержки ~1 с), объём ограничен тем, что стоит в стакане.
Денег не трогает. Стакан Polymarket: WebSocket wss://api.polymarket.us/v1/ws/markets, если заданы PM_KEY_ID/PM_SECRET
(ключ используется только для чтения данных); иначе — публичный REST (он отстаёт и в начале окна отдаёт 404).

Запуск:  python pm/lag.py --minutes 330 --coins btc,eth,sol,xrp --tfs 5m,15m,1h,1d
Файлы:   pm/paper/<run>/trades.jsonl, snaps.jsonl.gz, lag.jsonl, summary.txt
"""
import argparse, asyncio, gzip, json, math, os, statistics, time, urllib.request
from collections import deque
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import websockets

G = 'https://gateway.polymarket.us'
ET = ZoneInfo('America/New_York')
TF = {'5m': 300, '15m': 900, '1h': 3600, '1d': 86400}
# coinbase, kraken, bitstamp, gemini, crypto.com — все входят в состав BRTI
SYM = {'btc': ('BTC-USD', 'BTC/USD', 'btcusd', 'BTCUSD', 'BTC_USD'), 'eth': ('ETH-USD', 'ETH/USD', 'ethusd', 'ETHUSD', 'ETH_USD'),
       'sol': ('SOL-USD', 'SOL/USD', 'solusd', 'SOLUSD', 'SOL_USD'), 'xrp': ('XRP-USD', 'XRP/USD', 'xrpusd', 'XRPUSD', 'XRP_USD')}

ap = argparse.ArgumentParser()
ap.add_argument('--minutes', type=float, default=350)
ap.add_argument('--coins', default='btc,eth,sol,xrp')
ap.add_argument('--tfs', default='5m,15m,1h,1d')
ap.add_argument('--edge', type=float, default=0.04, help='мин. перевес после комиссии для входа lag')
ap.add_argument('--edge-last', type=float, default=0.03, help='мин. перевес в последней минуте')
ap.add_argument('--edge-model', type=float, default=0.08, help='мин. перевес для входа без лага')
ap.add_argument('--jump', type=float, default=0.03, help='на сколько должна сдвинуться fair за --look с')
ap.add_argument('--look', type=float, default=2, help='окно поиска рывка, с (по WebSocket расхождения живут 0.5–2 с)')
ap.add_argument('--max-spread', type=float, default=0.04, help='шире спред — рынок неликвиден, не торгуем')
ap.add_argument('--min-depth', type=float, default=20.0, help='$ минимум на лучшей цене, иначе не торгуем')
ap.add_argument('--alt5m', action='store_true', help='торговать 5m по ETH/SOL/XRP')
ap.add_argument('--no-exit', action='store_true', help='всегда держать до расчёта')
ap.add_argument('--stake', type=float, default=5.0, help='$ на одну виртуальную сделку')
ap.add_argument('--max-per-market', type=float, default=15.0, help='$ максимум в одном окне')
ap.add_argument('--basis-bp', type=float, default=3.0, help='расхождение нашего индекса с BRTI, б.п.')
ap.add_argument('--min-px', type=float, default=0.10, help='не покупать дешевле/дороже этого')
ap.add_argument('--poll', type=float, default=1.0, help='с между опросами стакана')
ap.add_argument('--latency', type=float, default=0.3, help='имитация задержки ордера, с (при WebSocket)')
ap.add_argument('--out', default='pm/paper')
A = ap.parse_args()
COINS = [c.strip() for c in A.coins.split(',') if c.strip()]
TFS = [t.strip() for t in A.tfs.split(',') if t.strip()]
RUN = os.environ.get('RUN_ID') or datetime.now(timezone.utc).strftime('%Y%m%d-%H%M')
OUT = os.path.join(A.out, RUN); os.makedirs(OUT, exist_ok=True)


def log(*a):
    print(datetime.now(timezone.utc).strftime('%H:%M:%S'), *a, flush=True)


def jw(name, obj):
    with open(os.path.join(OUT, name), 'a') as f: f.write(json.dumps(obj, default=str) + '\n')


def http(u):
    r = urllib.request.urlopen(urllib.request.Request(u, headers={'User-Agent': 'pm-lag-paper'}), timeout=10)
    return json.loads(r.read())


def ncdf(x): return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def fee(theta, p): return theta * p * (1 - p)


# ---------- индекс (прокси BRTI) ----------
class Index:
    """Миды по биржам -> медиана свежих, раз в секунду в историю; EWMA дисперсии лог-доходностей за 1 с."""
    def __init__(s):
        s.q = {}                      # биржа -> (t, mid)
        s.hist = deque(maxlen=7200)   # (сек, цена)
        s.var = None                  # дисперсия 1-с лог-доходности
        s.ret = deque(maxlen=900)

    def put(s, ex, mid):
        if mid and mid > 0: s.q[ex] = (time.time(), mid)

    def now(s):
        """Медиана свежих мидов; биржу, ушедшую от медианы > 0.3%, выкидываем как выброс."""
        t = time.time(); v = [m for (tt, m) in s.q.values() if t - tt < 5]
        if not v: return None
        md = statistics.median(v); w = [x for x in v if abs(x / md - 1) < 0.003]
        return statistics.median(w or v)

    def fresh(s): t = time.time(); return sum(t - tt < 5 for tt, _ in s.q.values())

    def tick(s, sec):
        p = s.now()
        if p is None: return
        if s.hist and s.hist[-1][0] == sec: return
        if s.hist:
            prev_t, prev = s.hist[-1]
            r = math.log(p / prev) / max(1, sec - prev_t) ** 0.5
            s.ret.append(r)
            a = 1 - 0.5 ** (1 / 300)  # полураспад 5 минут
            s.var = r * r if s.var is None else (1 - a) * s.var + a * r * r
        s.hist.append((sec, p))

    def sigma2(s):
        """Дисперсия за 1 с с полом/потолком: не даём модели верить в нулевую волатильность."""
        if s.var is None or len(s.ret) < 120: return None
        rv = statistics.fmean(x * x for x in s.ret)
        v = max(s.var, rv)
        return min(max(v, (0.10 / math.sqrt(365 * 86400)) ** 2), (3.0 / math.sqrt(365 * 86400)) ** 2)

    def avg(s, t0, t1):
        """Среднее секундных цен в (t0, t1] — так BRTI-рынки фиксируют цену."""
        v = [p for (t, p) in s.hist if t0 < t <= t1]
        return (statistics.fmean(v), len(v)) if v else (None, 0)


IDX = {c: Index() for c in COINS}


async def feed_coinbase():
    prod = {SYM[c][0]: c for c in COINS}
    while True:
        try:
            async with websockets.connect('wss://ws-feed.exchange.coinbase.com', ping_interval=20, max_size=2**22) as w:
                await w.send(json.dumps({'type': 'subscribe', 'product_ids': list(prod), 'channels': ['ticker']}))
                async for m in w:
                    d = json.loads(m)
                    if d.get('type') == 'ticker' and d.get('product_id') in prod:
                        b, a = float(d.get('best_bid') or 0), float(d.get('best_ask') or 0)
                        IDX[prod[d['product_id']]].put('coinbase', (a + b) / 2 if a and b else float(d['price']))
        except Exception as e:
            log('coinbase reconnect', repr(e)[:120]); await asyncio.sleep(3)


async def feed_kraken():
    sym = {SYM[c][1]: c for c in COINS}
    while True:
        try:
            async with websockets.connect('wss://ws.kraken.com/v2', ping_interval=20) as w:
                await w.send(json.dumps({'method': 'subscribe', 'params': {'channel': 'ticker', 'symbol': list(sym)}}))
                async for m in w:
                    d = json.loads(m)
                    if d.get('channel') == 'ticker':
                        for x in d.get('data', []):
                            if x.get('symbol') in sym and x.get('bid') and x.get('ask'):
                                IDX[sym[x['symbol']]].put('kraken', (float(x['bid']) + float(x['ask'])) / 2)
        except Exception as e:
            log('kraken reconnect', repr(e)[:120]); await asyncio.sleep(3)


async def feed_bitstamp():
    ch = {f'order_book_{SYM[c][2]}': c for c in COINS}
    while True:
        try:
            async with websockets.connect('wss://ws.bitstamp.net', ping_interval=20, max_size=2**22) as w:
                for k in ch: await w.send(json.dumps({'event': 'bts:subscribe', 'data': {'channel': k}}))
                async for m in w:
                    d = json.loads(m)
                    if d.get('event') == 'data' and d.get('channel') in ch:
                        b, a = d['data'].get('bids'), d['data'].get('asks')
                        if b and a: IDX[ch[d['channel']]].put('bitstamp', (float(b[0][0]) + float(a[0][0])) / 2)
        except Exception as e:
            log('bitstamp reconnect', repr(e)[:120]); await asyncio.sleep(3)


async def feed_gemini(c):
    """Gemini v1, только лучший bid/ask (top_of_book) — одна монета на соединение."""
    bb = {}
    while True:
        try:
            async with websockets.connect(f'wss://api.gemini.com/v1/marketdata/{SYM[c][3]}?top_of_book=true&bids=true&offers=true&trades=false', ping_interval=20) as w:
                async for m in w:
                    for e in json.loads(m).get('events', []):
                        if e.get('type') == 'change' and e.get('side') in ('bid', 'ask'): bb[e['side']] = float(e['price'])
                    if 'bid' in bb and 'ask' in bb: IDX[c].put('gemini', (bb['bid'] + bb['ask']) / 2)
        except Exception as e:
            log('gemini reconnect', repr(e)[:120]); await asyncio.sleep(3)


async def feed_cryptocom():
    ins = {SYM[c][4]: c for c in COINS}
    while True:
        try:
            async with websockets.connect('wss://stream.crypto.com/exchange/v1/market', ping_interval=None) as w:
                await asyncio.sleep(1)
                await w.send(json.dumps({'id': 1, 'method': 'subscribe', 'params': {'channels': [f'ticker.{i}' for i in ins]}}))
                async for m in w:
                    d = json.loads(m)
                    if d.get('method') == 'public/heartbeat':
                        await w.send(json.dumps({'id': d.get('id'), 'method': 'public/respond-heartbeat'})); continue
                    r = d.get('result') or {}
                    if r.get('instrument_name') in ins:
                        for x in r.get('data', []):
                            if x.get('b') and x.get('k'): IDX[ins[r['instrument_name']]].put('cryptocom', (float(x['b']) + float(x['k'])) / 2)
        except Exception as e:
            log('cryptocom reconnect', repr(e)[:120]); await asyncio.sleep(3)


async def ticker():
    while True:
        sec = int(time.time())
        for i in IDX.values(): i.tick(sec)
        await asyncio.sleep(1 - (time.time() % 1) + 0.01)


# ---------- стакан Polymarket US по WebSocket ----------
KEY_ID, SECRET = os.environ.get('PM_KEY_ID', '').strip(), os.environ.get('PM_SECRET', '').strip()
WS_ON = bool(KEY_ID and SECRET)
BOOK = {}      # slug -> (время получения, side_quotes(...))
WSSTAT = {'msgs': 0, 'books': 0, 'errors': 0, 'raw': 0}
WANT = set()   # слаги, на которые нужно быть подписанным


def pm_headers(path):
    import base64
    from cryptography.hazmat.primitives.asymmetric import ed25519
    key = ed25519.Ed25519PrivateKey.from_private_bytes(base64.b64decode(SECRET)[:32])
    ts = str(int(time.time() * 1000))
    sig = base64.b64encode(key.sign(f'{ts}GET{path}'.encode())).decode()
    return {'X-PM-Access-Key': KEY_ID, 'X-PM-Timestamp': ts, 'X-PM-Signature': sig}


def on_pm(d):
    WSSTAT['msgs'] += 1
    if WSSTAT['raw'] < 4 and 'heartbeat' not in d:
        WSSTAT['raw'] += 1; log('ws сообщение:', json.dumps(d)[:400])
    if 'error' in d: WSSTAT['errors'] += 1; log('ws ошибка', json.dumps(d)[:300]); return
    md = d.get('marketData')
    if md and md.get('marketSlug'):
        BOOK[md['marketSlug']] = (time.time(), side_quotes(md)); WSSTAT['books'] += 1


async def feed_pm():
    """Подписка на полный стакан всех нужных рынков; при появлении новых окон — новая подписка."""
    n = 0
    while True:
        try:
            async with websockets.connect('wss://api.polymarket.us/v1/ws/markets', additional_headers=pm_headers('/v1/ws/markets'),
                                          ping_interval=20, max_size=2**23) as w:
                log('ws Polymarket подключён'); subbed = set()

                async def sub_loop():
                    nonlocal n
                    while True:
                        new = sorted(WANT - subbed)
                        for i in range(0, len(new), 90):
                            n += 1; part = new[i:i + 90]
                            await w.send(json.dumps({'subscribe': {'requestId': f'md-{n}', 'subscriptionType': 'SUBSCRIPTION_TYPE_MARKET_DATA', 'marketSlugs': part}}))
                            subbed.update(part)
                        if len(subbed - WANT) > 150: await w.close(); return  # переподключаемся, чтобы сбросить старые подписки
                        await asyncio.sleep(0.5)
                t = asyncio.create_task(sub_loop())
                try:
                    async for m in w: on_pm(json.loads(m))
                finally:
                    t.cancel()
        except Exception as e:
            log('ws Polymarket reconnect', repr(e)[:200])
        BOOK.clear()  # без соединения стаканы могли устареть — ждём свежие после переподключения
        await asyncio.sleep(3)


# ---------- рынки ----------
def window(tf, now):
    """Начало текущего окна в UTC. Дневные окна начинаются в 17:00 ET (21:00Z летом)."""
    if tf != '1d':
        n = TF[tf]; return datetime.fromtimestamp(int(now.timestamp()) // n * n, timezone.utc)
    e = now.astimezone(ET); s = e.replace(hour=17, minute=0, second=0, microsecond=0)
    if e < s: s -= timedelta(days=1)
    return s.astimezone(timezone.utc)


PFX = {'btc': 'cpc', 'eth': 'cpceth', 'sol': 'cpcsol', 'xrp': 'cpcxrp'}  # префиксы слагов Polymarket US


def slug(coin, tf, start): return f'{PFX[coin]}-{coin}-updown-{tf}-{start:%Y-%m-%d-%H%M}z'


class Mkt:
    def __init__(s, coin, tf, start):
        s.coin, s.tf, s.start = coin, tf, start
        s.end = start + timedelta(seconds=TF[tf]) if tf != '1d' else (start.astimezone(ET) + timedelta(days=1)).replace(hour=17).astimezone(timezone.utc)
        s.slug = slug(coin, tf, start)
        s.meta = None; s.theta = 0.07; s.K = None; s.ksrc = None
        s.book = None; s.pending = []; s.pos = []; s.spent = 0.0; s.last_entry = {}
        s.gap = None  # (t, знак) когда fair ушла от mid > 0.05 — для замера лага
        s.tape = deque(maxlen=60)  # (t, fair, mid) — для поиска рывков
        s.exits = []  # заявки на продажу
        s.polls = 0; s.illiquid = 0  # доля времени, когда рынок был неликвиден
        s.done = False; s.dead = False; s.k_own = None

    def t0(s): return s.start.timestamp()

    def t1(s): return s.end.timestamp()


MK = {}
ERRS = {}
STOP = float('inf')


def side_quotes(book):
    """Стакан long-стороны (Up). Down ask = 1 - лучший bid, объём = объём bid."""
    md = book.get('marketData', book)
    bids = sorted(((float(x['px']['value']), float(x['qty'])) for x in md.get('bids') or []), reverse=True)
    offs = sorted((float(x['px']['value']), float(x['qty'])) for x in md.get('offers') or [])
    up = offs[0] if offs else None
    dn = (round(1 - bids[0][0], 4), bids[0][1]) if bids else None
    bb = bids[0][0] if bids else None
    return up, dn, bb, md.get('state'), (bids[0] if bids else None)


DVOL = {}  # монета -> годовая подразумеваемая волатильность Deribit (только BTC, ETH)


async def dvol_loop():
    while True:
        n = int(time.time() * 1000)
        for c in ('btc', 'eth'):
            if c not in COINS: continue
            try:
                d = await asyncio.to_thread(http, f'https://www.deribit.com/api/v2/public/get_volatility_index_data?currency={c.upper()}&resolution=60&start_timestamp={n - 900000}&end_timestamp={n}')
                pts = d['result']['data']
                if pts: DVOL[c] = pts[-1][4] / 100
            except Exception: pass
        await asyncio.sleep(300)


def fair_up(m, S, idx, now):
    """P(финальное среднее BRTI за последнюю минуту >= K).
    Для окон длиннее 30 минут волатильность — среднее между реализованной и подразумеваемой Deribit DVOL:
    реализованная за последние минуты плохо предсказывает час-день вперёд, особенно перед новостями."""
    v = idx.sigma2()
    if v is None or m.K is None or S is None: return None
    tau = m.t1() - now
    if tau >= 1800 and m.coin in DVOL: v = 0.5 * v + 0.5 * DVOL[m.coin] ** 2 / (365 * 86400)
    b2 = (A.basis_bp * 1e-4) ** 2
    if tau >= 60:
        var = v * (tau - 60) + v * 60 * 61 * 121 / 6 / 3600 + b2
        mu = math.log(S / m.K)
        return ncdf(mu / math.sqrt(var))
    t1 = int(m.t1()); got, n = idx.avg(t1 - 60, int(now))
    k = max(0, 60 - n)  # секунд ещё впереди
    if got is None: got, n, k = S, 0, 60
    mean = (got * n + S * k) / 60
    sd = S * math.sqrt(v * k * (k + 1) * (2 * k + 1) / 6 + b2 * 3600) / 60 + 1e-9
    return ncdf((mean - m.K) / sd)


def ensure_markets(now):
    for c in COINS:
        for tf in TFS:
            if tf == '5m' and c != 'btc' and not A.alt5m: continue  # 5-минутки ETH/SOL/XRP неликвидны 70–98% времени, «расхождения» там — шум стакана
            st = window(tf, now); sl = slug(c, tf, st)
            if sl not in MK: MK[sl] = Mkt(c, tf, st)


async def load_meta(m):
    try:
        d = await asyncio.to_thread(http, f'{G}/v1/market/slug/{m.slug}')
        mk = d.get('market', d)
        m.meta = {k: mk.get(k) for k in ('id', 'question', 'outcomes', 'feeCoefficient', 'endDate', 'description')}
        m.meta['sides'] = [(x.get('description'), x.get('long')) for x in mk.get('marketSides', [])]
        m.theta = float(mk.get('feeCoefficient') or 0.07)
        jw('markets.jsonl', {'slug': m.slug, **m.meta})
        log('рынок', m.slug, 'θ', m.theta, m.meta['sides'])
    except Exception as e:
        log('нет рынка', m.slug, repr(e)[:100]); m.dead = True


STRIKES_F = os.path.join(A.out, 'strikes.json')
try:
    STRIKES = json.load(open(STRIKES_F))   # слаг -> K, общий для всех прогонов (иначе дневные окна никогда не торгуются)
except Exception:
    STRIKES = {}


KERR = {}
KOFF = {}  # (монета, начало окна в сек) -> официальная цена старта из Kalshi (floor_strike 15-минуток)


async def kalshi_k_loop():
    """Kalshi публикует floor_strike своих 15-минуток — это то же самое среднее CF RTI за минуту до начала окна,
    по которому считается и Polymarket. Окна Polymarket, начинающиеся в :00/:15/:30/:45 (все 15m и 1h, треть 5m),
    получают точную цену старта вместо нашей оценки."""
    names = {'btc': 'BTC', 'eth': 'ETH', 'sol': 'SOL', 'xrp': 'XRP'}
    while True:
        for c in COINS:
            for q in ('status=open&limit=5', 'status=closed&limit=6'):   # текущее окно + закрытые недавно (для часовых окон)
                try:
                    d = await asyncio.to_thread(http, 'https://api.elections.kalshi.com/trade-api/v2/markets?series_ticker=KX%s15M&%s' % (names[c], q))
                    for mk in d.get('markets', []):
                        if mk.get('floor_strike') and mk.get('open_time'):
                            KOFF[(c, int(datetime.fromisoformat(mk['open_time'].replace('Z', '+00:00')).timestamp()))] = float(mk['floor_strike'])
                except Exception as e:
                    KERR[type(e).__name__] = KERR.get(type(e).__name__, 0) + 1
        await asyncio.sleep(10)


def save_strikes():
    try:
        keep = {(datetime.now(timezone.utc) - timedelta(days=d)).strftime('%Y-%m-%d') for d in range(3)}
        json.dump({sl: v for sl, v in STRIKES.items() if any(f'-{d}-' in sl for d in keep)}, open(STRIKES_F, 'w'))
    except Exception: pass


def set_strike(m, idx, now):
    """K: официальная цена старта (Kalshi) > сохранённая из прошлого прогона > наша оценка по индексу.
    Если ни один источник не знает старта окна — окно пропускаем."""
    if now < m.t0() + 1: return
    off = KOFF.get((m.coin, int(m.t0())))
    if off is not None and m.ksrc != 'kalshi':
        if m.K is not None: m.k_own = m.K       # наша оценка остаётся для замера точности индекса
        m.K, m.ksrc = off, 'kalshi'; STRIKES[m.slug] = off; save_strikes(); return
    if m.K is not None: return
    if m.slug in STRIKES: m.K, m.ksrc = STRIKES[m.slug], 'saved'; return
    k, n = idx.avg(m.t0() - 60, m.t0())
    if n >= 45:
        m.K, m.ksrc = k, f'own{n}'; STRIKES[m.slug] = k; save_strikes()
    elif now - m.t0() < 5:
        return
    else:
        m.ksrc = 'missed'


def jumped(m, f, mid, now, side):
    """Рывок: fair сдвинулась в сторону side на --jump за --look с, а mid Polymarket — меньше чем на 1¢."""
    old = [x for x in m.tape if now - A.look - 1.5 <= x[0] <= now - A.look + 0.5]
    if not old or old[0][2] is None or mid is None: return False
    sg = 1 if side == 'UP' else -1
    return sg * (f - old[0][1]) >= A.jump and sg * (mid - old[0][2]) < 0.01


def decide(m, up, dn, bb, f, mid, now, S):
    tau = m.t1() - now
    if f is None or tau < 5 or m.spent >= A.max_per_market: return
    if bb is None or not up or up[0] - bb > A.max_spread + 1e-9:  # ликвидность: узкий спред обязателен
        m.illiquid += 1; return
    if m.t1() > STOP + 300: return  # окно не закроется до конца прогона — не открываем
    for side, q, p in (('UP', up, f), ('DOWN', dn, 1 - f)):
        if not q: continue
        ask, qty = q
        if ask * qty < A.min_depth: continue  # на лучшей цене слишком мало
        edge = p - ask - fee(m.theta, ask)
        if tau < 60:
            reason, need, band = 'last', A.edge_last, 0.05
        elif jumped(m, f, mid, now, side):
            reason, need, band = 'lag', A.edge, A.min_px
        else:
            reason, need, band = 'model', A.edge_model, A.min_px
        if not (band <= ask <= 1 - band): continue  # хвосты модели ненадёжны
        if edge < need or now - m.last_entry.get(side, 0) < 30: continue
        m.last_entry[side] = now
        m.pending.append({'side': side, 'reason': reason, 'limit': round(ask + 0.01, 2), 'fair': p, 'ask0': ask, 'edge': edge,
                          't': now, 'tau': tau, 'S': S})


def manage_exits(m, f, now, bb, bidq, up):
    """Рынок догнал: можно продать нашу сторону не хуже справедливой цены — фиксируем.
    UP продаём в bid, DOWN продаём по 1 - ask(Up)."""
    if A.no_exit or f is None: return
    for p in m.pos:
        if p.get('exit') or p.get('exiting'): continue
        if p['side'] == 'UP':
            sell, q, fv = bb, (bidq[1] if bidq else 0), f
        else:
            sell, q, fv = (None if not up else round(1 - up[0], 4)), (up[1] if up else 0), 1 - f
        if sell is None: continue
        if q < p['qty']: continue  # в стакане не хватает объёма, чтобы выйти целиком
        net = sell - fee(m.theta, sell)
        if net >= fv - 0.005 and net - p['px'] - p['fee'] / p['qty'] > 0:  # продать не хуже, чем держать по модели, и в плюс с учётом обеих комиссий
            p['exiting'] = True
            m.exits.append({'pos': p, 'limit': round(sell - 0.01, 2), 'fair_exit': fv, 't_sig': now})


def fill_exits(m, bb, up, now):
    if m.exits and now - min(e['t_sig'] for e in m.exits) < (A.latency if WS_ON else 0.5): return
    for e in m.exits:
        p = e['pos']; p.pop('exiting', None)
        sell = bb if p['side'] == 'UP' else (None if not up else round(1 - up[0], 4))
        if sell is not None and sell >= e['limit']:
            fe = fee(m.theta, sell) * p['qty']
            p['exit'] = {'px': sell, 't': now, 'tau': round(m.t1() - now), 'fee': round(fe, 4), 'fair': round(e['fair_exit'], 4)}
            p['pnl_exit'] = round(p['qty'] * (sell - p['px']) - p['fee'] - fe, 4)
            log(m.slug, p['side'], 'выход', p['px'], '->', sell, 'P&L', p['pnl_exit'])
        else:
            jw('exit_missed.jsonl', {'slug': m.slug, 'side': p['side'], 'limit': e['limit'], 'px_next': sell, 't': now})
    m.exits = []


def fill(m, up, dn, now):
    if WS_ON and m.pending:
        bt = BOOK.get(m.slug, (0,))[0]
        if bt < min(o['t'] for o in m.pending) + A.latency and now - min(o['t'] for o in m.pending) < A.latency + 1: return
    for o in m.pending:
        q = up if o['side'] == 'UP' else dn
        rec = {'slug': m.slug, 'coin': m.coin, 'tf': m.tf, **o, 'K': m.K, 'ksrc': m.ksrc, 't_fill': now, 'src': 'ws' if WS_ON else 'rest'}
        if q and q[0] <= o['limit']:
            px = q[0]; n = min(A.stake / px, q[1], (A.max_per_market - m.spent) / px)
            if n * px < 0.5: rec['status'] = 'thin'
            else:
                fe = fee(m.theta, px) * n
                m.spent += n * px; rec.update(status='filled', px=px, qty=round(n, 2), fee=round(fe, 4))
                m.pos.append(rec)
        else:
            rec.update(status='missed', px_next=q[0] if q else None)
        jw('trades.jsonl', rec); log(m.slug, rec['side'], rec['status'], 'ask', o['ask0'], 'fair %.3f edge %.3f' % (o['fair'], o['edge']))
    m.pending = []


def lag_track(m, f, bb, up, now):
    if f is None or not up or bb is None: return
    mid = (bb + up[0]) / 2; d = f - mid
    if m.gap is None and abs(d) > 0.05: m.gap = (now, 1 if d > 0 else -1, d, mid)
    elif m.gap and (abs(d) < 0.02 or (d > 0) != (m.gap[1] > 0)):
        jw('lag.jsonl', {'slug': m.slug, 'tf': m.tf, 'opened': m.gap[0], 'gap0': m.gap[2], 'secs': round(now - m.gap[0], 1), 'tau': m.t1() - now})
        m.gap = None


async def settle(m):
    """Своя оценка исхода сразу, официальная — после публикации settlement."""
    idx = IDX[m.coin]; await asyncio.sleep(max(0, m.t1() - time.time()) + 5)
    F, n = idx.avg(m.t1() - 60, m.t1())
    own = None if F is None or m.K is None else ('UP' if F >= m.K else 'DOWN')
    off = None
    for _ in range(20):
        await asyncio.sleep(30)
        try:
            d = await asyncio.to_thread(http, f'{G}/v1/markets/{m.slug}/settlement')
            v = d.get('settlement', d.get('settlementPx'))
            v = float(v['value'] if isinstance(v, dict) else v) if v is not None else None
            if v is not None: off = 'UP' if v >= 0.5 else 'DOWN'; break
        except Exception: pass
    res = off or own
    for p in m.pos:
        win = (p['side'] == res)
        hold = round((1 if win else 0) * p['qty'] - p['px'] * p['qty'] - p['fee'], 4)
        p.update(F=F, own=own, official=off, result=res, pnl_hold=hold, pnl=p.get('pnl_exit', hold))
        jw('settled.jsonl', p)
    jw('windows.jsonl', {'slug': m.slug, 'coin': m.coin, 'tf': m.tf, 'K': m.K, 'ksrc': m.ksrc, 'K_own': m.k_own,
                         'K_err_bp': None if m.k_own is None or not m.K else round((m.k_own / m.K - 1) * 1e4, 2), 'F': F, 'own': own, 'official': off, 'n_pos': len(m.pos),
                         'polls': m.polls, 'illiquid': m.illiquid})
    log('итог', m.slug, 'K', m.K, 'F', F, 'own', own, 'official', off, 'сделок', len(m.pos))


async def poll(m):
    if WS_ON:
        WANT.add(m.slug)
        b = BOOK.get(m.slug)
        return b[1] if b and time.time() - b[0] < 120 else None  # стакан шлётся при изменениях; тишина = без изменений
    try:
        b = await asyncio.to_thread(http, f'{G}/v1/markets/{m.slug}/book')
        return side_quotes(b)
    except urllib.error.HTTPError as e:
        ERRS[e.code] = ERRS.get(e.code, 0) + 1
        if e.code == 429: await asyncio.sleep(5)
        return None
    except Exception as e:
        k = type(e).__name__; ERRS[k] = ERRS.get(k, 0) + 1
        return None


async def engine(stop_at):
    snap_f = gzip.open(os.path.join(OUT, 'snaps.jsonl.gz'), 'at')
    last_snap = {}; hb = {'t': time.time(), 'ok': 0, 'fail': 0}
    while time.time() < stop_at:
        t_loop = time.time(); now_dt = datetime.now(timezone.utc)
        ensure_markets(now_dt)
        live = [m for m in MK.values() if not m.done and not m.dead]
        await asyncio.gather(*(load_meta(m) for m in live if m.meta is None))
        for m in live: set_strike(m, IDX[m.coin], time.time())
        live = [m for m in live if not m.dead and m.ksrc != 'missed']  # окна без K не опрашиваем — экономим лимит API
        res = await asyncio.gather(*(poll(m) for m in live))
        now = time.time()
        hb['ok'] += sum(r is not None for r in res); hb['fail'] += sum(r is None for r in res)
        if now - hb['t'] >= 60:
            v = {c: (round(i.now() or 0, 2), None if i.sigma2() is None else round((i.sigma2() * 365 * 86400) ** .5, 2), len(i.q)) for c, i in IDX.items()}
            v['dvol'] = dict(DVOL); v['kalshi_K'] = len(KOFF); v['kalshi_err'] = dict(KERR)
            log('пульс: рынков', len(live), 'с K', sum(m.K is not None for m in live), 'стакан ок/ошибок', hb['ok'], hb['fail'], 'индекс (цена, годовая vol, бирж)', v, 'коды', dict(ERRS))
            ERRS.clear()
            if WS_ON: log('ws:', dict(WSSTAT), 'стаканов', len(BOOK)); WSSTAT.update(msgs=0, books=0, errors=0)
            hb.update(t=now, ok=0, fail=0)
        for m, r in zip(live, res):
            idx = IDX[m.coin]; S = idx.now()
            if now >= m.t1():
                m.done = True; m.pending = []; asyncio.create_task(settle(m)); continue
            set_strike(m, idx, now)
            if r is None or m.K is None: continue
            up, dn, bb, state, bidq = r
            if state and 'OPEN' not in str(state): continue
            fill(m, up, dn, now)
            fill_exits(m, bb, up, now)
            f = fair_up(m, S, idx, now)
            mid = (bb + up[0]) / 2 if (bb is not None and up) else None
            lag_track(m, f, bb, up, now)
            m.polls += 1
            decide(m, up, dn, bb, f, mid, now, S)
            manage_exits(m, f, now, bb, bidq, up)
            if f is not None: m.tape.append((now, f, mid))
            if now - last_snap.get(m.slug, 0) >= 10:
                last_snap[m.slug] = now
                snap_f.write(json.dumps({'t': round(now, 1), 'slug': m.slug, 'S': S, 'K': m.K, 'fair': None if f is None else round(f, 4),
                                         'bid': bb, 'ask': up[0] if up else None, 'tau': round(m.t1() - now)}) + '\n')
        for k in [k for k, m in MK.items() if (m.done or m.ksrc == 'missed' or m.dead) and now - m.t1() > 900]:
            del MK[k]; WANT.discard(k); BOOK.pop(k, None)
        await asyncio.sleep(max(0.05, (0.25 if WS_ON else A.poll) - (time.time() - t_loop)))
    snap_f.close()


async def main():
    log('старт', RUN, 'монеты', COINS, 'окна', TFS, 'edge', A.edge, 'минут', A.minutes)
    feeds = [asyncio.create_task(x()) for x in (feed_coinbase, feed_kraken, feed_bitstamp, feed_cryptocom, ticker, dvol_loop, kalshi_k_loop)]
    feeds += [asyncio.create_task(feed_gemini(c)) for c in COINS]
    if WS_ON: feeds.append(asyncio.create_task(feed_pm()))
    log('стакан Polymarket:', 'WebSocket (ключ задан)' if WS_ON else 'публичный REST (ключа нет)', '| длина KEY_ID', len(KEY_ID), 'SECRET', len(SECRET))
    global STOP
    stop_at = STOP = time.time() + A.minutes * 60
    await asyncio.sleep(75)  # набираем минуту истории для K и волатильности
    try:
        await engine(stop_at)
    finally:
        log('ждём расчёт открытых окон')
        for m in MK.values():
            if not m.done and m.pos: m.done = True; asyncio.create_task(settle(m))
        pend = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and t not in feeds]
        if pend: await asyncio.wait(pend, timeout=1200)
        for t in feeds: t.cancel()
        log('стоп')


if __name__ == '__main__':
    asyncio.run(main())
