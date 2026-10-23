"""pm/ksettle: проверка официальных результатов Kalshi для виртуальных сделок погодного и индексного ботов.
Рынки погоды рассчитываются на следующий день, поэтому сверка идёт при каждом прогоне по всем прошлым сделкам.
Результаты кэшируются в pm/paper/k_results.json (тикер -> 'yes'/'no'), расчёт сделок — pm/paper/k_settled.jsonl."""
import glob, json, os, time, urllib.request

R = 'pm/paper'; K = 'https://api.elections.kalshi.com/trade-api/v2'


def get(u):
    try:
        return json.loads(urllib.request.urlopen(urllib.request.Request(u, headers={'User-Agent': 'pm-paper', 'Accept': 'application/json'}), timeout=15).read())
    except Exception:
        return {}


def main():
    cache_f = os.path.join(R, 'k_results.json')
    res = json.load(open(cache_f)) if os.path.exists(cache_f) else {}
    done_f = os.path.join(R, 'k_settled.jsonl')
    done = set()
    if os.path.exists(done_f):
        for l in open(done_f):
            if l.strip(): x = json.loads(l); done.add((x['run'], x['t'], x['ticker']))
    trades, tickers = [], set()
    for pfx in ('wx', 'spx'):
        for f in sorted(glob.glob(os.path.join(R, '*', f'{pfx}_trades.jsonl'))):
            run = os.path.basename(os.path.dirname(f))
            for l in open(f):
                if not l.strip(): continue
                x = json.loads(l); x['run'], x['bot'] = run, pfx
                if x.get('status') == 'filled' and (run, x['t'], x['ticker']) not in done: trades.append(x)
        for f in sorted(glob.glob(os.path.join(R, '*', f'{pfx}_snaps.jsonl'))):
            for l in open(f):
                if l.strip(): tickers.update(m['ticker'] for m in json.loads(l).get('markets', []))
    tickers |= {x['ticker'] for x in trades}
    n = 0
    for t in sorted(tickers - set(res)):
        m = get(f'{K}/markets/{t}').get('market') or {}
        if m.get('result') in ('yes', 'no'): res[t] = m['result']; n += 1
        time.sleep(0.06)
    json.dump(res, open(cache_f, 'w'))
    new = 0
    with open(done_f, 'a') as out:
        for x in trades:
            r = res.get(x['ticker'])
            if not r: continue
            win = (x['side'] == 'YES') == (r == 'yes')
            x.update(result=r, win=win, pnl=round((x['qty'] if win else 0) - x['px'] * x['qty'] - x['fee'], 4))
            out.write(json.dumps(x) + '\n'); new += 1
    print(f'ksettle: новых результатов рынков {n}, рассчитано сделок {new}, ждут расчёта {len(trades) - new}')


if __name__ == '__main__':
    main()
