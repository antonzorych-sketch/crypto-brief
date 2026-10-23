"""pm/discover: программы наград за ликвидность Kalshi — что активно, размеры, поля; пример стакана и сделок."""
import json, urllib.request, urllib.parse, collections
K = 'https://api.elections.kalshi.com/trade-api/v2'
def get(u):
    try: return json.loads(urllib.request.urlopen(urllib.request.Request(u, headers={'User-Agent': 'pm-discover', 'Accept': 'application/json'}), timeout=20).read())
    except urllib.error.HTTPError as e: return {'_error': f'{e.code} {e.read()[:200]}'}
    except Exception as e: return {'_error': repr(e)[:200]}
progs, cur = [], None
for _ in range(20):
    q = {'status': 'active', 'type': 'liquidity', 'limit': 1000}
    if cur: q['cursor'] = cur
    r = get(f'{K}/incentive_programs?' + urllib.parse.urlencode(q))
    if '_error' in r: print(r); break
    progs += r.get('incentive_programs', []); cur = r.get('next_cursor')
    if not cur: break
print('активных программ ликвидности:', len(progs))
print('пример:', json.dumps(progs[:2])[:800])
from datetime import datetime
def days(p):
    try: return max(1/24, (datetime.fromisoformat(p['end_date'].replace('Z', '+00:00')) - datetime.fromisoformat(p['start_date'].replace('Z', '+00:00'))).total_seconds() / 86400)
    except Exception: return None
rows = []
for p in progs:
    d = days(p); rew = p['period_reward'] / 10000
    rows.append((rew / d if d else 0, rew, d, p['market_ticker'], p.get('target_size_fp'), p.get('discount_factor_bps'), p.get('max_reward_per_account')))
rows.sort(reverse=True)
tot = sum(r[0] for r in rows); print(f'сумма наград в день: ${tot:,.0f}')
cat = collections.Counter(); catd = collections.Counter()
for r in rows:
    pre = r[3].split('-')[0]; cat[pre] += 1; catd[pre] += r[0]
print('по сериям ($/день):', [(k, round(v), cat[k]) for k, v in catd.most_common(30)])
print('\nтоп-25 рынков:')
for r in rows[:25]: print(f'  ${r[0]:8.1f}/день  всего ${r[1]:8.0f} за {r[2]:.1f} дн  {r[3]}  target {r[4]} df {r[5]} cap {r[6]}')
print('\nраспределение $/день:', [sum(1 for r in rows if lo <= r[0] < hi) for lo, hi in ((0, 1), (1, 5), (5, 20), (20, 100), (100, 1e9))])
for r in rows[:3]:
    t = r[3]
    m = get(f'{K}/markets/{t}'); mk = m.get('market', {})
    print('\n', t, mk.get('title'), mk.get('status'), 'vol24', mk.get('volume_24h_fp') or mk.get('volume_24h'), 'close', mk.get('close_time'))
    print('  стакан:', json.dumps(get(f'{K}/markets/{t}/orderbook'))[:500])
    print('  сделки:', json.dumps(get(f'{K}/markets/trades?' + urllib.parse.urlencode({'ticker': t, 'limit': 5})))[:600])
