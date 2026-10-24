"""pm/lip: paper-оценка заработка на программе наград за ликвидность Kalshi (Liquidity Incentive Program).

Как платит Kalshi (help.kalshi.com, Liquidity Incentive Program): раз в секунду снимок стакана; на каждой стороне
(YES-заявки и NO-заявки) опорная цена — первый уровень от лучшего, где накопленный объём ≥ target/5; заявки не хуже
опорной цены идут с весом 1, хуже — с весом discount^(тиков от опорной). Доля участника на стороне = его вес / весь вес,
снимок стоит максимум 2.0 (сумма долей двух сторон). Снимок не засчитывается, если на любой стороне меньше target.
Выплата за период = награда × Σ(наших значений по снимкам) / (2 × всех снимков).

Что делает бот (ничего не выставляет — только считает):
  1. Раз в 5 минут — активные программы; выбираем рынки с самой высокой наградой в час прямо сейчас.
  2. Каждые ~20 с по стакану считаем, какую долю наград получили бы наши заявки: --usd-per-side на лучшем bid
     каждой стороны (встаём в конец очереди), и копим ожидаемые награды.
  3. По реальным сделкам Kalshi (публичная лента) считаем, когда наши заявки бы исполнили (с учётом очереди впереди нас).
     Исполненные позиции держим до расчёта — убыток на них и есть «плата» за награды. Сторону, где заявку исполнили
     полностью, больше не выставляем до конца периода (ограничение риска).
Запуск: python pm/lip.py --minutes 320
"""
import argparse, asyncio, math, time, urllib.parse
from datetime import datetime, timezone

import kalshi as K
from kalshi import log

ap = argparse.ArgumentParser()
ap.add_argument('--minutes', type=float, default=320)
ap.add_argument('--top', type=int, default=20, help='сколько рынков отслеживать одновременно')
ap.add_argument('--usd-per-side', type=float, default=50.0, help='$ в заявке на каждой стороне рынка')
ap.add_argument('--max-contracts', type=float, default=2000)
ap.add_argument('--stop-before', type=float, default=600, help='за сколько секунд до конца периода снимаем заявки (под конец торгуют осведомлённые)')
ap.add_argument('--cycle', type=float, default=20.0, help='с между снимками одного рынка')
ap.add_argument('--series', default='', help='только серии с этими префиксами через запятую (пусто — все)')
A = ap.parse_args()
SER = tuple(x.strip() for x in A.series.split(',') if x.strip())
J = K.Journal('lip')
PROG = {}     # ticker -> программа {reward, start, end, target, df}
ST = {}       # (ticker, start) -> состояние нашего виртуального участия


def ts(s): return datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()


async def programs_loop():
    while True:
        out, cur = {}, None
        for _ in range(20):
            q = {'status': 'active', 'type': 'liquidity', 'limit': 1000}
            if cur: q['cursor'] = cur
            r = await K.aget(f'{K.K}/incentive_programs?' + urllib.parse.urlencode(q))
            if '_error' in r: break
            for p in r.get('incentive_programs', []):
                try:
                    s0, s1 = ts(p['start_date']), ts(p['end_date'])
                    out[p['market_ticker']] = {'reward': p['period_reward'] / 10000, 'start': s0, 'end': s1, 'rate_h': p['period_reward'] / 10000 / max((s1 - s0) / 3600, 1 / 60),
                                               'target': float(p.get('target_size_fp') or 0), 'df': (p.get('discount_factor_bps') or 0) / 10000, 'cap': p.get('max_reward_per_account')}
                except Exception: pass
            cur = r.get('next_cursor')
            if not cur: break
        if out:
            PROG.clear(); PROG.update(out)
            J.w('programs', {'t': time.time(), 'n': len(out), 'total_rate_h': round(sum(p['rate_h'] for p in out.values()), 2)})
        await asyncio.sleep(300)


def side_levels(ob, side):
    """Заявки на покупку стороны (YES или NO), от лучшей цены к худшей: [(цена, кол-во)]."""
    o = ob.get('orderbook_fp') or {}
    xs = [(float(p), float(q)) for p, q in (o.get('yes_dollars' if side == 'YES' else 'no_dollars') or [])]
    return sorted(xs, reverse=True)


def score_side(levels, ours, target, df, tick=0.01):
    """Наша доля на стороне по правилам LIP, если мы стоим `ours` контрактов на лучшем уровне (в конце очереди).
    Возвращает (доля, засчитан ли снимок по этой стороне, опорная цена)."""
    if not levels: return 0.0, False, None
    lv = [list(x) for x in levels]; lv[0][1] += ours
    total = sum(q for _, q in lv)
    if total < target: return 0.0, False, None
    cum, ref = 0.0, lv[-1][0]
    for p, q in lv:
        cum += q
        if cum >= target / 5: ref = p; break
    w_all = 0.0
    for p, q in lv:
        ticks = max(0, round((ref - p) / tick))
        w_all += q * (df ** ticks if ticks else 1.0)
    return (ours / w_all if w_all else 0.0), True, ref


def qty_for(price): return min(A.max_contracts, A.usd_per_side / max(price, 0.01))


