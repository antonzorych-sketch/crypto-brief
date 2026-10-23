"""pm/arb: paper-сканер арбитража без риска направления.

Три вида связок, каждая гарантированно платит фиксированную сумму при любом исходе:
  xv15  — Polymarket US 15m Up/Down против Kalshi KX*15M. Контракты одинаковые: индекс CF RTI, среднее 60 секунд
          до начала и до конца окна, ничья = Up/Yes. Up на одной площадке + Down на другой = ровно $1.
  xvhr  — Polymarket «цена ≥ X в HH:00» против Kalshi KX*D с тем же страйком и временем: Yes + No = $1.
  mono  — внутри Polymarket: Yes(≥X) + No(≥Y) при X < Y платит ≥ $1 всегда.
  box   — внутри Polymarket: Yes(корзина [X, X+w)) + Yes(≥X+w) + No(≥X) = ровно $1; и зеркальная тройка = $2.
Если связка стоит дешевле выплаты после комиссий обеих площадок — это арбитраж. Сделки виртуальные:
исполнение по следующему снимку обоих стаканов, затем проверяем официальные расчёты обеих площадок
(главный риск xv — что площадки рассчитаются по-разному; это и проверяем).

Запуск: python pm/arb.py --minutes 330   (нужны PM_KEY_ID/PM_SECRET для стаканов Polymarket)
"""
import argparse, asyncio, json, math, os, re, time, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

import pmws
from pmws import G, log

K = 'https://api.elections.kalshi.com/trade-api/v2'
PFX = {'btc': 'cpc', 'eth': 'cpceth', 'sol': 'cpcsol', 'xrp': 'cpcxrp'}
CB = {'btc': 'BTC-USD', 'eth': 'ETH-USD', 'sol': 'SOL-USD', 'xrp': 'XRP-USD'}
NAME = {'btc': 'BTC', 'eth': 'ETH', 'sol': 'SOL', 'xrp': 'XRP'}

ap = argparse.ArgumentParser()
ap.add_argument('--minutes', type=float, default=330)
ap.add_argument('--coins', default='btc,eth,sol,xrp')
ap.add_argument('--min-edge', type=float, default=0.01, help='$ гарантированной прибыли на контракт после комиссий')
ap.add_argument('--min-usd', type=float, default=5.0, help='минимум $ в связке (ликвидность)')
ap.add_argument('--max-usd', type=float, default=50.0, help='максимум $ в одной связке')
ap.add_argument('--band', type=float, default=0.02, help='страйки в пределах ±band от цены')
ap.add_argument('--k-fee', type=float, default=0.07, help='коэффициент комиссии Kalshi')
ap.add_argument('--maker-rebate', type=float, default=0.0125, help='скидка мейкера Polymarket US, коэффициент при p(1-p)')
ap.add_argument('--out', default='pm/paper')
A = ap.parse_args()
COINS = [c for c in A.coins.split(',') if c]
RUN = os.environ.get('RUN_ID') or datetime.now(timezone.utc).strftime('%Y%m%d-%H%M')
OUT = os.path.join(A.out, RUN); os.makedirs(OUT, exist_ok=True)
FEED = pmws.Feed('arb')
STAT = {'eval': {}, 'best': {}, 'sig': 0, 'fill': 0, 'kreq': 0, 'kerr': 0}


def jw(name, obj):
    with open(os.path.join(OUT, name), 'a') as f: f.write(json.dumps(obj, default=str) + '\n')


def http(u):
    r = urllib.request.urlopen(urllib.request.Request(u, headers={'User-Agent': 'pm-arb-paper', 'Accept': 'application/json'}), timeout=10)
    return json.loads(r.read())


async def aget(u):
    try:
        return await asyncio.to_thread(http, u)
    except Exception as e:
        return {'_error': repr(e)[:150]}


def pm_fee(theta, p): return theta * p * (1 - p)
def k_fee(p): return A.k_fee * p * (1 - p)
def k_fee_order(p, c): return math.ceil(round(A.k_fee * c * p * (1 - p) * 100, 6)) / 100


