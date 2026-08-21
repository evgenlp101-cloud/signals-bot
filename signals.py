# -*- coding: utf-8 -*-
"""
Бот сигналов. Запускается по расписанию на GitHub Actions,
считает индикаторы и шлёт сообщение в Telegram при появлении сигнала.

Настройки берутся из переменных окружения (Secrets в GitHub):
    TG_TOKEN   — токен бота от @BotFather
    TG_CHAT    — chat_id получателя
    SYMBOLS    — список пар через запятую (по умолчанию BTCUSDT,ETHUSDT)
    INTERVAL   — таймфрейм: 15m, 1h, 4h, 1d (по умолчанию 1h)
    TD_KEY     — ключ Twelve Data, нужен только для валютных пар

Локальный запуск для проверки:
    set TG_TOKEN=... & set TG_CHAT=... & py signals.py
"""

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

TOKEN = os.environ.get('TG_TOKEN', '').strip()
CHAT = os.environ.get('TG_CHAT', '').strip()

# Локальные настройки для запуска на своём компьютере.
# Файл config_local.py перечислен в .gitignore и НИКОГДА не попадает
# в репозиторий — поэтому токен из него не утечёт.
# На GitHub Actions этого файла нет, там значения берутся из Secrets.
try:
    import config_local
    TOKEN = TOKEN or getattr(config_local, 'TG_TOKEN', '').strip()
    CHAT = CHAT or getattr(config_local, 'TG_CHAT', '').strip()
except ImportError:
    config_local = None

def _cfg(name, default):
    """Значение из переменной окружения, иначе из config_local.py, иначе default."""
    v = os.environ.get(name, '').strip()
    if v:
        return v
    if config_local is not None:
        return str(getattr(config_local, name, default)).strip()
    return default


SYMBOLS = [s.strip().upper() for s in
           _cfg('SYMBOLS', 'BTCUSDT,ETHUSDT').split(',') if s.strip()]
INTERVAL = _cfg('INTERVAL', '1h')
TD_KEY = _cfg('TD_KEY', '')

# --- Фильтры (проверены бэктестом: снижают число сделок и убирают худшие) ---

# Порог входа: сколько индикаторов должны сойтись. По умолчанию 3, а не 2:
# при пороге 2 система торгует втрое чаще без выигрыша в качестве сигнала.
THRESHOLD = int(_cfg('THRESHOLD', '3'))

# Фильтр старшего таймфрейма: сделка только по направлению большого тренда.
HTF_FILTER = _cfg('HTF_FILTER', 'on').lower() != 'off'

# Торговые часы в UTC, например '7-20'. Пусто — торгуем круглосуточно.
# Для валютных пар смысл есть (азиатская сессия — флэт), для крипты обычно нет.
TRADE_HOURS = _cfg('TRADE_HOURS', '')

# Какой таймфрейм считать старшим для каждого рабочего
HIGHER_TF = {'15m': '4h', '1h': '4h', '4h': '1d', '1d': '1d'}

STATE_FILE = 'state.json'          # чтобы не слать один и тот же сигнал повторно
TIMEOUT = 25


# ---------------------------------------------------------------------------
# Загрузка данных
# ---------------------------------------------------------------------------

def get_json(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'signals-bot/1.0'})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode())


BYBIT_TF = {'15m': '15', '1h': '60', '4h': '240', '1d': 'D'}
OKX_TF = {'15m': '15m', '1h': '1H', '4h': '4H', '1d': '1D'}
TD_TF = {'15m': '15min', '1h': '1h', '4h': '4h', '1d': '1day'}
QUOTES = ['USDT', 'USDC', 'FDUSD', 'BUSD', 'BTC', 'ETH']


def split_pair(sym):
    for q in QUOTES:
        if sym.endswith(q) and len(sym) > len(q):
            return sym[:-len(q)], q
    return sym, 'USDT'


def fetch_crypto(symbol, interval):
    """Три источника по очереди: Binance -> Bybit -> OKX."""
    errors = []

    for host in ('https://api.binance.com', 'https://data-api.binance.vision'):
        try:
            j = get_json(f'{host}/api/v3/klines?symbol={symbol}'
                         f'&interval={interval}&limit=500')
            if isinstance(j, list) and len(j) >= 60:
                return [(float(x[1]), float(x[2]), float(x[3]), float(x[4])) for x in j], 'Binance'
        except Exception as e:
            errors.append(f'Binance: {e}')

    try:
        iv = BYBIT_TF.get(interval)
        j = get_json(f'https://api.bybit.com/v5/market/kline?category=spot'
                     f'&symbol={symbol}&interval={iv}&limit=500')
        rows = j.get('result', {}).get('list', [])
        if j.get('retCode') == 0 and len(rows) >= 60:
            rows = rows[::-1]      # Bybit отдаёт от новых к старым
            return [(float(x[1]), float(x[2]), float(x[3]), float(x[4])) for x in rows], 'Bybit'
    except Exception as e:
        errors.append(f'Bybit: {e}')

    try:
        base, quote = split_pair(symbol)
        iv = OKX_TF.get(interval)
        j = get_json(f'https://www.okx.com/api/v5/market/candles'
                     f'?instId={base}-{quote}&bar={iv}&limit=300')
        rows = j.get('data', [])
        if j.get('code') == '0' and len(rows) >= 60:
            rows = rows[::-1]
            return [(float(x[1]), float(x[2]), float(x[3]), float(x[4])) for x in rows], 'OKX'
    except Exception as e:
        errors.append(f'OKX: {e}')

    raise RuntimeError('; '.join(errors) or 'нет данных')


