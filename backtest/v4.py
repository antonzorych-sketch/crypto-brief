"""v4: дневные трендовые системы на длинной истории (2019/2020–2026), без подбора параметров.
Позиция ставится по закрытию дня t и держится день t+1. Издержки 4.5 б.п. на единицу оборота + 0.5 б.п. проскальзывания.
"""
import io, json, sys, zipfile, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date
import numpy as np, pandas as pd

BASE = 'https://data.binance.vision/data/futures/um/monthly/klines'
COINS = {'BTC': 'BTCUSDT', 'ETH': 'ETHUSDT', 'SOL': 'SOLUSDT'}
COST = 5.0 / 1e4
TARGET_VOL = 0.40   # годовая цель по волатильности на монету
MAXLEV = 2.0

def fetch(u):
    for _ in range(3):
        try:
            return urllib.request.urlopen(u, timeout=60).read()
        except Exception:
            pass
    return None

def load(sym):
    ms = []
    y, m = 2019, 9
    while (y, m) <= (2026, 10):
        ms.append(f'{y}-{m:02d}'); m += 1
        if m == 13: y, m = y + 1, 1
    urls = [f'{BASE}/{sym}/1d/{sym}-1d-{x}.zip' for x in ms]
    with ThreadPoolExecutor(8) as p: bs = list(p.map(fetch, urls))
    fr = []
    for b in bs:
        if not b: continue
        z = zipfile.ZipFile(io.BytesIO(b)); df = pd.read_csv(z.open(z.namelist()[0]), header=None)
        if not str(df.iloc[0, 0]).isdigit(): df = df.iloc[1:]
        fr.append(df.iloc[:, :6].astype(float))
    d = pd.concat(fr); d.columns = ['t', 'o', 'h', 'l', 'c', 'v']
    d['t'] = pd.to_datetime(d['t'].astype('int64'), unit='ms') if d['t'].iloc[0] < 1e14 else pd.to_datetime(d['t'].astype('int64') // 1000, unit='ms')
    return d.drop_duplicates('t').sort_values('t').set_index('t')

def sig_all(d):
    c = d['c']; out = {}
    for n in (20, 50, 100, 200):
        out[f'mom{n}'] = np.sign(c / c.shift(n) - 1)
    for f, s in ((10, 40), (20, 100), (50, 200)):
        out[f'ema{f}/{s}'] = np.sign(c.ewm(span=f).mean() - c.ewm(span=s).mean())
    for n in (20, 55, 100):
        hi = d['h'].rolling(n).max().shift(1); lo = d['l'].rolling(n).min().shift(1)
        st = pd.Series(np.nan, index=c.index); st[c > hi] = 1; st[c < lo] = -1
        out[f'don{n}'] = st.ffill().fillna(0)
    return pd.DataFrame(out)

def run_coin(d, sig, long_only, volt=True):
    r = d['c'].pct_change()
    vol = r.rolling(30).std() * np.sqrt(365)
    size = (TARGET_VOL / vol).clip(upper=MAXLEV) if volt else pd.Series(1.0, index=r.index)
    pos = sig.clip(lower=0) if long_only else sig
    pos = (pos * size).shift(0)
    turn = pos.diff().abs().fillna(0)
    ret = pos.shift(1) * r - turn * COST
    return ret.dropna()

def stats(ret):
    ret = ret.dropna()
    if len(ret) < 30: return None
    eq = (1 + ret).cumprod()
    yrs = len(ret) / 365
    ann = eq.iloc[-1] ** (1 / yrs) - 1
    sh = ret.mean() / ret.std() * np.sqrt(365) if ret.std() > 0 else 0
    dd = (eq / eq.cummax() - 1).min()
    return {'ann': round(ann * 100, 1), 'sharpe': round(sh, 2), 'maxDD': round(dd * 100, 1), 'ret': round((eq.iloc[-1] - 1) * 100, 1), 'days': len(ret)}

PER = {'2020-22': ('2020-01-01', '2022-12-31'), '2023-24': ('2023-01-01', '2024-12-31'), '2025-26': ('2025-01-01', '2026-12-31')}

def per_stats(ret):
    o = {}
    for k, (a, b) in PER.items():
        o[k] = stats(ret[a:b])
    o['all'] = stats(ret['2020-01-01':])
    return o

def main():
    data = {}
    for k, s in COINS.items():
        data[k] = load(s); print(k, data[k].index[0].date(), data[k].index[-1].date(), len(data[k]), flush=True)
    sigs = {k: sig_all(v) for k, v in data.items()}
    names = list(next(iter(sigs.values())).columns)
    res = {}
    # ансамбль = среднее всех сигналов (без выбора параметров)
    variants = {n: (lambda s, n=n: s[n]) for n in names}
    variants['ENSEMBLE'] = lambda s: s.mean(axis=1)
    variants['ENS_mom'] = lambda s: s[[c for c in s.columns if c.startswith('mom')]].mean(axis=1)
    variants['ENS_don'] = lambda s: s[[c for c in s.columns if c.startswith('don')]].mean(axis=1)
    variants['ENS_ema'] = lambda s: s[[c for c in s.columns if c.startswith('ema')]].mean(axis=1)
    curves = {}
    for vn, fn in variants.items():
        for lo in (True, False):
            for volt in (True, False):
                rets = {k: run_coin(data[k], fn(sigs[k]), lo, volt) for k in data}
                port = pd.concat(rets, axis=1).dropna(how='all').fillna(0).mean(axis=1)
                key = f'{vn}|{"long" if lo else "ls"}|{"vol" if volt else "flat"}'
                res[key] = {'port': per_stats(port), 'coins': {k: per_stats(v) for k, v in rets.items()}}
                curves[key] = port
    bh = pd.concat({k: v['c'].pct_change() for k, v in data.items()}, axis=1).mean(axis=1).dropna()
    res['BUYHOLD'] = {'port': per_stats(bh)}
    # сводка
    print('\n%-28s %s' % ('вариант', '   '.join(f'{p:>20}' for p in PER)))
    for k, v in sorted(res.items(), key=lambda kv: -(kv[1]['port']['2025-26'] or {'sharpe': -9})['sharpe']):
        row = []
        for p in PER:
            s = v['port'][p]
            row.append('%5.1f%% sh%5.2f dd%6.1f' % (s['ann'], s['sharpe'], s['maxDD']) if s else '-')
        print('%-28s %s' % (k, '   '.join(row)))
    # кривая ансамбля
    key = 'ENSEMBLE|ls|vol'; key2 = 'ENSEMBLE|long|vol'
    eq = {kk: ((1 + curves[kk]).cumprod().resample('W').last().round(4)) for kk in (key, key2)}
    eq['BUYHOLD'] = (1 + bh).cumprod().resample('W').last().round(4)
    out = {'generated': pd.Timestamp.utcnow().isoformat(), 'res': res,
           'eq': {k: [[int(t.value // 10**6), float(x)] for t, x in v.dropna().items()] for k, v in eq.items()},
           'now': {k: {n: float(sigs[k][n].iloc[-1]) for n in names} for k in data},
           'periods': PER, 'cost_bps': COST * 1e4, 'target_vol': TARGET_VOL}
    json.dump(out, open('backtest/v4.json', 'w'))
    print('saved')

if __name__ == '__main__':
    main()