# ---------- цены спота (для выбора страйков) ----------
SPOT = {}


async def spot_loop():
    while True:
        for c in COINS:
            d = await aget(f'https://api.exchange.coinbase.com/products/{CB[c]}/ticker')
            if 'price' in d: SPOT[c] = float(d['price'])
        await asyncio.sleep(15)


# ---------- лестницы Polymarket ----------
LAD = {}   # (coin, kind 'above'|'range', end_iso) -> [{'slug','lo','hi','theta'}]


def strike_bounds(title):
    t = (title or '').replace(',', ''); n = [float(x) for x in re.findall(r'(\d+(?:\.\d+)?)', t)]
    if not n: return None
    if 'or above' in t: return n[0], math.inf
    if 'or below' in t: return -math.inf, round(n[0] + 0.01, 6) if n[0] != int(n[0]) else n[0]
    if ' to ' in t and len(n) >= 2: return n[0], round(n[1] + 0.01, 6) if n[1] != int(n[1]) else n[1]
    return None


async def ladder_loop():
    while True:
        found = {}
        for c in COINS:
            for q in (f'{NAME[c]} Price at', f'{NAME[c]} Price Range at'):
                r = await aget(f'{G}/v1/search?' + urllib.parse.urlencode({'query': q}))
                for e in r.get('events', []) if isinstance(r, dict) else []:
                    sl = e.get('slug', '')
                    m = re.search(r'-(above|range)-(hr|wk)-', sl)
                    if not m or e.get('closed') or not sl.startswith(f'{c}-'): continue
                    end = e.get('endDate')
                    try: endt = datetime.fromisoformat(end.replace('Z', '+00:00'))
                    except Exception: continue
                    if not (datetime.now(timezone.utc) < endt < datetime.now(timezone.utc) + timedelta(hours=26)): continue
                    rows = []
                    for mk in e.get('markets', []):
                        b = strike_bounds(mk.get('title') or mk.get('question'))
                        if b: rows.append({'slug': mk['slug'], 'lo': b[0], 'hi': b[1], 'theta': float(mk.get('feeCoefficient') or 0.0695)})
                    if rows: found[(c, m.group(1), endt.isoformat())] = sorted(rows, key=lambda x: (x['lo'], x['hi']))
        if found:
            LAD.clear(); LAD.update(found)
        await asyncio.sleep(120)


def near(c, row):
    s = SPOT.get(c)
    if not s: return False
    lo = row['lo'] if row['lo'] != -math.inf else row['hi']
    return abs(lo / s - 1) <= A.band


# ---------- Kalshi ----------
K15 = {}   # coin -> {'ticker','close','book':{yes_bid,yes_ask,no_bid,no_ask},'t'}
KHR = {}   # (coin, close_iso) -> {strike: {'ticker','yes_bid','yes_ask'}}
KRULES = set()


def k_book(ob):
    o = ob.get('orderbook_fp') or ob.get('orderbook') or {}
    def lv(key):
        xs = o.get(key) or []
        return max(((float(p), float(q)) for p, q in xs), default=None)
    yb, nb = lv('yes_dollars'), lv('no_dollars')
    return {'yes_bid': yb, 'no_bid': nb,
            'yes_ask': (round(1 - nb[0], 4), nb[1]) if nb else None,
            'no_ask': (round(1 - yb[0], 4), yb[1]) if yb else None}


