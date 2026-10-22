"""
Поиск закономерностей: какие признаки позиционирования реально предсказывают движение цены.

Честная схема:
  - обучение и подбор порогов — только на 2023–2024 (train);
  - вывод — только по 2025–2026 (test), которые правила и модели не видели;
  - модели переобучаются поквартально (walk-forward) с зазором 24 часа;
  - доходность сделок считается после комиссий Hyperliquid (вход и выход по рынку 0.09%, лимитами 0.03%).

Запуск: python backtest/research.py [--synthetic] [--start 2023-01-01] [--out backtest/research.json]
"""
import argparse, json, math, os, sys, time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from run import (BASE, COINS, H, MIN, CACHE, fetch, read_zip_csv, days, load_klines, load_metrics, load_funding,
                 synthetic, thermo_series, clean, rolling_pct)

SPLIT = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
COST_TAKER, COST_MAKER = 9.0, 3.0      # bps за сделку туда-обратно

NAMES = {
    'ret1': 'Доходность за 1ч', 'ret4': 'Доходность за 4ч', 'ret24': 'Доходность за 24ч', 'ret72': 'Доходность за 3 дня',
    'atrPct': 'Волатильность (ATR, %)', 'rvRatio': 'Сжатие/расширение волатильности (6ч к 7д)', 'rangePos': 'Положение в диапазоне 14д',
    'distEma': 'Отрыв от средней (EMA, в ATR)', 'trend': 'Тренд (EMA быстрая − медленная)',
    'imbF1': 'Поток фьючерсов за 1ч', 'imbF4': 'Поток фьючерсов за 4ч', 'imbF24': 'Поток фьючерсов за 24ч',
    'imbS1': 'Поток спота за 1ч', 'imbS4': 'Поток спота за 4ч', 'imbS24': 'Поток спота за 24ч', 'imbDiv24': 'Спот минус фьючерсы (24ч)',
    'volRatio': 'Объём к норме', 'oiCh1': 'OI за 1ч', 'oiCh4': 'OI за 4ч', 'oiCh24': 'OI за 24ч', 'oiPrice24': 'OI вместе с ценой (24ч)',
    'fund': 'Фандинг', 'fundDev': 'Фандинг к среднему за 30д', 'crowd': 'L/S толпы', 'crowdPct': 'L/S толпы, перцентиль 20д',
    'crowdCh24': 'Изменение L/S толпы за 24ч', 'top': 'L/S топ-трейдеров', 'topPct': 'L/S топов, перцентиль 20д', 'crowdTopDiv': 'Толпа против топов',
    'book1': 'Доля бидов ±1%', 'book2': 'Доля бидов ±2%', 'book5': 'Доля бидов ±5%', 'book2Ch4': 'Изменение доли бидов за 4ч',
    'thermoP': 'Термометр P', 'usOpen': 'Открытие США (13–16 UTC)',
}


