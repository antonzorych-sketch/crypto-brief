"""v14: поиск актива под 5–7%/мес. Трендовый ансамбль (лонг/шорт, вол-таргет 30%) на ~40 активах, 2000–2026.
Выбор актива по обучению (2000–2012), проверка (2013–2026). Фьючерсы NG=F и др. — непрерывные ряды yfinance (есть скачки на роллах)."""
import json
import numpy as np, pandas as pd, yfinance as yf
A = {'NG=F': 'Природный газ', 'CL=F': 'Нефть WTI', 'BZ=F': 'Brent', 'HO=F': 'Мазут', 'RB=F': 'Бензин', 'GC=F': 'Золото', 'SI=F': 'Серебро', 'HG=F': 'Медь', 'PL=F': 'Платина', 'PA=F': 'Палладий',
     'ZC=F': 'Кукуруза', 'ZW=F': 'Пшеница', 'ZS=F': 'Соя', 'KC=F': 'Кофе', 'SB=F': 'Сахар', 'CC=F': 'Какао', 'CT=F': 'Хлопок', 'LE=F': 'Скот', 'HE=F': 'Свиньи',
     'ES=F': 'S&P фьюч', 'NQ=F': 'Nasdaq фьюч', 'RTY=F': 'Russell', 'YM=F': 'Dow', 'ZN=F': 'US 10Y', 'ZB=F': 'US 30Y',
     'EURUSD=X': 'EUR/USD', 'JPY=X': 'USD/JPY', 'GBPUSD=X': 'GBP/USD', 'AUDUSD=X': 'AUD/USD', 'USDCAD=X': 'USD/CAD',
     'BTC-USD': 'BTC', 'ETH-USD': 'ETH', 'SOL-USD': 'SOL', 'NVDA': 'NVDA', 'TSLA': 'TSLA', 'UNG': 'UNG ETF', 'XLE': 'Энергетика ETF', 'GDX': 'Золотодобыча', 'TLT': 'TLT'}
PER = {'train 2000-12': ('2000', '2012'), 'test 2013-26': ('2013', '2026'), '2022-26': ('2022', '2026')}
def sigs(d):
    c = d['Close']; o = {}
    for n in (20, 50, 100, 200): o[f'm{n}'] = np.sign(c / c.shift(n) - 1)
    for f, s in ((10, 40), (20, 100), (50, 200)): o[f'e{f}'] = np.sign(c.ewm(span=f).mean() - c.ewm(span=s).mean())
    for n in (20, 55, 100):
        hi = d['High'].rolling(n).max().shift(1); lo = d['Low'].rolling(n).min().shift(1)
        st = pd.Series(np.nan, index=c.index); st[c > hi] = 1; st[c < lo] = -1; o[f'd{n}'] = st.ffill().fillna(0)
    return pd.DataFrame(o).mean(axis=1)
def stats(r):
    r = r.dropna()
    if len(r) < 250: return None
    eq = (1 + r).cumprod(); y = len(r) / 252
    return {'ann': round((eq.iloc[-1] ** (1 / y) - 1) * 100, 1), 'mo': round((eq.iloc[-1] ** (21 / len(r)) - 1) * 100, 2), 'sharpe': round(r.mean() / r.std() * np.sqrt(252), 2) if r.std() > 0 else 0, 'maxDD': round((eq / eq.cummax() - 1).min() * 100, 1)}
def main():
    res = {}; rets = {}
    for t, nm in A.items():
        try:
            d = yf.download(t, start='1999-01-01', auto_adjust=False, progress=False)
            d.columns = [c[0] if isinstance(c, tuple) else c for c in d.columns]; d = d.dropna(subset=['Close'])
            if len(d) < 1500: continue
            c = d['Close']; r = c.pct_change()
            vol = r.rolling(30).std() * np.sqrt(252); sig = sigs(d)
            pos = (sig * (0.30 / vol).clip(upper=3)).fillna(0)
            ret = (pos.shift(1) * r - pos.diff().abs().fillna(0) * 5e-4).dropna(); rets[t] = ret
            res[t] = {'name': nm, **{k: stats(ret[a:b]) for k, (a, b) in PER.items()}, 'bh': stats(r['2013':].dropna())}
        except Exception as e: print('skip', t, e)
    rows = [(t, v) for t, v in res.items() if v['train 2000-12'] and v['test 2013-26']]
    print('Актив | train: год/мес/Шарп | test 2013-26: год/мес/Шарп/dd | 2022-26 год')
    for t, v in sorted(rows, key=lambda x: -x[1]['test 2013-26']['mo']):
        a, b, c3 = v['train 2000-12'], v['test 2013-26'], v['2022-26']
        print('%-9s %-16s | %6.1f%% %5.2fм sh%5.2f | %6.1f%% %5.2fм sh%5.2f dd%6.1f | %s' % (t, v['name'], a['ann'], a['mo'], a['sharpe'], b['ann'], b['mo'], b['sharpe'], b['maxDD'], ('%.1f%%' % c3['ann']) if c3 else '-'))
    # выбор на обучении -> проверка
    top = sorted(rows, key=lambda x: -x[1]['train 2000-12']['sharpe'])[:8]
    print('\nТоп-8 по Шарпу на обучении -> их проверка:')
    for t, v in top: print(t, v['name'], 'train sh', v['train 2000-12']['sharpe'], '-> test', v['test 2013-26']['ann'], '% год, sh', v['test 2013-26']['sharpe'])
    print('\nдоля активов с test mo>=5%%: %d из %d' % (sum(v['test 2013-26']['mo'] >= 5 for t, v in rows), len(rows)))
    allr = pd.concat(rets, axis=1).fillna(0).mean(axis=1)
    print('равновесный портфель всех активов:', {k: stats(allr[a:b]) for k, (a, b) in PER.items()})
    print('\nNG сезонность (по месяцам, средняя доходность за месяц NG=F, 2000-26):')
    ng = yf.download('NG=F', start='1999-01-01', progress=False, auto_adjust=False); ng.columns = [c[0] if isinstance(c, tuple) else c for c in ng.columns]
    m = ng['Close'].resample('ME').last().pct_change().dropna()
    print({k: round(float(v) * 100, 1) for k, v in m.groupby(m.index.month).mean().items()})
    json.dump(res, open('backtest/v14.json', 'w'))
main()
