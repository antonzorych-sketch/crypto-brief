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


def sig(pnls, n_boot=4000, seed=7):
    """Статистика по сделкам: среднее, t-статистика, 95% бутстрап-интервал среднего и доля бутстрап-выборок со средним > 0.
    t > 2 и интервал целиком выше нуля — перевес вряд ли случаен."""
    import random
    n = len(pnls)
    if n < 5: return None
    m = statistics.fmean(pnls); sd = statistics.pstdev(pnls) or 1e-9
    rnd = random.Random(seed); bs = sorted(statistics.fmean(rnd.choices(pnls, k=n)) for _ in range(n_boot))
    # Закон больших чисел: после N сделок итог ≈ N·μ ± 2·√N·σ (95%). Итог стабильно положителен, когда N·μ > 2√N·σ,
    # то есть N > 4σ²/μ² — столько сделок нужно, чтобы плюс перестал зависеть от удачи (имеет смысл только при μ > 0).
    need = math.ceil(4 * sd * sd / (m * m)) if m > 0 else None
    return {'n': n, 'mean': round(m, 4), 'sd': round(sd, 4), 't': round(m / (sd / math.sqrt(n)), 2), 'ci': [round(bs[int(.025 * n_boot)], 4), round(bs[int(.975 * n_boot)], 4)],
            'p_pos': round(sum(b > 0 for b in bs) / n_boot, 3), 'n_stable': need,
            'verdict': 'работает' if m > 0 and bs[int(.025 * n_boot)] > 0 else ('убыточна' if bs[int(.975 * n_boot)] < 0 else 'не доказано')}


def fit_lambda(pairs):
    """Подбор веса модели λ в смеси p = λ·модель + (1−λ)·рынок по минимуму log-loss на рассчитанных снимках."""
    if len(pairs) < 100: return None
    def ll(lam):
        t = 0.0
        for f, mid, y in pairs:
            p = min(max(lam * f + (1 - lam) * mid, 1e-4), 1 - 1e-4); t -= math.log(p if y else 1 - p)
        return t / len(pairs)
    grid = [i / 20 for i in range(21)]; best = min(grid, key=ll)
    return {'lambda': best, 'logloss_model': round(ll(1.0), 4), 'logloss_market': round(ll(0.0), 4), 'logloss_best': round(ll(best), 4), 'n': len(pairs)}