def load_book_multi(sym, start, end, pool):
    urls = [f'{BASE}/futures/um/daily/bookDepth/{sym}/{sym}-bookDepth-{d:%Y-%m-%d}.zip' for d in days(start, end)]
    out = []
    for b in pool.map(fetch, urls):
        f = read_zip_csv(b)
        if f is None or 'percentage' not in f:
            continue
        f = f[f['percentage'].isin([-5, -2, -1, 1, 2, 5])]
        if f.empty:
            continue
        p = f.pivot_table(index='timestamp', columns='percentage', values='notional', aggfunc='last')
        if not set([-5, -2, -1, 1, 2, 5]).issubset(p.columns):
            continue
        p.index = pd.to_datetime(p.index).astype('datetime64[ms]').astype('int64')
        p['hour'] = (p.index // H) * H + H          # значение на закрытие часа
        out.append(p.groupby('hour').last())
    if not out:
        return None
    df = pd.concat(out); df = df[~df.index.duplicated()]
    r = pd.DataFrame(index=df.index)
    for k in (1, 2, 5):
        r[f'book{k}'] = 100 * df[-k] / (df[-k] + df[k])
    return r


def hourly_frame(coin, fut, spot, met, fund, book):
    g = fut.groupby(fut.index // H * H)
    k = g.agg(o=('open', 'first'), h=('high', 'max'), l=('low', 'min'), c=('close', 'last'), qv=('quote_volume', 'sum'), tb=('taker_buy_quote_volume', 'sum'))
    gs = spot.groupby(spot.index // H * H)
    ks = gs.agg(qv=('quote_volume', 'sum'), tb=('taker_buy_quote_volume', 'sum')).reindex(k.index)
    close_t = k.index.values + H
    D = pd.DataFrame(index=close_t)
    c = k.c.values
    lc = np.log(c)
    r1 = pd.Series(np.r_[np.nan, np.diff(lc)], index=close_t)
    vol = r1.rolling(720, min_periods=240).std()
    for h_ in (1, 4, 24, 72):
        D[f'ret{h_}'] = (pd.Series(lc, index=close_t) - pd.Series(lc, index=close_t).shift(h_)) / (vol * math.sqrt(h_))
    hh, ll = pd.Series(k.h.values, index=close_t), pd.Series(k.l.values, index=close_t)
    cs = pd.Series(c, index=close_t)
    rng4 = hh.rolling(4).max() - ll.rolling(4).min()
    atr = rng4.rolling(56).mean()
    D['atrPct'] = atr / cs * 100
    D['rvRatio'] = r1.rolling(6).std() / r1.rolling(168).std()
    lo14, hi14 = ll.rolling(336).min(), hh.rolling(336).max()
    D['rangePos'] = (cs - lo14) / (hi14 - lo14)
    e80, e200 = cs.ewm(span=80, adjust=False).mean(), cs.ewm(span=200, adjust=False).mean()
    D['distEma'] = (cs - e80) / atr
    D['trend'] = (e80 - e200) / atr
    df_ = pd.Series(2 * k.tb.values - k.qv.values, index=close_t); qf = pd.Series(k.qv.values, index=close_t)
    ds_ = pd.Series(2 * ks.tb.values - ks.qv.values, index=close_t); qs = pd.Series(ks.qv.values, index=close_t)
    for h_ in (1, 4, 24):
        D[f'imbF{h_}'] = df_.rolling(h_).sum() / qf.rolling(h_).sum()
        D[f'imbS{h_}'] = ds_.rolling(h_).sum() / qs.rolling(h_).sum()
    D['imbDiv24'] = D.imbS24 - D.imbF24
    D['volRatio'] = qf.rolling(24).sum() / (qf.rolling(720, min_periods=240).sum() / 30)
    m = met.reindex(close_t, method='ffill')
    oi = pd.Series(m['sum_open_interest'].values, index=close_t)
    for h_ in (1, 4, 24):
        D[f'oiCh{h_}'] = np.log(oi / oi.shift(h_)) * 100
    D['oiPrice24'] = np.sign(D.ret24) * D.oiCh24
    f8 = fund['rate'] * (8 / fund['ih'].replace(0, 8)) * 100
    fr = f8.reindex(close_t, method='ffill')
    D['fund'] = fr.values
    D['fundDev'] = D.fund - pd.Series(D.fund.values, index=close_t).rolling(720, min_periods=240).mean()
    crowd = pd.Series(m['count_long_short_ratio'].values, index=close_t); top = pd.Series(m['sum_toptrader_long_short_ratio'].values, index=close_t)
    D['crowd'] = crowd; D['top'] = top
    D['crowdPct'] = rolling_pct(crowd.values, 480); D['topPct'] = rolling_pct(top.values, 480)
    D['crowdCh24'] = crowd - crowd.shift(24)
    D['crowdTopDiv'] = D.crowdPct - D.topPct
    if book is not None:
        b = book.reindex(close_t, method='ffill', tolerance=2 * H)
        for k_ in ('book1', 'book2', 'book5'):
            D[k_] = b[k_].values
        D['book2Ch4'] = D.book2 - pd.Series(D.book2.values, index=close_t).shift(4)
    else:
        for k_ in ('book1', 'book2', 'book5', 'book2Ch4'):
            D[k_] = np.nan
    th = thermo_series(k.rename(columns={'h': 'high', 'l': 'low', 'c': 'close'}))          # индекс = время открытия часа; P посчитан на окне, заканчивающемся этим часом
    D['thermoP'] = th['P'].reindex(k.index).values
    hour = ((close_t // H) % 24)
    D['hour'] = hour; D['dow'] = pd.to_datetime(close_t, unit='ms').dayofweek.values
    D['usOpen'] = ((hour >= 13) & (hour <= 16)).astype(float)
    # цели
    fwd4 = pd.Series(lc, index=close_t).shift(-4) - pd.Series(lc, index=close_t)
    fwd24 = pd.Series(lc, index=close_t).shift(-24) - pd.Series(lc, index=close_t)
    D['y4bps'] = fwd4 * 1e4; D['y24bps'] = fwd24 * 1e4
    D['y4'] = fwd4 / (vol * 2); D['y24'] = fwd24 / (vol * math.sqrt(24))
    fut_hi = hh[::-1].rolling(4).max()[::-1].shift(-1); fut_lo = ll[::-1].rolling(4).min()[::-1].shift(-1)
    D['big4'] = ((np.maximum(fut_hi, cs) - np.minimum(fut_lo, cs)) > 1.5 * atr).astype(float)
    D.loc[fut_hi.isna(), 'big4'] = np.nan
    D['coin'] = coin
    D['t'] = close_t
    return D


FEATS = [k for k in NAMES]


def spearman(a, b):
    m = ~(np.isnan(a) | np.isnan(b))
    if m.sum() < 200:
        return np.nan
    return float(pd.Series(a[m]).rank().corr(pd.Series(b[m]).rank()))


def ic_table(D):
    rows = []
    yrs = pd.to_datetime(D.t, unit='ms').dt.year.values
    for f in FEATS:
        x = D[f].values.astype(float)
        if np.isnan(x).all():
            rows.append({'key': f, 'name': NAMES[f], 'noData': True}); continue
        r = {'key': f, 'name': NAMES[f]}
        for tgt, neff in (('y4', 4), ('y24', 24)):
            y = D[tgt].values.astype(float)
            tr, te = D.t.values < SPLIT, D.t.values >= SPLIT
            ic_tr, ic_te = spearman(x[tr], y[tr]), spearman(x[te], y[te])
            n_te = int((~np.isnan(x[te]) & ~np.isnan(y[te])).sum())
            r[f'{tgt}_train'] = ic_tr; r[f'{tgt}_test'] = ic_te
            r[f'{tgt}_tTest'] = ic_te * math.sqrt(n_te / neff) if n_te and not np.isnan(ic_te) else None
            per = {}
            for coin in COINS:
                mc = (D.coin.values == coin)
                per[coin] = {'train': spearman(x[tr & mc], y[tr & mc]), 'test': spearman(x[te & mc], y[te & mc])}
            r[f'{tgt}_coins'] = per
            years = {}
            for y_ in sorted(set(yrs)):
                my = (yrs == y_)
                years[str(y_)] = spearman(x[my], y[my])
            r[f'{tgt}_years'] = years
            same = [np.sign(per[c]['train'] or 0) == np.sign(per[c]['test'] or 0) == np.sign(ic_tr or 0) for c in COINS]
            r[f'{tgt}_stable'] = bool(ic_tr is not None and ic_te is not None and not np.isnan(ic_tr) and not np.isnan(ic_te)
                                      and np.sign(ic_tr) == np.sign(ic_te) and abs(ic_te) >= 0.015 and sum(same) >= 2
                                      and abs(r[f'{tgt}_tTest'] or 0) >= 2)
        rows.append(r)
    return rows


def deciles(D, f, tgt_bps):
    tr = D.t.values < SPLIT
    out = {'train': [], 'test': []}
    for coin in COINS:
        mc = D.coin.values == coin
        x_tr = D.loc[tr & mc, f].dropna()
        if len(x_tr) < 500:
            continue
        edges = np.unique(np.quantile(x_tr, np.linspace(0, 1, 11)))
        edges[0], edges[-1] = -np.inf, np.inf
        D.loc[mc, '_dec'] = pd.cut(D.loc[mc, f], edges, labels=False, include_lowest=True)
    if '_dec' not in D:
        return None
    for part, mask in (('train', tr), ('test', ~tr)):
        g = D[mask].groupby('_dec')[tgt_bps].agg(['mean', 'count'])
        out[part] = [{'d': int(i) + 1, 'bps': round(float(v['mean']), 2), 'n': int(v['count'])} for i, v in g.iterrows()]
    del D['_dec']
    return out


def rule_search(D):
    """Правила «признак в верхнем/нижнем дециле → направление по train». Сделка держится 4ч или 24ч, без пересечений."""
    res = []
    tr = D.t.values < SPLIT
    for f in FEATS:
        if D[f].isna().all() or f in ('usOpen',):
            continue
        for horizon, tgt in ((4, 'y4bps'), (24, 'y24bps')):
            step = D.hour.values % horizon == 0 if horizon == 4 else D.hour.values == 0
            for side_dec in ('top', 'bottom'):
                sig = np.zeros(len(D), bool)
                for coin in COINS:
                    mc = D.coin.values == coin
                    x_tr = D.loc[tr & mc, f].dropna()
                    if len(x_tr) < 500:
                        continue
                    q = np.quantile(x_tr, 0.9 if side_dec == 'top' else 0.1)
                    sig |= mc & ((D[f].values >= q) if side_dec == 'top' else (D[f].values <= q))
                sig &= step & ~np.isnan(D[tgt].values)
                y = D[tgt].values
                if (sig & tr).sum() < 60 or (sig & ~tr).sum() < 40:
                    continue
                direction = 1 if np.mean(y[sig & tr]) > 0 else -1
                base_m = step & ~np.isnan(y)
                def st(mask):
                    r = direction * y[mask]
                    part = tr if mask[tr].any() and not mask[~tr].any() else ~tr
                    drift = float(np.nanmean(y[base_m & part]))
                    return {'n': int(mask.sum()), 'gross': round(float(r.mean()), 2), 'vsDrift': round(float(r.mean() - direction * drift), 2), 'netTaker': round(float(r.mean() - COST_TAKER), 2),
                            'netMaker': round(float(r.mean() - COST_MAKER), 2), 'hit': round(100 * float((r > 0).mean()), 1),
                            't': round(float(r.mean() / (r.std(ddof=1) / math.sqrt(len(r)))), 2) if len(r) > 2 and r.std() > 0 else None}
                res.append({'feature': f, 'name': NAMES[f], 'when': 'высокий (верхние 10%)' if side_dec == 'top' else 'низкий (нижние 10%)',
                            'horizonH': horizon, 'side': 'long' if direction == 1 else 'short', 'train': st(sig & tr), 'test': st(sig & ~tr)})
    good = [r for r in res if r['train']['gross'] > COST_MAKER and r['test']['gross'] > 0]
    good.sort(key=lambda r: -(r['test']['t'] or 0))
    stable = [r for r in good if (r['train']['t'] or 0) >= 1.5 and (r['test']['t'] or 0) >= 2 and r['test']['netMaker'] > 0 and r['test']['vsDrift'] > 0]
    return {'all': len(res), 'passedTrain': sum(r['train']['gross'] > COST_MAKER for r in res), 'top': good[:15], 'stable': stable[:10],
            'testT2': sum((r['test']['t'] or 0) >= 2 for r in res), 'expectedByChance': round(0.025 * len(res), 1)}


def walk_forward(D, target, horizon):
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    from sklearn.pipeline import make_pipeline
    from sklearn.impute import SimpleImputer
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.metrics import roc_auc_score
    feats = [f for f in FEATS if not D[f].isna().all()] + ['hs', 'hc']
    D = D.copy()
    D['hs'] = np.sin(2 * np.pi * D.hour / 24); D['hc'] = np.cos(2 * np.pi * D.hour / 24)
    D = D[~D[target].isna()].reset_index(drop=True)
    y = (D[target].values > 0).astype(int)
    X = D[feats].values
    starts = pd.date_range('2024-01-01', pd.to_datetime(D.t.max(), unit='ms'), freq='QS', tz='UTC')
    preds = {'logit': np.full(len(D), np.nan), 'gbm': np.full(len(D), np.nan)}
    last_models = {}
    for i, s in enumerate(starts):
        s_ms = int(s.timestamp() * 1000)
        e_ms = int(starts[i + 1].timestamp() * 1000) if i + 1 < len(starts) else int(D.t.max()) + 1
        trm = D.t.values < s_ms - 24 * H
        tem = (D.t.values >= s_ms) & (D.t.values < e_ms)
        if trm.sum() < 2000 or tem.sum() == 0:
            continue
        Xtr, Xte = X[trm], X[tem]
        lg = make_pipeline(SimpleImputer(strategy='median'), StandardScaler(), LogisticRegression(C=0.05, max_iter=500))
        lg.fit(Xtr, y[trm]); preds['logit'][tem] = lg.predict_proba(Xte)[:, 1]
        gb = HistGradientBoostingClassifier(max_depth=3, learning_rate=0.04, max_iter=250, min_samples_leaf=300, l2_regularization=1.0, random_state=1)
        gb.fit(X[trm], y[trm]); preds['gbm'][tem] = gb.predict_proba(X[tem])[:, 1]
        last_models = {'logit': lg, 'gbm': gb, 'Xte': X[tem], 'yte': y[tem]}
    out = {}
    bps = D['y4bps' if horizon == 4 else 'y24bps'].values
    step = (D.hour.values % horizon == 0) if horizon == 4 else (D.hour.values == 0)
    for name, p in preds.items():
        ok = ~np.isnan(p)
        if ok.sum() < 500:
            continue
        r = {'auc': round(float(roc_auc_score(y[ok], p[ok])), 4)}
        ok25 = ok & (D.t.values >= SPLIT)
        r['auc2025'] = round(float(roc_auc_score(y[ok25], p[ok25])), 4) if ok25.sum() > 500 else None
        r['acc'] = round(100 * float(((p[ok] > 0.5) == y[ok]).mean()), 2)
        trades = {}
        for th in (0.52, 0.55, 0.58):
            sig = ok & step & ((p > th) | (p < 1 - th))
            dirn = np.where(p > 0.5, 1, -1)
            rr = dirn[sig] * bps[sig]
            if len(rr) < 30:
                continue
            ts = D.t.values[sig]
            months = pd.Series(rr - COST_MAKER, index=pd.to_datetime(ts, unit='ms')).resample('ME').sum()
            trades[str(th)] = {'n': int(len(rr)), 'gross': round(float(rr.mean()), 2), 'netTaker': round(float(rr.mean() - COST_TAKER), 2),
                               'netMaker': round(float(rr.mean() - COST_MAKER), 2), 'hit': round(100 * float((rr > 0).mean()), 1),
                               't': round(float(rr.mean() / (rr.std(ddof=1) / math.sqrt(len(rr)))), 2),
                               'equityMaker': [[int(t.timestamp() * 1000), round(float(v), 1)] for t, v in months.cumsum().items()]}
        r['trades'] = trades
        out[name] = r
    # важность признаков у логистической модели последнего квартала
    if last_models:
        coef = last_models['logit'][-1].coef_[0]
        out['logitWeights'] = sorted([{'key': f, 'name': NAMES.get(f, 'Время суток'), 'w': round(float(w), 4)} for f, w in zip(feats, coef)], key=lambda x: -abs(x['w']))[:15]
        try:
            from sklearn.inspection import permutation_importance
            Xte, yte = last_models['Xte'], last_models['yte']
            if len(yte) > 300:
                pi = permutation_importance(last_models['gbm'], Xte, yte, n_repeats=3, random_state=1, scoring='roc_auc')
                out['gbmImportance'] = sorted([{'key': f, 'name': NAMES.get(f, 'Время суток'), 'imp': round(float(v), 4)} for f, v in zip(feats, pi.importances_mean)], key=lambda x: -x['imp'])[:12]
        except Exception as e:
            out['gbmImportance'] = [{'error': repr(e)[:100]}]
    return out


def vol_model(D):
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.metrics import roc_auc_score
    feats = ['atrPct', 'rvRatio', 'volRatio', 'oiCh1', 'oiCh4', 'oiCh24', 'imbF1', 'imbF4', 'fund', 'fundDev', 'crowdPct', 'book1', 'book2', 'book5', 'thermoP', 'usOpen', 'ret1', 'ret4', 'ret24']
    feats = [f for f in feats if not D[f].isna().all()] + ['hour']
    D = D[~D.big4.isna() & ~D.thermoP.isna()].reset_index(drop=True)
    tr, te = D.t.values < SPLIT, D.t.values >= SPLIT
    y = D.big4.values.astype(int)
    gb = HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=250, min_samples_leaf=300, random_state=1)
    gb.fit(D.loc[tr, feats].values, y[tr]); p = gb.predict_proba(D.loc[te, feats].values)[:, 1]
    out = {'baseRate': round(100 * float(y[te].mean()), 1),
           'aucThermo': round(float(roc_auc_score(y[te], D.thermoP.values[te])), 4),
           'aucModel': round(float(roc_auc_score(y[te], p)), 4), 'calib': []}
    for a, b in [(0, .1), (.1, .2), (.2, .3), (.3, .4), (.4, .6), (.6, 1.01)]:
        m = (p >= a) & (p < b)
        if m.sum() > 50:
            out['calib'].append({'from': round(a * 100), 'to': round(min(b, 1) * 100), 'n': int(m.sum()), 'predicted': round(100 * float(p[m].mean()), 1), 'actual': round(100 * float(y[te][m].mean()), 1)})
    return out


def by_time(D):
    tr = D.t.values < SPLIT
    hours = []
    for h_ in range(24):
        m = D.hour.values == h_
        hours.append({'h': h_, 'trainBps': round(float(np.nanmean(D.y4bps.values[m & tr])), 2), 'testBps': round(float(np.nanmean(D.y4bps.values[m & ~tr])), 2),
                      'absBps': round(float(np.nanmean(np.abs(D.y4bps.values[m]))), 1)})
    dows = []
    for d in range(7):
        m = (D.dow.values == d) & (D.hour.values == 0)
        dows.append({'d': d, 'trainBps': round(float(np.nanmean(D.y24bps.values[m & tr])), 1), 'testBps': round(float(np.nanmean(D.y24bps.values[m & ~tr])), 1)})
    return {'hours': hours, 'dows': dows}


def baselines(D):
    out = {}
    for horizon, tgt in ((4, 'y4bps'), (24, 'y24bps')):
        step = (D.hour.values % horizon == 0) if horizon == 4 else (D.hour.values == 0)
        for part, mask in (('train', D.t.values < SPLIT), ('test', D.t.values >= SPLIT)):
            m = step & mask & ~np.isnan(D[tgt].values)
            y = D[tgt].values[m]
            mom = np.sign(D.ret24.values[m]) * y
            out[f'{horizon}h_{part}'] = {'alwaysLong': round(float(y.mean()), 2), 'momentum24': round(float(np.nanmean(mom)), 2), 'n': int(m.sum())}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--synthetic', action='store_true')
    ap.add_argument('--start', default='2023-01-01')
    ap.add_argument('--end', default=None)
    ap.add_argument('--out', default='backtest/research.json')
    a = ap.parse_args()
    start = date.fromisoformat(a.start)
    end = date.fromisoformat(a.end) if a.end else (datetime.now(timezone.utc).date() - timedelta(days=1))
    os.makedirs(CACHE, exist_ok=True)
    t0 = time.time(); frames = []
    with ThreadPoolExecutor(24) as pool:
        for i, coin in enumerate(COINS):
            sym = coin + 'USDT'
            if a.synthetic:
                fut, spot, met, fund, _ = synthetic(start, end, seed=i + 1)
                t5 = met.index.values
                rng = np.random.default_rng(i)
                book = pd.DataFrame({f'book{k}': np.clip(50 + rng.normal(0, 6, len(t5)), 5, 95) for k in (1, 2, 5)}, index=(t5 // H) * H + H)
                book = book[~book.index.duplicated()]
            else:
                fut = load_klines('fut', sym, start, end, pool); spot = load_klines('spot', sym, start, end, pool)
                met = load_metrics(sym, start, end, pool); fund = load_funding(sym, start, end, pool)
                book = load_book_multi(sym, start, end, pool)
            frames.append(hourly_frame(coin, fut, spot, met, fund, book))
            print(coin, 'frame', len(frames[-1]), round(time.time() - t0), flush=True)
    D = pd.concat(frames, ignore_index=True)
    D = D[D.t >= D.t.min() + 45 * 24 * H].reset_index(drop=True)
    D[FEATS] = D[FEATS].replace([np.inf, -np.inf], np.nan)
    for f in FEATS:   # подрезаем выбросы по каждой монете (0.1% с каждой стороны)
        for coin in COINS:
            m = D.coin == coin
            lo, hi = D.loc[m, f].quantile([0.001, 0.999])
            D.loc[m, f] = D.loc[m, f].clip(lo, hi)
    print('rows', len(D), flush=True)
    res = {'generated': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'synthetic': a.synthetic,
           'split': SPLIT, 'period': {'from': int(D.t.min()), 'to': int(D.t.max())}, 'costs': {'taker': COST_TAKER, 'maker': COST_MAKER},
           'rows': int(len(D)), 'names': NAMES}
    res['ic'] = ic_table(D); print('ic', round(time.time() - t0), flush=True)
    stable4 = sorted([r for r in res['ic'] if r.get('y4_stable')], key=lambda r: -abs(r['y4_test']))
    stable24 = sorted([r for r in res['ic'] if r.get('y24_stable')], key=lambda r: -abs(r['y24_test']))
    pick = []
    for r in stable4 + stable24 + sorted([r for r in res['ic'] if not r.get('noData')], key=lambda r: -abs(r.get('y4_train') or 0)):
        if r['key'] not in pick:
            pick.append(r['key'])
    res['deciles'] = {}
    for f in pick[:12]:
        res['deciles'][f] = {'y4': deciles(D, f, 'y4bps'), 'y24': deciles(D, f, 'y24bps')}
    print('deciles', round(time.time() - t0), flush=True)
    res['rules'] = rule_search(D); print('rules', round(time.time() - t0), flush=True)
    res['models'] = {'h4': walk_forward(D, 'y4', 4), 'h24': walk_forward(D, 'y24', 24)}; print('models', round(time.time() - t0), flush=True)
    res['vol'] = vol_model(D); print('vol', round(time.time() - t0), flush=True)
    res['time'] = by_time(D)
    res['baselines'] = baselines(D)
    with open(a.out, 'w') as f:
        json.dump(clean(res), f, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    print('saved', a.out, os.path.getsize(a.out), round(time.time() - t0), flush=True)


if __name__ == '__main__':
    main()
