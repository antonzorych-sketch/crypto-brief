"""v10: дневной трендовый ансамбль (v4) + свинг-пробой 4ч (v9). Совместный портфель, честная проверка."""
import json, sys
import numpy as np, pandas as pd
sys.path.insert(0, 'backtest')
import v4, v9
RISK = 0.01   # доля капитала под риск на одну свинг-сделку
def main():
    # дневной тренд (лонг, вол-таргет), плечо 1 (макс 2× на монету внутри, как в v4)
    dd = {k: v4.load(s) for k, s in v4.COINS.items()}
    sg = {k: v4.sig_all(d).mean(axis=1) for k, d in dd.items()}
    trend = pd.concat({k: v4.run_coin(dd[k], sg[k], True, True) for k in dd}, axis=1).dropna(how='all').fillna(0).mean(axis=1)
    trend.index = pd.to_datetime(trend.index).normalize()
    # свинг: пробой 20 свечей в тренде, только лонг
    sd = {k: v9.feats(v9.load(s)) for k, s in v9.COINS.items()}
    sleeves = {}
    for name, p in {'swing sl1.5': dict(tp=0, sl=1.5, hold=18, trend=True, both=False), 'swing sl2.5': dict(tp=0, sl=2.5, hold=18, trend=True, both=False)}.items():
        pnl = {}
        for k, d in sd.items():
            for t, R, tx in v9.run(d, 'breakout', p):
                day = pd.Timestamp(tx).normalize(); pnl[day] = pnl.get(day, 0) + R * RISK
        sleeves[name] = pd.Series(pnl).sort_index()
    idx = trend.index
    PER = {'2020-22': ('2020', '2022'), '2023-24': ('2023', '2024'), '2025-26': ('2025', '2026'), 'all': ('2020', '2026')}
    res = {}
    for sn, sl in sleeves.items():
        sl = sl.reindex(idx).fillna(0)
        print(sn, 'корреляция с трендом: %.2f' % np.corrcoef(trend.values, sl.values)[0, 1])
        for w_t, w_s, nm in ((1, 0, 'только тренд'), (0, 1, 'только свинг'), (1, 1, 'тренд + свинг'), (0.7, 1, 'тренд 70% + свинг')):
            r = w_t * trend + w_s * sl
            res[f'{sn} | {nm}'] = {p: v4.stats(r[a:b]) for p, (a, b) in PER.items()}
    print('\n%-36s' % 'вариант', '  '.join('%22s' % p for p in PER))
    for k, v in res.items():
        print('%-36s' % k, '  '.join(('%5.1f%% sh%5.2f dd%6.1f' % (s['ann'], s['sharpe'], s['maxDD'])) if s else '%22s' % '-' for s in v.values()))
    json.dump(res, open('backtest/v10.json', 'w'))
main()
