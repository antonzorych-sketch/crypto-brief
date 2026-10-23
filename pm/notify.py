"""pm/notify: сводка прогона в Telegram.
Токен бота — секрет TG_TOKEN. Чат — секрет TG_CHAT_ID; если его нет, берём чат из последних сообщений боту
(достаточно написать боту /start — Telegram хранит сообщения 24 часа). Ничего не печатает в лог, кроме статуса."""
import json, os, urllib.parse, urllib.request

TOKEN = os.environ.get('TG_TOKEN', '').strip()
CHAT = os.environ.get('TG_CHAT_ID', '').strip()
R = 'pm/paper'
DASH = 'https://antonzorych-sketch.github.io/crypto-brief/pm/'


def api(method, **params):
    data = urllib.parse.urlencode(params).encode() if params else None
    r = urllib.request.urlopen(f'https://api.telegram.org/bot{TOKEN}/{method}', data=data, timeout=20)
    return json.loads(r.read())


def chat_id():
    if CHAT: return CHAT
    try:
        ups = api('getUpdates').get('result', [])
        for u in reversed(ups):
            m = u.get('message') or u.get('edited_message') or {}
            if m.get('chat', {}).get('id'): return str(m['chat']['id'])
    except Exception:
        pass
    return None


def usd(v): return ('+' if v >= 0 else '−') + f'${abs(v):.2f}'


def build():
    S = json.load(open(os.path.join(R, 'summary.json')))
    L = ['<b>Paper-боты: итог прогона</b>']
    lag = S.get('lag') or {}
    if lag.get('trades'):
        L.append(f"Лаг Polymarket: {lag['trades']} сделок, P&L {usd(lag['pnl'])} (на WebSocket: {lag.get('ws_trades', 0)}, {usd(lag.get('ws_pnl', 0))})")
    pr = S.get('pair') or {}
    if pr.get('windows'): L.append(f"Пары: {pr['windows']} окон, P&L {usd(pr['pnl'])}")
    a = S.get('arb') or {}
    if a.get('settled'): L.append(f"Арбитраж: {a['settled']} связок, P&L {usd(a['pnl'])}, расхождений площадок {a.get('mismatch', 0)}")
    near = a.get('near') or {}
    if near:
        best = max(near.items(), key=lambda kv: kv[1]['max'])
        L.append(f"Арбитраж, лучшая связка: {best[0]} {best[1]['max'] * 100:+.1f}¢ (минут с перевесом > 0: {sum(v['pos'] for v in near.values())})")
    for b, name in (('wx', 'Погода'), ('spx', 'S&P/Nasdaq')):
        x = S.get(b) or {}
        sig = sum((x.get('signals') or {}).values())
        if x.get('settled'): L.append(f"{name}: {x['settled']} рассчитано, P&L {usd(x['pnl'])}, сигналов {sig}")
        elif sig: L.append(f"{name}: сигналов {sig}, ждут расчёта")
    if len(L) == 1: L.append('Сделок пока нет.')
    L.append(f'<a href="{DASH}">Дашборд</a>')
    return '\n'.join(L)


def main():
    if not TOKEN: print('notify: нет TG_TOKEN — пропускаем'); return
    cid = chat_id()
    if not cid: print('notify: чат не найден — напишите боту /start'); return
    try:
        api('sendMessage', chat_id=cid, text=build(), parse_mode='HTML', disable_web_page_preview='true')
        print('notify: отправлено')
    except Exception as e:
        print('notify: ошибка отправки', type(e).__name__)


if __name__ == '__main__':
    main()
