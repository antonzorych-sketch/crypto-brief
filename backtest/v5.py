"""v5: тот же трендовый ансамбль на ETF (акции, облигации, золото) с 2000 года. Только лонг, таргет вол 12%."""
import io, json, sys, urllib.request
import numpy as np, pandas as pd
TICK = ['SPY', 'QQQ', 'IWM', 'TLT', 'GLD']
COST = 2.0 / 1e4; TARGET = 0.12; MAXLEV = 1.0
PER = {'2000-09': ('2000-01-01', '2009-12-31'), '2010-19': ('2010-01-01', '2019-12-31'), '2020-26': ('2020-01-01', '2026-12-31')}

def load(t):
    try:
        import yfinance as yf
        d = yf.download(t, start='1999-01-01', auto_adjust=True, progress=False)
        d.columns = [c[0] if isinstance(c, tuple) else c for c in d.columns]
        d = d.rename(columns=str.lower)[['open', 'high', 'low', 'close']]
        if len(d) > 1000: return d.dropna()
    except Exception as e:
        print('yf fail', t, e)
    u = f'https://stooq.com/q/d/l/?s={t.lower()}.us&i=d'
    d = pd.read_csv(io.BytesIO(urllib.request.urlopen(u, timeout=60).read()), parse_dates=['Date'], index_col='Date')
    return d.rename(columns=str.lower)[['open', 'high', 'low', 'close']].dropna()

def sig_all(d):
    c = d['close']; out = {}
    for n in (20, 50, 100, 200): out[f'mom{n}'] = np.sign(c / c.shift(n) - 1)
    for f, s in ((10, 40), (20, 100), (50, 200)): out[f'ema{f}/{s}'] = np.sign(c.ewm(span=f).mean() - c.ewm(span=s).mean())
    for n in (20, 55, 100):
        hi = d['high'].rolling(n).max().shift(1); lo = d['low'].rolling(n).min().shift(1)
        st = pd.Series(np.nan, index=c.index); st[c > hi] = 1; st[c < lo] = -1
        out[f'don{n}'] = st.ffill().fillna(0)
    return pd.DataFrame(out)

def run(d, sig, volt=True):
    r = d['close'].pct_change()
    vol = r.rolling(30).std() * np.sqrt(252)
    size = (TARGET / vol).clip(upper=MAXLEV) if volt else 1.0
    pos = sig.clip(lower=0) * size
    return (pos.shift(1) * r - pos.diff().abs().fillna(0) * COST).dropna()

def stats(r):
    r = r.dropna()
    if len(r) < 60: return None
    eq = (1 + r).cumprod(); y = len(r) / 252
    return {'ann': round((eq.iloc[-1] ** (1 / y) - 1) * 100, 1), 'sharpe': round(r.mean() / r.std() * np.sqrt(252), 2) if r.std() > 0 else 0,
            'maxDD': round((eq / eq.cummax() - 1).min() * 100, 1), 'ret': round((eq.iloc[-1] - 1) * 100, 1)}

def ps(r):
    o = {k: stats(r[a:b]) for k, (a, b) in PER.items()}; o['all'] = stats(r); return o

def main():
    data = {t: load(t) for t in TICK}
    for t, d in data.items(): print(t, d.index[0].date(), d.index[-1].date(), len(d))
    sg = {t: sig_all(d) for t, d in data.items()}
    names = list(sg[TICK[0]].columns)
    V = {n: (lambda s, n=n: s[n]) for n in names}
    V['ENSEMBLE'] = lambda s: s.mean(axis=1)
    V['ENS_mom'] = lambda s: s[[c for c in s.columns if c.startswith('mom')]].mean(axis=1)
    V['ENS_ema'] = lambda s: s[[c for c in s.columns if c.startswith('ema')]].mean(axis=1)
    V['ENS_don'] = lambda s: s[[c for c in s.columns if c.startswith('don')]].mean(axis=1)
    res = {}; curves = {}
    for vn, fn in V.items():
        for volt in (True, False):
            rets = {t: run(data[t], fn(sg[t]), volt) for t in TICK}
            port = pd.concat(rets, axis=1).dropna(how='all').fillna(0).mean(axis=1)
            k = f'{vn}|{"vol" if volt else "flat"}'
            res[k] = {'port': ps(port), 'assets': {t: ps(v) for t, v in rets.items()}}; curves[k] = port
    bh = {t: d['close'].pct_change().dropna() for t, d in data.items()}
    res['BUYHOLD_eq'] = {'port': ps(pd.concat(bh, axis=1).fillna(0).mean(axis=1))}
    res['BUYHOLD_SPY'] = {'port': ps(bh['SPY'])}
    # 60/40 SPY+TLT
    res['6040'] = {'port': ps(0.6 * bh['SPY'] + 0.4 * bh['TLT'])}
    print('\n%-22s %s' % ('вариант', '  '.join(f'{p:>22}' for p in list(PER) + ['all'])))
    for k, v in sorted(res.items(), key=lambda kv: -(kv[1]['port']['all'] or {'sharpe': -9})['sharpe']):
        row = ['%5.1f%% sh%5.2f dd%6.1f' % (s['ann'], s['sharpe'], s['maxDD']) if s else '-' for s in (v['port'][p] for p in list(PER) + ['all'])]
        print('%-22s %s' % (k, '  '.join(row)))
    for t in TICK:
        print(t, 'ENSEMBLE|vol', res['ENSEMBLE|vol']['assets'][t]['all'], 'BH', stats(bh[t]))
    eq = {k: (1 + curves[k]).cumprod().resample('W').last().round(4) for k in ('ENSEMBLE|vol', 'ENSEMBLE|flat')}
    eq['BUYHOLD_SPY'] = (1 + bh['SPY']).cumprod().resample('W').last().round(4)
    json.dump({'res': res, 'eq': {k: [[int(t.value // 10**6), float(x)] for t, x in v.dropna().items()] for k, v in eq.items()},
               'now': {t: {n: float(sg[t][n].iloc[-1]) for n in names} for t in TICK}, 'periods': PER}, open('backtest/v5.json', 'w'))
    print('saved')
main()