async def watch(ticker):
    """Один снимок рынка: начисляем ожидаемую награду за прошедшее время и проверяем исполнения по ленте сделок."""
    p = PROG.get(ticker)
    if not p: return
    now = time.time()
    key = (ticker, p['start'])
    st = ST.setdefault(key, {'reward': 0.0, 'snaps': 0, 'counted': 0, 'last_t': now, 'last_trade': now, 'quote': {}, 'filled': {'YES': 0.0, 'NO': 0.0},
                             'cost': {'YES': 0.0, 'NO': 0.0}, 'val_sum': 0.0, 'end': p['end'], 'rate_h': p['rate_h']})
    ob = await K.aget(f'{K.K}/markets/{ticker}/orderbook')
    if '_error' in ob: return
    val, ok_all, info = 0.0, True, {}
    for side in ('YES', 'NO'):
        lv = side_levels(ob, side)
        q = st['quote'].get(side)
        best = lv[0][0] if lv else None
        if best is None or best <= 0.0 or st['filled'][side] >= qty_for(best) * 0.999 or p['end'] - now < A.stop_before:
            ours = 0.0
        else:
            ours = max(0.0, qty_for(best) - st['filled'][side])
            if not q or abs(q['px'] - best) > 1e-9:   # цена сдвинулась — переставляем заявку, встаём в конец очереди
                st['quote'][side] = q = {'px': best, 'ahead': lv[0][1], 'qty': ours}
            q['qty'] = ours
        sh, ok, ref = score_side(lv, ours, p['target'], p['df'])
        ok_all &= ok; val += sh; info[side] = {'best': best, 'ours': round(ours), 'share': round(sh, 3), 'ref': ref}
    dt = min(now - st['last_t'], A.cycle * 3)
    if ok_all: st['reward'] += p['rate_h'] / 3600 * dt * val / 2; st['counted'] += 1; st['val_sum'] += val
    st['snaps'] += 1; st['last_t'] = now
    # исполнения: taker_side 'no' бьёт в YES-заявки по yes_price; 'yes' — в NO-заявки по no_price
    tr = await K.aget(f'{K.K}/markets/trades?' + urllib.parse.urlencode({'ticker': ticker, 'min_ts': int(st['last_trade']), 'limit': 200}))
    for t in sorted(tr.get('trades', []) if isinstance(tr, dict) else [], key=lambda x: x['created_time']):
        tt = ts(t['created_time'])
        if tt <= st['last_trade']: continue
        st['last_trade'] = tt
        side = 'YES' if t.get('taker_side') == 'no' else 'NO'
        px = float(t['yes_price_dollars'] if side == 'YES' else t['no_price_dollars']); n = float(t['count_fp'])
        q = st['quote'].get(side)
        if not q or q['qty'] <= 0: continue
        if px < q['px'] - 1e-9: got = n                              # прошли сквозь наш уровень — нас исполнили
        elif abs(px - q['px']) < 1e-9: got = max(0.0, n - q['ahead']); q['ahead'] = max(0.0, q['ahead'] - n)   # очередь впереди
        else: continue
        got = min(got, q['qty'])
        if got <= 0: continue
        q['qty'] -= got; st['filled'][side] += got; st['cost'][side] += got * q['px']
        J.w('trades', {'t': tt, 'ticker': ticker, 'side': side, 'px': q['px'], 'qty': round(got, 2), 'fee': 0.0, 'status': 'filled', 'series': ticker.split('-')[0]})
        log('LIP исполнение', ticker, side, q['px'], round(got))
    if st['snaps'] % 15 == 1: J.w('snaps', {'t': now, 'ticker': ticker, 'rate_h': round(p['rate_h'], 2), 'val': round(val, 3), 'ok': ok_all, 'info': info})


def finish(key, st):
    J.w('periods', {'ticker': key[0], 'start': key[1], 'end': st['end'], 'rate_h': round(st['rate_h'], 2), 'reward_est': round(st['reward'], 4),
                    'snaps': st['snaps'], 'counted': st['counted'], 'avg_val': round(st['val_sum'] / st['counted'], 4) if st['counted'] else 0,
                    'filled': {k: round(v, 2) for k, v in st['filled'].items()}, 'fill_cost': {k: round(v, 4) for k, v in st['cost'].items()},
                    'capital': round(2 * A.usd_per_side, 2), 'series': key[0].split('-')[0]})


async def main():
    log('LIP-оценка: рынков', A.top, '$ на сторону', A.usd_per_side)
    pt = asyncio.create_task(programs_loop())
    stop = time.time() + A.minutes * 60; hb = 0
    while time.time() < stop:
        now = time.time()
        for key, st in list(ST.items()):
            if now >= st['end']: finish(key, st); del ST[key]
        live = sorted((t for t, p in PROG.items() if p['start'] <= now < p['end'] - 30 and (not SER or t.startswith(SER))), key=lambda t: -PROG[t]['rate_h'])[:A.top]
        t0 = time.time()
        for t in live:
            await watch(t)
            await asyncio.sleep(max(0.05, A.cycle / max(len(live), 1) - 0.0))
        if time.time() - hb > 300:
            hb = time.time()
            tot = sum(s['reward'] for s in ST.values())
            log('пульс LIP: программ', len(PROG), 'следим', len(live), 'наград в открытых периодах $%.2f' % tot,
                'исполнено YES/NO', round(sum(s['filled']['YES'] for s in ST.values())), round(sum(s['filled']['NO'] for s in ST.values())),
                'запросов', K.STAT['req'], 'ошибок', K.STAT['err'])
        if not live: await asyncio.sleep(10)
    for key, st in list(ST.items()): finish(key, st)
    pt.cancel(); log('LIP: стоп')


if __name__ == '__main__':
    asyncio.run(main())
