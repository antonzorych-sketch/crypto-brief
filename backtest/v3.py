"""
Система v3: паттерны курса YVI (объёмный анализ) + фильтр тренда + риск-менеджмент курса.

Паттерны (H1):
  breakout  (1.1)  — консолидация ≥ N часов, бар пробоя с объёмом ≥ M× среднего и тенью ≤ 20% → вход по закрытию бара.
  retest    (1.2/V4) — после объёмного пробоя цена ушла > 50% высоты диапазона, затем вернулась к границе → лимит на границе.
  false     (V1)   — новый максимум/минимум за N часов на объёмном баре с длинной тенью (пин-бар), закрытие обратно в диапазон → вход против пробоя.
Фильтр тренда: EMA ≈ 50/200 на 4ч (вход только по тренду) или без фильтра.
Выход (как в курсе): риск фиксирован; 40% на 1R, 30% на 2R, 30% на 3R; после 1R стоп в безубыток, после 2R — на +1R; максимум 5 дней.
Исполнение по часовым свечам Binance фьючерсов; если в одном часе задеты стоп и цель — стоп. Комиссии: рынок 4.5 б.п., лимит 1.5 б.п. за сторону.
Результат в R (1R = риск сделки). При риске 1% на сделку 1R = 1% капитала.
"""
import argparse, itertools, json, math, os, sys, time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from run import COINS, H, CACHE, load_klines, synthetic, clean

SPLIT = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
MAKER, TAKER = 1.5, 4.5


