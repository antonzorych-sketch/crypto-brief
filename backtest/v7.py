"""v7: S&P 500 (^GSPC, с 1950) тренд-ансамбль с плечом; кэш под ставку T-bill, заём по ставке+0.5%."""
import json
import numpy as np, pandas as pd, yfinance as yf
PER = {'1950-79': ('1950', '1979'), '1980-99': ('1980', '1999'), '2000-12': ('2000', '2012'), '2013-26': ('2013', '2026')}
def get(t):
    d = yf.download(t, start='1950-01-01', auto_adjust=False, progress=False)
    d.columns = [c[0] if isinstance(c, tuple) else c for c in d.columns]; return d.rename(columns=str.lower)
d = get('^GSPC').dropna(subset=['close'])
try:
    irx = get('^IRX')['close'].reindex(d.index).ffill().bfill() / 100 / 252
except Exception: irx = pd.Series(0.03 / 252, index=d.index)
irx = irx.fillna(0.03 / 252)
c = d['close']; hi_, lo_ = d['high'].fillna(c), d['low'].fillna(c)
r = c.pct_change()
S = {}
for n in (20, 50, 100, 200, 252): S[f'mom{n}'] = np.sign(c / c.shift(n) - 1)
for f, s in ((10, 40), (20, 100), (50, 200)): S[f'ema{f}/{s}'] = np.sign(c.ewm(span=f).mean() - c.ewm(span=s).mean())
for n in (20, 55, 100):
    hi = hi_.rolling(n).max().shift(1); lo = lo_.rolling(n).min().shift(1)
    st = pd.Series(np.nan, index=c.index); st[c > hi] = 1; st[c < lo] = -1; S[f'don{n}'] = st.ffill().fillna(0)
S['sma200'] = np.sign(c - c.rolling(200).mean())
S = pd.DataFrame(S)
ens = S.drop(columns='sma200').mean(axis=1).clip(lower=0)
vol = r.rolling(30).std() * np.sqrt(252)
def run(sig, L, voltarget=None, cost=2e-4, spread=0.005 / 252):
    pos = sig.clip(lower=0)
    pos = pos * ((voltarget / vol).clip(upper=L) if voltarget else L)
    p = pos.shift(1)
    fin = np.where(p > 1, -(p - 1) * (irx + spread), (1 - p).clip(lower=0) * irx)
    ret = p * r + fin - pos.diff().abs().fillna(0) * cost
    return ret.dropna()
def st(x):
    x = x.dropna()
    if len(x) < 100: return None
    eq = (1 + x).cumprod(); y = len(x) / 252
    return {'ann': round((eq.iloc[-1] ** (1 / y) - 1) * 100, 1), 'sharpe': round(x.mean() / x.std() * np.sqrt(252), 2), 'maxDD': round((eq / eq.cummax() - 1).min() * 100, 1), 'worstDay': round(x.min() * 100, 1)}
def ps(x): o = {k: st(x[a:b]) for k, (a, b) in PER.items()}; o['all'] = st(x); return o
res = {}
bh = r.dropna(); res['BH 1x'] = ps(bh)
for L in (2, 3): res[f'BH {L}x (daily)'] = ps(run(pd.Series(1.0, index=c.index), L))
for nm, sg in (('ENS', ens), ('SMA200', S['sma200'].clip(lower=0)), ('mom252', S['mom252'].clip(lower=0)), ('ema50/200', S['ema50/200'].clip(lower=0))):
    for L in (1, 2, 3): res[f'{nm} {L}x'] = ps(run(sg, L))
for L in (2, 3): res[f'ENS volTarget15 max{L}x'] = ps(run(ens, L, 0.15))
print('%-24s' % 'вариант', '  '.join('%22s' % p for p in list(PER) + ['all']))
for k, v in res.items():
    print('%-24s' % k, '  '.join(('%5.1f%% sh%5.2f dd%6.1f' % (s['ann'], s['sharpe'], s['maxDD'])) if s else '%22s' % '-' for s in (v[p] for p in list(PER) + ['all'])))
print('worst day ENS 3x', st(run(ens, 3))['worstDay'], 'BH', st(bh)['worstDay'])
print('now: ens=%.2f price=%.0f sma200=%.0f' % (ens.iloc[-1], c.iloc[-1], c.rolling(200).mean().iloc[-1]), 'sig', {k: float(v) for k, v in S.iloc[-1].items()})
eq = {k: (1 + x).cumprod().resample('M').last().round(4) for k, x in (('ENS 2x', run(ens, 2)), ('ENS 1x', run(ens, 1)), ('BH', bh))}
json.dump({'res': res, 'eq': {k: [[int(t.value // 10**6), float(v)] for t, v in s.dropna().items()] for k, s in eq.items()}, 'now': {k: float(v) for k, v in S.iloc[-1].items()}, 'ens': float(ens.iloc[-1])}, open('backtest/v7.json', 'w'))
