"""
Система v2: подбор логики входа.

Сигнал: бустинг (направление на 4 часа) + модель волатильности (ждём ли значимый ход),
обе переобучаются поквартально только на прошлых данных (walk-forward), прогнозы с 2024 года.
Исполнение: минутные свечи Binance фьючерсов, комиссии Hyperliquid 1.5 б.п. лимитом / 4.5 б.п. по рынку за сторону.
Если в одной минуте задеты и стоп, и тейк — засчитывается стоп. Лимит, который не исполнился, — сделки нет.

Параметры входа/выхода выбираются ТОЛЬКО по 2024 году. 2025–2026 — проверка.
"""
import argparse, itertools, json, math, os, sys, time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from run import COINS, H, MIN, CACHE, load_klines, load_metrics, load_funding, synthetic, clean
from research import FEATS, NAMES, hourly_frame, load_book_multi

SPLIT = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
MAKER, TAKER = 1.5, 4.5
PATH = 720  # минут после сигнала, которые смотрим


def wf_predict(D, kind):
    from sklearn.ensemble import HistGradientBoostingClassifier
    feats = [f for f in FEATS if not D[f].isna().all()] + ['hs', 'hc']
    if kind == 'dir':
        tgt = (D.y4.values > 0).astype(float); valid = ~D.y4.isna().values
        params = dict(max_depth=3, learning_rate=0.04, max_iter=250, min_samples_leaf=300, l2_regularization=1.0, random_state=1)
    else:
        tgt = D.big4.values; valid = ~D.big4.isna().values
        params = dict(max_depth=3, learning_rate=0.05, max_iter=250, min_samples_leaf=300, random_state=1)
    X = D[feats].values
    p = np.full(len(D), np.nan)
    starts = pd.date_range('2024-01-01', pd.to_datetime(D.t.max(), unit='ms'), freq='QS', tz='UTC')
    for i, s in enumerate(starts):
        s_ms = int(s.timestamp() * 1000)
        e_ms = int(starts[i + 1].timestamp() * 1000) if i + 1 < len(starts) else int(D.t.max()) + 1
        trm = (D.t.values < s_ms - 24 * H) & valid
        tem = (D.t.values >= s_ms) & (D.t.values < e_ms)
        if trm.sum() < 2000 or not tem.any():
            continue
        m = HistGradientBoostingClassifier(**params).fit(X[trm], tgt[trm].astype(int))
        p[tem] = m.predict_proba(X[tem])[:, 1]
    return p


def build_signals(D, fut_by_coin):
    sig = D[~np.isnan(D.pdir.values) & ~np.isnan(D.pvol.values) & ~D.atrPct.isna().values].copy()
    out = []
    for coin in COINS:
        s = sig[sig.coin == coin]
        fut = fut_by_coin[coin]
        t1 = fut.index.values
        hi, lo, cl = fut.high.values, fut.low.values, fut.close.values
        idx = np.searchsorted(t1, s.t.values)          # первая минута после закрытия часа
        for (_, r), i in zip(s.iterrows(), idx):
            if i + PATH >= len(t1):
                continue
            c0 = cl[i - 1]
            out.append({'coin': coin, 't': int(r.t), 'hour': int(r.hour), 'p': float(r.pdir), 'pv': float(r.pvol), 'c0': float(c0),
                        'atr': float(r.atrPct) / 100 * c0, 'h': hi[i:i + PATH], 'l': lo[i:i + PATH], 'c': cl[i:i + PATH]})
    return out


def first(mask):
    i = int(np.argmax(mask))
    return i if mask[i] else -1


