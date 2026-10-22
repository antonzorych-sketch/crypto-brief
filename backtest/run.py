"""
Бэктест системы позиционирования на архиве Binance (data.binance.vision).

Восстанавливает по истории то, что живая система считает каждые 5 минут:
факторы направления, термометр, план уровней, сигнал на вход, и прогоняет
бумажные сделки по тем же правилам, что и движок paper (Supabase).

Чего нет в истории и чем заменено:
  - опционы Deribit (поток, max pain, стены, гамма) — факторы = 0, уровни не используются;
  - ликвидации — фактор = 0;
  - кластеры заявок в стакане — заменены узлами объёма (где реально торговали за 72 часа);
  - доля бидов ±2% — из архива глубины стакана Binance фьючерсов (bookDepth).

Запуск: python backtest/run.py [--synthetic] [--start 2023-01-01] [--out backtest/results.json]
"""
import argparse, io, json, math, os, sys, time, zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

BASE = 'https://data.binance.vision/data'
COINS = ['BTC', 'ETH', 'SOL']
MIN = 60_000
H = 60 * MIN
RISK_USD = 100.0
MAKER, TAKER = 0.00015, 0.00045
VALID_H, TIME_STOP_H, COOLDOWN_MIN = 4, 24, 60
CACHE = os.environ.get('BT_CACHE', 'data_cache')


# ---------------------------------------------------------------- загрузка
def fetch(url):
    import requests
    path = os.path.join(CACHE, url.replace(BASE + '/', '').replace('/', '_'))
    if os.path.exists(path):
        with open(path, 'rb') as f:
            b = f.read()
        return b or None
    for attempt in range(4):
        try:
            r = requests.get(url, timeout=60)
            if r.status_code == 404:
                open(path, 'wb').close()
                return None
            r.raise_for_status()
            with open(path, 'wb') as f:
                f.write(r.content)
            return r.content
        except Exception:
            time.sleep(2 * (attempt + 1))
    return None


PARSE_ERRORS = []


def read_zip_csv(b, names=None):
    if not b:
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(b)) as z:
            raw = z.read(z.namelist()[0])
        if not raw.strip():
            return None
        first = raw[:20].decode(errors='ignore')
        header = 0 if not first[:1].isdigit() else None
        return pd.read_csv(io.BytesIO(raw), header=header, names=None if header == 0 else names)
    except Exception as e:
        PARSE_ERRORS.append(repr(e)[:120])
        return None


def months(start, end):
    d = date(start.year, start.month, 1)
    while d <= end:
        yield d
        d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)


def days(start, end):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


KCOLS = ['open_time', 'open', 'high', 'low', 'close', 'volume', 'close_time', 'quote_volume', 'count', 'taker_buy_volume', 'taker_buy_quote_volume', 'ignore']


def kline_urls(market, sym, start, end):
    root = f'{BASE}/futures/um' if market == 'fut' else f'{BASE}/spot'
    cur = date(end.year, end.month, 1)
    urls = []
    for m in months(start, end):
        if m < cur:
            urls.append(f'{root}/monthly/klines/{sym}/1m/{sym}-1m-{m:%Y-%m}.zip')
        else:
            urls += [f'{root}/daily/klines/{sym}/1m/{sym}-1m-{d:%Y-%m-%d}.zip' for d in days(m, end)]
    return urls


def load_klines(market, sym, start, end, pool):
    frames = [read_zip_csv(b, KCOLS) for b in pool.map(fetch, kline_urls(market, sym, start, end))]
    df = pd.concat([f for f in frames if f is not None], ignore_index=True)
    df = df[['open_time', 'open', 'high', 'low', 'close', 'quote_volume', 'taker_buy_quote_volume']].astype(float)
    df['open_time'] = df['open_time'].astype('int64')
    # в 2025 году Binance перешёл на микросекунды в спотовых файлах
    df.loc[df.open_time > 10**14, 'open_time'] //= 1000
    df = df.drop_duplicates('open_time').sort_values('open_time').set_index('open_time')
    return df


def load_metrics(sym, start, end, pool):
    urls = [f'{BASE}/futures/um/daily/metrics/{sym}/{sym}-metrics-{d:%Y-%m-%d}.zip' for d in days(start, end)]
    frames = [read_zip_csv(b) for b in pool.map(fetch, urls)]
    df = pd.concat([f for f in frames if f is not None and 'create_time' in f], ignore_index=True)
    df['t'] = pd.to_datetime(df['create_time']).astype('datetime64[ms]').astype('int64')
    df = df.drop_duplicates('t').sort_values('t').set_index('t')
    return df[['sum_open_interest', 'count_long_short_ratio', 'sum_toptrader_long_short_ratio']].astype(float)


def load_funding(sym, start, end, pool):
    urls = [f'{BASE}/futures/um/monthly/fundingRate/{sym}/{sym}-fundingRate-{m:%Y-%m}.zip' for m in months(start, end)]
    frames = [read_zip_csv(b) for b in pool.map(fetch, urls)]
    df = pd.concat([f for f in frames if f is not None], ignore_index=True)
    df = df.rename(columns={'calc_time': 't', 'last_funding_rate': 'rate', 'funding_interval_hours': 'ih'})
    df['t'] = df['t'].astype('int64')
    return df.drop_duplicates('t').sort_values('t').set_index('t')[['rate', 'ih']].astype(float)


