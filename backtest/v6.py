"""v6: торговля от поддержек/сопротивлений внутри консолидации (рейндж), 1ч, BTC/ETH/SOL, 2020-2026.
Параметры выбираются на 2020–2023, проверка 2024–2026."""
import io, itertools, json, urllib.request, zipfile
from concurrent.futures import ThreadPoolExecutor
import numpy as np, pandas as pd
BASE = 'https://data.binance.vision/data/futures/um/monthly/klines'
COINS = {'BTC': 'BTCUSDT', 'ETH': 'ETHUSDT', 'SOL': 'SOLUSDT'}
COST_BPS = 6.0  # вход+выход тейкером 2×4.5 + проскальзывание
SPLIT = pd.Timestamp('2024-01-01')

def fetch(u):
    for _ in range(3):
        try: return urllib.request.urlopen(u, timeout=60).read()
        except Exception: pass

def load(sym):
    ms = []; y, m = 2020, 1
    while (y, m) <= (2026, 10):
        ms.append(f'{y}-{m:02d}'); m += 1
        if m == 13: y, m = y + 1, 1
    with ThreadPoolExecutor(8) as p: bs = list(p.map(fetch, [f'{BASE}/{sym}/1h/{sym}-1h-{x}.zip' for x in ms]))
    fr = []
    for b in bs:
        if not b: continue
        z = zipfile.ZipFile(io.BytesIO(b)); df = pd.read_csv(z.open(z.namelist()[0]), header=None)
        if not str(df.iloc[0, 0]).isdigit(): df = df.iloc[1:]
        fr.append(df.iloc[:, :6].astype(float))
    d = pd.concat(fr); d.columns = ['t', 'o', 'h', 'l', 'c', 'v']
    t = d['t'].astype('int64'); t = t.where(t < 1e14, t // 1000)
    d['t'] = pd.to_datetime(t, unit='ms')
    return d.drop_duplicates('t').sort_values('t').reset_index(drop=True)

def prep(d, N):
    h, l = d['h'], d['l']
    d = d.copy()
    d['hi'] = h.rolling(N).max().shift(1); d['lo'] = l.rolling(N).min().shift(1)
    d['drift'] = (d['c'].shift(1) - d['c'].shift(N)).abs()
    tr = pd.concat([h - l, (h - d['c'].shift()).abs(), (l - d['c'].shift()).abs()], axis=1).max(axis=1)
    d['atr'] = tr.rolling(24).mean().shift(1)
    return d

def trades(d, N, w, z, sk, tgt, sides, hold=48):
    a = {k: d[k].values for k in ['o', 'h', 'l', 'c', 'hi', 'lo', 'drift', 'atr']}; t = d['t'].values
    width = a['hi'] - a['lo']
    cons = (width / a['c'] <= w) & (a['drift'] <= 0.35 * width) & (width > 0)
    longs = cons & (a['l'] <= a['lo'] + z * width) & (a['c'] > a['o']) & (a['c'] > a['lo'])
    shorts = cons & (a['h'] >= a['hi'] - z * width) & (a['c'] < a['o']) & (a['c'] < a['hi']) if sides == 'both' else np.zeros(len(d), bool)
    out = []; i = N + 30; n = len(d)
    while i < n - 1:
        side = 1 if longs[i] else (-1 if shorts[i] else 0)
        if side == 0 or not np.isfinite(a['atr'][i]): i += 1; continue
        e = a['c'][i]
        if side == 1:
            stop = a['lo'][i] - sk * a['atr'][i]; target = a['lo'][i] + 0.5 * width[i] if tgt == 'mid' else a['hi'][i]
            risk = e - stop
        else:
            stop = a['hi'][i] + sk * a['atr'][i]; target = a['hi'][i] - 0.5 * width[i] if tgt == 'mid' else a['lo'][i]
            risk = stop - e
        if risk <= 0 or (target - e) * side <= 0: i += 1; continue
        ex = a['c'][min(i + hold, n - 1)]; j_end = min(i + hold, n - 1); jx = j_end
        for j in range(i + 1, j_end + 1):
            if side == 1:
                if a['l'][j] <= stop: ex = stop; jx = j; break
                if a['h'][j] >= target: ex = target; jx = j; break
            else:
                if a['h'][j] >= stop: ex = stop; jx = j; break
                if a['l'][j] <= target: ex = target; jx = j; break
        R = ((ex - e) * side - COST_BPS / 1e4 * e) / risk
        out.append((t[i], R, side)); i = jx + 1
    return out

def summ(tr):
    if len(tr) < 5: return None
    R = np.array([x[1] for x in tr]); n = len(R)
    pf = R[R > 0].sum() / max(1e-9, -R[R < 0].sum())
    return {'n': n, 'avgR': round(R.mean(), 3), 'sumR': round(R.sum(), 1), 'win': round((R > 0).mean() * 100, 1), 'pf': round(pf, 2),
            't': round(R.mean() / (R.std() / np.sqrt(n)), 2) if R.std() > 0 else 0}

def main():
    data = {k: load(s) for k, s in COINS.items()}
    for k, d in data.items(): print(k, d.t.iloc[0], d.t.iloc[-1], len(d), flush=True)
    grid = list(itertools.product([48, 96, 168], [0.04, 0.06, 0.08, 0.12], [0.1, 0.2], [0.5, 1.0], ['mid', 'top'], ['long', 'both']))
    prepped = {N: {k: prep(d, N) for k, d in data.items()} for N in (48, 96, 168)}
    rows = []
    for (N, w, z, sk, tgt, sides) in grid:
        allt = []
        for k in data: allt += [(*x, k) for x in trades(prepped[N][k], N, w, z, sk, tgt, sides)]
        tr = [x for x in allt if pd.Timestamp(x[0]) < SPLIT]; te = [x for x in allt if pd.Timestamp(x[0]) >= SPLIT]
        rows.append({'cfg': dict(N=N, w=w, z=z, sk=sk, tgt=tgt, sides=sides), 'train': summ(tr), 'test': summ(te), 'trades': allt})
    ok = [r for r in rows if r['train'] and r['test'] and r['train']['n'] >= 60]
    print('конфигов', len(rows), 'с достаточной статистикой', len(ok))
    best = sorted(ok, key=lambda r: -r['train']['avgR'])
    print('\nТОП-10 по обучению (2020–23) и их проверка (2024–26):')
    for r in best[:10]: print(r['cfg'], 'train', r['train'], 'TEST', r['test'])
    pos = np.mean([r['test']['avgR'] > 0 for r in ok]); posTrain = np.mean([r['train']['avgR'] > 0 for r in ok])
    print('\nдоля конфигов с avgR>0: обучение %.0f%%, проверка %.0f%%' % (posTrain * 100, pos * 100))
    top = best[:20]; print('среднее avgR топ-20: train %.3f test %.3f' % (np.mean([r['train']['avgR'] for r in top]), np.mean([r['test']['avgR'] for r in top])))
    b = best[0]; tr = sorted(b['trades'], key=lambda x: x[0])
    by = {}
    for x in tr: by.setdefault(pd.Timestamp(x[0]).year, []).append(x)
    print('\nЛучшая по обучению, по годам:'); [print(y, summ(v)) for y, v in sorted(by.items())]
    bc = {}
    for x in tr: bc.setdefault(x[3], []).append(x)
    print('по монетам:'); [print(k, summ(v)) for k, v in bc.items()]
    json.dump({'best': [{k: v for k, v in r.items() if k != 'trades'} for r in best[:15]], 'share_pos_train': posTrain, 'share_pos_test': pos, 'nCfg': len(ok),
               'bestByYear': {str(y): summ(v) for y, v in by.items()}, 'cost_bps': COST_BPS}, open('backtest/v6.json', 'w'))
main()