def fetch_forex(symbol, interval):
    if not TD_KEY:
        raise RuntimeError('для валютных пар нужен TD_KEY')
    iv = TD_TF.get(interval, '1h')
    url = ('https://api.twelvedata.com/time_series'
           f'?symbol={urllib.parse.quote(symbol)}&interval={iv}'
           f'&outputsize=500&apikey={urllib.parse.quote(TD_KEY)}')
    j = get_json(url)
    if j.get('status') == 'error' or j.get('code'):
        raise RuntimeError(j.get('message', 'ошибка Twelve Data'))
    vals = j.get('values', [])
    if len(vals) < 60:
        raise RuntimeError('мало данных')
    vals = vals[::-1]
    return [(float(v['open']), float(v['high']), float(v['low']), float(v['close']))
            for v in vals], 'Twelve Data'


def fetch(symbol, interval=None):
    iv = interval or INTERVAL
    return fetch_forex(symbol, iv) if '/' in symbol else fetch_crypto(symbol, iv)


# ---------------------------------------------------------------------------
# Индикаторы (без numpy — чистый Python, чтобы workflow стартовал быстрее)
# ---------------------------------------------------------------------------

def ema(vals, period):
    k = 2 / (period + 1)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def wilder(vals, period):
    a = 1 / period
    out = [vals[0]]
    for v in vals[1:]:
        out.append(v * a + out[-1] * (1 - a))
    return out


def rsi(close, period=14):
    gain, loss = [0.0], [0.0]
    for i in range(1, len(close)):
        d = close[i] - close[i - 1]
        gain.append(max(d, 0.0))
        loss.append(max(-d, 0.0))
    ag, al = wilder(gain, period), wilder(loss, period)
    return [100.0 if al[i] == 0 else 100 - 100 / (1 + ag[i] / al[i])
            for i in range(len(close))]


def macd(close, f=12, s=26, sg=9):
    ef, es = ema(close, f), ema(close, s)
    line = [ef[i] - es[i] for i in range(len(close))]
    sig = ema(line, sg)
    return line, sig, [line[i] - sig[i] for i in range(len(close))]


def sma(vals, period):
    out = [None] * len(vals)
    for i in range(period - 1, len(vals)):
        window = vals[i - period + 1:i + 1]
        if all(v is not None for v in window):
            out[i] = sum(window) / period
    return out


def bollinger(close, period=20, mult=2.0):
    mid = sma(close, period)
    up, lo = [None] * len(close), [None] * len(close)
    for i in range(period - 1, len(close)):
        w = close[i - period + 1:i + 1]
        m = mid[i]
        var = sum((x - m) ** 2 for x in w) / (period - 1)
        sd = var ** 0.5
        up[i], lo[i] = m + mult * sd, m - mult * sd
    return up, mid, lo


def stochastic(high, low, close, kp=14, dp=3):
    k = [None] * len(close)
    for i in range(kp - 1, len(close)):
        hh = max(high[i - kp + 1:i + 1])
        ll = min(low[i - kp + 1:i + 1])
        k[i] = 50.0 if hh == ll else 100 * (close[i] - ll) / (hh - ll)
    return k, sma(k, dp)


def atr(high, low, close, period=14):
    tr = [high[0] - low[0]]
    for i in range(1, len(close)):
        tr.append(max(high[i] - low[i],
                      abs(high[i] - close[i - 1]),
                      abs(low[i] - close[i - 1])))
    return wilder(tr, period)


def adx(high, low, close, period=14):
    pdm, mdm = [0.0], [0.0]
    for i in range(1, len(close)):
        up = high[i] - high[i - 1]
        dn = low[i - 1] - low[i]
        pdm.append(up if (up > dn and up > 0) else 0.0)
        mdm.append(dn if (dn > up and dn > 0) else 0.0)
    tr = atr(high, low, close, period)
    pdi = [100 * v / tr[i] if tr[i] else 0 for i, v in enumerate(wilder(pdm, period))]
    mdi = [100 * v / tr[i] if tr[i] else 0 for i, v in enumerate(wilder(mdm, period))]
    dx = []
    for i in range(len(close)):
        s = pdi[i] + mdi[i]
        dx.append(0.0 if s == 0 else 100 * abs(pdi[i] - mdi[i]) / s)
    return wilder(dx, period)


