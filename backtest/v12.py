"""v12: фандинг-carry по альткоинам: лонг спот + шорт перп, ротация в монеты с высоким фандингом. Включает базисный риск (спот vs перп) и издержки ротации."""
import io, json, urllib.request, zipfile
from concurrent.futures import ThreadPoolExecutor
import numpy as np, pandas as pd
F = 'https://data.binance.vision/data/futures/um/monthly'
S = 'https://data.binance.vision/data/spot/monthly/klines'
SYMS = 'BTC ETH SOL BNB XRP DOGE ADA AVAX LINK DOT LTC BCH ATOM NEAR APT ARB OP INJ SUI SEI TIA FIL UNI AAVE ETC TRX XLM ALGO HBAR SAND MANA AXS GALA APE DYDX LDO RUNE WLD FET ENS CRV SNX COMP ICP IMX STX EGLD THETA FLOW CHZ KAVA ONE ZIL ENJ BAT ZEC DASH NEO EOS IOTA XTZ VET QTUM MASK GMT BLUR PENDLE JUP ONDO ENA STRK PYTH TIA'.split()
CYC = 0.0028
def fetch(u):
    for _ in range(2):
        try: return urllib.request.urlopen(u, timeout=60).read()
        except Exception: pass
def months():
    m = []; y, mo = 2021, 1
    while (y, mo) <= (2026, 9):
        m.append(f'{y}-{mo:02d}'); mo += 1
        if mo == 13: y, mo = y + 1, 1
    return m
MS = months()
def rd(b, header=None):
    z = zipfile.ZipFile(io.BytesIO(b)); return pd.read_csv(z.open(z.namelist()[0]), header=header)
def load(sym):
    s = sym + 'USDT'
    with ThreadPoolExecutor(8) as p:
        fb = list(p.map(fetch, [f'{F}/fundingRate/{s}/{s}-fundingRate-{m}.zip' for m in MS]))
        pb = list(p.map(fetch, [f'{F}/klines/{s}/1d/{s}-1d-{m}.zip' for m in MS]))
        sb = list(p.map(fetch, [f'{S}/{s}/1d/{s}-1d-{m}.zip' for m in MS]))
    if sum(1 for b in sb if b) < 12 or sum(1 for b in fb if b) < 12: return None
    fund = pd.concat([rd(b, 0) for b in fb if b]); fund['t'] = pd.to_datetime(fund['calc_time'].astype('int64'), unit='ms')
    fund = fund.drop_duplicates('t').set_index('t')['last_funding_rate'].astype(float).resample('D').sum()
    def kl(bs):
        fr = []
        for b in bs:
            if not b: continue
            d = rd(b); 
            if not str(d.iloc[0, 0]).isdigit(): d = d.iloc[1:]
            fr.append(d.iloc[:, [0, 4]].astype(float))
        d = pd.concat(fr); t = d.iloc[:, 0].astype('int64'); t = t.where(t < 1e14, t // 1000)
        d.index = pd.to_datetime(t, unit='ms').dt.normalize(); return d.iloc[:, 1][~d.index.duplicated()].sort_index()
    perp, spot = kl(pb), kl(sb)
    df = pd.concat({'f': fund.rename(lambda x: x.normalize()), 'p': perp, 's': spot}, axis=1).dropna()
    df['basis'] = df['s'].pct_change() - df['p'].pct_change()
    df['up'] = df['p'].pct_change()
    return df.dropna()
def stats(r):
    r = r.dropna()
    if len(r) < 30: return None
    eq = (1 + r).cumprod(); y = len(r) / 365
    return {'ann': round((eq.iloc[-1] ** (1 / y) - 1) * 100, 1), 'sharpe': round(r.mean() / r.std() * np.sqrt(365), 2) if r.std() > 0 else 0, 'maxDD': round((eq / eq.cummax() - 1).min() * 100, 1), 'worstDay': round(r.min() * 100, 2)}
def main():
    D = {}
    with ThreadPoolExecutor(2) as p:
        for s, d in zip(SYMS, p.map(lambda s: load(s), SYMS)):
            if d is not None and len(d) > 200: D[s] = d; print(s, len(d), 'фандинг %.1f%%' % (d['f'].mean() * 365 * 100), flush=True)
    print('монет с данными:', len(D))
    f = pd.concat({k: v['f'] for k, v in D.items()}, axis=1); b = pd.concat({k: v['basis'] for k, v in D.items()}, axis=1); up = pd.concat({k: v['up'] for k, v in D.items()}, axis=1)
    PER = {'2021': ('2021', '2021'), '2022': ('2022', '2022'), '2023': ('2023', '2023'), '2024': ('2024', '2024'), '2025': ('2025', '2025'), '2026': ('2026', '2026'), 'all': ('2021', '2026')}
    out = {}
    print('\nСредний фандинг по всем монетам по годам (годовых):', {y: round(float(f[a:b_].mean().mean() * 365 * 100), 1) for y, (a, b_) in PER.items() if y != 'all'})
    for K, thr, win in ((3, 0.15, 7), (5, 0.15, 7), (5, 0.30, 7), (10, 0.15, 7), (5, 0.15, 3), (5, 0.15, 30)):
        score = f.rolling(win).mean().shift(1) * 365
        w = pd.DataFrame(0.0, index=f.index, columns=f.columns)
        rank = score.rank(axis=1, ascending=False)
        sel = (rank <= K) & (score > thr)
        w = sel.astype(float); w = w.div(w.sum(axis=1).replace(0, np.nan), axis=0).fillna(0) * 1.0   # доля нотионала на монету, всего ≤1
        gross = (w.shift(1) * (f + b.fillna(0))).sum(axis=1)
        turn = w.diff().abs().sum(axis=1).fillna(0)
        r = gross - turn * CYC / 2
        key = f'топ-{K}, порог {int(thr*100)}%, окно {win}д'
        out[key] = {y: stats(r[a:b_]) for y, (a, b_) in PER.items()}
        inpos = (w.sum(axis=1) > 0).mean() * 100
        print('\n' + key, '| в позиции %.0f%% дней' % inpos)
        print('  ', ' | '.join(f"{y}: {s['ann']}%" for y, s in out[key].items() if s), '| Шарп %.2f maxDD %.1f%% худший день %.2f%%' % (out[key]['all']['sharpe'], out[key]['all']['maxDD'], out[key]['all']['worstDay']))
        # риск шорт-ноги: макс. дневной рост удерживаемых монет
        hold = (w.shift(1) > 0)
        mx = up.where(hold).max().max(); n20 = int(((up.where(hold)) > 0.2).sum().sum())
        print('   риск шорт-ноги: макс. рост за день %.0f%%, дней с ростом >20%%: %d' % (mx * 100, n20))
    json.dump(out, open('backtest/v12.json', 'w'))
main()