def main():
    st, tr, lg, wn, sn = rows('settled.jsonl'), rows('trades.jsonl'), rows('lag.jsonl'), rows('windows.jsonl'), rows('snaps.jsonl.gz')
    ast, atr, anr = rows('arb_settled.jsonl'), rows('arb_trades.jsonl'), rows('arb_near.jsonl')
    S = {'updated': time.time(), 'lag': {}, 'arb': {}}
    L = ['# Polymarket US — paper: лаг цены', '']
    runs = sorted({os.path.basename(os.path.dirname(f)) for f in glob.glob(os.path.join(R, '*', 'trades.jsonl'))} |
                  {os.path.basename(os.path.dirname(f)) for f in glob.glob(os.path.join(R, '*', 'windows.jsonl'))})
    L.append(f'Прогонов: {len(runs)} ({runs[0] if runs else "-"} … {runs[-1] if runs else "-"}), окон рассчитано: {len(wn)}')
    agree = [w for w in wn if w.get('own') and w.get('official')]
    kerr = [abs(w['K_err_bp']) for w in wn if w.get('K_err_bp') is not None]
    if kerr: L.append(f'Точность нашего индекса против официальной цены старта (Kalshi): медиана {statistics.median(kerr):.1f} б.п., 90% — {sorted(kerr)[int(len(kerr) * .9)]:.1f} б.п. ({len(kerr)} окон)')
    ksrc = defaultdict(int)
    for w in wn: ksrc[w.get('ksrc') or '—'] += 1
    if ksrc: L.append('Источник цены старта: ' + ', '.join(f'{k} {v}' for k, v in sorted(ksrc.items())))
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
        mk = rows('arb_maker.jsonl')
        if mk:
            ed = [x['edge_real'] for x in mk]
            L.append(f'Мейкерский вариант с Kalshi (заявка на Polymarket, хедж на Kalshi): исполнений {len(mk)}, средний фактический перевес {statistics.fmean(ed) * 100:+.1f}¢, '
                     f'в плюс {sum(e > 0 for e in ed)} из {len(mk)}, итого ≈ ${sum(ed) * 10:+.2f} на 10 контрактов в каждой')
            S['arb']['maker'] = {'n': len(mk), 'avg_edge': round(statistics.fmean(ed), 4), 'pos': sum(e > 0 for e in ed)}
    else: L.append('Данных пока нет.')
    pr = rows('pair_settled.jsonl')
    if pr:
        ok = [x for x in pr if x.get('pnl') is not None]
        L += ['', '## Стратегия пар (Up + Down дешевле $1)']
        if ok:
            L.append(f'Окон с позициями: {len(ok)}, P&L ${sum(x["pnl"] for x in ok):+.2f}, гарантировано было ${sum(x["guaranteed"] for x in ok):+.2f}, '
                     f'окон с непарным остатком {sum(1 for x in ok if x["unpaired_qty"] > 0.01)}, вложено ${sum(x["spent"] for x in ok):.2f}')
            g = defaultdict(list)
            for x in ok: g[f"{x['coin'].upper()} {x['tf']}"].append(x)
            L += ['', '| Рынок | Окон | P&L $ | Гарантировано $ |', '|---|---|---|---|'] + [f'| {k} | {len(v)} | {sum(x["pnl"] for x in v):+.2f} | {sum(x["guaranteed"] for x in v):+.2f} |' for k, v in sorted(g.items())]
            cur = 0; curve = []
            for x in ok: cur += x['pnl']; curve.append([0, round(cur, 2), x['tf']])
            S['pair'] = {'windows': len(ok), 'pnl': round(sum(x['pnl'] for x in ok), 2), 'guaranteed': round(sum(x['guaranteed'] for x in ok), 2),
                         'by_market': {k: {'n': len(v), 'pnl': round(sum(x['pnl'] for x in v), 2)} for k, v in g.items()}, 'curve': curve}
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
    # --- LIP: награды за ликвидность Kalshi ---
    per = rows('lip_periods.jsonl'); lst = [x for x in kst if x.get('bot') == 'lip']
    L += ['', '## Награды за ликвидность Kalshi (оценка)']
    if per:
        rew = sum(x['reward_est'] for x in per); fl = rows('lip_trades.jsonl')
        loss = sum(x['pnl'] for x in lst)
        L.append(f'Периодов: {len(per)}, ожидаемые награды ${rew:.2f}, исполнений {len(fl)}, рассчитано {len(lst)} с P&L ${loss:+.2f}, '
                 f'нерассчитанных {len(fl) - len(lst)}. Чистый результат по рассчитанному: ${rew + loss:+.2f}')
        g = defaultdict(lambda: [0.0, 0, 0.0])
        for x in per: g[x['series']][0] += x['reward_est']; g[x['series']][1] += 1
        for x in lst: g[x.get('series', '?')][2] += x['pnl']
        L += ['', '| Серия | Периодов | Награды $ | P&L исполнений $ | Итого $ |', '|---|---|---|---|---|']
        for k, (r, n, pl) in sorted(g.items(), key=lambda kv: -(kv[1][0] + kv[1][2]))[:15]:
            L.append(f'| {k} | {n} | {r:.2f} | {pl:+.2f} | {r + pl:+.2f} |')
        S['lip'] = {'periods': len(per), 'reward': round(rew, 2), 'fills': len(fl), 'settled_pnl': round(loss, 2), 'net': round(rew + loss, 2), 'settled_n': len(lst),
                    'by_series': {k: {'n': n, 'reward': round(r, 2), 'pnl': round(pl, 2)} for k, (r, n, pl) in g.items()}}
    else: L.append('Данных пока нет.')
    # --- статистическая значимость и вес модели ---
    L += ['', '## Значимость (бутстрап) и вес модели против рынка']
    groups = {'лаг (все)': [x['pnl'] for x in st], 'лаг WebSocket': [x['pnl'] for x in st if x.get('src') == 'ws'],
              'пары': [x['pnl'] for x in rows('pair_settled.jsonl') if x.get('pnl') is not None]}
    for bot, nm in (('wx', 'погода'), ('spx', 'S&P'), ('lip', 'LIP исполнения')): groups[nm] = [x['pnl'] for x in kst if x.get('bot') == bot]
    S['stats'] = {}
    for nm, v in groups.items():
        r = sig(v)
        if r:
            S['stats'][nm] = r
            L.append(f"{nm}: **{r['verdict']}** — {r['n']} сделок, среднее ${r['mean']:+.3f} (σ ${r['sd']:.2f}), t = {r['t']}, "
                     f"95% интервал [{r['ci'][0]:+.3f}; {r['ci'][1]:+.3f}], P(перевес > 0) ≈ {r['p_pos']:.0%}"
                     + (f", до стабильного плюса нужно ~{r['n_stable']} сделок" if r['n_stable'] else ''))
    if not S['stats']: L.append('Мало сделок (нужно хотя бы 5 на стратегию).')
    pairs = []
    for x in sn:
        y = res.get(x['slug'])
        if y is not None and x.get('fair') is not None and x.get('bid') is not None and x.get('ask') is not None: pairs.append((x['fair'], (x['bid'] + x['ask']) / 2, y))
    fl = fit_lambda(pairs)
    if fl:
        S['lambda'] = fl
        L.append(f"Лучший вес модели λ = {fl['lambda']} (log-loss: модель {fl['logloss_model']}, рынок {fl['logloss_market']}, смесь {fl['logloss_best']}, {fl['n']} снимков). "
                 f"Сейчас в боте λ = 0.75 — если оценка устойчиво другая, поправим.")
    L += ['', '_Paper trading: реальные ордера не отправляются._']
    open(os.path.join(R, 'REPORT.md'), 'w').write('\n'.join(L) + '\n')
    json.dump(S, open(os.path.join(R, 'summary.json'), 'w'), default=str)
    print('\n'.join(L))


if __name__ == '__main__':
    main()