def load_book(sym, start, end, pool):
    urls = [f'{BASE}/futures/um/daily/bookDepth/{sym}/{sym}-bookDepth-{d:%Y-%m-%d}.zip' for d in days(start, end)]
    out = []
    for b in pool.map(fetch, urls):
        f = read_zip_csv(b)
        if f is None or 'percentage' not in f:
            continue
        f = f[f['percentage'].isin([-2, 2])]
        if f.empty or f['percentage'].nunique() < 2:
            continue
        p = f.pivot_table(index='timestamp', columns='percentage', values='notional', aggfunc='last')
        p.index = pd.to_datetime(p.index).astype('datetime64[ms]').astype('int64')
        p = p.rename(columns={-2: 'bid2', 2: 'ask2'})
        # 5-минутная сетка: последнее значение внутри интервала
        p['bucket'] = (p.index // (5 * MIN)) * (5 * MIN) + 5 * MIN
        out.append(p.groupby('bucket')[['bid2', 'ask2']].last())
    if not out:
        return None
    df = pd.concat(out)
    df = df[~df.index.duplicated()]
    df['bidShare'] = 100 * df.bid2 / (df.bid2 + df.ask2)
    return df[['bidShare']]


# ---------------------------------------------------------------- синтетика для проверки кода
def synthetic(start, end, seed=1):
    rng = np.random.default_rng(seed)
    t0 = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp() * 1000)
    n = int((datetime(end.year, end.month, end.day, tzinfo=timezone.utc).timestamp() * 1000 - t0) // MIN)
    ts = t0 + np.arange(n) * MIN
    vol = 0.0009 * (1 + 0.6 * np.sin(np.arange(n) / 9000) ** 2) * (1 + 0.8 * ((ts // H) % 24 >= 13) * ((ts // H) % 24 <= 16))
    ret = rng.normal(0, vol)
    c = 30000 * np.exp(np.cumsum(ret))
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, vol / 2)))
    l = np.minimum(o, c) * (1 - np.abs(rng.normal(0, vol / 2)))
    qv = rng.gamma(2, 5e5, n)
    tb = qv * np.clip(0.5 + ret * 80 + rng.normal(0, 0.05, n), 0.05, 0.95)
    fut = pd.DataFrame({'open': o, 'high': h, 'low': l, 'close': c, 'quote_volume': qv, 'taker_buy_quote_volume': tb}, index=ts)
    spot = fut.copy(); spot['taker_buy_quote_volume'] = qv * np.clip(0.5 + rng.normal(0, 0.05, n), 0.1, 0.9)
    t5 = ts[::5]
    met = pd.DataFrame({'sum_open_interest': 1e5 * np.exp(np.cumsum(rng.normal(0, 0.002, len(t5)))),
                        'count_long_short_ratio': 1.5 + 0.4 * np.sin(np.arange(len(t5)) / 700) + rng.normal(0, 0.05, len(t5)),
                        'sum_toptrader_long_short_ratio': 1.2 + 0.3 * np.cos(np.arange(len(t5)) / 900) + rng.normal(0, 0.05, len(t5))}, index=t5)
    tf = ts[::480]
    fund = pd.DataFrame({'rate': rng.normal(0.00008, 0.0001, len(tf)), 'ih': 8.0}, index=tf)
    book = pd.DataFrame({'bidShare': np.clip(50 + 10 * np.sin(np.arange(len(t5)) / 300) + rng.normal(0, 5, len(t5)), 5, 95)}, index=t5 + 5 * MIN)
    return fut, spot, met, fund, book


# ---------------------------------------------------------------- признаки
def rolling_pct(values, window):
    """Доля значений в окне (включая текущее), строго меньших текущего — как pctRank в живой системе."""
    v = np.asarray(values, float)
    out = np.full(len(v), np.nan)
    if len(v) < window:
        return out
    from numpy.lib.stride_tricks import sliding_window_view
    w = sliding_window_view(v, window)
    out[window - 1:] = np.round(100 * (w < w[:, -1:]).sum(1) / window)
    return out


def thermo_series(k1h):
    """Термометр как в живой системе, для каждого часа T: окно из 1000 часовых свечей, заканчивающееся на T."""
    hi, lo, cl = k1h['high'].values, k1h['low'].values, k1h['close'].values
    t = k1h.index.values
    n = len(cl)
    hours = (t // H) % 24
    pc = np.r_[cl[0], cl[:-1]]
    tr1 = np.maximum(hi - lo, np.maximum(np.abs(hi - pc), np.abs(lo - pc)))
    R4 = np.full(n, np.nan)
    for i in range(1, n - 3):
        R4[i] = max(hi[i:i + 4].max(), cl[i - 1]) - min(lo[i:i + 4].min(), cl[i - 1])
    ATR = np.full(n, np.nan)
    for i in range(57, n):
        ATR[i] = np.mean(R4[i - 4 * np.arange(1, 15)])
    cs = np.cumsum(np.r_[0, tr1])
    act = np.full(n, np.nan)
    for i in range(169, n):
        a = (cs[i] - cs[i - 6]) / 6
        b = (cs[i] - cs[i - 168]) / 168
        act[i] = math.log(a / b) if a > 0 and b > 0 else 0.0
    sig = (R4 > 1.5 * ATR).astype(float)
    P = np.full(n, np.nan); base = np.full(n, np.nan); pct = np.full(n, np.nan)
    for T in range(1000, n):
        s0, s1 = T - 999 + 170, T - 4           # окно выборок, как в thermo() живой системы
        idx = np.arange(s0, s1 + 1)
        idx = idx[~np.isnan(R4[idx]) & ~np.isnan(ATR[idx]) & ~np.isnan(act[idx])]
        hN = hours[T]
        dh = np.minimum(np.abs(hours[idx] - hN), 24 - np.abs(hours[idx] - hN))
        m = dh <= 1
        si, zz, ww = sig[idx][m], act[idx][m], np.where(dh[m] == 0, 1.0, 0.5)
        if not len(si):
            continue
        def Pf(z):
            w = np.exp(-(((zz - z) / 0.3) ** 2)) * ww
            return (w * si).sum() / w.sum() if w.sum() > 0 else np.nan
        P[T] = Pf(act[T])
        same = idx[(hours[idx] == hN)]
        base[T] = sig[same].mean() if len(same) else np.nan
        past = same[same < T - 20]
        if len(past):
            w = np.exp(-(((zz[None, :] - act[past][:, None]) / 0.3) ** 2)) * ww[None, :]
            pp = (w * si[None, :]).sum(1) / w.sum(1)
            pct[T] = round(100 * (pp < P[T]).sum() / len(pp))
    lab = np.where(pct >= 85, 'ГОРЯЧО', np.where(pct >= 60, 'ТЕПЛО', 'ТИХО'))
    # фактический исход: был ли значимый ход в следующие 4 часа (для калибровки)
    return pd.DataFrame({'P': P * 100, 'base': base * 100, 'pct': pct, 'label': lab, 'sigNext': np.where(np.isnan(R4) | np.isnan(ATR), np.nan, sig), 'R4': R4, 'ATRh': ATR}, index=t)


def build_features(fut, spot, met, fund, book):
    """Признаки на 5-минутной сетке. Время t — момент расчёта, данные строго до t."""
    t_all = fut.index.values
    grid = t_all[(t_all % (5 * MIN)) == 0]
    grid = grid[grid >= t_all[0] + 45 * 24 * H]
    last = np.searchsorted(t_all, grid) - 1          # последняя закрытая минута
    cl = fut['close'].values
    price = cl[last]
    p24 = cl[np.clip(last - 1440, 0, None)]
    d_f = (2 * fut['taker_buy_quote_volume'] - fut['quote_volume']).values
    cs_f = np.cumsum(np.r_[0, d_f])
    cvd_f = cs_f[last + 1] - cs_f[np.clip(last + 1 - 1440, 0, None)]
    tb = np.cumsum(np.r_[0, fut['taker_buy_quote_volume'].values]); qv = np.cumsum(np.r_[0, fut['quote_volume'].values])
    buy24 = tb[last + 1] - tb[np.clip(last + 1 - 1440, 0, None)]
    all24 = qv[last + 1] - qv[np.clip(last + 1 - 1440, 0, None)]
    taker = buy24 / np.maximum(all24 - buy24, 1e-9)
    sp = spot.reindex(fut.index)
    d_s = (2 * sp['taker_buy_quote_volume'] - sp['quote_volume']).fillna(0).values
    cs_s = np.cumsum(np.r_[0, d_s])
    cvd_s = cs_s[last + 1] - cs_s[np.clip(last + 1 - 1440, 0, None)]

    # 4-часовые свечи
    k4 = fut.groupby(fut.index // (4 * H) * (4 * H)).agg(open=('open', 'first'), high=('high', 'max'), low=('low', 'min'), close=('close', 'last'))
    pc = k4['close'].shift(1)
    tr = np.maximum(k4.high - k4.low, np.maximum((k4.high - pc).abs(), (k4.low - pc).abs()))
    atr = tr.rolling(14).mean()                       # по закрытым свечам
    e20 = k4['close'].ewm(span=20, adjust=False).mean(); e50 = k4['close'].ewm(span=50, adjust=False).mean()
    hi84 = k4['high'].rolling(83).max(); lo84 = k4['low'].rolling(83).min()
    k4t = k4.index.values
    bar = np.searchsorted(k4t, grid, side='right') - 1    # текущая (незакрытая) 4ч-свеча
    prev = bar - 1
    a20, a50 = 2 / 21, 2 / 51
    ema20 = a20 * price + (1 - a20) * e20.values[prev]
    ema50 = a50 * price + (1 - a50) * e50.values[prev]
    # максимум/минимум текущей свечи до момента t
    hi_cur = fut['high'].groupby(fut.index // (4 * H)).cummax().values[last]
    lo_cur = fut['low'].groupby(fut.index // (4 * H)).cummin().values[last]
    r_hi = np.maximum(hi84.values[prev], hi_cur); r_lo = np.minimum(lo84.values[prev], lo_cur)

    # метрики (OI, L/S) — 5 минут; L/S перцентиль по часовому ряду за 480 часов, как в живой системе
    m = met.reindex(grid, method='ffill')
    oi = m['sum_open_interest'].values
    oi24 = met['sum_open_interest'].reindex(grid - 24 * H, method='ffill').values
    mh = met[(met.index % H) == 0]
    crowd_pct = pd.Series(rolling_pct(mh['count_long_short_ratio'].values, 480), index=mh.index).reindex(grid, method='ffill').values
    top_pct = pd.Series(rolling_pct(mh['sum_toptrader_long_short_ratio'].values, 480), index=mh.index).reindex(grid, method='ffill').values

    # фандинг: последняя выплата, среднее и доля плюсовых за 30 дней (в % за 8ч)
    f8 = fund['rate'] * (8 / fund['ih'].replace(0, 8)) * 100
    fi = np.searchsorted(fund.index.values, grid, side='right') - 1
    fv = f8.values
    fcs = np.cumsum(np.r_[0, fv]); fpos = np.cumsum(np.r_[0, (fv > 0).astype(float)])
    f30 = np.searchsorted(fund.index.values, grid - 30 * 24 * H, side='right')
    cnt = np.maximum(fi + 1 - f30, 1)
    f_now = np.where(fi >= 0, fv[np.clip(fi, 0, None)], np.nan)
    f_avg = (fcs[fi + 1] - fcs[f30]) / cnt
    f_pos = np.round(100 * (fpos[fi + 1] - fpos[f30]) / cnt)

    bshare = book['bidShare'].reindex(grid, method='ffill', tolerance=30 * MIN).values if book is not None else np.full(len(grid), np.nan)

    # термометр — по часовым свечам, значение текущего часа
    k1h = fut.groupby(fut.index // H * H).agg(open=('open', 'first'), high=('high', 'max'), low=('low', 'min'), close=('close', 'last'))
    th = thermo_series(k1h)
    hidx = np.searchsorted(th.index.values, grid, side='right') - 1

    F = pd.DataFrame({
        't': grid, 'last': last, 'price': price, 'ch24': (price / p24 - 1) * 100,
        'cvdFut': cvd_f, 'cvdSpot': cvd_s, 'taker': taker,
        'atr': atr.values[prev], 'ema20': ema20, 'ema50': ema50, 'rhi': r_hi, 'rlo': r_lo,
        'oiCh24': (oi / oi24 - 1) * 100, 'crowdPct': crowd_pct, 'topPct': top_pct,
        'fNow': f_now, 'fAvg': f_avg, 'fPos': f_pos, 'bidShare': bshare,
        'P': th['P'].values[hidx], 'thPct': th['pct'].values[hidx], 'label': th['label'].values[hidx],
    })
    F['rpos'] = np.round((F.price - F.rlo) / (F.rhi - F.rlo) * 100)
    return F.dropna(subset=['atr', 'ema50', 'P', 'crowdPct']).reset_index(drop=True), th, k1h


# ---------------------------------------------------------------- факторы (перенос из живой системы)
def factor_scores(F):
    up, dn = F.ch24 > 0.5, F.ch24 < -0.5
    fu, sp = F.cvdFut, F.cvdSpot
    s1 = np.select([(fu < 0) & (sp > 0), (fu < 0) & ~dn, (fu > 0) & (sp > 0), (fu > 0) & ~up & (sp < 0), (fu < 0) & (sp < 0)], [1, .5, .5, -.5, -.5], 0)
    t, flat = F.taker, F.ch24.abs() < 0.5
    s2 = np.select([t > 1.05, t < 0.95], [np.where(dn | flat, -.5, .5), np.where(up | flat, .5, -.5)], 0)
    p = F.crowdPct
    s3 = np.select([p >= 90, p >= 75, p <= 10, p <= 25], [-1, -.5, 1, .5], 0).astype(float)
    s3 = np.where((s3 < 0) & (F.topPct < 50), s3 - .25, s3)
    fn, fa, fp = F.fNow, F.fAvg, F.fPos
    s4 = np.select([(fn > 0) & (fp >= 80) & (fn > fa * 1.2), (fn > fa * 1.5) & (fn > 0.01), (fn < 0) & dn, fn < 0], [-1, -.5, 1, .5], 0)
    o = F.oiCh24
    s5 = np.select([up & (o > 1), up & (o < -1), dn & (o > 1), dn & (o < -1)], [1, .25, -1, .25], 0)
    s6 = np.zeros(len(F))   # ликвидации — нет истории
    s7 = np.zeros(len(F))   # поток опционов — нет истории
    s8 = np.zeros(len(F))   # магниты — нет истории
    s9 = np.select([(F.price > F.ema20) & (F.ema20 > F.ema50), (F.price < F.ema20) & (F.ema20 < F.ema50)], [.5, -.5], 0).astype(float)
    s9 = s9 + np.select([F.rpos >= 90, F.rpos <= 10], [-.25, .25], 0)
    b = F.bidShare
    s10 = np.select([b < 40, b < 45, b > 60, b > 55], [-1, -.5, 1, .5], 0)
    S = np.vstack([s1, s2, s3, s4, s5, s6, s7, s8, s9, s10]).T
    return np.clip(S, -1, 1)


FACTOR_NAMES = ['CVD фьючерсов и спота', 'Тейкеры', 'Толпа против крупных', 'Фандинг', 'OI и цена', 'Ликвидации', 'Поток опционов', 'Магниты', 'Структура цены', 'Стакан']


# ---------------------------------------------------------------- уровни, вход, сделка
def volume_nodes(fut_c, fut_v, last, price):
    """Узлы объёма за 72 часа (замена кластеров стакана): топ-3 корзины по 0.25% с каждой стороны, ±5%, не ближе 0.4%."""
    s = max(0, last - 4320)
    c, v = fut_c[s:last + 1], fut_v[s:last + 1]
    bucket = price * 0.0025
    k = np.round(c / bucket) * bucket
    ok = (np.abs(k / price - 1) <= 0.05) & (np.abs(k / price - 1) >= 0.004)
    if not ok.any():
        return [], []
    ser = pd.Series(v[ok]).groupby(k[ok]).sum()
    med = ser.median()
    below = ser[ser.index < price].sort_values(ascending=False).head(3)
    above = ser[ser.index > price].sort_values(ascending=False).head(3)
    mk = lambda s: [{'px': float(px), 'usd': float(u), 'big': bool(u >= 2 * med)} for px, u in s.items()]
    return mk(below), mk(above)


def plan(row, bids, asks):
    P, A = row.price, row.atr
    lv = [{'px': c['px'], 'src': 'узел объёма', 'kind': 'bid'} for c in bids] + [{'px': c['px'], 'src': 'узел объёма', 'kind': 'ask'} for c in asks]
    lv += [{'px': row.rhi, 'src': 'верх диапазона 14д', 'kind': 'range'}, {'px': row.rlo, 'src': 'низ диапазона 14д', 'kind': 'range'}]
    out = {}
    for side, d in (('long', 1), ('short', -1)):
        fwd = sorted([x for x in lv if d * (x['px'] - P) >= A], key=lambda x: d * x['px'])
        t1 = fwd[0] if fwd else {'px': P + d * 1.5 * A, 'src': '1.5 ATR'}
        t2 = next((x for x in fwd if d * (x['px'] - t1['px']) >= A), None) or {'px': t1['px'] + d * A, 'src': '+1 ATR'}
        big = [c for c in (bids if d == 1 else asks) if c['big']]
        big.sort(key=lambda c: abs(c['px'] - P))
        if big:
            l1 = {'px': big[0]['px'], 'src': 'узел объёма'}
        else:
            cand = sorted([x for x in lv if x['kind'] == 'range' and d * (P - x['px']) > 0], key=lambda x: abs(x['px'] - P))
            l1 = cand[0] if cand else {'px': P - d * A, 'src': '1 ATR'}
        out[side] = {'t1': t1, 't2': t2, 'l1': l1}
    return out


def entry_plan(row, pl, pLong, use_thermo=True, market_only=False, rr_filter=True):
    A, P = row.atr, row.price
    active = row.label in ('ТЕПЛО', 'ГОРЯЧО') or not use_thermo
    side = 'long' if pLong >= 55 else 'short' if (100 - pLong) >= 55 else None
    if not side or not active:
        return None
    d = 1 if side == 'long' else -1
    p = pl[side]; L1 = p['l1']['px']
    if d * (P - L1) <= 0:
        return None
    typ, E = 'limit', L1 + d * 0.5 * A
    if market_only or d * (P - E) <= 0.15 * A:
        typ, E = 'market', P
    elif d * (P - E) > A:
        E = P - d * A
    stop = L1 - d * 0.2 * A
    if d * (E - stop) < 0.5 * A:
        stop = E - d * 0.5 * A
    dist = d * (E - stop)
    if dist > 2.5 * A:
        return None
    t1, t2 = p['t1']['px'], p['t2']['px']
    if d * (t1 - E) <= 0:
        return None
    rr1, rr2 = d * (t1 - E) / dist, d * (t2 - E) / dist
    if rr_filter and (rr1 < 0.8 or 0.5 * rr1 + 0.5 * rr2 < 1.5):
        return None
    return {'side': side, 'type': typ, 'entry': E, 'stop': stop, 't1': t1, 't2': t2, 'rr1': rr1, 'rr2': rr2, 't1src': p['t1']['src'], 'l1src': p['l1']['src']}


def run_variant(coin, F, S, fut, fund, cfg):
    """Прогон сделок по 5-минутной сетке. cfg: scale, use_thermo, market_only, rr_filter."""
    t1m = fut.index.values; o, h, l, c = (fut[k].values for k in ('open', 'high', 'low', 'close'))
    qv = fut['quote_volume'].values
    ft, fr = fund.index.values, fund['rate'].values
    score = S.sum(1) * cfg.get('scale', 1.0)
    pLong = np.round(50 + 20 * np.tanh(score / 4)).astype(int)
    trades, tr, cooldown_until = [], None, 0
    nodes_cache = {}

    def finish(tr, px, t, reason, rate):
        d = tr['dir']; part = 0.5 if tr['status'] == 'tp1' else 1.0
        tr['R'] += part * d * (px - tr['fill']) / tr['dist']
        tr['fees'] += tr['qty'] * part * px * rate
        tr.update(status='closed', closed=int(t), exit=float(px), reason=reason)

    def advance(tr, i0, i1, flip_px=None):
        """Минутные свечи с индексами [i0, i1)."""
        d = tr['dir']
        for i in range(i0, i1):
            if tr['status'] in ('closed', 'cancelled'):
                return
            bt = t1m[i]
            if tr['status'] == 'pending':
                if bt > tr['expires']:
                    tr.update(status='cancelled', closed=int(bt), reason='лимит не исполнился за 4 часа'); return
                filled = l[i] <= tr['entry'] if d == 1 else h[i] >= tr['entry']
                touched = h[i] >= tr['t1'] if d == 1 else l[i] <= tr['t1']
                if touched and not filled:
                    tr.update(status='cancelled', closed=int(bt), reason='цена ушла к цели без отката'); return
                if not filled:
                    continue
                tr.update(status='open', fill=tr['entry'], filled=int(bt)); tr['fees'] += tr['qty'] * tr['entry'] * MAKER
                if (l[i] <= tr['stop']) if d == 1 else (h[i] >= tr['stop']):
                    finish(tr, tr['stop'], bt, 'стоп', TAKER)
                continue
            # фандинг за время удержания
            while tr['fidx'] < len(ft) and ft[tr['fidx']] <= bt:
                if ft[tr['fidx']] > tr['filled']:
                    rem = 0.5 if tr['status'] == 'tp1' else 1.0
                    tr['funding'] += d * fr[tr['fidx']] * tr['qty'] * rem * c[i]
                tr['fidx'] += 1
            stop_px = tr['fill'] if tr['status'] == 'tp1' else tr['stop']
            if (l[i] <= stop_px) if d == 1 else (h[i] >= stop_px):
                finish(tr, stop_px, bt, 'безубыток после цели 1' if tr['status'] == 'tp1' else 'стоп', TAKER); return
            if tr['status'] == 'open' and ((h[i] >= tr['t1']) if d == 1 else (l[i] <= tr['t1'])):
                tr['status'] = 'tp1'; tr['tp1'] = int(bt)
                tr['R'] = 0.5 * d * (tr['t1'] - tr['fill']) / tr['dist']; tr['fees'] += tr['qty'] * 0.5 * tr['t1'] * MAKER
            if tr['status'] == 'tp1' and ((h[i] >= tr['t2']) if d == 1 else (l[i] <= tr['t2'])):
                finish(tr, tr['t2'], bt, 'цель 2', MAKER); return
            if bt - tr['filled'] > TIME_STOP_H * H:
                finish(tr, c[i], bt, 'время вышло (24 ч)', TAKER); return
        if flip_px is not None and tr['status'] in ('open', 'tp1'):
            finish(tr, flip_px, t1m[i1 - 1], 'сигнал развернулся', TAKER)

    def close_out(tr):
        if tr['status'] == 'closed':
            pnl = tr['R'] * RISK_USD - tr['fees'] - tr['funding']
            tr['pnl'] = pnl; tr['r'] = pnl / RISK_USD
        trades.append(tr)

    for k in range(len(F)):
        t, last = int(F.t.values[k]), int(F['last'].values[k])
        # 1) ведём открытую сделку до момента t
        if tr is not None:
            pl_side = pLong[k]
            sig_side = 'long' if pl_side >= 55 else 'short' if pl_side <= 45 else None
            active = (F.label.values[k] in ('ТЕПЛО', 'ГОРЯЧО')) or not cfg.get('use_thermo', True)
            flip = sig_side is not None and active and sig_side != tr['side'] and tr['status'] != 'pending'
            advance(tr, tr['i'], last + 1, c[last] if flip else None)
            tr['i'] = last + 1
            if tr['status'] == 'pending' and sig_side and active and sig_side != tr['side']:
                tr.update(status='cancelled', closed=t, reason='сигнал развернулся до входа')
            if tr['status'] in ('closed', 'cancelled'):
                close_out(tr); cooldown_until = tr['closed'] + COOLDOWN_MIN * MIN; tr = None
            continue
        if t < cooldown_until:
            continue
        # 2) новый сигнал
        pL = pLong[k]
        if not (pL >= 55 or pL <= 45):
            continue
        if cfg.get('use_thermo', True) and F.label.values[k] not in ('ТЕПЛО', 'ГОРЯЧО'):
            continue
        r = F.iloc[k]
        bids, asks = volume_nodes(c, qv, last, r.price)
        sig = entry_plan(r, plan(r, bids, asks), pL, cfg.get('use_thermo', True), cfg.get('market_only', False), cfg.get('rr_filter', True))
        if not sig:
            continue
        d = 1 if sig['side'] == 'long' else -1
        dist = d * (sig['entry'] - sig['stop'])
        tr = {'coin': coin, 'side': sig['side'], 'dir': d, 'type': sig['type'], 'created': t, 'expires': t + VALID_H * H,
              'entry': sig['entry'], 'stop': sig['stop'], 't1': sig['t1'], 't2': sig['t2'], 'dist': dist, 'qty': RISK_USD / dist,
              'status': 'pending', 'R': 0.0, 'fees': 0.0, 'funding': 0.0, 'i': last + 1, 'fidx': int(np.searchsorted(ft, t, side='right')),
              'pLong': int(pL), 'label': str(F.label.values[k]), 'P': float(F.P.values[k]), 'rr1': sig['rr1'], 'rr2': sig['rr2'],
              't1src': sig['t1src'], 'l1src': sig['l1src'], 'atr': float(r.atr), 'f': [float(x) for x in S[k]]}
        if sig['type'] == 'market':
            tr.update(status='open', fill=sig['entry'], filled=t); tr['fees'] += tr['qty'] * sig['entry'] * TAKER
    if tr is not None and tr['status'] in ('open', 'tp1'):
        finish(tr, c[-1], t1m[-1], 'открыта на конец периода', TAKER); close_out(tr)
    return trades


# ---------------------------------------------------------------- статистика
def stats(trades):
    cl = [t for t in trades if t['status'] == 'closed']
    if not cl:
        return {'trades': 0, 'cancelled': sum(t['status'] == 'cancelled' for t in trades)}
    r = np.array([t['r'] for t in cl]); cl_t = np.array([t['closed'] for t in cl])
    order = np.argsort(cl_t); r, cl_t = r[order], cl_t[order]
    eq = np.cumsum(r); dd = eq - np.maximum.accumulate(np.r_[0, eq])[1:]
    streak = best = 0
    for x in r:
        streak = streak + 1 if x <= 0 else 0; best = max(best, streak)
    gains, losses = r[r > 0].sum(), -r[r <= 0].sum()
    hold = [(t['closed'] - t['filled']) / H for t in cl if t.get('filled')]
    return {'trades': int(len(r)), 'wins': int((r > 0).sum()), 'winRate': round(100 * (r > 0).mean(), 1), 'avgR': round(float(r.mean()), 3),
            'sumR': round(float(r.sum()), 2), 'pf': round(float(gains / losses), 2) if losses > 0 else None, 'maxDD': round(float(dd.min()), 2),
            'maxLossStreak': int(best), 'avgHoldH': round(float(np.mean(hold)), 1) if hold else None,
            'pnl': round(float(r.sum() * RISK_USD), 0), 'fees': round(float(sum(t['fees'] for t in cl)), 0), 'funding': round(float(sum(t['funding'] for t in cl)), 0),
            'cancelled': int(sum(t['status'] == 'cancelled' for t in trades)),
            'tStat': round(float(r.mean() / (r.std(ddof=1) / math.sqrt(len(r)))), 2) if len(r) > 2 and r.std() > 0 else None}


def summarize(trades):
    out = {'all': stats(trades)}
    for coin in COINS:
        out[coin] = stats([t for t in trades if t['coin'] == coin])
    for side in ('long', 'short'):
        out[side] = stats([t for t in trades if t['side'] == side])
    years = sorted({datetime.fromtimestamp(t['created'] / 1000, timezone.utc).year for t in trades})
    out['years'] = {str(y): stats([t for t in trades if datetime.fromtimestamp(t['created'] / 1000, timezone.utc).year == y]) for y in years}
    cl = sorted([t for t in trades if t['status'] == 'closed'], key=lambda t: t['closed'])
    eq, s = [], 0.0
    for t in cl:
        s += t['r']; eq.append([t['closed'], round(s, 3)])
    if len(eq) > 800:
        step = len(eq) / 800; eq = [eq[int(i * step)] for i in range(800)] + [eq[-1]]
    months_r = {}
    for t in cl:
        m = datetime.fromtimestamp(t['closed'] / 1000, timezone.utc).strftime('%Y-%m')
        months_r[m] = round(months_r.get(m, 0) + t['r'], 3)
    out['equity'] = eq; out['months'] = months_r
    return out


def compact(t, i):
    return {'id': i, 'coin': t['coin'], 'side': t['side'], 'type': t['type'], 'status': t['status'], 'created': t['created'],
            'filled': t.get('filled'), 'tp1': t.get('tp1'), 'closed': t.get('closed'),
            'entry': round(t['entry'], 6), 'fill': round(t['fill'], 6) if t.get('fill') is not None else None, 'stop': round(t['stop'], 6),
            't1': round(t['t1'], 6), 't2': round(t['t2'], 6), 'exit': round(t['exit'], 6) if t.get('exit') is not None else None,
            'r': round(t['r'], 3) if 'r' in t else None, 'pnl': round(t['pnl'], 2) if 'pnl' in t else None,
            'fees': round(t['fees'], 2), 'funding': round(t['funding'], 2), 'reason': t.get('reason'),
            'pLong': t['pLong'], 'label': t['label'], 'P': round(t['P']), 'rr1': round(t['rr1'], 2), 'rr2': round(t['rr2'], 2),
            't1src': t['t1src'], 'l1src': t['l1src'], 'atr': round(t['atr'], 6), 'f': t['f']}


# ---------------------------------------------------------------- диагностика факторов и калибровка
def diagnostics(all_F, all_S, all_th):
    rows = []
    for coin in COINS:
        F, S = all_F[coin], all_S[coin]
        hourly = (F.t.values % H) == 0
        Fh, Sh = F[hourly].reset_index(drop=True), S[hourly]
        # исход: изменение цены за 4 часа в единицах ATR
        px = pd.Series(F.price.values, index=F.t.values)
        fut4 = px.reindex(Fh.t.values + 4 * H).values
        move = (fut4 - Fh.price.values) / Fh.atr.values
        rows.append((coin, Fh, Sh, move))
    out = {'factors': [], 'pLong': [], 'thermo': []}
    for j, name in enumerate(FACTOR_NAMES):
        pos = neg = hit_p = hit_n = 0; mv_p = []; mv_n = []
        for coin, Fh, Sh, move in rows:
            ok = ~np.isnan(move)
            s = Sh[ok, j]; m = move[ok]
            pos += (s > 0).sum(); neg += (s < 0).sum()
            hit_p += ((s > 0) & (m > 0)).sum(); hit_n += ((s < 0) & (m < 0)).sum()
            mv_p += list(m[s > 0]); mv_n += list(m[s < 0])
        n = pos + neg
        out['factors'].append({'name': name, 'n': int(n), 'nUp': int(pos), 'nDown': int(neg),
                               'hit': round(100 * (hit_p + hit_n) / n, 1) if n else None,
                               'avgMoveWhenUp': round(float(np.mean(mv_p)), 3) if mv_p else None,
                               'avgMoveWhenDown': round(float(np.mean(mv_n)), 3) if mv_n else None,
                               'noData': n == 0})
    edges = [0, 40, 45, 50, 55, 60, 101]
    allp, allm = [], []
    for coin, Fh, Sh, move in rows:
        pl = np.round(50 + 20 * np.tanh(Sh.sum(1) / 4))
        ok = ~np.isnan(move); allp += list(pl[ok]); allm += list(move[ok])
    allp, allm = np.array(allp), np.array(allm)
    for a, b in zip(edges[:-1], edges[1:]):
        m = (allp >= a) & (allp < b)
        if m.sum():
            out['pLong'].append({'from': a, 'to': min(b, 100), 'n': int(m.sum()), 'upShare': round(100 * (allm[m] > 0).mean(), 1), 'avgMoveAtr': round(float(allm[m].mean()), 3)})
    th_all = pd.concat([all_th[c] for c in COINS])
    th_all = th_all.dropna(subset=['P', 'sigNext'])
    for a, b in [(0, 20), (20, 30), (30, 40), (40, 50), (50, 60), (60, 101)]:
        m = (th_all.P >= a) & (th_all.P < b)
        if m.sum():
            out['thermo'].append({'from': a, 'to': min(b, 100), 'n': int(m.sum()), 'predicted': round(float(th_all.P[m].mean()), 1), 'actual': round(100 * float(th_all.sigNext[m].mean()), 1)})
    for lab in ['ТИХО', 'ТЕПЛО', 'ГОРЯЧО']:
        m = th_all.label == lab
        if m.sum():
            out.setdefault('labels', []).append({'label': lab, 'n': int(m.sum()), 'actual': round(100 * float(th_all.sigNext[m].mean()), 1)})
    return out


VARIANTS = [
    ('live', 'Как в живой системе', 'Правила живой системы: термометр ТЕПЛО/ГОРЯЧО, перевес ≥55%, лимит на откате, фильтр R:R. Факторы без истории считаются нулём.', {}),
    ('scaled', 'Перевес с поправкой на 7 факторов', 'Счёт умножен на 10/7, чтобы три фактора без истории не занижали перевес.', {'scale': 10 / 7}),
    ('market', 'Вход по рынку', 'Те же сигналы, но вход сразу по рынку, без ожидания отката.', {'scale': 10 / 7, 'market_only': True}),
    ('nothermo', 'Без термометра', 'Вход по перевесу без фильтра ТЕПЛО/ГОРЯЧО.', {'scale': 10 / 7, 'use_thermo': False}),
    ('norr', 'Без фильтра R:R', 'Вход без требования R:R ≥0.8 до цели 1 и ≥1.5 в среднем.', {'scale': 10 / 7, 'rr_filter': False}),
]


def clean(x):
    if isinstance(x, dict):
        return {str(k): clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [clean(v) for v in x]
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (float, np.floating)):
        return None if not math.isfinite(float(x)) else float(x)
    if isinstance(x, np.bool_):
        return bool(x)
    return x


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--synthetic', action='store_true')
    ap.add_argument('--start', default='2023-01-01')
    ap.add_argument('--end', default=None)
    ap.add_argument('--out', default='backtest/results.json')
    a = ap.parse_args()
    start = date.fromisoformat(a.start)
    end = date.fromisoformat(a.end) if a.end else (datetime.now(timezone.utc).date() - timedelta(days=1))
    os.makedirs(CACHE, exist_ok=True)
    all_F, all_S, all_th, data = {}, {}, {}, {}
    t0 = time.time()
    with ThreadPoolExecutor(24) as pool:
        for i, coin in enumerate(COINS):
            sym = coin + 'USDT'
            if a.synthetic:
                fut, spot, met, fund, book = synthetic(start, end, seed=i + 1)
            else:
                fut = load_klines('fut', sym, start, end, pool); print(coin, 'fut', len(fut), round(time.time() - t0), flush=True)
                spot = load_klines('spot', sym, start, end, pool); print(coin, 'spot', len(spot), round(time.time() - t0), flush=True)
                met = load_metrics(sym, start, end, pool); print(coin, 'metrics', len(met), round(time.time() - t0), flush=True)
                fund = load_funding(sym, start, end, pool); print(coin, 'funding', len(fund), flush=True)
                book = load_book(sym, start, end, pool); print(coin, 'book', 0 if book is None else len(book), round(time.time() - t0), flush=True)
            print(coin, 'parse errors', len(PARSE_ERRORS), PARSE_ERRORS[:3], flush=True)
            F, th, _ = build_features(fut, spot, met, fund, book)
            S = factor_scores(F)
            all_F[coin], all_S[coin], all_th[coin], data[coin] = F, S, th, (fut, fund)
            print(coin, 'features', len(F), round(time.time() - t0), flush=True)
    res = {'generated': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'synthetic': a.synthetic,
           'period': {'from': str(start), 'to': str(end), 'evalFrom': int(min(all_F[c].t.min() for c in COINS)), 'evalTo': int(max(all_F[c].t.max() for c in COINS))},
           'rules': {'riskUsd': RISK_USD, 'maker': MAKER, 'taker': TAKER, 'validH': VALID_H, 'timeStopH': TIME_STOP_H, 'cooldownMin': COOLDOWN_MIN},
           'notes': ['Опционы Deribit и ликвидации в истории недоступны: эти три фактора равны нулю, опционные уровни не используются.',
                     'Кластеры заявок заменены узлами объёма за 72 часа; доля бидов ±2% — из архива глубины стакана Binance.',
                     'Исполнение по минутным свечам Binance фьючерсов; если в одной минуте задеты стоп и цель, засчитывается стоп.',
                     'Комиссии Hyperliquid 0,015% / 0,045% и фандинг Binance учтены. Риск $100 на сделку.'],
           'variants': [], 'factorNames': FACTOR_NAMES}
    for key, name, desc, cfg in VARIANTS:
        trades = []
        for coin in COINS:
            fut, fund = data[coin]
            trades += run_variant(coin, all_F[coin], all_S[coin], fut, fund, cfg)
        trades.sort(key=lambda t: t['created'])
        v = {'key': key, 'name': name, 'desc': desc, 'stats': summarize(trades)}
        v['trades'] = [compact(t, i + 1) for i, t in enumerate(trades) if t['status'] == 'closed'] if key in ('live', 'scaled') else []
        res['variants'].append(v)
        s = v['stats']['all']
        print(key, s.get('trades'), s.get('winRate'), s.get('avgR'), s.get('sumR'), s.get('maxDD'), round(time.time() - t0), flush=True)
    res['diagnostics'] = diagnostics(all_F, all_S, all_th)
    os.makedirs(os.path.dirname(a.out) or '.', exist_ok=True)
    with open(a.out, 'w') as f:
        json.dump(clean(res), f, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    print('saved', a.out, os.path.getsize(a.out), round(time.time() - t0))


if __name__ == '__main__':
    main()