# ---------------------------------------------------------------------------
# Сведение голосов
# ---------------------------------------------------------------------------

def analyze(candles):
    o = [c[0] for c in candles]
    h = [c[1] for c in candles]
    l = [c[2] for c in candles]
    c = [c[3] for c in candles]
    i = len(c) - 1
    price = c[i]
    votes = []

    r = rsi(c)[i]
    if r < 30:
        votes.append(('RSI', 1, f'{r:.1f} — перепроданность'))
    elif r > 70:
        votes.append(('RSI', -1, f'{r:.1f} — перекупленность'))
    else:
        votes.append(('RSI', 0, f'{r:.1f} — нейтрально'))

    line, sig, hist = macd(c)
    if line[i] > sig[i] and hist[i] > hist[i - 1]:
        votes.append(('MACD', 1, 'импульс вверх'))
    elif line[i] < sig[i] and hist[i] < hist[i - 1]:
        votes.append(('MACD', -1, 'импульс вниз'))
    else:
        votes.append(('MACD', 0, 'смешанно'))

    e50 = ema(c, 50)[i]
    e200 = ema(c, 200)[i] if len(c) >= 200 else None
    if e200 is None:
        votes.append(('EMA 50/200', 0, 'мало истории'))
    elif price > e50 > e200:
        votes.append(('EMA 50/200', 1, 'восходящий тренд'))
    elif price < e50 < e200:
        votes.append(('EMA 50/200', -1, 'нисходящий тренд'))
    else:
        votes.append(('EMA 50/200', 0, 'тренд не выражен'))

    up, mid, lo = bollinger(c)
    if up[i] is None:
        votes.append(('Боллинджер', 0, 'мало истории'))
    elif price <= lo[i]:
        votes.append(('Боллинджер', 1, 'у нижней границы'))
    elif price >= up[i]:
        votes.append(('Боллинджер', -1, 'у верхней границы'))
    else:
        pos = (price - lo[i]) / (up[i] - lo[i]) * 100
        votes.append(('Боллинджер', 0, f'внутри канала ({pos:.0f}%)'))

    k, d = stochastic(h, l, c)
    if k[i] is None or d[i] is None:
        votes.append(('Stochastic', 0, 'мало истории'))
    elif k[i] < 20 and k[i] > d[i]:
        votes.append(('Stochastic', 1, f'{k[i]:.1f} — разворот вверх'))
    elif k[i] > 80 and k[i] < d[i]:
        votes.append(('Stochastic', -1, f'{k[i]:.1f} — разворот вниз'))
    else:
        votes.append(('Stochastic', 0, f'{k[i]:.1f}'))

    a = adx(h, l, c)[i]
    at = atr(h, l, c)[i]
    strong = a >= 25
    score = sum(v for _, v, _ in votes)

    if score >= THRESHOLD and strong:
        verdict, direction = 'СИЛЬНЫЙ BUY', 1
    elif score >= THRESHOLD:
        verdict, direction = 'BUY', 1
    elif score <= -THRESHOLD and strong:
        verdict, direction = 'СИЛЬНЫЙ SELL', -1
    elif score <= -THRESHOLD:
        verdict, direction = 'SELL', -1
    else:
        verdict, direction = 'ЖДАТЬ', 0

    return {
        'price': price, 'votes': votes, 'score': score, 'adx': a, 'atr': at,
        'verdict': verdict, 'direction': direction,
        'stop': price - direction * 1.5 * at if direction else None,
        'take': price + direction * 3.0 * at if direction else None,
    }


def higher_trend(symbol):
    """
    Направление тренда на старшем таймфрейме: 1 вверх, -1 вниз, 0 не определено.
    Это единственный фильтр, который добавляет действительно новую информацию —
    остальные осцилляторы пересчитывают те же данные, что уже учтены.
    """
    htf = HIGHER_TF.get(INTERVAL)
    if not htf or htf == INTERVAL:
        return 0
    try:
        candles, _ = fetch(symbol, htf)
    except Exception as e:
        print(f'   старший ТФ недоступен ({e}), фильтр пропущен')
        return 0

    c = [x[3] for x in candles]
    if len(c) < 200:
        return 0
    price, e50, e200 = c[-1], ema(c, 50)[-1], ema(c, 200)[-1]
    if price > e50 > e200:
        return 1
    if price < e50 < e200:
        return -1
    return 0


