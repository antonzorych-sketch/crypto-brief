"""v8: скальпинг BTC/ETH/SOL на 5-минутках, 2023-2026. Параметры на 2023–24, проверка 2025–26.
Комиссии Hyperliquid: taker 4.5 б.п., maker 1.5 б.п., проскальзывание 1 б.п. на taker-ногу. Лимитка исполняется только если цена прошла сквозь уровень."""
import io, itertools, json, urllib.request, zipfile
from concurrent.futures import ThreadPoolExecutor
import numpy as np, pandas as pd
BASE = 'https://data.binance.vision/data/futures/um/monthly/klines'
COINS = {'BTC': 'BTCUSDT', 'ETH': 'ETHUSDT', 'SOL': 'SOLUSDT'}
TAKER, MAKER, SLIP = 4.5e-4, 1.5e-4, 1e-4
SPLIT = pd.Timestamp('2025-01-01')

def fetch(u):
    for _ in range(3):
        try: return urllib.request.urlopen(u, timeout=90).read()
        except Exception: pass

def load(sym):
    ms = []; y, m = 2023, 1
    while (y, m) <= (2026, 9):
        ms.append(f'{y}-{m:02d}'); m += 1
        if m == 13: y, m = y + 1, 1
    with ThreadPoolExecutor(8) as p: bs = list(p.map(fetch, [f'{BASE}/{sym}/5m/{sym}-5m-{x}.zip' for x in ms]))
    fr = []
    for b in bs:
        if not b: continue
        z = zipfile.ZipFile(io.BytesIO(b)); df = pd.read_csv(z.open(z.namelist()[0]), header=None)
        if not str(df.iloc[0, 0]).isdigit(): df = df.iloc[1:]
        fr.append(df.iloc[:, [0, 1, 2, 3, 4, 7]].astype(float))
    d = pd.concat(fr); d.columns = ['t', 'o', 'h', 'l', 'c', 'v']
    t = d['t'].astype('int64'); t = t.where(t < 1e14, t // 1000)
    d['t'] = pd.to_datetime(t, unit='ms')
    return d.drop_duplicates('t').sort_values('t').reset_index(drop=True)

def feats(d):
    d = d.copy(); c = d['c']
    tr = pd.concat([d['h'] - d['l'], (d['h'] - c.shift()).abs(), (d['l'] - c.shift()).abs()], axis=1).max(axis=1)
    d['atr'] = tr.rolling(24).mean()
    d['z'] = (c - c.rolling(48).mean()) / c.rolling(48).std()
    d['vr'] = d['v'] / d['v'].rolling(288).mean()
    d['rng'] = (d['h'] - d['l']) / d['atr']
    d['pos'] = (c - d['l']) / (d['h'] - d['l']).replace(0, np.nan)
    d['hour'] = d['t'].dt.hour + d['t'].dt.minute / 60
    d['day'] = d['t'].dt.floor('D')
    return d

def sim(a, i0, side, entry_limit, entry_px, tp, sl, hold, maker_tp=True, end_idx=None):
    """вход в бар i0 (цена entry_px), выход по tp/sl/времени. Возвращает (R, индекс выхода)"""
    n = len(a['c'])
    if entry_limit:
        if side == 1 and not a['l'][i0] < entry_px: return None
        if side == -1 and not a['h'][i0] > entry_px: return None
        e = entry_px; fe = MAKER
    else:
        e = a['o'][i0]; e = e * (1 + side * SLIP); fe = TAKER
    risk = abs(e - sl)
    if risk <= 0: return None
    last = min(i0 + hold, n - 1) if end_idx is None else min(end_idx, n - 1)
    ex = a['c'][last]; fx = TAKER; jx = last; ex = ex * (1 - side * SLIP)
    for j in range(i0, last + 1):
        h, l = a['h'][j], a['l'][j]
        if j == i0 and entry_limit: pass
        if side == 1:
            if l <= sl: ex = sl * (1 - SLIP); fx = TAKER; jx = j; break
            if tp is not None and h > tp: ex = tp; fx = MAKER if maker_tp else TAKER; jx = j; break
        else:
            if h >= sl: ex = sl * (1 + SLIP); fx = TAKER; jx = j; break
            if tp is not None and l < tp: ex = tp; fx = MAKER if maker_tp else TAKER; jx = j; break
    gross = (ex - e) * side
    cost = fe * e + fx * ex
    return (gross - cost) / risk, jx

def arr(d): return {k: d[k].values for k in ['o', 'h', 'l', 'c', 'atr', 'z', 'vr', 'rng', 'pos', 'hour']}

def run_mr(d, k, tpm, slm, lim, both):
    a = arr(d); n = len(d); out = []; i = 300
    while i < n - 14:
        z = a['z'][i]; at = a['atr'][i]
        if not np.isfinite(z) or not np.isfinite(at) or abs(z) < k or (z > 0 and not both): i += 1; continue
        side = 1 if z < 0 else -1
        px = a['c'][i]
        r = sim(a, i + 1, side, lim, px, px + side * tpm * at, px - side * slm * at, 12)
        if r is None: i += 1; continue
        out.append((d['t'].values[i], r[0])); i = r[1] + 1
    return out

def run_mom(d, k, vm, tpm, both):
    a = arr(d); n = len(d); out = []; i = 300
    while i < n - 14:
        at = a['atr'][i]
        if not (a['rng'][i] >= k and a['vr'][i] >= vm and np.isfinite(at)): i += 1; continue
        up = a['c'][i] > a['o'][i] and a['pos'][i] >= 0.75; dn = a['c'][i] < a['o'][i] and a['pos'][i] <= 0.25
        side = 1 if up else (-1 if dn and both else 0)
        if side == 0: i += 1; continue
        e = a['o'][i + 1]
        r = sim(a, i + 1, side, False, None, e + side * tpm * at, e - side * 1.0 * at, 12)
        if r is None: i += 1; continue
        out.append((d['t'].values[i], r[0])); i = r[1] + 1
    return out

def run_orb(d, rr, both, hold_end=20.0):
    out = []; a = arr(d)
    for day, g in d.groupby('day'):
        idx = g.index.values
        hrs = a['hour'][idx]
        rng = idx[(hrs >= 13.5) & (hrs < 14.0)]
        if len(rng) < 6: continue
        hi = a['h'][rng].max(); lo = a['l'][rng].min()
        aft = idx[(hrs >= 14.0) & (hrs < hold_end)]
        for j in aft:
            c = a['c'][j]
            side = 1 if c > hi else (-1 if c < lo and both else 0)
            if side == 0: continue
            e = a['o'][j + 1] if j + 1 < len(d) else c
            sl = lo if side == 1 else hi; risk = abs(e - sl)
            if risk <= 0: break
            r = sim(a, j + 1, side, False, None, e + side * rr * risk, sl, 999, end_idx=aft[-1] if aft[-1] > j else j + 1)
            if r is not None: out.append((d['t'].values[j], r[0]))
            break
    return out

def summ(tr):
    if len(tr) < 20: return None
    R = np.array([x[1] for x in tr]); n = len(R)
    pf = R[R > 0].sum() / max(1e-9, -R[R < 0].sum())
    return {'n': n, 'avgR': round(float(R.mean()), 3), 'sumR': round(float(R.sum()), 1), 'win': round(float((R > 0).mean() * 100), 1), 'pf': round(float(pf), 2),
            't': round(float(R.mean() / (R.std() / np.sqrt(n))), 2) if R.std() > 0 else 0}

def main():
    data = {k: feats(load(s)) for k, s in COINS.items()}
    for k, d in data.items(): print(k, d.t.iloc[0], d.t.iloc[-1], len(d), flush=True)
    rows = []
    def add(name, cfg, fn):
        allt = []
        for k, d in data.items(): allt += [(*x, k) for x in fn(d)]
        tr = [x for x in allt if pd.Timestamp(x[0]) < SPLIT]; te = [x for x in allt if pd.Timestamp(x[0]) >= SPLIT]
        rows.append({'name': name, 'cfg': cfg, 'train': summ(tr), 'test': summ(te)})
    for k, tpm, slm, lim, both in itertools.product([2.0, 2.5, 3.0], [0.5, 1.0], [1.0, 1.5], [True, False], [True, False]):
        add('MR', dict(k=k, tp=tpm, sl=slm, limit=lim, both=both), lambda d, k=k, tpm=tpm, slm=slm, lim=lim, both=both: run_mr(d, k, tpm, slm, lim, both))
    for k, vm, tpm, both in itertools.product([2.0, 3.0], [2.0, 3.0], [1.0, 2.0], [True, False]):
        add('MOM', dict(k=k, vm=vm, tp=tpm, both=both), lambda d, k=k, vm=vm, tpm=tpm, both=both: run_mom(d, k, vm, tpm, both))
    for rr, both in itertools.product([1.0, 1.5, 2.0, 3.0], [True, False]):
        add('ORB', dict(rr=rr, both=both), lambda d, rr=rr, both=both: run_orb(d, rr, both))
    json.dump(rows, open('backtest/v8.json', 'w'))
    for name in ('MR', 'MOM', 'ORB'):
        rs = [r for r in rows if r['name'] == name and r['train'] and r['test'] and r['train']['n'] >= 50]
        if not rs: print(name, 'мало сделок'); continue
        rs.sort(key=lambda r: -r['train']['avgR'])
        print(f'\n=== {name}: конфигов {len(rs)}; в плюсе: обучение {np.mean([r["train"]["avgR"]>0 for r in rs])*100:.0f}% проверка {np.mean([r["test"]["avgR"]>0 for r in rs])*100:.0f}%; среднее avgR топ-10: train {np.mean([r["train"]["avgR"] for r in rs[:10]]):.3f} test {np.mean([r["test"]["avgR"] for r in rs[:10]]):.3f}')
        for r in rs[:4]: print(r['cfg'], 'TRAIN', r['train'], 'TEST', r['test'])
main()
