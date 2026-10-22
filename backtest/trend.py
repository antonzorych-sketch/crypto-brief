"""
Трендовая торговля: определение тренда и входы по тренду.

Все стратегии исполняются по часовым свечам Binance фьючерсов:
  - вход по цене закрытия часа сигнала + комиссия по рынку 4.5 б.п. (лимитный вход — 1.5 б.п., если стратегия ставит лимит);
  - стоп проверяется по минимуму/максимуму каждого следующего часа; гэп через стоп — выход по открытию;
  - одна позиция на монету, фиксированный размер.
Подбор параметров — 2023–2024, проверка — 2025–2026.
"""
import argparse, itertools, json, math, os, sys, time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from run import COINS, H, CACHE, load_klines, load_metrics, load_funding, synthetic, clean
from research import FEATS, hourly_frame, load_book_multi
from v2 import wf_predict

SPLIT = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
MAKER, TAKER = 1.5, 4.5


def prep(coin, fut, Dc):
    g = fut.groupby(fut.index // H * H)
    k = g.agg(o=('open', 'first'), h=('high', 'max'), l=('low', 'min'), c=('close', 'last'))
    k.index = k.index + H                     # время закрытия часа
    k = k.join(Dc.set_index('t')[['ret24', 'distEma', 'book5', 'pdir', 'pvol']], how='left')
    c = k.c
    k['emaF'] = c.ewm(span=80, adjust=False).mean()      # ≈ EMA20 на 4ч
    k['emaM'] = c.ewm(span=200, adjust=False).mean()     # ≈ EMA50 на 4ч
    k['emaS'] = c.ewm(span=800, adjust=False).mean()     # ≈ EMA200 на 4ч
    tr = np.maximum(k.h - k.l, np.maximum((k.h - c.shift()).abs(), (k.l - c.shift()).abs()))
    k['atr'] = tr.rolling(24 * 14).mean() * math.sqrt(24)   # дневной ATR-эквивалент из часовых
    lc = np.log(c)
    x = np.arange(168) - 83.5
    k['slope'] = lc.rolling(168).apply(lambda y: np.dot(x, y - y.mean()) / np.dot(x, x), raw=True) * 168 / (lc.diff().rolling(720).std() * math.sqrt(168))
    for n in (10, 20, 55):
        k[f'hi{n}'] = k.h.rolling(24 * n).max().shift(1); k[f'lo{n}'] = k.l.rolling(24 * n).min().shift(1)
    k['coin'] = coin
    return k.dropna(subset=['atr', 'emaS', 'hi55'])


def trend_state(k, kind):
    if kind == 'ema':
        return np.where((k.c > k.emaM) & (k.emaM > k.emaS), 1, np.where((k.c < k.emaM) & (k.emaM < k.emaS), -1, 0))
    if kind == 'slope':
        return np.where(k.slope > 1.0, 1, np.where(k.slope < -1.0, -1, 0))
    if kind == 'donch':
        up = (k.c >= k.hi20 * 0.98); dn = (k.c <= k.lo20 * 1.02)
        return np.where(up, 1, np.where(dn, -1, 0))
    return np.zeros(len(k), int)


def run_strategy(k, cfg):
    """Возвращает список сделок: вход по сигналу, выход по стопу/цели/времени/развороту."""
    o, h, l, c, atr = (k[x].values for x in ('o', 'h', 'l', 'c', 'atr'))
    t = k.index.values
    trend = trend_state(k, cfg.get('trend', 'none')) if cfg.get('trend') else np.zeros(len(k), int)
    st = cfg['type']
    trades = []; pos = 0; i_in = 0; px = 0.0; stop = 0.0; tgt = None; ext = 0.0; cost_in = TAKER
    entry_sig = np.zeros(len(k), int)
    if st == 'breakout':
        n = cfg['n']
        entry_sig = np.where(c > k[f'hi{n}'].values, 1, np.where(c < k[f'lo{n}'].values, -1, 0))
        if cfg.get('trend'):
            entry_sig = np.where(entry_sig == trend, entry_sig, 0)
    elif st == 'pullback':
        z = cfg['z']
        r24 = k.ret24.values; de = k.distEma.values
        entry_sig = np.where((trend == 1) & ((r24 <= -z) | (de <= -z)), 1, np.where((trend == -1) & ((r24 >= z) | (de >= z)), -1, 0))
    elif st == 'cross':
        f, s = k.emaF.values, k.emaM.values
        entry_sig = np.where(f > s, 1, -1)
    elif st == 'model':
        p = k.pdir.values; pv = k.pvol.values
        entry_sig = np.where((p > cfg['th']) & (trend == 1), 1, np.where((p < 1 - cfg['th']) & (trend == -1), -1, 0))
        entry_sig = np.where(np.nan_to_num(pv) >= cfg.get('vth', 0), entry_sig, 0)
        entry_sig = np.where(np.isnan(p), 0, entry_sig)
    for i in range(1, len(k)):
        if pos != 0:
            # стоп/цель по текущему часу
            exit_px = None; why = None
            if pos == 1:
                if o[i] <= stop: exit_px, why = o[i], 'стоп'
                elif l[i] <= stop: exit_px, why = stop, 'стоп'
                elif tgt is not None and h[i] >= tgt: exit_px, why = tgt, 'цель'
            else:
                if o[i] >= stop: exit_px, why = o[i], 'стоп'
                elif h[i] >= stop: exit_px, why = stop, 'стоп'
                elif tgt is not None and l[i] <= tgt: exit_px, why = tgt, 'цель'
            if exit_px is None and st == 'cross' and entry_sig[i] != pos:
                exit_px, why = c[i], 'разворот'
            if exit_px is None and cfg.get('maxH') and i - i_in >= cfg['maxH']:
                exit_px, why = c[i], 'время'
            if exit_px is not None:
                bps = pos * (exit_px / px - 1) * 1e4 - cost_in - (MAKER if why == 'цель' else TAKER)
                trades.append({'coin': k.coin.iloc[0], 't': int(t[i_in]), 'tOut': int(t[i]), 'side': 'long' if pos == 1 else 'short', 'bps': bps, 'why': why, 'holdH': int(i - i_in), 'px': float(px), 'exit': float(exit_px)})
                pos = 0
                if st != 'cross':
                    continue
            else:
                # трейлинг-стоп по закрытию часа
                if cfg.get('trail'):
                    if pos == 1:
                        ext = max(ext, h[i]); stop = max(stop, ext - cfg['trail'] * atr[i])
                    else:
                        ext = min(ext, l[i]); stop = min(stop, ext + cfg['trail'] * atr[i])
                continue
        sgn = entry_sig[i]
        if sgn == 0 or np.isnan(atr[i]):
            continue
        pos, i_in, px, ext, cost_in = sgn, i, c[i], c[i], TAKER
        stop = px - sgn * cfg['stop'] * atr[i]
        tgt = px + sgn * cfg['tp'] * atr[i] if cfg.get('tp') else None
    return trades


def summ(tr, years):
    if not tr:
        return {'n': 0}
    b = np.array([x['bps'] for x in tr])
    ret = b.sum() / len(COINS) / 100              # % на капитал: равные доли на 3 монеты, плечо 1×
    eq = np.cumsum(b) / len(COINS) / 100
    dd = float((eq - np.maximum.accumulate(np.r_[0, eq])[1:]).min())
    return {'n': int(len(b)), 'avg': round(float(b.mean()), 1), 'win': round(100 * float((b > 0).mean()), 1),
            't': round(float(b.mean() / (b.std(ddof=1) / math.sqrt(len(b)))), 2) if len(b) > 2 and b.std() > 0 else None,
            'retPct': round(float(ret), 1), 'annPct': round(float(ret / years), 1), 'maxDDPct': round(dd, 1),
            'holdH': round(float(np.mean([x['holdH'] for x in tr])), 0)}


def label(cfg):
    tr = {'ema': 'тренд по EMA 4ч', 'slope': 'тренд по наклону недели', 'donch': 'тренд по 20-дневному каналу', None: 'без фильтра тренда'}[cfg.get('trend')]
    if cfg['type'] == 'breakout':
        s = f"пробой {cfg['n']}-дневного максимума/минимума"
    elif cfg['type'] == 'pullback':
        s = f"откат против тренда ≥{cfg['z']}σ → вход по тренду"
    elif cfg['type'] == 'cross':
        s = 'пересечение EMA20/50 (4ч), всегда в рынке'
    else:
        s = f"модель ≥{round(cfg['th'] * 100)}% по тренду" + (f", движение ≥{round(cfg['vth'] * 100)}%" if cfg.get('vth') else '')
    x = f"стоп {cfg['stop']} ATR" + (f", трейлинг {cfg['trail']} ATR" if cfg.get('trail') else '') + (f", цель {cfg['tp']} ATR" if cfg.get('tp') else '') + (f", максимум {cfg['maxH'] // 24 if cfg['maxH'] >= 24 else cfg['maxH']}{'д' if cfg['maxH'] >= 24 else 'ч'}" if cfg.get('maxH') else '')
    return f'{s}; {tr}; {x}'


def grid():
    G = []
    for n, trail, stop, tf in itertools.product((10, 20, 55), (2.0, 3.0, 4.0), (2.0, 3.0), (None, 'ema')):
        G.append({'type': 'breakout', 'n': n, 'trail': trail, 'stop': stop, 'trend': tf})
    for z, tf, (stop, tp, mh) in itertools.product((0.5, 1.0, 1.5), ('ema', 'slope', 'donch'), ((1.0, 2.0, 72), (1.5, 3.0, 120), (1.0, None, 120), (1.5, None, 240))):
        cfg = {'type': 'pullback', 'z': z, 'trend': tf, 'stop': stop, 'tp': tp, 'maxH': mh}
        if tp is None:
            cfg['trail'] = 2.0
        G.append(cfg)
    for stop in (2.0, 3.0, 5.0):
        G.append({'type': 'cross', 'stop': stop, 'trend': None})
    for th, tf, vth, (stop, tp, mh) in itertools.product((0.55, 0.58), ('ema', 'slope'), (0.0, 0.25), ((0.5, 1.0, 8), (1.0, 2.0, 24), (1.0, None, 24))):
        cfg = {'type': 'model', 'th': th, 'trend': tf, 'vth': vth, 'stop': stop, 'tp': tp, 'maxH': mh}
        if tp is None:
            cfg['trail'] = 1.0
        G.append(cfg)
    return G


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--synthetic', action='store_true')
    ap.add_argument('--start', default='2023-01-01')
    ap.add_argument('--end', default=None)
    ap.add_argument('--out', default='backtest/trend.json')
    a = ap.parse_args()
    start = date.fromisoformat(a.start)
    end = date.fromisoformat(a.end) if a.end else (datetime.now(timezone.utc).date() - timedelta(days=1))
    os.makedirs(CACHE, exist_ok=True)
    t0 = time.time(); frames, futs = [], {}
    with ThreadPoolExecutor(24) as pool:
        for i, coin in enumerate(COINS):
            sym = coin + 'USDT'
            if a.synthetic:
                fut, spot, met, fund, _ = synthetic(start, end, seed=i + 1)
                t5 = met.index.values; rng = np.random.default_rng(i)
                book = pd.DataFrame({f'book{k}': np.clip(50 + rng.normal(0, 6, len(t5)), 5, 95) for k in (1, 2, 5)}, index=(t5 // H) * H + H)
                book = book[~book.index.duplicated()]
            else:
                fut = load_klines('fut', sym, start, end, pool); spot = load_klines('spot', sym, start, end, pool)
                met = load_metrics(sym, start, end, pool); fund = load_funding(sym, start, end, pool)
                book = load_book_multi(sym, start, end, pool)
            frames.append(hourly_frame(coin, fut, spot, met, fund, book)); futs[coin] = fut
            print(coin, 'frame', round(time.time() - t0), flush=True)
    D = pd.concat(frames, ignore_index=True)
    D = D[D.t >= D.t.min() + 45 * 24 * H].reset_index(drop=True)
    D[FEATS] = D[FEATS].replace([np.inf, -np.inf], np.nan)
    D['hs'] = np.sin(2 * np.pi * D.hour / 24); D['hc'] = np.cos(2 * np.pi * D.hour / 24)
    D['pdir'] = wf_predict(D, 'dir'); D['pvol'] = wf_predict(D, 'vol')
    K = {coin: prep(coin, futs[coin], D[D.coin == coin]) for coin in COINS}
    print('prep', round(time.time() - t0), flush=True)
    t_first = min(int(K[c].index.min()) for c in COINS); t_last = max(int(K[c].index.max()) for c in COINS)
    yrs_tr = (SPLIT - t_first) / (365.25 * 24 * H); yrs_te = (t_last - SPLIT) / (365.25 * 24 * H)
    # купить и держать — для сравнения
    bh = {}
    for part, (a_, b_) in (('train', (t_first, SPLIT)), ('test', (SPLIT, t_last))):
        r = []
        for coin in COINS:
            kk = K[coin]; seg = kk[(kk.index >= a_) & (kk.index <= b_)]
            r.append((seg.c.iloc[-1] / seg.c.iloc[0] - 1) * 100)
        bh[part] = round(float(np.mean(r)), 1)
    rows = []
    for cfg in grid():
        tr = []
        for coin in COINS:
            tr += run_strategy(K[coin], cfg)
        trn = [x for x in tr if x['t'] < SPLIT]; tst = [x for x in tr if x['t'] >= SPLIT]
        if cfg['type'] == 'model':     # прогнозы модели есть только с 2024
            yrs_m = (SPLIT - int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)) / (365.25 * 24 * H)
            rows.append({'cfg': cfg, 'label': label(cfg), 'train': summ(trn, yrs_m), 'test': summ(tst, yrs_te), '_tr': tr})
        else:
            rows.append({'cfg': cfg, 'label': label(cfg), 'train': summ(trn, yrs_tr), 'test': summ(tst, yrs_te), '_tr': tr})
    print('grid', len(rows), round(time.time() - t0), flush=True)
    fam_names = {'breakout': 'Пробой с трейлинг-стопом', 'pullback': 'Откат по тренду', 'cross': 'Пересечение средних', 'model': 'Модель только по тренду'}
    fams = {}
    for fam in fam_names:
        rr = [r for r in rows if r['cfg']['type'] == fam and r['train'].get('n', 0) >= 20]
        if not rr:
            continue
        rr.sort(key=lambda r: -(r['train']['annPct'] / max(1.0, abs(r['train']['maxDDPct']))))
        best = rr[0]
        trd = best['_tr']
        eq, s_ = [], 0.0
        for x in sorted(trd, key=lambda x: x['tOut']):
            s_ += x['bps'] / len(COINS) / 100; eq.append([x['tOut'], round(s_, 2)])
        fams[fam] = {'name': fam_names[fam], 'best': {k: best[k] for k in ('cfg', 'label', 'train', 'test')}, 'equity': eq[-800:] if len(eq) <= 800 else [eq[int(i * len(eq) / 800)] for i in range(800)],
                     'byCoin': {c: {'train': summ([x for x in trd if x['coin'] == c and x['t'] < SPLIT], yrs_tr), 'test': summ([x for x in trd if x['coin'] == c and x['t'] >= SPLIT], yrs_te)} for c in COINS},
                     'bySide': {s: {'train': summ([x for x in trd if x['side'] == s and x['t'] < SPLIT], yrs_tr), 'test': summ([x for x in trd if x['side'] == s and x['t'] >= SPLIT], yrs_te)} for s in ('long', 'short')},
                     'positiveTestShare': round(100 * sum(1 for r in rr if r['test'].get('annPct', -1) > 0) / len(rr), 0), 'nVariants': len(rr),
                     'top': [{k: r[k] for k in ('label', 'train', 'test')} for r in rr[:8]],
                     'recent': [{k: (round(v, 1) if isinstance(v, float) else v) for k, v in x.items()} for x in sorted(trd, key=lambda x: x['t'])[-12:]]}
    # текущее состояние тренда по монетам
    now = {}
    for coin in COINS:
        kk = K[coin].iloc[-1]
        now[coin] = {'ema': int(trend_state(K[coin].iloc[-1:], 'ema')[0]), 'slope': round(float(kk.slope), 2), 'slopeState': int(trend_state(K[coin].iloc[-1:], 'slope')[0]),
                     'price': float(kk.c), 'emaM': float(kk.emaM), 'emaS': float(kk.emaS), 'hi20': float(kk.hi20), 'lo20': float(kk.lo20)}
    res = {'generated': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'synthetic': a.synthetic, 'split': SPLIT,
           'period': {'from': t_first, 'to': t_last}, 'years': {'train': round(yrs_tr, 2), 'test': round(yrs_te, 2)}, 'buyHold': bh,
           'families': fams, 'trendNow': now, 'nVariants': len(rows), 'costs': {'maker': MAKER, 'taker': TAKER}}
    with open(a.out, 'w') as f:
        json.dump(clean(res), f, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    for fam, v in fams.items():
        print(fam, v['best']['label'], v['best']['train'], v['best']['test'], v['positiveTestShare'], flush=True)
    print('saved', a.out, os.path.getsize(a.out), round(time.time() - t0), flush=True)


if __name__ == '__main__':
    main()
