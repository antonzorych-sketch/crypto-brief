"""pm/pair: paper-стратегия «пары» на рынках Up or Down 5m/15m Polymarket US.

Самый задокументированный рабочий подход на коротких крипторынках: цены Up и Down за окно сильно качаются,
и в разные моменты можно дёшево купить сначала одну сторону, потом другую. Если средняя стоимость пары
Up + Down вместе с комиссиями меньше $1 — прибыль зафиксирована при любом исходе (одна сторона платит $1).

Правила (все сделки виртуальные, по лучшей цене стакана с задержкой --latency):
  1. Первая нога: покупаем сторону, когда её цена ≤ --entry (по умолчанию 35¢).
  2. Вторая нога: докупаем другую сторону, если пара (средняя цена первой + цена второй + комиссии обеих) ≤ --target.
  3. Повторяем, пока в окне не исчерпан лимит --max-usd; непарный остаток в конце держим до расчёта
     (или продаём в безубыток, если можно) — это единственный риск стратегии, его и меряем.
Важно про Polymarket US: у Up и Down один общий стакан (Down ask = 1 − Up bid), поэтому купить обе стороны
сразу дешевле $1 нельзя, а «пара» — это по сути купить Up и позже продать его дороже. Это ставка на возврат цены
после резкого движения, а не арбитраж: при продолжении тренда остаётся непарная позиция. Стратегия здесь —
эталон «как делают боты», её реальный результат с учётом непарного риска и показывает отчёт.
Запуск: python pm/pair.py --minutes 320
"""
import argparse, asyncio, json, math, os, time
from datetime import datetime, timedelta, timezone

import pmws
from pmws import G, log

PFX = {'btc': 'cpc', 'eth': 'cpceth', 'sol': 'cpcsol', 'xrp': 'cpcxrp'}
TF = {'5m': 300, '15m': 900}
ap = argparse.ArgumentParser()
ap.add_argument('--minutes', type=float, default=320)
ap.add_argument('--coins', default='btc,eth,sol,xrp')
ap.add_argument('--tfs', default='5m,15m')
ap.add_argument('--entry', type=float, default=0.30, help='цена первой ноги не выше')
ap.add_argument('--max-unpaired', type=float, default=5.0, help='$ максимум в непарных лотах одной стороны')
ap.add_argument('--min-tau', type=float, default=120, help='первую ногу берём, только если до конца окна больше, с')
ap.add_argument('--target', type=float, default=0.97, help='стоимость пары с комиссиями не выше')
ap.add_argument('--clip', type=float, default=5.0, help='$ на одну покупку')
ap.add_argument('--max-usd', type=float, default=15.0, help='$ максимум в одном окне')
ap.add_argument('--min-depth', type=float, default=10.0, help='$ минимум на лучшей цене')
ap.add_argument('--latency', type=float, default=0.5)
ap.add_argument('--theta', type=float, default=0.0695)
ap.add_argument('--out', default='pm/paper')
A = ap.parse_args()
COINS = [c for c in A.coins.split(',') if c]; TFS = [t for t in A.tfs.split(',') if t]
RUN = os.environ.get('RUN_ID') or datetime.now(timezone.utc).strftime('%Y%m%d-%H%M')
OUT = os.path.join(A.out, RUN); os.makedirs(OUT, exist_ok=True)
FEED = pmws.Feed('pair')


def jw(name, obj):
    with open(os.path.join(OUT, name), 'a') as f: f.write(json.dumps(obj, default=str) + '\n')


def fee(p): return A.theta * p * (1 - p)


class W:
    """Одно окно: позиции по сторонам (кол-во, стоимость с комиссиями)."""
    def __init__(s, coin, tf, start):
        s.coin, s.tf, s.start = coin, tf, start
        s.end = start + timedelta(seconds=TF[tf])
        s.slug = f'{PFX[coin]}-{coin}-updown-{tf}-{start:%Y-%m-%d-%H%M}z'
        s.q = {'UP': 0.0, 'DOWN': 0.0}; s.cost = {'UP': 0.0, 'DOWN': 0.0}; s.spent = 0.0
        s.open = {'UP': [], 'DOWN': []}   # непарные лоты [кол-во, цена с комиссией за штуку] (FIFO)
        s.pending = None; s.done = False; s.sold = []

    def avg(s, side): return s.cost[side] / s.q[side] if s.q[side] else None
    def unpaired(s, side): return sum(l[0] for l in s.open[side])
    def guaranteed(s): return min(s.q.values()) - s.cost['UP'] - s.cost['DOWN']  # P&L при худшем исходе

    def add(s, side, n, unit):
        """Покупка: сначала закрываем непарные лоты другой стороны, остаток — новый непарный лот."""
        other = 'DOWN' if side == 'UP' else 'UP'; left = n
        while left > 1e-9 and s.open[other]:
            lot = s.open[other][0]; m = min(lot[0], left); lot[0] -= m; left -= m
            if lot[0] <= 1e-9: s.open[other].pop(0)
        if left > 1e-9: s.open[side].append([left, unit])
        s.q[side] += n; s.cost[side] += n * unit
    def t1(s): return s.end.timestamp()


