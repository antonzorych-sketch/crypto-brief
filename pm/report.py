"""pm/report: сводка по всем paper-прогонам pm/lag -> pm/paper/REPORT.md
Отвечает на три вопроса: есть ли лаг (сколько секунд Polymarket догоняет нашу справедливую цену),
точнее ли наша модель цены рынка (Brier), и что дают виртуальные сделки после комиссий."""
import glob, gzip, json, os, statistics
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
    L += ['', '## Лаг']
    if lg:
        s = sorted(x['secs'] for x in lg)
        L.append(f'Расхождений >5¢ между нашей ценой и mid Polymarket: {len(s)}. Сколько держатся: медиана {statistics.median(s):.0f} с, 75% — {s[int(len(s) * .75)]:.0f} с, 90% — {s[int(len(s) * .9)]:.0f} с')
        g = defaultdict(list)
        for x in lg: g[x['tf']].append(x['secs'])
        L.append('По окнам: ' + ', '.join(f'{k}: {len(v)} шт, медиана {statistics.median(v):.0f} с' for k, v in sorted(g.items())))
    else: L.append('Данных пока нет.')
    L += ['', '## Калибровка (Brier, меньше — лучше)']
    res = {w['slug']: (1 if (w.get('official') or w.get('own')) == 'UP' else 0) for w in wn if (w.get('official') or w.get('own'))}
    bf, bm = [], []
    for x in sn:
        y = res.get(x['slug'])
        if y is None or x.get('fair') is None or x.get('bid') is None or x.get('ask') is None: continue
        bf.append((x['fair'] - y) ** 2); bm.append(((x['bid'] + x['ask']) / 2 - y) ** 2)
    if bf: L.append(f'Наша модель {statistics.fmean(bf):.4f} против рынка {statistics.fmean(bm):.4f} на {len(bf)} снимках')
    else: L.append('Данных пока нет.')
    L += ['', '_Paper trading: реальные ордера не отправляются._']
    open(os.path.join(R, 'REPORT.md'), 'w').write('\n'.join(L) + '\n')
    print('\n'.join(L))


if __name__ == '__main__':
    main()