def simulate(S, cfg):
    th, vth, entry, k, wait, tp, sl, hold, tod = cfg['th'], cfg['vth'], cfg['entry'], cfg['k'], cfg['wait'], cfg['tp'], cfg['sl'], cfg['hold'], cfg['tod']
    trades = []; missed = 0; last_t = {c: -10**15 for c in COINS}
    for s in S:
        if s['p'] > th: d = 1
        elif s['p'] < 1 - th: d = -1
        else: continue
        if s['pv'] < vth:
            continue
        if tod and ((d == 1 and 10 <= s['hour'] <= 14) or (d == -1 and 19 <= s['hour'] <= 21)):
            continue
        if s['t'] < last_t[s['coin']]:
            continue                                   # одна позиция на монету
        h, l, c, A = s['h'], s['l'], s['c'], s['atr']
        if entry == 'market':
            i0, fill, ce = 0, s['c0'], TAKER
        else:
            lvl = s['c0'] - d * k * A
            i0 = first((l[:wait] <= lvl) if d == 1 else (h[:wait] >= lvl))
            if i0 < 0:
                missed += 1; continue
            fill, ce = lvl, MAKER
        end = min(i0 + hold * 60, PATH - 1)
        seg_h, seg_l = h[i0:end + 1], l[i0:end + 1]
        if tp:
            tpl, sll = fill + d * tp * A, fill - d * sl * A
            ist = first((seg_l <= sll) if d == 1 else (seg_h >= sll))
            hit_tp = (seg_h >= tpl) if d == 1 else (seg_l <= tpl)
            if entry != 'market':
                hit_tp = hit_tp.copy(); hit_tp[0] = False      # в минуту исполнения лимита тейк не засчитываем
            itp = first(hit_tp)
            if ist >= 0 and (itp < 0 or ist <= itp):
                ex, cx, why, j = sll, TAKER, 'стоп', ist
            elif itp >= 0:
                ex, cx, why, j = tpl, MAKER, 'тейк', itp
            else:
                ex, cx, why, j = c[end], TAKER, 'время', end - i0
        else:
            ex, cx, why, j = c[end], TAKER, 'время', end - i0
        bps = d * (ex / fill - 1) * 1e4 - ce - cx
        trades.append({'coin': s['coin'], 't': s['t'], 'side': 'long' if d == 1 else 'short', 'p': round(s['p'], 3), 'pv': round(s['pv'], 3),
                       'fill': fill, 'exit': ex, 'why': why, 'bps': bps, 'holdMin': int(i0 + j), 'waitMin': int(i0)})
        last_t[s['coin']] = s['t'] + (i0 + j + 1) * MIN
    return trades, missed


def summ(tr, missed=0):
    if not tr:
        return {'n': 0}
    b = np.array([x['bps'] for x in tr])
    eq = np.cumsum(b); dd = float((eq - np.maximum.accumulate(np.r_[0, eq])[1:]).min())
    return {'n': int(len(b)), 'avg': round(float(b.mean()), 2), 'sum': round(float(b.sum()), 0), 'win': round(100 * float((b > 0).mean()), 1),
            't': round(float(b.mean() / (b.std(ddof=1) / math.sqrt(len(b)))), 2) if len(b) > 2 and b.std() > 0 else None,
            'maxDD': round(dd, 0), 'missed': int(missed), 'fillRate': round(100 * len(b) / (len(b) + missed), 1) if missed else 100.0}