async def k15_loop():
    while True:
        for c in COINS:
            ser = f'KX{NAME[c]}15M'
            cur = K15.get(c)
            if not cur or time.time() > cur['close'] - 1:
                r = await aget(f'{K}/markets?' + urllib.parse.urlencode({'series_ticker': ser, 'status': 'open', 'limit': 5})); STAT['kreq'] += 1
                ms = r.get('markets', []) if isinstance(r, dict) else []
                if not ms: K15.pop(c, None); continue
                m = min(ms, key=lambda x: x['close_time'])
                close = datetime.fromisoformat(m['close_time'].replace('Z', '+00:00')).timestamp()
                K15[c] = cur = {'ticker': m['ticker'], 'close': close, 'strike': m.get('floor_strike'), 'book': None, 't': 0}
                if ser not in KRULES: KRULES.add(ser); jw('arb_rules.jsonl', {'series': ser, 'rules': m.get('rules_primary')})
            ob = await aget(f'{K}/markets/{cur["ticker"]}/orderbook'); STAT['kreq'] += 1
            if '_error' in ob: STAT['kerr'] += 1; continue
            cur['book'] = k_book(ob); cur['t'] = time.time()
        await asyncio.sleep(1.0)


async def khr_loop():
    while True:
        for c in COINS:
            ser = f'KX{NAME[c]}D'
            r = await aget(f'{K}/events?' + urllib.parse.urlencode({'series_ticker': ser, 'status': 'open', 'limit': 5})); STAT['kreq'] += 1
            evs = r.get('events', []) if isinstance(r, dict) else []
            for ev in sorted(evs, key=lambda e: e.get('strike_date') or '')[:2]:
                mr = await aget(f'{K}/markets?' + urllib.parse.urlencode({'event_ticker': ev['event_ticker'], 'limit': 1000})); STAT['kreq'] += 1
                ms = mr.get('markets', []) if isinstance(mr, dict) else []
                if not ms: continue
                if ser not in KRULES: KRULES.add(ser); jw('arb_rules.jsonl', {'series': ser, 'rules': ms[0].get('rules_primary')})
                close = datetime.fromisoformat(ms[0]['close_time'].replace('Z', '+00:00')).isoformat()
                d = {}
                for m in ms:
                    st, fs = m.get('strike_type'), m.get('floor_strike')
                    if fs is None or st not in ('greater', 'greater_or_equal'): continue
                    strike = round(fs + 0.01, 2) if st == 'greater' else fs  # «> 90799.99» на центовой сетке = «≥ 90800»
                    yb, ya = float(m.get('yes_bid_dollars') or 0), float(m.get('yes_ask_dollars') or 0)
                    d[strike] = {'ticker': m['ticker'], 'yes_bid': yb, 'yes_ask': ya}
                KHR[(c, close)] = d
        await asyncio.sleep(2.0)


# ---------- оценка связок ----------
OPEN = {}      # key -> сигнал, ждёт подтверждения следующим снимком
LAST = {}      # key -> время последнего сигнала (кулдаун)
POS = []       # исполненные связки


def note(kind, edge):
    STAT['eval'][kind] = STAT['eval'].get(kind, 0) + 1
    if edge > STAT['best'].get(kind, -9): STAT['best'][kind] = round(edge, 4)


def combo(kind, key, legs, payoff, close_ts, meta):
    """legs: [(venue, ref, side, price, qty, fee_per_contract)]. Проверяем перевес и ликвидность, ставим сигнал."""
    if any(l[3] is None or l[4] is None or l[4] <= 0 or not (0 < l[3] < 1) for l in legs): return
    cost = sum(l[3] for l in legs) + sum(l[5] for l in legs)
    edge = payoff - cost; note(kind, edge)
    qty = min(l[4] for l in legs); qty = min(qty, A.max_usd / max(0.01, sum(l[3] for l in legs)))
    if edge < A.min_edge or qty * sum(l[3] for l in legs) < A.min_usd: return
    if time.time() - LAST.get(key, 0) < 30 or key in OPEN or time.time() > close_ts - 10: return
    LAST[key] = time.time(); STAT['sig'] += 1
    sig = {'kind': kind, 'key': key, 't': time.time(), 'edge': round(edge, 4), 'cost': round(cost, 4), 'payoff': payoff, 'qty': round(qty, 2),
           'legs': [list(l) for l in legs], 'close': close_ts, **meta}
    OPEN[key] = sig; jw('arb_signals.jsonl', sig)
    log('СИГНАЛ', kind, key, 'перевес %.3f, %d конт.' % (edge, qty))


