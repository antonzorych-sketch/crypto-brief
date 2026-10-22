"""v11: дельта-нейтральный фандинг-carry (лонг спот + шорт перп) по истории фандинга Binance 2020–2026."""
import io, json, urllib.request, zipfile
from concurrent.futures import ThreadPoolExecutor
import numpy as np, pandas as pd
BASE = 'https://data.binance.vision/data/futures/um/monthly/fundingRate'
COINS = {'BTC': 'BTCUSDT', 'ETH': 'ETHUSDT', 'SOL': 'SOLUSDT'}
CYCLE_COST = 0.0028   # вход+выход: 4 ноги (спот+перп) ≈ 23 б.п. + проскальзывание
def fetch(u):
    for _ in range(3):
        try: return urllib.request.urlopen(u, timeout=60).read()
        except Exception: pass
def load(sym):
    ms = []; y, m = 2020, 1
    while (y, m) <= (2026, 9):
        ms.append(f'{y}-{m:02d}'); m += 1
        if m == 13: y, m = y + 1, 1
    with ThreadPoolExecutor(8) as p: bs = list(p.map(fetch, [f'{BASE}/{sym}/{sym}-fundingRate-{x}.zip' for x in ms]))
    fr = []
    for b in bs:
        if not b: continue
        z = zipfile.ZipFile(io.BytesIO(b)); df = pd.read_csv(z.open(z.namelist()[0]))
        fr.append(df)
    d = pd.concat(fr)
    d['t'] = pd.to_datetime(d['calc_time'].astype('int64'), unit='ms'); d['rate'] = d['last_funding_rate'].astype(float)
    s = d.drop_duplicates('t').sort_values('t').set_index('t')['rate']
    return s.resample('D').sum()   # дневной фандинг (доля от нотионала)
def stats(r):
    r = r.dropna()
    if len(r) < 30: return None
    eq = (1 + r).cumprod(); y = len(r) / 365
    return {'ann': round((eq.iloc[-1] ** (1 / y) - 1) * 100, 1), 'sharpe': round(r.mean() / r.std() * np.sqrt(365), 2) if r.std() > 0 else 0, 'maxDD': round((eq / eq.cummax() - 1).min() * 100, 2), 'negDays': round((r < 0).mean() * 100, 1)}
def main():
    F = {k: load(s) for k, s in COINS.items()}
    for k, f in F.items(): print(k, f.index[0].date(), f.index[-1].date(), 'ср. годовой фандинг %.1f%%' % (f.mean() * 365 * 100))
    PER = {'2020': ('2020', '2020'), '2021': ('2021', '2021'), '2022': ('2022', '2022'), '2023': ('2023', '2023'), '2024': ('2024', '2024'), '2025': ('2025', '2025'), '2026': ('2026', '2026'), 'all': ('2020', '2026')}
    print('\nГодовой фандинг (шорт перпа получает), % годовых от нотионала:')
    for k, f in F.items(): print(k, {y: round(f[a:b].mean() * 365 * 100, 1) for y, (a, b) in PER.items()})
    port = pd.concat(F, axis=1).dropna(how='all').fillna(0).mean(axis=1)
    out = {}
    def strat(name, f, thr=None, win=7):
        if thr is None: pos = pd.Series(1.0, index=f.index)
        else: pos = (f.rolling(win).mean().shift(1) * 365 > thr).astype(float).fillna(0)
        r = pos.shift(1).fillna(0) * f - pos.diff().abs().fillna(0) / 2 * CYCLE_COST
        if thr is None: r.iloc[0] -= CYCLE_COST / 2
        return r
    for nm, thr in (('всегда в позиции', None), ('только если фандинг>5% год', 0.05), ('только если >10% год', 0.10), ('только если >0', 0.0)):
        r = strat(nm, port, thr)
        out[nm] = {y: stats(r[a:b]) for y, (a, b) in PER.items()}
    print('\nПортфель BTC+ETH+SOL (доходность на нотионал хеджа):')
    for nm, v in out.items():
        print(nm, ' | '.join(f"{y}: {s['ann']}%" for y, s in v.items() if s), '| all Шарп', out[nm]['all']['sharpe'], 'maxDD', out[nm]['all']['maxDD'])
    # на капитал: перп 2× → нотионал = капитал/1.5
    print('\nна весь капитал (перп 2×, нотионал = 2/3 капитала): умножить на 0.67')
    # отрицательные недели
    w = port.resample('W').sum() * 52 * 100
    print('недель с отрицательным фандингом: %.0f%%; худшая неделя (год.): %.0f%%' % ((w < 0).mean() * 100, w.min()))
    json.dump(out, open('backtest/v11.json', 'w'))
main()