def label(cfg):
    e = 'по рынку' if cfg['entry'] == 'market' else f"лимит −{cfg['k']} ATR, ждать {cfg['wait'] // 60}ч"
    x = f"тейк {cfg['tp']} / стоп {cfg['sl']} ATR, максимум {cfg['hold']}ч" if cfg['tp'] else f"выход через {cfg['hold']}ч"
    f = f"уверенность ≥{round(cfg['th'] * 100)}%" + (f", движение ≥{round(cfg['vth'] * 100)}%" if cfg['vth'] else '') + (', фильтр времени' if cfg['tod'] else '')
    return f'{e}; {x}; {f}'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--synthetic', action='store_true')
    ap.add_argument('--start', default='2023-01-01')
    ap.add_argument('--end', default=None)
    ap.add_argument('--out', default='backtest/v2.json')
    a = ap.parse_args()
    start = date.fromisoformat(a.start)
    end = date.fromisoformat(a.end) if a.end else (datetime.now(timezone.utc).date() - timedelta(days=1))
    os.makedirs(CACHE, exist_ok=True)
    t0 = time.time(); frames, futs = [], {}
    with ThreadPoolExecutor(24) as pool:
        for i, coin in enumerate(COINS):
            sym = coin + 'USDT'
            if a.synthetic:
                fut, spot, met, fund, _ = synthetic(start, end, seed=i + 1)
                t5 = met.index.values; rng = np.random.default_rng(i)
                book = pd.DataFrame({f'book{k}': np.clip(50 + rng.normal(0, 6, len(t5)), 5, 95) for k in (1, 2, 5)}, index=(t5 // H) * H + H)
                book = book[~book.index.duplicated()]
            else:
                fut = load_klines('fut', sym, start, end, pool); spot = load_klines('spot', sym, start, end, pool)
                met = load_metrics(sym, start, end, pool); fund = load_funding(sym, start, end, pool)
                book = load_book_multi(sym, start, end, pool)
            frames.append(hourly_frame(coin, fut, spot, met, fund, book)); futs[coin] = fut[['high', 'low', 'close']]
            print(coin, 'frame', round(time.time() - t0), flush=True)
    D = pd.concat(frames, ignore_index=True)
    D = D[D.t >= D.t.min() + 45 * 24 * H].reset_index(drop=True)
    D[FEATS] = D[FEATS].replace([np.inf, -np.inf], np.nan)
    for f in FEATS:
        for coin in COINS:
            m = D.coin == coin
            lo_, hi_ = D.loc[m, f].quantile([0.001, 0.999]); D.loc[m, f] = D.loc[m, f].clip(lo_, hi_)
    D['hs'] = np.sin(2 * np.pi * D.hour / 24); D['hc'] = np.cos(2 * np.pi * D.hour / 24)
    D['pdir'] = wf_predict(D, 'dir'); D['pvol'] = wf_predict(D, 'vol')
    print('models', round(time.time() - t0), flush=True)
    S = build_signals(D, futs); S.sort(key=lambda s: s['t'])
    print('signals', len(S), round(time.time() - t0), flush=True)
    grid = []
    for th, vth, (entry, k, wait), (tp, sl, hold), tod in itertools.product(
            [0.55, 0.58, 0.61], [0.0, 0.2, 0.3],
            [('market', 0, 0), ('limit', 0.1, 60), ('limit', 0.25, 60), ('limit', 0.25, 120), ('limit', 0.5, 120)],
            [(None, None, 4), (None, None, 8), (1.0, 0.75, 8), (1.5, 1.0, 8), (2.0, 1.0, 12), (1.0, 1.0, 4)],
            [False, True]):
        cfg = {'th': th, 'vth': vth, 'entry': entry, 'k': k, 'wait': wait, 'tp': tp, 'sl': sl, 'hold': hold, 'tod': tod}
        tr, missed = simulate(S, cfg)
        trn = [x for x in tr if x['t'] < SPLIT]; tst = [x for x in tr if x['t'] >= SPLIT]
        grid.append({'cfg': cfg, 'label': label(cfg), 'train': summ(trn), 'test': summ(tst), 'missed': missed})
    print('grid', len(grid), round(time.time() - t0), flush=True)
    ok = [g for g in grid if g['train'].get('n', 0) >= 150]
    ok.sort(key=lambda g: -(g['train']['avg'] * math.sqrt(g['train']['n']) / 100))   # качество на 2024 с учётом числа сделок
    best = ok[0]
    trades, missed = simulate(S, best['cfg'])
    res = {'generated': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'synthetic': a.synthetic, 'split': SPLIT,
           'costs': {'maker': MAKER, 'taker': TAKER}, 'nGrid': len(grid), 'nSignals': len(S),
           'best': {**best, 'all': summ(trades, missed)},
           'topTrain': [{k: g[k] for k in ('label', 'cfg', 'train', 'test')} for g in ok[:15]],
           'testPositiveShare': round(100 * sum(1 for g in ok if g['test'].get('avg', -1) > 0) / max(1, len(ok)), 1),
           'testMedianAvg': round(float(np.median([g['test'].get('avg', 0) for g in ok])), 2),
           'baselineMarket4h': next(({k: g[k] for k in ('label', 'train', 'test')} for g in grid if g['cfg'] == {'th': best['cfg']['th'], 'vth': 0.0, 'entry': 'market', 'k': 0, 'wait': 0, 'tp': None, 'sl': None, 'hold': 4, 'tod': False}), None)}
    tb = [x for x in trades]
    for key in ('coin', 'side', 'why'):
        res['best'][f'by_{key}'] = {v: {'train': summ([x for x in tb if x[key] == v and x['t'] < SPLIT]), 'test': summ([x for x in tb if x[key] == v and x['t'] >= SPLIT])} for v in sorted({x[key] for x in tb})}
    eq, s_ = [], 0.0
    for x in tb:
        s_ += x['bps']; eq.append([x['t'], round(s_, 1)])
    if len(eq) > 800:
        st = len(eq) / 800; eq = [eq[int(i * st)] for i in range(800)] + [eq[-1]]
    res['best']['equity'] = eq
    mo = {}
    for x in tb:
        m = datetime.fromtimestamp(x['t'] / 1000, timezone.utc).strftime('%Y-%m'); mo[m] = round(mo.get(m, 0) + x['bps'], 1)
    res['best']['months'] = mo
    res['best']['trades'] = [{**{k: x[k] for k in ('coin', 't', 'side', 'p', 'pv', 'why', 'holdMin', 'waitMin')}, 'fill': round(x['fill'], 6), 'exit': round(x['exit'], 6), 'bps': round(x['bps'], 1)} for x in tb[-600:]]
    with open(a.out, 'w') as f:
        json.dump(clean(res), f, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    print('best', best['label'], best['train'], best['test'], flush=True)
    print('saved', a.out, os.path.getsize(a.out), round(time.time() - t0), flush=True)


if __name__ == '__main__':
    main()