def evaluate():
    now = time.time()
    # xv15: Polymarket 15m vs Kalshi 15m
    for c in COINS:
        kc = K15.get(c)
        if not kc or not kc['book'] or now - kc['t'] > 5: continue
        start = datetime.fromtimestamp(kc['close'] - 900, timezone.utc)
        slug = f'{PFX[c]}-{c}-updown-15m-{start:%Y-%m-%d-%H%M}z'; FEED.want.add(slug)
        pb = FEED.get(slug, 60)
        if not pb: continue
        kb = kc['book']; th = 0.0695
        legs_sets = []
        if pb['yes_ask'] and kb['no_ask']: legs_sets.append([('pm', slug, 'YES', pb['yes_ask'][0], pb['yes_ask'][1], pm_fee(th, pb['yes_ask'][0])),
                                                             ('k', kc['ticker'], 'NO', kb['no_ask'][0], kb['no_ask'][1], k_fee(kb['no_ask'][0]))])
        if pb['no_ask'] and kb['yes_ask']: legs_sets.append([('pm', slug, 'NO', pb['no_ask'][0], pb['no_ask'][1], pm_fee(th, pb['no_ask'][0])),
                                                             ('k', kc['ticker'], 'YES', kb['yes_ask'][0], kb['yes_ask'][1], k_fee(kb['yes_ask'][0]))])
        for legs in legs_sets:
            combo('xv15', f'xv15|{slug}|{legs[0][2]}', legs, 1.0, kc['close'], {'coin': c, 'pm_slug': slug, 'k_ticker': kc['ticker']})
        maker_xv(c, slug, pb, kc, now)
    # лестницы
    for (c, kind, end), rows in list(LAD.items()):
        close = datetime.fromisoformat(end).timestamp()
        if close < now: continue
        live = [r for r in rows if near(c, r)]
        for r in live: FEED.want.add(r['slug'])
        if kind == 'above':
            ab = [(r, FEED.get(r['slug'], 300)) for r in live if r['hi'] == math.inf]
            ab = [(r, b) for r, b in ab if b]
            # mono: Yes(≥X) + No(≥Y), X < Y — платит ≥ $1
            for i, (r1, b1) in enumerate(ab):
                for r2, b2 in ab[i + 1:]:
                    if b1['yes_ask'] and b2['no_ask']:
                        combo('mono', f"mono|{r1['slug']}|{r2['slug']}", [('pm', r1['slug'], 'YES', *b1['yes_ask'], pm_fee(r1['theta'], b1['yes_ask'][0])),
                                                                          ('pm', r2['slug'], 'NO', *b2['no_ask'], pm_fee(r2['theta'], b2['no_ask'][0]))], 1.0, close, {'coin': c})
            # xvhr: тот же страйк и время на Kalshi
            kd = KHR.get((c, datetime.fromisoformat(end).isoformat()))
            if kd:
                for r, b in ab:
                    k = kd.get(round(r['lo'], 2))
                    if not k: continue
                    kya, kna = k['yes_ask'], (round(1 - k['yes_bid'], 4) if k['yes_bid'] else None)
                    # размер Kalshi неизвестен из списка рынков — берём 50 конт. как оценку; подтверждаем стаканом при исполнении
                    if b['yes_ask'] and kna and 0 < kna < 1:
                        combo('xvhr', f"xvhr|{r['slug']}|YES", [('pm', r['slug'], 'YES', *b['yes_ask'], pm_fee(r['theta'], b['yes_ask'][0])),
                                                                 ('k', k['ticker'], 'NO', kna, 50, k_fee(kna))], 1.0, close, {'coin': c, 'k_ticker': k['ticker']})
                    if b['no_ask'] and kya and 0 < kya < 1:
                        combo('xvhr', f"xvhr|{r['slug']}|NO", [('pm', r['slug'], 'NO', *b['no_ask'], pm_fee(r['theta'], b['no_ask'][0])),
                                                                ('k', k['ticker'], 'YES', kya, 50, k_fee(kya))], 1.0, close, {'coin': c, 'k_ticker': k['ticker']})
        else:
            # box: Yes[X,X+w) + Yes(≥X+w) + No(≥X) = $1;  No[X,X+w) + No(≥X+w) + Yes(≥X) = $2
            above = {round(r['lo'], 6): r for r in LAD.get((c, 'above', end), []) if r['hi'] == math.inf}
            for r in live:
                if r['lo'] == -math.inf or r['hi'] == math.inf: continue
                a1, a2 = above.get(round(r['lo'], 6)), above.get(round(r['hi'], 6))
                if not a1 or not a2: continue
                FEED.want.update((a1['slug'], a2['slug']))
                br, b1, b2 = FEED.get(r['slug'], 300), FEED.get(a1['slug'], 300), FEED.get(a2['slug'], 300)
                if not (br and b1 and b2): continue
                if br['yes_ask'] and b2['yes_ask'] and b1['no_ask']:
                    combo('box', f"box1|{r['slug']}", [('pm', r['slug'], 'YES', *br['yes_ask'], pm_fee(r['theta'], br['yes_ask'][0])),
                                                       ('pm', a2['slug'], 'YES', *b2['yes_ask'], pm_fee(a2['theta'], b2['yes_ask'][0])),
                                                       ('pm', a1['slug'], 'NO', *b1['no_ask'], pm_fee(a1['theta'], b1['no_ask'][0]))], 1.0, close, {'coin': c})
                if br['no_ask'] and b2['no_ask'] and b1['yes_ask']:
                    combo('box', f"box2|{r['slug']}", [('pm', r['slug'], 'NO', *br['no_ask'], pm_fee(r['theta'], br['no_ask'][0])),
                                                       ('pm', a2['slug'], 'NO', *b2['no_ask'], pm_fee(a2['theta'], b2['no_ask'][0])),
                                                       ('pm', a1['slug'], 'YES', *b1['yes_ask'], pm_fee(a1['theta'], b1['yes_ask'][0]))], 2.0, close, {'coin': c})