def hourly(fut):
    g = fut.groupby(fut.index // H * H)
    k = g.agg(o=('open', 'first'), h=('high', 'max'), l=('low', 'min'), c=('close', 'last'), v=('quote_volume', 'sum'))
    k.index = k.index + H
    c = k.c
    k['vavg'] = k.v.shift(1).rolling(480, min_periods=200).mean()          # средний объём часа за 20 дней
    rng4 = k.h.rolling(4).max() - k.l.rolling(4).min()
    k['A'] = rng4.rolling(56).mean()                                        # обычный 4-часовой ход
    e200, e800 = c.ewm(span=200, adjust=False).mean(), c.ewm(span=800, adjust=False).mean()
    k['trend'] = np.where((c > e200) & (e200 > e800), 1, np.where((c < e200) & (e200 < e800), -1, 0))
    for n in (48, 120):
        k[f'bh{n}'] = k.h.shift(1).rolling(n).max(); k[f'bl{n}'] = k.l.shift(1).rolling(n).min()
    return k.dropna(subset=['vavg', 'A', 'bh120'])


def signals(k, cfg):
    """Список (i, dir, entry_type, entry_px, stop_px, meta)."""
    o, h, l, c, v, va, A, tr = (k[x].values for x in ('o', 'h', 'l', 'c', 'v', 'vavg', 'A', 'trend'))
    n, boxk, vm = cfg['n'], cfg['boxk'], cfg['vm']
    bh, bl = k[f'bh{n}'].values, k[f'bl{n}'].values
    height = bh - bl
    tight = height <= boxk * A                     # консолидация: диапазон N часов не шире boxk обычных ходов
    vol = v >= vm * va
    rng = np.maximum(h - l, 1e-12)
    out = []
    pat = cfg['pattern']
    if pat == 'breakout':
        up = tight & vol & (c > bh) & ((h - c) / rng <= 0.2)
        dn = tight & vol & (c < bl) & ((c - l) / rng <= 0.2)
        for i in np.where(up | dn)[0]:
            d = 1 if up[i] else -1
            stop = (l[i] if d == 1 else h[i]) - d * 0.1 * A[i]
            if cfg['entry'] == 'market':
                out.append((i, d, 'market', c[i], stop, None))
            else:
                out.append((i, d, 'limit', (c[i] + (l[i] if d == 1 else h[i])) / 2, stop, 6))
    elif pat == 'retest':
        up = tight & vol & (c > bh)
        dn = tight & vol & (c < bl)
        for i in np.where(up | dn)[0]:
            d = 1 if up[i] else -1
            lvl = bh[i] if d == 1 else bl[i]
            far = lvl + d * 0.5 * height[i]
            j_end = min(len(c) - 1, i + 48)
            # ушли дальше чем на 50% высоты, затем возврат к уровню на объёме ниже среднего
            went = None
            for j in range(i + 1, j_end + 1):
                if (h[j] >= far) if d == 1 else (l[j] <= far):
                    went = j; break
            if went is None:
                continue
            stop = lvl - d * max(0.5 * height[i], 0.75 * A[i])
            out.append((went, d, 'retest', lvl + d * 0.1 * A[i], stop, 48))
    elif pat == 'false':
        body = np.abs(c - o)
        up_w = h - np.maximum(o, c); dn_w = np.minimum(o, c) - l
        sh = vol & (h > bh) & (c < bh) & (up_w >= 2 * body) & tight   # ложный пробой вверх → шорт
        lg = vol & (l < bl) & (c > bl) & (dn_w >= 2 * body) & tight   # ложный пробой вниз → лонг
        for i in np.where(sh | lg)[0]:
            d = 1 if lg[i] else -1
            stop = (l[i] if d == 1 else h[i]) - d * 0.1 * A[i]
            out.append((i, d, 'market', c[i], stop, None))
    if cfg['trend']:
        out = [s for s in out if tr[s[0]] == s[1]]
    return out


def simulate(k, cfg):
    o, h, l, c = (k[x].values for x in ('o', 'h', 'l', 'c'))
    t = k.index.values
    trades = []; busy_until = -1
    for (i, d, et, px, stop, wait) in signals(k, cfg):
        if i <= busy_until or i + 2 >= len(c):
            continue
        risk = d * (px - stop)
        if risk <= 0 or risk / px > 0.08:
            continue
        # вход
        if et == 'market':
            i0, fill, fee_in = i, px, TAKER
        else:
            i0 = None
            for j in range(i + 1, min(len(c), i + 1 + wait)):
                if (l[j] <= px) if d == 1 else (h[j] >= px):
                    i0 = j; break
                if (h[j] - stop) * d < 0 if False else ((l[j] <= stop) if d == 1 else (h[j] >= stop)):
                    break
            if i0 is None:
                continue
            fill, fee_in = px, MAKER
        R = d * (fill - stop)
        cur_stop = stop; left = 1.0; res = 0.0; fees = fee_in / 1e4 * fill; hit = 0; why = 'время'; j_exit = None
        levels = [(1, 0.4), (2, 0.3), (3, 0.3)] if cfg['exit'] == 'course' else [(2, 1.0)] if cfg['exit'] == 'r2' else [(1, 0.5), (99, 0.5)]
        for j in range(i0 + 1 if et == 'market' else i0, min(len(c), i0 + 120)):
            # стоп первым
            if (o[j] <= cur_stop) if d == 1 else (o[j] >= cur_stop):
                ex = o[j]
            elif (l[j] <= cur_stop) if d == 1 else (h[j] >= cur_stop):
                ex = cur_stop
            else:
                ex = None
            if ex is not None and not (j == i0 and et != 'market' and False):
                res += left * d * (ex - fill); fees += left * TAKER / 1e4 * ex; left = 0; why = 'стоп' if hit == 0 else ('безубыток' if hit == 1 else 'стоп в плюсе'); j_exit = j; break
            for m, part in levels[hit:]:
                tp = fill + d * m * R
                if (h[j] >= tp) if d == 1 else (l[j] <= tp):
                    res += part * d * (tp - fill); fees += part * MAKER / 1e4 * tp; left -= part; hit += 1
                else:
                    break
            if cfg['exit'] == 'trail' and hit >= 1:
                ext = max(h[i0:j + 1]) if d == 1 else min(l[i0:j + 1])
                cur_stop = max(cur_stop, ext - 2 * k.A.values[j]) if d == 1 else min(cur_stop, ext + 2 * k.A.values[j])
            if hit >= 1 and cfg['exit'] != 'r2':
                cur_stop = max(cur_stop, fill) if d == 1 else min(cur_stop, fill)
            if hit >= 2 and cfg['exit'] == 'course':
                cur_stop = max(cur_stop, fill + R) if d == 1 else min(cur_stop, fill - R)
            if left <= 1e-9:
                why = 'все цели'; j_exit = j; break
        if left > 1e-9:
            j_exit = j_exit if j_exit is not None else min(len(c) - 1, i0 + 119)
            res += left * d * (c[j_exit] - fill); fees += left * TAKER / 1e4 * c[j_exit]
        r = (res - fees) / R
        trades.append({'t': int(t[i]), 'tIn': int(t[i0]), 'tOut': int(t[j_exit]), 'side': 'long' if d == 1 else 'short', 'entry': et,
                       'fill': float(fill), 'stop': float(stop), 'r': float(r), 'why': why, 'targets': hit, 'riskPct': float(R / fill * 100)})
        busy_until = j_exit
    return trades


def summ(tr, years):
    if not tr:
        return {'n': 0}
    r = np.array([x['r'] for x in sorted(tr, key=lambda x: x['tOut'])])
    eq = np.cumsum(r); dd = float((eq - np.maximum.accumulate(np.r_[0, eq])[1:]).min())
    streak = best = 0
    for x in r:
        streak = streak + 1 if x <= 0 else 0; best = max(best, streak)
    g, ls = r[r > 0].sum(), -r[r <= 0].sum()
    return {'n': int(len(r)), 'avgR': round(float(r.mean()), 3), 'sumR': round(float(r.sum()), 1), 'win': round(100 * float((r > 0).mean()), 1),
            'pf': round(float(g / ls), 2) if ls > 0 else None, 'maxDD': round(dd, 1), 'lossStreak': int(best),
            't': round(float(r.mean() / (r.std(ddof=1) / math.sqrt(len(r)))), 2) if len(r) > 2 and r.std() > 0 else None,
            'perYear': round(float(r.sum() / years), 1), 'tradesPerMonth': round(len(r) / years / 12, 1)}


PNAMES = {'breakout': 'Пробой консолидации на объёме (1.1)', 'retest': 'Тест уровня после пробоя (1.2 / V4)', 'false': 'Ложный пробой с пин-баром (V1)'}
XNAMES = {'course': 'выход по курсу: 40% на 1R, 30% на 2R, 30% на 3R, стоп в б/у после 1R', 'r2': 'одна цель 2R', 'trail': '50% на 1R, остаток трейлинг 2 ATR'}


def label(cfg):
    return (f"{PNAMES[cfg['pattern']]}; консолидация {cfg['n'] // 24} дн. не шире {cfg['boxk']} ATR; объём ≥{cfg['vm']}× среднего; "
            f"{'по тренду EMA 4ч' if cfg['trend'] else 'без фильтра тренда'}; "
            f"{'вход по закрытию бара' if cfg['entry'] == 'market' else 'лимит на 50% бара пробоя'}; {XNAMES[cfg['exit']]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--synthetic', action='store_true')
    ap.add_argument('--start', default='2023-01-01')
    ap.add_argument('--end', default=None)
    ap.add_argument('--out', default='backtest/v3.json')
    a = ap.parse_args()
    start = date.fromisoformat(a.start)
    end = date.fromisoformat(a.end) if a.end else (datetime.now(timezone.utc).date() - timedelta(days=1))
    os.makedirs(CACHE, exist_ok=True)
    t0 = time.time(); K = {}
    with ThreadPoolExecutor(24) as pool:
        for i, coin in enumerate(COINS):
            fut = synthetic(start, end, seed=i + 1)[0] if a.synthetic else load_klines('fut', coin + 'USDT', start, end, pool)
            K[coin] = hourly(fut); print(coin, len(K[coin]), round(time.time() - t0), flush=True)
    tf = min(int(K[c].index.min()) for c in COINS); tl = max(int(K[c].index.max()) for c in COINS)
    ytr, yte = (SPLIT - tf) / (365.25 * 24 * H), (tl - SPLIT) / (365.25 * 24 * H)
    rows = []
    for pat, n, boxk, vm, trend, entry, ex in itertools.product(['breakout', 'retest', 'false'], [48, 120], [2.0, 3.0], [2.0, 3.0], [False, True], ['market', 'limit'], ['course', 'r2', 'trail']):
        if entry == 'limit' and pat != 'breakout':
            continue
        cfg = {'pattern': pat, 'n': n, 'boxk': boxk, 'vm': vm, 'trend': trend, 'entry': entry, 'exit': ex}
        tr = []
        for coin in COINS:
            for x in simulate(K[coin], cfg):
                x['coin'] = coin; tr.append(x)
        rows.append({'cfg': cfg, 'label': label(cfg), 'train': summ([x for x in tr if x['t'] < SPLIT], ytr), 'test': summ([x for x in tr if x['t'] >= SPLIT], yte), '_tr': tr})
    print('grid', len(rows), round(time.time() - t0), flush=True)
    pats = {}
    best_trades = []
    for pat in PNAMES:
        rr = [r for r in rows if r['cfg']['pattern'] == pat and r['train'].get('n', 0) >= 25]
        if not rr:
            pats[pat] = {'name': PNAMES[pat], 'empty': True}; continue
        rr.sort(key=lambda r: -(r['train']['avgR'] * math.sqrt(r['train']['n'])))
        b = rr[0]; tr = b['_tr']
        best_trades += [dict(x, pattern=pat) for x in tr]
        pats[pat] = {'name': PNAMES[pat], 'best': {k: b[k] for k in ('cfg', 'label', 'train', 'test')},
                     'positiveTestShare': round(100 * sum(1 for r in rr if r['test'].get('avgR', -1) > 0) / len(rr), 0), 'nVariants': len(rr),
                     'testMedianAvgR': round(float(np.median([r['test'].get('avgR', 0) for r in rr])), 3),
                     'trendEffect': {'with': round(float(np.median([r['test'].get('avgR', 0) for r in rr if r['cfg']['trend']])), 3) if any(r['cfg']['trend'] for r in rr) else None,
                                     'without': round(float(np.median([r['test'].get('avgR', 0) for r in rr if not r['cfg']['trend']])), 3) if any(not r['cfg']['trend'] for r in rr) else None},
                     'exitEffect': {x: round(float(np.median([r['test'].get('avgR', 0) for r in rr if r['cfg']['exit'] == x])), 3) for x in XNAMES if any(r['cfg']['exit'] == x for r in rr)},
                     'byCoin': {c: {'train': summ([x for x in tr if x['coin'] == c and x['t'] < SPLIT], ytr), 'test': summ([x for x in tr if x['coin'] == c and x['t'] >= SPLIT], yte)} for c in COINS},
                     'bySide': {s: {'train': summ([x for x in tr if x['side'] == s and x['t'] < SPLIT], ytr), 'test': summ([x for x in tr if x['side'] == s and x['t'] >= SPLIT], yte)} for s in ('long', 'short')},
                     'top': [{k: r[k] for k in ('label', 'train', 'test')} for r in rr[:8]]}
    # система v3 = три лучших паттерна вместе
    sysd = {'train': summ([x for x in best_trades if x['t'] < SPLIT], ytr), 'test': summ([x for x in best_trades if x['t'] >= SPLIT], yte)}
    eq, s_ = [], 0.0
    for x in sorted(best_trades, key=lambda x: x['tOut']):
        s_ += x['r']; eq.append([x['tOut'], round(s_, 2)])
    if len(eq) > 800:
        eq = [eq[int(i * len(eq) / 800)] for i in range(800)] + [eq[-1]]
    mo = {}
    for x in best_trades:
        m = datetime.fromtimestamp(x['tOut'] / 1000, timezone.utc).strftime('%Y-%m'); mo[m] = round(mo.get(m, 0) + x['r'], 2)
    recent = sorted(best_trades, key=lambda x: x['t'])[-40:]
    res = {'generated': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'synthetic': a.synthetic, 'split': SPLIT,
           'period': {'from': tf, 'to': tl}, 'years': {'train': round(ytr, 2), 'test': round(yte, 2)}, 'nVariants': len(rows),
           'patterns': pats, 'system': {**sysd, 'equity': eq, 'months': mo,
                                        'byPattern': {p: {'train': summ([x for x in best_trades if x['pattern'] == p and x['t'] < SPLIT], ytr), 'test': summ([x for x in best_trades if x['pattern'] == p and x['t'] >= SPLIT], yte)} for p in PNAMES}},
           'recent': [{'coin': x['coin'], 'pattern': x['pattern'], 't': x['t'], 'side': x['side'], 'fill': round(x['fill'], 4), 'stop': round(x['stop'], 4), 'r': round(x['r'], 2), 'why': x['why'], 'targets': x['targets'], 'riskPct': round(x['riskPct'], 2)} for x in recent]}
    with open(a.out, 'w') as f:
        json.dump(clean(res), f, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    for p, v in pats.items():
        if not v.get('empty'):
            print(p, v['best']['label'], v['best']['train'], v['best']['test'], v['positiveTestShare'], flush=True)
    print('system', sysd, flush=True)
    print('saved', a.out, os.path.getsize(a.out), round(time.time() - t0), flush=True)


if __name__ == '__main__':
    main()
