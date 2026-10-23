"""pm/weather: paper-бот на рынках Kalshi «максимальная температура дня» (KXHIGH*).

Идея: к вечеру максимум дня почти известен — метеостанция уже его зафиксировала, а прогноз говорит, что теплее не будет.
Бот берёт наблюдения станции (METAR, aviationweather.gov: температура с десятыми и 6-часовые максимумы),
почасовой прогноз NWS на остаток дня, и методом Монте-Карло считает вероятность каждой корзины температур.
Если стакан Kalshi расходится с этим больше чем на --edge после комиссии — виртуальная покупка, держим до расчёта.

Расчёт рынков — по данным The Weather Company (целые °F) за климатические сутки (полночь–полночь по местному
стандартному времени). Возможные расхождения источников заложены шумом в модель; проверяем по факту.
Запуск: python pm/weather.py --minutes 320
"""
import argparse, asyncio, math, random, re, time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import kalshi as K
from kalshi import log

CITIES = {  # серия: (станция ICAO, часовой пояс, широта, долгота)
    'KXHIGHNY': ('KNYC', 'America/New_York', 40.779, -73.969),
    'KXHIGHCHI': ('KMDW', 'America/Chicago', 41.786, -87.752),
    'KXHIGHMIA': ('KMIA', 'America/New_York', 25.788, -80.317),
    'KXHIGHAUS': ('KAUS', 'America/Chicago', 30.183, -97.680),
    'KXHIGHLAX': ('KLAX', 'America/Los_Angeles', 33.938, -118.389),
    'KXHIGHDEN': ('KDEN', 'America/Denver', 39.847, -104.656),
    'KXHIGHPHIL': ('KPHL', 'America/New_York', 39.868, -75.231),
}
ap = argparse.ArgumentParser()
ap.add_argument('--minutes', type=float, default=320)
ap.add_argument('--edge', type=float, default=0.06)
ap.add_argument('--stake', type=float, default=5.0)
ap.add_argument('--max-per-market', type=float, default=15.0)
ap.add_argument('--max-spread', type=float, default=0.06)
A = ap.parse_args()
J = K.Journal('wx')
NWS = {'User-Agent': 'pm-paper (antonzorych@gmail.com)', 'Accept': 'application/geo+json'}
OBS, FC, FCURL = {}, {}, {}   # станция -> [(utc_ts, °F)] ; серия -> [(utc_ts, °F)] ; серия -> url прогноза


def c2f(c): return c * 9 / 5 + 32


def climate_day(tz, now):
    """Климатические сутки по местному СТАНДАРТНОМУ времени: [start, end) в UTC."""
    z = ZoneInfo(tz); loc = now.astimezone(z)
    std = loc.utcoffset() - (loc.dst() or timedelta(0))
    lst = now.astimezone(timezone(std))
    start = lst.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc), lst.date()


async def obs_loop():
    ids = ','.join(v[0] for v in CITIES.values())
    while True:
        d = await K.aget(f'https://aviationweather.gov/api/data/metar?ids={ids}&format=json&hours=30')
        if isinstance(d, list):
            acc = {}
            for o in d:
                st, t, raw = o.get('icaoId'), o.get('obsTime'), o.get('rawOb') or ''
                if not st or not t: continue
                m = re.search(r'\sT([01])(\d{3})([01])(\d{3})', raw)
                tc = (-1 if m.group(1) == '1' else 1) * int(m.group(2)) / 10 if m else o.get('temp')
                if tc is not None: acc.setdefault(st, []).append((t, c2f(tc), 'obs'))
                rmk = raw.split(' RMK ', 1)[1] if ' RMK ' in raw else ''
                for mm in re.finditer(r'(?:^|\s)1([01])(\d{3})(?=\s|$)', rmk):  # 6-часовой максимум
                    acc.setdefault(st, []).append((t, c2f((-1 if mm.group(1) == '1' else 1) * int(mm.group(2)) / 10), 'max6'))
            OBS.update(acc)
        await asyncio.sleep(300)


async def fc_loop():
    while True:
        for ser, (st, tz, lat, lon) in CITIES.items():
            if ser not in FCURL:
                p = await K.aget(f'https://api.weather.gov/points/{lat},{lon}', NWS)
                u = (p.get('properties') or {}).get('forecastHourly') if isinstance(p, dict) else None
                if u: FCURL[ser] = u
            if ser in FCURL:
                f = await K.aget(FCURL[ser], NWS)
                per = (f.get('properties') or {}).get('periods') if isinstance(f, dict) else None
                if per:
                    FC[ser] = [(datetime.fromisoformat(x['startTime']).timestamp(), float(x['temperature']) if x.get('temperatureUnit') == 'F' else c2f(float(x['temperature']))) for x in per]
        await asyncio.sleep(1800)


