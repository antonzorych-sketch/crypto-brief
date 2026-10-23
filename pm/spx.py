"""pm/spx: paper-бот на индексных рынках Kalshi — S&P 500 и Nasdaq-100 (где закроется / будет в HH:00).

Цена индекса — Yahoo (^GSPC, ^NDX, задержка ~2 с), волатильность — VIX/VXN, смешанная с реализованной за последний час.
Вероятность «индекс выше K к моменту расчёта» — лог-нормальная модель на оставшееся торговое время.
Если стакан Kalshi расходится больше чем на --edge после комиссии и спред узкий — виртуальная покупка до расчёта.
Работает только в часы торгов NYSE (будни 9:30–16:00 ET).
Запуск: python pm/spx.py --minutes 320
"""
import argparse, asyncio, math, statistics, time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import kalshi as K
from kalshi import log

ET = ZoneInfo('America/New_York')
IDX = {  # серии Kalshi -> (тикер индекса, тикер индекса волатильности)
    'KXINXU': ('^GSPC', '^VIX'), 'KXINX': ('^GSPC', '^VIX'),
    'KXNASDAQ100U': ('^NDX', '^VXN'), 'KXNASDAQ100': ('^NDX', '^VXN'),
}
YH = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15'}
ap = argparse.ArgumentParser()
ap.add_argument('--minutes', type=float, default=320)
ap.add_argument('--edge', type=float, default=0.05)
ap.add_argument('--stake', type=float, default=5.0)
ap.add_argument('--max-per-market', type=float, default=15.0)
ap.add_argument('--max-spread', type=float, default=0.04)
ap.add_argument('--band', type=float, default=0.015, help='страйки в пределах ±band от цены')
ap.add_argument('--basis-bp', type=float, default=3.0, help='неопределённость официального значения против Yahoo, б.п.')
A = ap.parse_args()
J = K.Journal('spx')
PX, VOLI, VOLR = {}, {}, {}   # индекс -> (ts, цена); индекс волатильности -> годовая; индекс -> дисперсия за секунду (реализованная)
TRADE_SEC = 252 * 6.5 * 3600


def market_open(now=None):
    e = (now or datetime.now(timezone.utc)).astimezone(ET)
    return e.weekday() < 5 and (e.hour, e.minute) >= (9, 30) and e.hour < 16


async def yahoo(sym, rng='1d'):
    d = await K.aget(f'https://query1.finance.yahoo.com/v8/finance/chart/{sym.replace("^", "%5E")}?interval=1m&range={rng}', YH)
    try: return d['chart']['result'][0]
    except Exception: return None


async def px_loop():
    while True:
        for sym in {v[0] for v in IDX.values()}:
            r = await yahoo(sym)
            if r:
                m = r['meta']; PX[sym] = (m.get('regularMarketTime') or 0, m.get('regularMarketPrice'))
        await asyncio.sleep(3)


async def vol_loop():
    while True:
        for sym, vsym in {v for v in IDX.values()}:
            r = await yahoo(vsym)
            if r and r['meta'].get('regularMarketPrice'): VOLI[sym] = r['meta']['regularMarketPrice'] / 100
            r = await yahoo(sym)
            if r:
                c = [x for x in (r['indicators']['quote'][0].get('close') or []) if x][-61:]
                if len(c) > 20:
                    rets = [math.log(b / a) for a, b in zip(c, c[1:])]
                    VOLR[sym] = statistics.pvariance(rets) / 60
        await asyncio.sleep(300)


def var_per_sec(sym):
    vi = VOLI.get(sym)
    if vi is None: return None
    v = vi ** 2 / TRADE_SEC
    return 0.5 * v + 0.5 * VOLR[sym] if sym in VOLR else v


def fair(m, S, v, tau):
    b2 = (A.basis_bp * 1e-4) ** 2; sd = math.sqrt(v * max(tau, 0) + b2)
    P = lambda k: 0.5 * (1 + math.erf(math.log(S / k) / sd / math.sqrt(2)))   # P(итог > k)
    st, lo, hi = m.get('strike_type'), m.get('floor_strike'), m.get('cap_strike')
    if st in ('greater', 'greater_or_equal'): return P(lo)
    if st in ('less', 'less_or_equal'): return 1 - P(hi)
    if st == 'between': return P(lo) - P(hi)
    return None


SPENT, LAST = {}, {}


async def series_loop(ser):
    sym = IDX[ser][0]; last_snap = 0
    while True:
        await asyncio.sleep(3)
        if not market_open(): await asyncio.sleep(60); continue
        p = PX.get(sym); v = var_per_sec(sym)
        if not p or not p[1] or time.time() - p[0] > 60 or v is None: continue
        S = p[1]
        ms = await K.markets(series_ticker=ser, status='open')
        today = datetime.now(ET).date(); snap = []
        for m in ms:
            close = datetime.fromisoformat(m['close_time'].replace('Z', '+00:00'))
            if close.astimezone(ET).date() != today: continue
            k = m.get('floor_strike') or m.get('cap_strike')
            if not k or abs(k / S - 1) > A.band: continue
            tau = close.timestamp() - time.time()
            if tau < 30: continue
            f = fair(m, S, v, tau); t = K.top(m)
            if f is None: continue
            snap.append({'ticker': m['ticker'], 'fair': round(f, 4), 'bid': t['yes_bid'], 'ask': t['yes_ask'], 'tau': round(tau)})
            if not t['yes_ask'] or not t['yes_bid'] or t['yes_ask'] - t['yes_bid'] > A.max_spread: continue
            for side, ask, pf in (('YES', t['yes_ask'], f), ('NO', t['no_ask'], 1 - f)):
                if not ask or not (0.05 <= ask <= 0.95): continue
                e = pf - ask - K.fee(ask); key = (m['ticker'], side)
                if e >= A.edge and SPENT.get(m['ticker'], 0) < A.max_per_market and time.time() - LAST.get(key, 0) > 300:
                    LAST[key] = time.time()
                    r = await K.paper_buy(J, m, side, f, A.edge, A.stake, {'series': ser, 'S': S, 'tau': round(tau), 'vol_ann': round(math.sqrt(v * TRADE_SEC), 4), 'ask0': ask, 'edge0': round(e, 4)})
                    if r.get('status') == 'filled': SPENT[m['ticker']] = SPENT.get(m['ticker'], 0) + r['px'] * r['qty']
        if time.time() - last_snap > 300 and snap:
            last_snap = time.time(); J.w('snaps', {'t': time.time(), 'series': ser, 'S': S, 'vol_ann': round(math.sqrt(v * TRADE_SEC), 4), 'markets': snap})


async def main():
    log('индексный бот: серии', list(IDX), 'перевес', A.edge, '| рынок открыт' if market_open() else '| рынок закрыт — ждём открытия')
    tasks = [asyncio.create_task(x) for x in (px_loop(), vol_loop(), *(series_loop(s) for s in IDX))]
    stop = time.time() + A.minutes * 60
    while time.time() < stop:
        await asyncio.sleep(60)
        log('пульс индексов:', {k: v[1] for k, v in PX.items()}, 'VIX/VXN', VOLI, 'реализ.', {k: round(math.sqrt(v * TRADE_SEC), 3) for k, v in VOLR.items()},
            'запросов', K.STAT['req'], 'ошибок', K.STAT['err'], 'торги' if market_open() else 'закрыто')
    for t in tasks: t.cancel()
    log('индексы: стоп')


if __name__ == '__main__':
    asyncio.run(main())