WIN = {}


def quotes(b):
    """-> {'UP': (ask, qty), 'DOWN': (ask, qty)} и лучшие bid'ы для продажи."""
    return ({'UP': b['yes_ask'], 'DOWN': b['no_ask']}, {'UP': b['yes_bid'], 'DOWN': b['no_bid']})


def plan(w, asks, now):
    """Решаем, что купить сейчас. Возвращает список (сторона, макс. цена, кол-во, причина)."""
    tau = w.t1() - now
    if tau < 10 or w.spent >= A.max_usd: return []
    U, D = asks['UP'], asks['DOWN']
    out = []
    for side, other in (('UP', 'DOWN'), ('DOWN', 'UP')):
        a = asks[side]
        if not a or a[0] * a[1] < A.min_depth: continue
        if w.open[other]:
            # 2) вторая нога: закрываем самый старый непарный лот другой стороны, если пара укладывается в target
            lot_q, lot_unit = w.open[other][0]
            if lot_unit + a[0] + fee(a[0]) <= A.target:
                n = min(lot_q, a[1], (A.max_usd - w.spent) / a[0])
                if n > 0.01: out.append((side, a[0], n, 'pair'))
        elif a[0] <= A.entry and sum(l[0] * l[1] for l in w.open[side]) < A.max_unpaired and tau > A.min_tau:
            # 1) первая нога. Риск считаем по ЗАПЛАЧЕННОЙ сумме непарных лотов: прошлый прогон показал, что лимит по текущей
            #    цене позволял усреднять падающую сторону до $30 (−$158 на 16 окнах, все потери — непарный остаток)
            n = min(A.clip / a[0], a[1], (A.max_usd - w.spent) / a[0])
            if n * a[0] >= 1: out.append((side, a[0], n, 'entry'))
    return out[:1]


def execute(w, orders, asks, now):
    """Исполнение спустя latency: берём, только если цена не ухудшилась."""
    for side, px0, n, why in orders:
        a = asks[side]
        rec = {'t': now, 'slug': w.slug, 'coin': w.coin, 'tf': w.tf, 'side': side, 'why': why, 'px0': px0, 'tau': round(w.t1() - now)}
        if not a or a[0] > px0 + 1e-9:
            rec['status'] = 'missed'; jw('pair_trades.jsonl', rec); continue
        n = min(n, a[1]); c = n * a[0] + fee(a[0]) * n
        w.add(side, n, a[0] + fee(a[0])); w.spent += n * a[0]
        rec.update(status='filled', px=a[0], qty=round(n, 2), cost=round(c, 4))
        jw('pair_trades.jsonl', rec)
        log('пара', w.slug, side, why, 'цена', a[0], 'кол-во %.1f' % n, '| пар', round(min(w.q.values()), 1), 'гарантировано $%.2f' % w.guaranteed())


def maybe_unwind(w, bids, now):
    """За минуту до конца: непарный остаток продаём, если можно не хуже безубытка, иначе держим до расчёта."""
    tau = w.t1() - now
    if not (10 < tau < 60): return
    for side, other in (('UP', 'DOWN'), ('DOWN', 'UP')):
        extra = w.q[side] - w.q[other]; b = bids[side]
        lot_cost = sum(l[0] * l[1] for l in w.open[side])
        if extra > 0.01 and b and b[0] - fee(b[0]) >= lot_cost / max(extra, 1e-9) and b[1] >= extra:
            proceeds = extra * (b[0] - fee(b[0])); cost = lot_cost
            w.cost[side] -= cost; w.q[side] -= extra; w.open[side] = []; w.sold.append({'side': side, 'qty': extra, 'pnl': round(proceeds - cost, 4)})
            jw('pair_trades.jsonl', {'t': now, 'slug': w.slug, 'side': side, 'why': 'unwind', 'status': 'sold', 'px': b[0], 'qty': round(extra, 2), 'pnl': round(proceeds - cost, 4)})


