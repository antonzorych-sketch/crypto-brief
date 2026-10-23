"""pm/report: сводка по всем paper-прогонам pm/lag -> pm/paper/REPORT.md
Отвечает на вопросы: есть ли лаг (сколько секунд Polymarket догоняет нашу справедливую цену),
точнее ли наша модель цены рынка (Brier, калибровка), что дают виртуальные сделки после комиссий,
и находит ли сканер pm/arb безрисковые связки. Пишет также pm/paper/summary.json для дашборда."""
import glob, gzip, json, math, os, statistics, time
from collections import defaultdict

R = 'pm/paper'


def rows(pat):
    out = []
    for f in sorted(glob.glob(os.path.join(R, '*', pat))):
        op = gzip.open if f.endswith('.gz') else open
        try:
            with op(f, 'rt') as h:
                out += [json.loads(x) for x in h if x.strip()]
        except Exception: pass
    return out


def main():
    st, tr, lg, wn, sn = rows('settled.jsonl'), rows('trades.jsonl'), rows('lag.jsonl'), rows('windows.jsonl'), rows('snaps.jsonl.gz')
    ast, atr, anr = rows('arb_settled.jsonl'), rows('arb_trades.jsonl'), rows('arb_near.jsonl')
    S = {'updated': time.time(), 'lag': {}, 'arb': {}}
    L = ['# Polymarket US — paper: лаг цены', '']
    runs = sorted({os.path.basename(os.path.dirname(f)) for f in glob.glob(os.path.join(R, '*', 'trades.jsonl'))} |
                  {os.path.basename(os.path.dirname(f)) for f in glob.glob(os.path.join(R, '*', 'windows.jsonl'))})
    L.append(f'Прогонов: {len(runs)} ({runs[0] if runs else "-"} … {runs[-1] if runs else "-"}), окон рассчитано: {len(wn)}')
    agree = [w for w in wn if w.get('own') and w.get('official')]
    if agree:
        L.append(f'Наш прокси BRTI совпал с официальным исходом: {sum(w["own"] == w["official"] for w in agree)}/{len(agree)}')
    L += ['', '## Сделки']
    by = defaultdict(int)
    for t in tr: by[t.get('status')] += 1
    L.append('Сигналы: ' + ', '.join(f'{k} {v}' for k, v in by.items()) if by else 'Сигналов пока нет.')
    if st:
        pnl = sum(x['pnl'] for x in st); cost = sum(x['px'] * x['qty'] for x in st)
        wins = sum(x['side'] == x['result'] for x in st)
        L.append(f'Закрыто {len(st)}, побед {wins} ({wins / len(st):.0%}), P&L ${pnl:+.2f} на оборот ${cost:.2f} ({pnl / cost:+.1%}), комиссии ${sum(x["fee"] + (x.get("exit") or {}).get("fee", 0) for x in st):.2f}')
        ws = [x for x in st if x.get('src') == 'ws']
        if ws: L.append(f'Из них на данных WebSocket (надёжные): {len(ws)}, P&L ${sum(x["pnl"] for x in ws):+.2f}. Остальные — на публичном REST, он запаздывает, им доверять меньше.')
        else: L.append('Все сделки пока на публичном REST, он запаздывает — этим цифрам доверять нельзя.')
        S['lag'].update(trades=len(st), wins=wins, pnl=round(pnl, 2), turnover=round(cost, 2), ws_trades=len(ws), ws_pnl=round(sum(x['pnl'] for x in ws), 2))
        cur = 0; S['lag']['curve'] = []
        for x in sorted(st, key=lambda x: x['t']): cur += x['pnl']; S['lag']['curve'].append([round(x['t']), round(cur, 2), x.get('src', 'rest')])
        L += ['', '| Окно | Сделок | Побед | P&L $ | Ср. перевес |', '|---|---|---|---|---|']
        g = defaultdict(list)
        for x in st: g[f"{x.get('coin', 'btc').upper()} {x['tf']}"].append(x)
        for k, v in sorted(g.items()):
            L.append(f'| {k} | {len(v)} | {sum(x["side"] == x["result"] for x in v)} | {sum(x["pnl"] for x in v):+.2f} | {statistics.fmean(x["edge"] for x in v):.3f} |')
        L += ['', '| Тип входа | Сделок | Побед % | P&L $ | Если держать до конца $ | Вышли досрочно |', '|---|---|---|---|---|---|']
        g = defaultdict(list)
        for x in st: g[x.get('reason', 'старая')].append(x)
        for k, v in sorted(g.items()):
            L.append(f'| {k} | {len(v)} | {sum(x["side"] == x["result"] for x in v) / len(v):.0%} | {sum(x["pnl"] for x in v):+.2f} | '
                     f'{sum(x.get("pnl_hold", x["pnl"]) for x in v):+.2f} | {sum(bool(x.get("exit")) for x in v)} |')
        S['lag']['by_reason'] = {k: {'n': len(v), 'pnl': round(sum(x['pnl'] for x in v), 2), 'hold': round(sum(x.get('pnl_hold', x['pnl']) for x in v), 2)} for k, v in g.items()}
        g2 = defaultdict(list)
        for x in st: g2[f"{x.get('coin', 'btc').upper()} {x['tf']}"].append(x)
        S['lag']['by_market'] = {k: {'n': len(v), 'pnl': round(sum(x['pnl'] for x in v), 2)} for k, v in g2.items()}
        L += ['', '| Перевес | Сделок | Побед % | P&L $ |', '|---|---|---|---|']
        b = defaultdict(list)
        for x in st: b['0.04–0.07' if x['edge'] < 0.07 else '0.07–0.12' if x['edge'] < 0.12 else '0.12+'].append(x)
        for k in ('0.04–0.07', '0.07–0.12', '0.12+'):
            if b[k]: L.append(f'| {k} | {len(b[k])} | {sum(x["side"] == x["result"] for x in b[k]) / len(b[k]):.0%} | {sum(x["pnl"] for x in b[k]):+.2f} |')
    L += ['', '## Ликвидность (доля времени, когда спред > 4¢ — бот не торгует)']
    g = defaultdict(lambda: [0, 0])
    for w in wn:
        if w.get('polls'): k = f"{w.get('coin', 'btc').upper()} {w['tf']}"; g[k][0] += w['illiquid']; g[k][1] += w['polls']
    L.append(', '.join(f'{k}: {a / b:.0%}' for k, (a, b) in sorted(g.items())) if g else 'Данных пока нет.')
    S['liquidity'] = {k: round(a / b, 3) for k, (a, b) in g.items()}
    L += ['', '## Лаг']
    if lg:
        s = sorted(x['secs'] for x in lg)
        L.append(f'Расхождений >5¢ между нашей ценой и mid Polymarket: {len(s)}. Сколько держатся: медиана {statistics.median(s):.0f} с, 75% — {s[int(len(s) * .75)]:.0f} с, 90% — {s[int(len(s) * .9)]:.0f} с')
        g = defaultdict(list)
        for x in lg: g[x['tf']].append(x['secs'])
        L.append('По окнам: ' + ', '.join(f'{k}: {len(v)} шт, медиана {statistics.median(v):.0f} с' for k, v in sorted(g.items())))
        S['lagstat'] = {'n': len(s), 'median': statistics.median(s), 'p90': s[int(len(s) * .9)]}
    else: L.append('Данных пока нет.')
    L += ['', '## Калибровка (Brier, меньше — лучше)']
    res = {w['slug']: (1 if (w.get('official') or w.get('own')) == 'UP' else 0) for w in wn if (w.get('official') or w.get('own'))}
    bf, bm = [], []
    for x in sn:
        y = res.get(x['slug'])
        if y is None or x.get('fair') is None or x.get('bid') is None or x.get('ask') is None: continue
        bf.append((x['fair'] - y) ** 2); bm.append(((x['bid'] + x['ask']) / 2 - y) ** 2)
    if bf:
        L.append(f'Наша модель {statistics.fmean(bf):.4f} против рынка {statistics.fmean(bm):.4f} на {len(bf)} снимках')
        S['brier'] = {'model': round(statistics.fmean(bf), 4), 'market': round(statistics.fmean(bm), 4), 'n': len(bf)}
        cal = defaultdict(list)
        for x in sn:
            y = res.get(x['slug'])
            if y is not None and x.get('fair') is not None: cal[min(9, int(x['fair'] * 10))].append(y)
        if sum(len(v) for v in cal.values()) >= 200:
            L += ['', 'Калибровка: модель говорит → сбывается на деле', '', '| Модель | Снимков | На деле Up |', '|---|---|---|']
            for k in sorted(cal): L.append(f'| {k * 10}–{k * 10 + 10}% | {len(cal[k])} | {statistics.fmean(cal[k]):.0%} |')
            S['calibration'] = {f'{k * 10}': [len(cal[k]), round(statistics.fmean(cal[k]), 3)] for k in sorted(cal)}
    else: L.append('Данных пока нет.')
    L += ['', '## Арбитраж без риска направления (pm/arb)']
    if atr or anr:
        by = defaultdict(lambda: defaultdict(int))
        for t in atr: by[t['kind']][t.get('status')] += 1
        L.append('Связки: ' + ('; '.join(f'{k}: ' + ', '.join(f'{s2} {n}' for s2, n in v.items()) for k, v in sorted(by.items())) or 'пока ни одной'))
        if ast:
            ok = [x for x in ast if x.get('pnl') is not None]
            L.append(f'Рассчитано {len(ok)}, P&L ${sum(x["pnl"] for x in ok):+.2f}, расхождений площадок: {sum(not x.get("as_expected") for x in ok)}')
            S['arb'].update(settled=len(ok), pnl=round(sum(x['pnl'] for x in ok), 2), mismatch=sum(not x.get('as_expected') for x in ok))
            cur = 0; S['arb']['curve'] = []
            for x in sorted(ok, key=lambda x: x['t']): cur += x['pnl']; S['arb']['curve'].append([round(x['t']), round(cur, 2), x['kind']])
        fl = [t for t in atr if t.get('status') == 'filled']
        if fl: L.append(f'Заблокировано при входе: ${sum(t["locked_pnl"] for t in fl):+.2f} в {len(fl)} связках')
        near = defaultdict(list)
        for r in anr:
            for k, v in r['best'].items(): near[k].append(v)
        if near:
            L += ['', 'Насколько близко к арбитражу (лучший перевес за минуту, после комиссий; > 0 — была возможность):', '', '| Тип | Минут | Медиана | Лучший | Минут с перевесом > 0 |', '|---|---|---|---|---|']
            for k, v in sorted(near.items()):
                L.append(f'| {k} | {len(v)} | {statistics.median(v):+.3f} | {max(v):+.3f} | {sum(x > 0 for x in v)} |')
            S['arb']['near'] = {k: {'minutes': len(v), 'median': round(statistics.median(v), 4), 'max': round(max(v), 4), 'pos': sum(x > 0 for x in v)} for k, v in near.items()}
        S['arb']['status'] = {k: dict(v) for k, v in by.items()}
    else: L.append('Данных пока нет.')
    # --- Kalshi: погода и индексы ---
    kres = json.load(open(os.path.join(R, 'k_results.json'))) if os.path.exists(os.path.join(R, 'k_results.json')) else {}
    kst = [json.loads(l) for l in open(os.path.join(R, 'k_settled.jsonl')) if l.strip()] if os.path.exists(os.path.join(R, 'k_settled.jsonl')) else []
    for bot, title in (('wx', 'Погода (Kalshi, максимум температуры дня)'), ('spx', 'Индексы S&P 500 / Nasdaq-100 (Kalshi)')):
        L += ['', f'## {title}']
        trk = rows(f'{bot}_trades.jsonl'); sn2 = rows(f'{bot}_snaps.jsonl'); stb = [x for x in kst if x.get('bot') == bot]
        by = defaultdict(int)
        for t in trk: by[t.get('status')] += 1
        L.append('Сигналы: ' + (', '.join(f'{k} {v}' for k, v in by.items()) or 'пока нет'))
        B = {'signals': dict(by)}
        if stb:
            pnl = sum(x['pnl'] for x in stb); w = sum(x['win'] for x in stb); cost = sum(x['px'] * x['qty'] for x in stb)
            L.append(f'Рассчитано {len(stb)}, побед {w} ({w / len(stb):.0%}), P&L ${pnl:+.2f} на оборот ${cost:.2f}')
            g = defaultdict(list)
            for x in stb: g[x.get('series', '?')].append(x)
            L += ['', '| Серия | Сделок | Побед | P&L $ |', '|---|---|---|---|'] + [f'| {k} | {len(v)} | {sum(x["win"] for x in v)} | {sum(x["pnl"] for x in v):+.2f} |' for k, v in sorted(g.items())]
            cur = 0; curve = []
            for x in sorted(stb, key=lambda x: x['t']): cur += x['pnl']; curve.append([round(x['t']), round(cur, 2), x.get('series')])
            B.update(settled=len(stb), wins=w, pnl=round(pnl, 2), curve=curve, by_series={k: {'n': len(v), 'pnl': round(sum(x['pnl'] for x in v), 2)} for k, v in g.items()})
        pending = sum(1 for t in trk if t.get('status') == 'filled') - len(stb)
        if pending > 0: L.append(f'Ждут официального расчёта: {pending}')
        bf, bm = [], []
        for snp in sn2:
            for m in snp.get('markets', []):
                r = kres.get(m['ticker'])
                if r is None or m.get('bid') is None or m.get('ask') is None: continue
                y = 1 if r == 'yes' else 0; bf.append((m['fair'] - y) ** 2); bm.append(((m['bid'] + m['ask']) / 2 - y) ** 2)
        if bf:
            L.append(f'Точность (Brier): модель {statistics.fmean(bf):.4f}, рынок {statistics.fmean(bm):.4f} на {len(bf)} снимках')
            B['brier'] = {'model': round(statistics.fmean(bf), 4), 'market': round(statistics.fmean(bm), 4), 'n': len(bf)}
        S[bot] = B
    L += ['', '_Paper trading: реальные ордера не отправляются._']
    open(os.path.join(R, 'REPORT.md'), 'w').write('\n'.join(L) + '\n')
    json.dump(S, open(os.path.join(R, 'summary.json'), 'w'), default=str)
    print('\n'.join(L))


if __name__ == '__main__':
    main()