# ---------- мейкерский вариант связки с Kalshi ----------
# Две тейкерские ноги платят ~3.5¢ комиссий при цене около 50¢ — больше типичного расхождения площадок (1–3¢).
# Поэтому оцениваем вариант, который используют на практике: на Polymarket стоит наша лимитная заявка
# (комиссии нет, есть скидка мейкера), а Kalshi берём по рынку только когда заявку исполнили.
# Модель исполнения консервативная: заявка считается исполненной, если лучший ask Polymarket опустился до нашей цены.
RESTING = {}   # (slug, side) -> {'px', 't', 'k_ticker', 'close'}


def maker_xv(c, slug, pb, kc, now):
    kb = kc['book']
    for side, pm_bid, k_hedge in (('YES', pb['yes_bid'], kb['no_ask']), ('NO', pb['no_bid'], kb['yes_ask'])):
        key = (slug, side); rest = RESTING.get(key)
        pm_ask = pb['yes_ask'] if side == 'YES' else pb['no_ask']
        if rest:
            if pm_ask and pm_ask[0] <= rest['px'] + 1e-9 and k_hedge:   # нашу заявку «съели» — хеджируемся на Kalshi по рынку
                del RESTING[key]
                kp = k_hedge[0]; edge = 1 - rest['px'] + A.maker_rebate * rest['px'] * (1 - rest['px']) - kp - k_fee(kp)
                rec = {'kind': 'xv15m', 'key': f'xv15m|{slug}|{side}', 't': now, 'coin': c, 'pm_slug': slug, 'k_ticker': kc['ticker'], 'pm_side': side,
                       'pm_px': rest['px'], 'k_px': kp, 'edge_planned': rest['edge'], 'edge_real': round(edge, 4), 'wait_s': round(now - rest['t'], 1), 'close': kc['close']}
                note('xv15m', edge); jw('arb_maker.jsonl', rec)
                q = 10.0; kside = 'NO' if side == 'YES' else 'YES'
                sett = {**rec, 'legs_fill': [['pm', slug, side, rest['px'], q, 0], ['k', kc['ticker'], kside, kp, q, 0]], 'qty_fill': q, 'payoff': 1.0,
                        'cost': round((rest['px'] + kp) * q, 4), 'fees': round(k_fee_order(kp, q) - A.maker_rebate * rest['px'] * (1 - rest['px']) * q, 4)}
                asyncio.create_task(settle(sett))
                log('МЕЙКЕР исполнен', slug, side, 'PM', rest['px'], 'Kalshi', kp, 'перевес факт %.3f' % edge)
            elif now - rest['t'] > 60 or now > kc['close'] - 30:
                del RESTING[key]
            continue
        if not pm_bid or not k_hedge or now > kc['close'] - 60: continue
        px = round(pm_bid[0] + 0.01, 2)                    # встаём на шаг лучше лучшего bid
        if pm_ask and px >= pm_ask[0]: continue             # иначе это уже не мейкер
        edge = 1 - px + A.maker_rebate * px * (1 - px) - k_hedge[0] - k_fee(k_hedge[0])
        note('xv15m_quote', edge)
        if edge >= A.min_edge: RESTING[key] = {'px': px, 't': now, 'edge': round(edge, 4)}


