"""v13: портфель из 4 источников: крипто-тренд (плечо по уверенности), свинг-пробой 4ч, S&P SMA200, фандинг при перегреве. + контроль просадки."""
import json, sys
import numpy as np, pandas as pd, yfinance as yf
sys.path.insert(0, 'backtest')
import v4, v9, v11
RISK = 0.01
def stats(r, per=365):
    r = r.dropna()
    if len(r) < 60: return None
    eq = (1 + r).cumprod(); y = len(r) / per
    return {'ann': round((eq.iloc[-1] ** (1 / y) - 1) * 100, 1), 'mo': round(((eq.iloc[-1]) ** (30.4 / len(r)) - 1) * 100, 2), 'sharpe': round(r.mean() / r.std() * np.sqrt(per), 2) if r.std() > 0 else 0,
            'maxDD': round((eq / eq.cummax() - 1).min() * 100, 1)}
def ddscale(r, lo=-0.10, hi=-0.20, floor=0.25):
    out = []; eq = 1.0; pk = 1.0
    for x in r.values:
        dd = eq / pk - 1
        s = 1.0 if dd >= lo else (floor if dd <= hi else 1 - (1 - floor) * (lo - dd) / (lo - hi))
        v = x * s; out.append(v); eq *= 1 + v; pk = max(pk, eq)
    return pd.Series(out, index=r.index)
def main():
    dd = {k: v4.load(s) for k, s in v4.COINS.items()}
    sg = {k: v4.sig_all(d).mean(axis=1) for k, d in dd.items()}
    def trend(hi_mult, thr=0.8):
        rets = {}
        for k, d in dd.items():
            r = d['c'].pct_change(); vol = r.rolling(30).std() * np.sqrt(365)
            size = (v4.TARGET_VOL / vol).clip(upper=v4.MAXLEV)
            s = sg[k].clip(lower=0); mult = pd.Series(1.0, index=s.index); mult[s >= thr] = hi_mult
            pos = s * size * mult
            rets[k] = (pos.shift(1) * r - pos.diff().abs().fillna(0) * v4.COST).dropna()
        x = pd.concat(rets, axis=1).dropna(how='all').fillna(0).mean(axis=1); x.index = pd.to_datetime(x.index).normalize(); return x
    T1, T15, T2 = trend(1.0), trend(1.5), trend(2.0)
    idx = T1.index
    sd = {k: v9.feats(v9.load(s)) for k, s in v9.COINS.items()}
    pnl = {}
    for k, d in sd.items():
        for t, R, tx in v9.run(d, 'breakout', dict(tp=0, sl=1.5, hold=18, trend=True, both=False)):
            day = pd.Timestamp(tx).normalize(); pnl[day] = pnl.get(day, 0) + R * RISK
    SW = pd.Series(pnl).sort_index().reindex(idx).fillna(0)
    sp = yf.download('^GSPC', start='2019-01-01', auto_adjust=False, progress=False); sp.columns = [c[0] if isinstance(c, tuple) else c for c in sp.columns]
    c = sp['Close']; sig = (c > c.rolling(200).mean()).astype(float).shift(1)
    spr = (c.pct_change() * sig - sig.diff().abs().fillna(0) * 2e-4).dropna()
    SP = spr.reindex(idx).fillna(0)
    F = {k: v11.load(s) for k, s in list(v11.COINS.items())[:2]}
    fp = pd.concat(F, axis=1).mean(axis=1); fp.index = pd.to_datetime(fp.index).normalize()
    on = (fp.rolling(30).mean().shift(1) * 365 > 0.10).astype(float)
    FU = ((on.shift(1) * fp - on.diff().abs().fillna(0) / 2 * v11.CYCLE_COST) * 0.67).reindex(idx).fillna(0)
    comp = {'тренд 1×': T1, 'тренд 1,5× на сильных': T15, 'тренд 2× на сильных': T2, 'свинг': SW, 'S&P SMA200': SP, 'фандинг>10%': FU}
    PER = {'2020-22': ('2020', '2022'), '2023-24': ('2023', '2024'), '2025-26': ('2025', '2026'), 'all': ('2020', '2026')}
    print('Корреляции (дневные):'); print(pd.DataFrame(comp).corr().round(2).to_string())
    print('\nОтдельные источники:')
    for k, v in comp.items(): print('%-24s' % k, ' | '.join(f"{p}: {s['ann']}%/dd{s['maxDD']}" for p, (a, b) in PER.items() if (s := stats(v[a:b]))))
    res = {}
    mixes = {'A: тренд1× + свинг + S&P + фандинг': (T1, 0.4, 1, 0.3, 0.3), 'B: тренд1,5× +...': (T15, 0.4, 1, 0.3, 0.3), 'C: тренд2× +...': (T2, 0.4, 1, 0.3, 0.3),
             'D: тренд1,5× (вес 0.6) + свинг + S&P 0.3 + фандинг 0.3': (T15, 0.6, 1, 0.3, 0.3)}
    print('\nПортфели:')
    for nm, (t, wt, ws, wp, wf) in mixes.items():
        p = wt * t + ws * SW + wp * SP + wf * FU; p2 = ddscale(p)
        for lab, x in ((nm, p), (nm + ' + контроль просадки', p2)):
            res[lab] = {pn: stats(x[a:b]) for pn, (a, b) in PER.items()}
            print('%-70s' % lab, ' | '.join(f"{pn}: {s['ann']}%/{s['mo']}м/dd{s['maxDD']}" for pn, s in res[lab].items() if s))
    json.dump(res, open('backtest/v13.json', 'w'))
main()
