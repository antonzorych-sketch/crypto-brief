"""v15: предсказывает ли поток ордеров (дельта тейкеров, дисбаланс стакана ±1%) движение цены на 1/5/15 мин вперёд после комиссий?
BTC, ETH, 2023-2026. Обучение 2023–24, проверка 2025–26. Смотрим валовое ожидание по децилям в б.п. против издержек."""
import io, json, urllib.request, zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
import numpy as np, pandas as pd
from scipy.stats import spearmanr
from sklearn.ensemble import HistGradientBoostingRegressor
COINS = {'BTC': 'BTCUSDT', 'ETH': 'ETHUSDT'}
B = 'https://data.binance.vision/data/futures/um'
SPLIT = pd.Timestamp('2025-01-01')
def fetch(u):
    for _ in range(3):
        try: return urllib.request.urlopen(u, timeout=90).read()
        except Exception: pass
def rd(b, header=None):
    z = zipfile.ZipFile(io.BytesIO(b)); return pd.read_csv(z.open(z.namelist()[0]), header=header)
def klines(sym):
    ms = []; y, m = 2023, 1
    while (y, m) <= (2026, 9):
        ms.append(f'{y}-{m:02d}'); m += 1
        if m == 13: y, m = y + 1, 1
    with ThreadPoolExecutor(8) as p: bs = list(p.map(fetch, [f'{B}/monthly/klines/{sym}/1m/{sym}-1m-{x}.zip' for x in ms]))
    fr = []
    for b in bs:
        if not b: continue
        d = rd(b)
        if not str(d.iloc[0, 0]).isdigit(): d = d.iloc[1:]
        fr.append(d.iloc[:, [0, 4, 7, 8, 10]].astype(float))
    d = pd.concat(fr); d.columns = ['t', 'c', 'q', 'n', 'tbq']
    t = d['t'].astype('int64'); t = t.where(t < 1e14, t // 1000); d['t'] = pd.to_datetime(t, unit='ms')
    return d.drop_duplicates('t').sort_values('t').set_index('t')
def book(sym):
    days = []; d0 = date(2023, 1, 1)
    while d0 <= date(2026, 9, 30): days.append(d0); d0 += timedelta(1)
    def one(d):
        b = fetch(f'{B}/daily/bookDepth/{sym}/{sym}-bookDepth-{d:%Y-%m-%d}.zip')
        if not b: return None
        x = rd(b, 0)
        x = x[x['percentage'].isin([-1, 1])]
        x['t'] = pd.to_datetime(x['timestamp']).dt.floor('min')
        p = x.pivot_table(index='t', columns='percentage', values='depth', aggfunc='last')
        if -1 not in p or 1 not in p: return None
        return ((p[-1] - p[1]) / (p[-1] + p[1])).rename('bimb')
    with ThreadPoolExecutor(16) as p: r = [x for x in p.map(one, days) if x is not None]
    return pd.concat(r).groupby(level=0).last()
def feats(k, bi):
    d = pd.DataFrame(index=k.index)
    sgn = (2 * k['tbq'] - k['q'])
    d['d1'] = sgn / k['q'].replace(0, np.nan)
    for w in (5, 15, 60): d[f'd{w}'] = sgn.rolling(w).sum() / k['q'].rolling(w).sum()
    r = k['c'].pct_change()
    for w in (1, 5, 15, 60): d[f'r{w}'] = k['c'].pct_change(w) * 1e4
    d['vol15'] = r.rolling(15).std() * 1e4
    d['qz'] = k['q'] / k['q'].rolling(1440).mean()
    d['nz'] = k['n'] / k['n'].rolling(1440).mean()
    d['bimb'] = bi.reindex(d.index).ffill(limit=5)
    d['bimb_ch'] = d['bimb'].diff(5)
    for h in (1, 5, 15): d[f'y{h}'] = (k['c'].shift(-h) / k['c'] - 1) * 1e4
    return d
def main():
    allf = []
    for c, s in COINS.items():
        k = klines(s); bi = book(s); print(c, len(k), 'book rows', len(bi), flush=True)
        f = feats(k, bi); f['coin'] = c; allf.append(f)
    D = pd.concat(allf).dropna(); print('строк', len(D))
    X = ['d1', 'd5', 'd15', 'd60', 'r1', 'r5', 'r15', 'r60', 'vol15', 'qz', 'nz', 'bimb', 'bimb_ch']
    tr = D[D.index < SPLIT]; te = D[D.index >= SPLIT]
    out = {}
    for h in (1, 5, 15):
        print(f'\n=== горизонт {h} мин ===')
        for x in X: print('IC %-8s train %+.4f test %+.4f' % (x, spearmanr(tr[x], tr[f'y{h}'])[0], spearmanr(te[x], te[f'y{h}'])[0]))
        m = HistGradientBoostingRegressor(max_depth=4, max_iter=150, learning_rate=0.05).fit(tr[X], tr[f'y{h}'])
        p = pd.Series(m.predict(te[X]), index=te.index); y = te[f'y{h}']
        print('IC модели на проверке: %+.4f' % spearmanr(p, y)[0])
        q = pd.qcut(p.rank(method='first'), 10, labels=False)
        dec = y.groupby(q.values).mean()
        print('средний ход по децилям прогноза (б.п.):', [round(float(v), 2) for v in dec.values])
        edge = (dec.iloc[-1] - dec.iloc[0]) / 2
        print('валовое ожидание на сделку: лонг топ-дециль %.2f б.п., шорт нижний дециль %.2f б.п.' % (dec.iloc[-1], -dec.iloc[0]))
        # топ-1% самых уверенных
        hi = p >= p.quantile(0.99); lo = p <= p.quantile(0.01)
        print('топ-1%% прогнозов: лонг %.2f б.п. (n=%d), шорт %.2f б.п. (n=%d)' % (y[hi].mean(), hi.sum(), -y[lo].mean(), lo.sum()))
        out[h] = {'ic': float(spearmanr(p, y)[0]), 'deciles': [float(v) for v in dec.values], 'top1_long': float(y[hi].mean()), 'top1_short': float(-y[lo].mean())}
    print('\nиздержки: taker вход+выход ≈ 11 б.п., maker вход + taker выход ≈ 7 б.п., maker оба ≈ 3 б.п. (без учёта неисполненных лимиток)')
    json.dump(out, open('backtest/v15.json', 'w'))
main()