async def confirm_loop():
    """Сигнал -> через 0.6 с перепроверяем по свежим стаканам обеих площадок (Kalshi — полный стакан) и «исполняем»."""
    while True:
        await asyncio.sleep(0.2)
        for key, sig in list(OPEN.items()):
            if time.time() - sig['t'] < 0.6: continue
            del OPEN[key]
            legs, qty = [], sig['qty']
            for venue, ref, side, *_ in sig['legs']:
                if venue == 'pm':
                    b = FEED.get(ref, 300); q = b and (b['yes_ask'] if side == 'YES' else b['no_ask'])
                    th = next((r['theta'] for rows in LAD.values() for r in rows if r['slug'] == ref), 0.0695)
                    if not q: legs = None; break
                    legs.append((venue, ref, side, q[0], q[1], pm_fee(th, q[0])))
                else:
                    ob = await aget(f'{K}/markets/{ref}/orderbook'); STAT['kreq'] += 1
                    if '_error' in ob: legs = None; break
                    kb = k_book(ob); q = kb['yes_ask'] if side == 'YES' else kb['no_ask']
                    if not q: legs = None; break
                    legs.append((venue, ref, side, q[0], q[1], k_fee(q[0])))
            rec = {**sig, 't_fill': time.time()}
            if not legs:
                rec['status'] = 'gone'
            else:
                q2 = min(qty, *(l[4] for l in legs))
                cost = sum(l[3] for l in legs)
                fees = sum(pm_fee(next((r['theta'] for rows in LAD.values() for r in rows if r['slug'] == l[1]), 0.0695), l[3]) * q2 if l[0] == 'pm' else k_fee_order(l[3], q2) for l in legs)
                edge_total = sig['payoff'] * q2 - cost * q2 - fees
                if q2 * cost < A.min_usd or edge_total <= 0:
                    rec.update(status='gone', legs_now=[list(l) for l in legs], edge_now=round(edge_total / max(q2, 1e-9), 4))
                else:
                    rec.update(status='filled', legs_fill=[list(l) for l in legs], qty_fill=round(q2, 2), cost=round(cost * q2, 4), fees=round(fees, 4),
                               locked_pnl=round(edge_total, 4))
                    POS.append(rec); STAT['fill'] += 1
                    asyncio.create_task(settle(rec))
                    log('ИСПОЛНЕНО', sig['kind'], key, 'заблокировано $%.2f на %.0f конт.' % (edge_total, q2))
            jw('arb_trades.jsonl', rec)