async def settle(w):
    await asyncio.sleep(max(0, w.t1() - time.time()) + 20)
    res = None
    for _ in range(30):
        d = await asyncio.to_thread(lambda: _get(f'{G}/v1/markets/{w.slug}/settlement'))
        v = d.get('settlement', d.get('settlementPx')) if isinstance(d, dict) else None
        v = v['value'] if isinstance(v, dict) else v
        if v is not None: res = 'UP' if float(v) >= 0.5 else 'DOWN'; break
        await asyncio.sleep(30)
    pairs = min(w.q.values()); extra_side = max(w.q, key=w.q.get); extra = w.q[extra_side] - pairs
    payout = w.q[res] if res else None
    rec = {'slug': w.slug, 'coin': w.coin, 'tf': w.tf, 'result': res, 'pairs': round(pairs, 2), 'q': {k: round(v, 2) for k, v in w.q.items()},
           'cost': round(w.cost['UP'] + w.cost['DOWN'], 4), 'guaranteed': round(w.guaranteed(), 4),
           'unpaired_side': extra_side if extra > 0.01 else None, 'unpaired_qty': round(extra, 2),
           'unwound_pnl': round(sum(x['pnl'] for x in w.sold), 4), 'spent': round(w.spent, 2)}
    rec['pnl'] = None if payout is None else round(payout - rec['cost'] + rec['unwound_pnl'], 4)
    rec['locked'] = round(min(rec['guaranteed'], rec['pnl'] if rec['pnl'] is not None else 9e9), 4)
    jw('pair_settled.jsonl', rec)
    log('итог пары', w.slug, 'пар', rec['pairs'], 'гарантировано', rec['guaranteed'], 'непарный', rec['unpaired_qty'], rec['unpaired_side'], 'P&L', rec['pnl'], 'исход', res)


def _get(u):
    import urllib.request
    try:
        return json.loads(urllib.request.urlopen(urllib.request.Request(u, headers={'User-Agent': 'pm-pair'}), timeout=10).read())
    except Exception:
        return {}


async def main():
    log('стратегия пар:', COINS, TFS, 'вход ≤', A.entry, 'пара ≤', A.target, 'стакан:', 'WebSocket' if pmws.WS_ON else 'публичный REST')
    feed = asyncio.create_task(FEED.run() if pmws.WS_ON else FEED.run_rest(only=lambda x: '-updown-' in x, period=1.0))
    stop = time.time() + A.minutes * 60; hb = time.time()
    while time.time() < stop:
        now_dt = datetime.now(timezone.utc); now = time.time()
        for c in COINS:
            for tf in TFS:
                n = TF[tf]; st = datetime.fromtimestamp(int(now) // n * n, timezone.utc)
                w = WIN.get((c, tf, st))
                if not w and st.timestamp() + n <= stop + 300: WIN[(c, tf, st)] = w = W(c, tf, st)
                if w: FEED.want.add(w.slug)
        for key, w in list(WIN.items()):
            if w.done: continue
            if now >= w.t1():
                w.done = True; FEED.want.discard(w.slug)
                if w.spent: asyncio.create_task(settle(w))
                continue
            b = FEED.get(w.slug, 120)
            if not b or (b['state'] and 'OPEN' not in str(b['state'])): continue
            asks, bids = quotes(b)
            if w.pending and now - w.pending[0] >= A.latency:
                execute(w, w.pending[1], asks, now); w.pending = None
            elif not w.pending:
                o = plan(w, asks, now)
                if o: w.pending = (now, o)
            maybe_unwind(w, bids, now)
        if now - hb > 60:
            hb = now; act = [w for w in WIN.values() if not w.done and w.spent]
            log('пульс пар: окон', sum(not w.done for w in WIN.values()), 'с позициями', len(act), 'стаканов', len(FEED.book), FEED.stat)
            FEED.stat.update(msgs=0, books=0, errors=0)
        await asyncio.sleep(0.25)
    for w in WIN.values():
        if not w.done and w.spent: w.done = True; asyncio.create_task(settle(w))
    pend = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and t is not feed]
    if pend: await asyncio.wait(pend, timeout=1200)
    feed.cancel(); log('пары: стоп')


if __name__ == '__main__':
    asyncio.run(main())
