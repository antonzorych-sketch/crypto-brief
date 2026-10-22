"""v9: свинг 1–3 дня на 4ч свечах BTC/ETH/SOL, 2020–2026. Обучение 2020–23, проверка 2024–26. Все сделки taker (4.5+1 б.п. на ногу)."""
import io, itertools, json, urllib.request, zipfile
from concurrent.futures import ThreadPoolExecutor
import numpy as np, pandas as pd
BASE = 'https://data.binance.vision/data/futures/um/monthly/klines'
COINS = {'BTC': 'BTCUSDT', 'ETH': 'ETHUSDT', 'SOL': 'SOLUSDT'}
FEE = 4.5e-4 + 1e-4
SPLIT = pd.Timestamp('2024-01-01')
def fetch(u):
    for _ in range(3):
        try: return urllib.request.urlopen(u, timeout=90).read()
        except Exception: pass
def load(sym):
    ms = []; y, m = 2020, 1
    while (y, m) <= (2026, 9):
        ms.append(f'{y}-{m:02d}'); m += 1
        if m == 13: y, m = y + 1, 1
    with ThreadPoolExecutor(8) as p: bs = list(p.map(fetch, [f'{BASE}/{sym}/4h/{sym}-4h-{x}.zip' for x in ms]))
    fr = []
    for b in bs:
        if not b: continue
        z = zipfile.ZipFile(io.BytesIO(b)); df = pd.read_csv(z.open(z.namelist()[0]), header=None)
        if not str(df.iloc[0, 0]).isdigit(): df = df.iloc[1:]
        fr.append(df.iloc[:, :6].astype(float))
    d = pd.concat(fr); d.columns = ['t', 'o', 'h', 'l', 'c', 'v']
    t = d['t'].astype('int64'); t = t.where(t < 1e14, t // 1000); d['t'] = pd.to_datetime(t, unit='ms')
    return d.drop_duplicates('t').sort_values('t').reset_index(drop=True)
def feats(d):
    d = d.copy(); c = d['c']
    tr = pd.concat([d['h'] - d['l'], (d['h'] - c.shift()).abs(), (d['l'] - c.shift()).abs()], axis=1).max(axis=1)
    d['atr'] = tr.rolling(14).mean()
    d['e20'] = c.ewm(span=20).mean(); d['e50'] = c.ewm(span=50).mean(); d['e200'] = c.ewm(span=200).mean()
    dl = c.diff(); up = dl.clip(lower=0).rolling(14).mean(); dn = (-dl.clip(upper=0)).rolling(14).mean()
    d['rsi'] = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    d['hi20'] = d['h'].rolling(20).max().shift(1); d['lo20'] = d['l'].rolling(20).min().shift(1)
    d['ret6'] = c / c.shift(6) - 1   # за сутки
    d['up'] = (d['e50'] > d['e200']).astype(int); d['dn'] = (d['e50'] < d['e200']).astype(int)
    return d
def sim(a, i0, side, tp, sl, hold):
    n = len(a['c']); e = a['o'][i0] * (1 + side * 1e-4)
    risk = abs(e - sl)
    if risk <= 0: return None
    last = min(i0 + hold, n - 1); ex = a['c'][last]; jx = last
    for j in range(i0, last + 1):
        if side == 1:
            if a['l'][j] <= sl: ex = sl; jx = j; break
            if tp and a['h'][j] >= tp: ex = tp; jx = j; break
        else:
            if a['h'][j] >= sl: ex = sl; jx = j; break
            if tp and a['l'][j] <= tp: ex = tp; jx = j; break
    return ((ex - e) * side - FEE * (e + ex)) / risk, jx
def run(d, kind, p):
    a = {k: d[k].values for k in d.columns if k != 't'}; n = len(d); out = []; i = 250
    while i < n - 20:
        at = a['atr'][i]; c = a['c'][i]; side = 0
        trend = (a['up'][i] == 1) if p['trend'] else True
        if kind == 'pullback':
            if a['up'][i] == 1 and a['rsi'][i] < p['rsi'] and trend: side = 1
            elif p['both'] and a['dn'][i] == 1 and a['rsi'][i] > 100 - p['rsi']: side = -1
        elif kind == 'breakout':
            if c > a['hi20'][i] and trend: side = 1
            elif p['both'] and c < a['lo20'][i] and a['dn'][i] == 1: side = -1
        elif kind == 'dip':
            if a['ret6'][i] < -p['drop'] and trend: side = 1
            elif p['both'] and a['ret6'][i] > p['drop'] and a['dn'][i] == 1: side = -1
        if side == 0 or not np.isfinite(at): i += 1; continue
        e = a['o'][i + 1]
        r = sim(a, i + 1, side, e + side * p['tp'] * at if p['tp'] else None, e - side * p['sl'] * at, p['hold'])
        if r is None: i += 1; continue
        out.append((d['t'].values[i], r[0])); i = r[1] + 1
    return out
def summ(tr):
    if len(tr) < 20: return None
    R = np.array([x[1] for x in tr]); n = len(R); pf = R[R > 0].sum() / max(1e-9, -R[R < 0].sum())
    return {'n': n, 'avgR': round(float(R.mean()), 3), 'sumR': round(float(R.sum()), 1), 'win': round(float((R > 0).mean() * 100), 1), 'pf': round(float(pf), 2), 't': round(float(R.mean() / (R.std() / np.sqrt(n))), 2) if R.std() > 0 else 0}
def main():
    data = {k: feats(load(s)) for k, s in COINS.items()}
    for k, d in data.items(): print(k, d.t.iloc[0], d.t.iloc[-1], len(d), flush=True)
    rows = []
    G = {'pullback': dict(rsi=[30, 40, 45], tp=[0, 2.0], sl=[1.5, 2.5], hold=[12, 18], trend=[True], both=[False, True]),
         'breakout': dict(tp=[0, 3.0], sl=[1.5, 2.5], hold=[12, 18], trend=[True, False], both=[False, True]),
         'dip': dict(drop=[0.03, 0.05, 0.08], tp=[0, 2.0], sl=[1.5, 2.5], hold=[12, 18], trend=[True, False], both=[False, True])}
    for kind, g in G.items():
        keys = list(g)
        for vals in itertools.product(*[g[k] for k in keys]):
            p = dict(zip(keys, vals)); allt = []
            for k, d in data.items(): allt += [(*x, k) for x in run(d, kind, p)]
            tr = [x for x in allt if pd.Timestamp(x[0]) < SPLIT]; te = [x for x in allt if pd.Timestamp(x[0]) >= SPLIT]
            rows.append({'name': kind, 'cfg': p, 'train': summ(tr), 'test': summ(te), 'tr': tr, 'te': te})
    json.dump([{k: v for k, v in r.items() if k not in ('tr', 'te')} for r in rows], open('backtest/v9.json', 'w'))
    for kind in G:
        rs = [r for r in rows if r['name'] == kind and r['train'] and r['test'] and r['train']['n'] >= 40]
        if not rs: print(kind, 'мало сделок'); continue
        rs.sort(key=lambda r: -r['train']['avgR'])
        print(f'\n=== {kind}: конфигов {len(rs)}; в плюсе: обучение {np.mean([r["train"]["avgR"]>0 for r in rs])*100:.0f}% проверка {np.mean([r["test"]["avgR"]>0 for r in rs])*100:.0f}%; топ-10 avgR: train {np.mean([r["train"]["avgR"] for r in rs[:10]]):.3f} test {np.mean([r["test"]["avgR"] for r in rs[:10]]):.3f}')
        for r in rs[:4]: print(r['cfg'], 'TRAIN', r['train'], 'TEST', r['test'])
        b = rs[0]; by = {}
        for x in sorted(b['tr'] + b['te'], key=lambda x: x[0]): by.setdefault(pd.Timestamp(x[0]).year, []).append(x)
        print('лучшая по годам:', {y: (summ(v) or {}).get('avgR') for y, v in by.items()})
main()