async def settle(rec):
    """Ждём официальные расчёты всех ног и считаем фактический результат."""
    await asyncio.sleep(max(0, rec['close'] - time.time()) + 60)
    res = {}
    for _ in range(30):
        for venue, ref, side, *_ in rec['legs_fill']:
            if ref in res: continue
            if venue == 'pm':
                d = await aget(f'{G}/v1/markets/{ref}/settlement'); v = d.get('settlement', d.get('settlementPx')) if isinstance(d, dict) else None
                v = v['value'] if isinstance(v, dict) else v
                if v is not None and '_error' not in d: res[ref] = 'YES' if float(v) >= 0.5 else 'NO'
            else:
                d = await aget(f'{K}/markets/{ref}'); m = d.get('market', {}) if isinstance(d, dict) else {}
                if m.get('result') in ('yes', 'no'): res[ref] = m['result'].upper()
        if len(res) == len(rec['legs_fill']): break
        await asyncio.sleep(30)
    q = rec['qty_fill']
    pay = sum(q for venue, ref, side, *_ in rec['legs_fill'] if res.get(ref) == side)
    out = {**rec, 'results': res, 'payout': round(pay, 4), 'pnl': round(pay - rec['cost'] - rec['fees'], 4) if len(res) == len(rec['legs_fill']) else None}
    out['as_expected'] = out['pnl'] is not None and abs(pay - rec['payoff'] * q) < 1e-6 if rec['kind'] != 'mono' else (pay >= rec['payoff'] * q - 1e-6)
    jw('arb_settled.jsonl', out)
    log('РАСЧЁТ', rec['kind'], rec['key'], 'P&L', out['pnl'], 'как ожидали' if out['as_expected'] else '!!! РАСХОЖДЕНИЕ ПЛОЩАДОК', res)


async def main():
    log('арбитраж-сканер', RUN, 'монеты', COINS, 'мин. перевес', A.min_edge,
        'стакан PM:', 'WebSocket' if pmws.WS_ON else 'нет ключа — публичный REST, только связки с Kalshi 15m (лестницы пропускаем)')
    pm_task = FEED.run() if pmws.WS_ON else FEED.run_rest(only=lambda x: '-updown-15m-' in x)
    tasks = [asyncio.create_task(x) for x in (pm_task, spot_loop(), k15_loop(), khr_loop(), confirm_loop())]
    if pmws.WS_ON: tasks.append(asyncio.create_task(ladder_loop()))
    stop = time.time() + A.minutes * 60; hb = time.time()
    while time.time() < stop:
        try: evaluate()
        except Exception as e: log('evaluate ошибка', repr(e)[:200])
        if time.time() - hb >= 60:
            hb = time.time()
            log('пульс арб: стаканов PM', len(FEED.book), 'подписок', len(FEED.want), 'лестниц', len(LAD), 'K15', len(K15), 'Khr', len(KHR),
                'запросов Kalshi', STAT['kreq'], 'ошибок', STAT['kerr'], 'сигналов', STAT['sig'], 'исполнено', STAT['fill'],
                'лучший перевес по типам', STAT['best'], 'проверок', STAT['eval'], 'ws', FEED.stat)
            jw('arb_near.jsonl', {'t': time.time(), 'best': dict(STAT['best']), 'eval': dict(STAT['eval'])})
            STAT['best'].clear(); STAT['eval'].clear(); STAT['kreq'] = STAT['kerr'] = 0
            FEED.stat.update(msgs=0, books=0, errors=0)
        await asyncio.sleep(0.25)
    log('арб: ждём расчёт открытых связок')
    pend = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and t not in tasks]
    if pend: await asyncio.wait(pend, timeout=1500)
    for t in tasks: t.cancel()
    log('арб: стоп')


if __name__ == '__main__':
    asyncio.run(main())