def hours_ok():
    """Проверка торгового окна. Пустая настройка — торгуем всегда."""
    if not TRADE_HOURS:
        return True, ''
    try:
        a, b = [int(x) for x in TRADE_HOURS.split('-')]
    except Exception:
        return True, ''
    h = datetime.now(timezone.utc).hour
    inside = (a <= h < b) if a <= b else (h >= a or h < b)
    return inside, f'{h:02d}:00 UTC вне окна {TRADE_HOURS}'


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def fmt(x):
    a = abs(x)
    d = 2 if a >= 1000 else 4 if a >= 1 else 6
    return f'{x:,.{d}f}'.replace(',', ' ')


def send(text):
    data = urllib.parse.urlencode({
        'chat_id': CHAT, 'text': text, 'parse_mode': 'HTML',
        'disable_web_page_preview': 'true',
    }).encode()
    req = urllib.request.Request(
        f'https://api.telegram.org/bot{TOKEN}/sendMessage', data=data)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode()).get('ok', False)


def build_message(symbol, res, source):
    arrow = '🟢 ⬆' if res['direction'] > 0 else '🔴 ⬇'
    now = datetime.now(timezone.utc).strftime('%d.%m %H:%M UTC')
    lines = [
        f"{arrow} <b>{res['verdict']}</b> · {symbol}",
        f"Цена <b>{fmt(res['price'])}</b> · {INTERVAL} · {now}",
        '',
    ]
    for name, v, note in res['votes']:
        mark = '▲' if v > 0 else '▼' if v < 0 else '·'
        lines.append(f'{mark} {name}: {note}')
    lines.append(f"· ADX: {res['adx']:.0f} "
                 f"({'тренд выражен' if res['adx'] >= 25 else 'флэт'})")
    if res.get('htf'):
        lines.append(f"✓ {res['htf']}")
    if res['stop'] is not None:
        lines += ['',
                  f"Стоп-лосс: <code>{fmt(res['stop'])}</code>  (1.5×ATR)",
                  f"Тейк-профит: <code>{fmt(res['take'])}</code>  (3×ATR)"]
    lines += ['', f'<i>Источник: {source}. Не гарантия — риск-менеджмент обязателен.</i>']
    return '\n'.join(lines)


# ---------------------------------------------------------------------------

def load_state():
    try:
        with open(STATE_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def main():
    if not TOKEN or not CHAT:
        print('ОШИБКА: не заданы TG_TOKEN и TG_CHAT.')
        print('Локально: скопируй config_local.example.py в config_local.py и заполни.')
        print('На GitHub: Settings -> Secrets and variables -> Actions.')
        sys.exit(1)

    if TOKEN.startswith('ВСТАВЬ'):
        print('ОШИБКА: в config_local.py остался текст-заглушка вместо токена.')
        sys.exit(1)

    # Защита от утечки: токен не должен лежать в файле, который уходит в репозиторий
    for fname in ('signals.py', 'README.md'):
        try:
            with open(fname, encoding='utf-8') as f:
                if TOKEN in f.read():
                    print(f'ОПАСНО: токен найден внутри {fname}. Убери его оттуда — '
                          f'этот файл попадает в репозиторий. Токен должен быть '
                          f'только в config_local.py или в Secrets.')
                    sys.exit(1)
        except FileNotFoundError:
            pass

    print(f'Настройки: порог ±{THRESHOLD}, '
          f'фильтр старшего ТФ {"вкл" if HTF_FILTER else "выкл"}'
          f'{", окно " + TRADE_HOURS + " UTC" if TRADE_HOURS else ""}')

    ok, why = hours_ok()
    if not ok:
        print(f'Пропуск: {why}')
        return

    state = load_state()
    sent = 0

    for symbol in SYMBOLS:
        key = f'{symbol}|{INTERVAL}'
        try:
            candles, source = fetch(symbol)
            res = analyze(candles)
        except Exception as e:
            print(f'[{symbol}] ошибка: {e}')
            continue

        now = res['direction']
        note = ''
        print(f"[{symbol}] {res['verdict']} (счёт {res['score']:+d}, "
              f"ADX {res['adx']:.0f}) ← {source}")

        # Фильтр старшего таймфрейма: сделка только по большому тренду
        if now != 0 and HTF_FILTER:
            htf_dir = higher_trend(symbol)
            if htf_dir != 0 and htf_dir != now:
                print(f'   отклонено: против тренда на {HIGHER_TF.get(INTERVAL)}')
                now = 0
            elif htf_dir == now:
                note = f'подтверждено трендом на {HIGHER_TF.get(INTERVAL)}'
                res['htf'] = note

        prev = state.get(key, 0)
        # шлём только при появлении сигнала или смене направления
        if now != 0 and now != prev:
            if send(build_message(symbol, res, source)):
                sent += 1
                print(f'   → отправлено в Telegram')
            else:
                print(f'   → не удалось отправить')
        state[key] = now

    save_state(state)
    print(f'Готово. Отправлено сообщений: {sent}')


if __name__ == '__main__':
    main()