def model(ser, now):
    """-> (наблюдённый максимум °F, словарь распределения целого максимума {°F: вероятность}, пояснение)."""
    st, tz, _, _ = CITIES[ser]
    d0, d1, day = climate_day(tz, now); t0, t1, tn = d0.timestamp(), d1.timestamp(), now.timestamp()
    obs = [(t, f) for t, f, kind in OBS.get(st, []) if t0 < t <= t1 and (kind == 'obs' or t - 6 * 3600 >= t0 - 60)]
    if not obs or ser not in FC: return None
    mobs = max(f for _, f in obs)
    # сутки могут считаться и по местному календарному времени — если максимум попадает в спорный час, не торгуем
    z = ZoneInfo(tz); c0 = now.astimezone(z).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    alt = [f for t, f, kind in OBS.get(st, []) if c0 < t <= c0 + 86400 and kind == 'obs']
    if alt and abs(max(alt) - max(f for t, f in obs if t <= tn)) > 0.5 and abs(c0 - t0) > 60: return None
    last_t, last_f = max(obs)
    fc = [(t, f) for t, f in FC[ser] if tn + 1800 <= t < t1]  # только будущие часы: текущий уже виден в наблюдениях
    cur = min(FC[ser], key=lambda x: abs(x[0] - last_t)) if FC[ser] else None
    adj = (last_f - cur[1]) if cur and abs(cur[0] - last_t) < 5400 else 0.0   # прогноз ошибается сейчас — поправка, затухающая со временем
    rem = [(t, f + adj * math.exp(-(t - tn) / 14400)) for t, f in fc]
    if rem:
        tp, fp = max(rem, key=lambda x: x[1]); h = max(0.0, (tp - tn) / 3600)
        sig = min(3.5, 0.6 + 0.3 * h)
    else:
        fp, sig, h = -999, 0, 0
    dist = {}
    rnd = random.Random(int(tn) // 600)
    for _ in range(6000):
        x = mobs + rnd.gauss(0.25, 0.5)                     # настоящий максимум бывает между замерами (смещение вверх) + расхождение источников
        if fp > -999: x = max(x, rnd.gauss(fp, sig))
        r = int(math.floor(x + 0.5)); dist[r] = dist.get(r, 0) + 1
    return mobs, {k: v / 6000 for k, v in dist.items()}, {'day': str(day), 'mobs': round(mobs, 1), 'fc_rest_max': None if fp == -999 else round(fp, 1),
                                                           'sig': round(sig, 2), 'h_to_peak': round(h, 1), 'adj': round(adj, 1), 'n_obs': len(obs)}


def p_yes(m, dist): return sum(p for x, p in dist.items() if K.wins(m, x))


SPENT, LAST = {}, {}


async def city_loop(ser):
    await asyncio.sleep(random.random() * 10)
    last_snap = 0
    while True:
        now = datetime.now(timezone.utc)
        res = model(ser, now)
        if res:
            mobs, dist, info = res
            ev_date = datetime.strptime(info['day'], '%Y-%m-%d').strftime('%y%b%d').upper()
            ms = [m for m in await K.markets(series_ticker=ser, status='open') if m['event_ticker'].endswith(ev_date)]
            snap = []
            for m in ms:
                f = p_yes(m, dist); t = K.top(m)
                snap.append({'ticker': m['ticker'], 'fair': round(f, 4), 'bid': t['yes_bid'], 'ask': t['yes_ask']})
                if not t['yes_ask'] or not t['yes_bid'] or t['yes_ask'] - t['yes_bid'] > A.max_spread: continue
                for side, ask, p in (('YES', t['yes_ask'], f), ('NO', t['no_ask'], 1 - f)):
                    if not ask or not (0.05 <= ask <= 0.95): continue
                    e = p - ask - K.fee(ask)
                    key = (m['ticker'], side)
                    if e >= A.edge and SPENT.get(m['ticker'], 0) < A.max_per_market and time.time() - LAST.get(key, 0) > 900:
                        LAST[key] = time.time()
                        r = await K.paper_buy(J, m, side, f, A.edge, A.stake, {'series': ser, 'model': info, 'ask0': ask, 'edge0': round(e, 4)})
                        if r.get('status') == 'filled': SPENT[m['ticker']] = SPENT.get(m['ticker'], 0) + r['px'] * r['qty']
            if time.time() - last_snap > 600 and snap:
                last_snap = time.time(); J.w('snaps', {'t': time.time(), 'series': ser, 'model': info, 'markets': snap})
        await asyncio.sleep(30)


async def main():
    log('погодный бот: города', list(CITIES), 'перевес', A.edge)
    tasks = [asyncio.create_task(x) for x in (obs_loop(), fc_loop(), *(city_loop(s) for s in CITIES))]
    stop = time.time() + A.minutes * 60
    while time.time() < stop:
        await asyncio.sleep(60)
        log('пульс погоды: станций с данными', len(OBS), 'прогнозов', len(FC), 'запросов', K.STAT['req'], 'ошибок', K.STAT['err'],
            {s: model(s, datetime.now(timezone.utc))[2] for s in CITIES if model(s, datetime.now(timezone.utc))} if int(time.time()) % 1800 < 60 else '')
    for t in tasks: t.cancel()
    log('погода: стоп')


if __name__ == '__main__':
    asyncio.run(main())
