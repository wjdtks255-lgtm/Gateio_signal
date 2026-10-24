import os, json, time, math
from datetime import datetime, timezone
import requests

VERSION = "2.0.0"
BASE = "https://api.gateio.ws/api/v4"
SETTLE = "usdt"
STATE_FILE = "gate_state.json"
LOG_FILE = "gate_log.json"
TOKEN = (os.getenv("GATE_TELEGRAM_TOKEN") or os.getenv("TELEGRAM_TOKEN") or "").strip()
CHAT = (os.getenv("GATE_TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID") or "").strip()
SESSION = requests.Session()
SESSION.headers.update({"Accept": "application/json", "User-Agent": "Gate-MultiAsset-SignalBot/2.0"})

MAX_SCAN = 90
MIN_TURNOVER_USDT = 500_000
MAX_ACTIVE = 10
MIN_SCORE = 6
COOLDOWN_MIN = 60
MAX_ENTRY_GAP_ATR = 0.45
MAX_ATR_PCT = 0.08
STOP_ATR = 1.4
TP1_R, TP2_R, TP3_R = 1.2, 2.0, 3.0
ALT_BTC_PENALTY = 1
ALT_STRONG_RELATIVE_24H = 3.0
BTC_CRASH_24H = -4.0
BTC_CRASH_15M = -1.5

def utcnow():
    return datetime.now(timezone.utc)

def stamp():
    return utcnow().isoformat(timespec="seconds")

def num(x, default=0.0):
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as e:
        if path == STATE_FILE:
            raise RuntimeError(f"{path} 읽기 실패: {e}")
        return default

def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
    os.replace(tmp, path)

def log(event, **data):
    rows = load_json(LOG_FILE, [])
    if not isinstance(rows, list):
        rows = []
    rows.append({"time": stamp(), "event": event, **data})
    save_json(LOG_FILE, rows[-1000:])

def api(path, params=None):
    r = SESSION.get(BASE + path, params=params, timeout=18)
    r.raise_for_status()
    return r.json()

def telegram(message):
    if not TOKEN or not CHAT:
        print("Telegram secrets missing")
        return False
    for attempt in range(3):
        try:
            r = SESSION.post(
                f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                json={
                    "chat_id": CHAT,
                    "text": message,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True
                },
                timeout=15
            )
            if r.ok and r.json().get("ok"):
                return True
            print("Telegram error:", r.status_code, r.text[:200])
            if 400 <= r.status_code < 500:
                return False
        except Exception as e:
            print("Telegram attempt failed:", e)
        time.sleep(attempt + 1)
    return False

def fmt(x):
    x = num(x)
    if abs(x) >= 1000:
        return f"{x:,.3f}".rstrip("0").rstrip(".")
    if abs(x) >= 1:
        return f"{x:.5f}".rstrip("0").rstrip(".")
    if abs(x) >= .01:
        return f"{x:.6f}".rstrip("0").rstrip(".")
    return f"{x:.8f}".rstrip("0").rstrip(".")

def ema(values, period):
    if not values:
        return []
    k = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out

def rsi(values, period=14):
    if len(values) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(values)):
        d = values[i] - values[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:]):
        ag = (ag * (period - 1) + g) / period
        al = (al * (period - 1) + l) / period
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)

def atr(candles, period=14):
    if len(candles) < period + 1:
        return 0.0
    tr = []
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1]
        tr.append(max(
            c["h"] - c["l"],
            abs(c["h"] - p["c"]),
            abs(c["l"] - p["c"])
        ))
    a = sum(tr[:period]) / period
    for x in tr[period:]:
        a = (a * (period - 1) + x) / period
    return a

def adx(candles, period=14):
    if len(candles) < period * 2 + 2:
        return 0.0
    tr, pdm, mdm = [], [], []
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1]
        up = c["h"] - p["h"]
        down = p["l"] - c["l"]
        pdm.append(up if up > down and up > 0 else 0.0)
        mdm.append(down if down > up and down > 0 else 0.0)
        tr.append(max(
            c["h"] - c["l"],
            abs(c["h"] - p["c"]),
            abs(c["l"] - p["c"])
        ))
    dxs = []
    for i in range(period, len(tr)):
        trn = sum(tr[i - period + 1:i + 1])
        if trn <= 0:
            continue
        pdi = 100 * sum(pdm[i - period + 1:i + 1]) / trn
        mdi = 100 * sum(mdm[i - period + 1:i + 1]) / trn
        dxs.append(100 * abs(pdi - mdi) / max(pdi + mdi, 1e-9))
    tail = dxs[-period:]
    return sum(tail) / len(tail) if tail else 0.0

def candles(symbol, interval, limit=100):
    raw = api(
        f"/futures/{SETTLE}/candlesticks",
        {"contract": symbol, "interval": interval, "limit": limit}
    )
    out = []
    for x in raw if isinstance(raw, list) else []:
        try:
            out.append({
                "t": int(x["t"]),
                "o": num(x["o"]),
                "h": num(x["h"]),
                "l": num(x["l"]),
                "c": num(x["c"]),
                "v": num(x.get("sum", x.get("v", 0)))
            })
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda z: z["t"])
    return out[:-1] if len(out) > 2 else []

def trend(c):
    closes = [x["c"] for x in c]
    if len(closes) < 55:
        return "NEUTRAL"
    e20, e50 = ema(closes, 20), ema(closes, 50)
    if closes[-1] > e20[-1] > e50[-1] and e20[-1] > e20[-4]:
        return "BULL"
    if closes[-1] < e20[-1] < e50[-1] and e20[-1] < e20[-4]:
        return "BEAR"
    return "NEUTRAL"

def classify(contract):
    raw = str(contract.get("contract_type") or "").lower()
    name = str(contract.get("name") or "")
    if raw in {"metals", "commodities", "indices", "stocks", "forex"}:
        return raw
    if raw in {"crypto", "cryptocurrency", "coin", "coins"}:
        return "crypto"
    if name == "BTC_USDT":
        return "btc"
    if raw in {"", "other", "unknown"} and name.endswith("_USDT"):
        return "crypto"
    return "other"

def market_data():
    contracts = api(f"/futures/{SETTLE}/contracts")
    tickers = api(f"/futures/{SETTLE}/tickers")
    ticker_map = {
        x.get("contract"): x
        for x in tickers if x.get("contract")
    }
    candidates = []
    for c in contracts:
        name = c.get("name", "")
        if c.get("status") != "trading" or not name.endswith("_USDT"):
            continue
        typ = classify(c)
        if typ == "other":
            continue
        t = ticker_map.get(name, {})
        turnover = num(
            t.get("volume_24h_quote")
            or t.get("volume_24h_settle")
            or t.get("volume_24h_usd")
        )
        if name != "BTC_USDT" and turnover < MIN_TURNOVER_USDT:
            continue
        c["_asset"] = typ
        c["_ticker"] = t
        c["_turnover"] = turnover
        candidates.append(c)
    candidates.sort(key=lambda x: x["_turnover"], reverse=True)
    btc = next((x for x in candidates if x["name"] == "BTC_USDT"), None)
    scan = [x for x in candidates if x["name"] != "BTC_USDT"][:MAX_SCAN]
    if btc:
        scan.insert(0, btc)
    return scan, ticker_map

def market_change_15m(symbol):
    c = candles(symbol, "15m", 8)
    if len(c) < 2:
        return 0.0
    if not c[-2]["c"]:
        return 0.0
    return (c[-1]["c"] / c[-2]["c"] - 1) * 100

def btc_regime(ticker_map):
    t = ticker_map.get("BTC_USDT", {})
    change24 = num(t.get("change_percentage"))
    try:
        m15 = market_change_15m("BTC_USDT")
    except Exception:
        m15 = 0.0
    h1trend = "NEUTRAL"
    try:
        h1trend = trend(candles("BTC_USDT", "1h", 90))
    except Exception:
        pass
    crash = change24 <= BTC_CRASH_24H or m15 <= BTC_CRASH_15M
    return {
        "trend": h1trend,
        "change24": change24,
        "m15": m15,
        "crash": crash
    }

def analyze(contract, ticker, btc):
    sym = contract["name"]
    typ = contract["_asset"]
    m15 = candles(sym, "15m", 110)
    h1 = candles(sym, "1h", 90)
    if len(m15) < 60 or len(h1) < 55:
        return None

    close = [x["c"] for x in m15]
    e20, e50 = ema(close, 20), ema(close, 50)
    hclose = [x["c"] for x in h1]
    h20, h50 = ema(hclose, 20), ema(hclose, 50)

    a = atr(m15)
    if a <= 0 or close[-1] <= 0 or a / close[-1] > MAX_ATR_PCT:
        return None

    strength = adx(m15)
    r = rsi(close)
    v = [x["v"] for x in m15]
    vbase = sum(v[-21:-1]) / max(len(v[-21:-1]), 1)
    vr = v[-1] / max(vbase, 1e-9)

    last = m15[-1]
    candle_range = max(last["h"] - last["l"], 1e-9)
    close_pos = (last["c"] - last["l"]) / candle_range
    high20 = max(x["h"] for x in m15[-21:-1])
    low20 = min(x["l"] for x in m15[-21:-1])

    bull15 = close[-1] > e20[-1] > e50[-1] and e20[-1] > e20[-4]
    bear15 = close[-1] < e20[-1] < e50[-1] and e20[-1] < e20[-4]
    bull1h = hclose[-1] > h20[-1] > h50[-1] and h20[-1] > h20[-4]
    bear1h = hclose[-1] < h20[-1] < h50[-1] and h20[-1] < h20[-4]

    long_checks = [
        bull15, bull1h, 48 <= r <= 72,
        strength >= 14, vr >= 1.05,
        close_pos >= .58 or last["c"] > high20
    ]
    short_checks = [
        bear15, bear1h, 28 <= r <= 54,
        strength >= 14, vr >= 1.05,
        close_pos <= .42 or last["c"] < low20
    ]
    ls = sum(bool(x) for x in long_checks)
    ss = sum(bool(x) for x in short_checks)
    btc_note = "미적용"

    # BTC 필터는 코인에만 적용. 금속·주식·지수 등에는 적용하지 않음.
    if typ == "crypto":
        btc_note = f"BTC {btc['trend']} / 24h {btc['change24']:+.2f}%"
        relative = num(ticker.get("change_percentage")) - btc["change24"]
        if btc["crash"]:
            if relative < ALT_STRONG_RELATIVE_24H:
                ls = -99
            else:
                ls -= ALT_BTC_PENALTY
        elif btc["trend"] == "BEAR":
            if not (
                relative >= ALT_STRONG_RELATIVE_24H
                and bull1h and bull15
            ):
                ls -= ALT_BTC_PENALTY
        elif btc["trend"] == "BULL":
            if bear1h and bear15:
                ss -= ALT_BTC_PENALTY

    direction = None
    score = max(ls, ss)
    if ls >= MIN_SCORE and ls > ss:
        direction = "LONG"
    elif ss >= MIN_SCORE and ss > ls:
        direction = "SHORT"
    if not direction:
        return None

    price = num(ticker.get("last"))
    if price <= 0:
        price = last["c"]
    gap_atr = abs(price - last["c"]) / a
    if gap_atr > MAX_ENTRY_GAP_ATR:
        return None

    risk = max(STOP_ATR * a, price * 0.002)
    if direction == "LONG":
        sl = price - risk
        tp1, tp2, tp3 = (
            price + TP1_R * risk,
            price + TP2_R * risk,
            price + TP3_R * risk
        )
    else:
        sl = price + risk
        tp1, tp2, tp3 = (
            price - TP1_R * risk,
            price - TP2_R * risk,
            price - TP3_R * risk
        )

    return {
        "symbol": sym, "asset": typ, "direction": direction,
        "entry": price, "sl": sl, "tp1": tp1, "tp2": tp2,
        "tp3": tp3, "risk": risk, "atr": a, "rsi": r,
        "adx": strength, "volume_ratio": vr, "score": score,
        "bar": last["t"], "btc_note": btc_note,
        "change24": num(ticker.get("change_percentage")),
        "created": stamp(), "tp1_hit": False,
        "tp2_hit": False, "last_monitor_bar": 0
    }

def asset_name(t):
    return {
        "crypto": "알트코인", "btc": "비트코인",
        "metals": "금속", "commodities": "원자재",
        "stocks": "주식", "indices": "주가지수",
        "forex": "외환"
    }.get(t, t)

def tv_link(symbol):
    return f"https://www.gate.com/futures/USDT/{symbol}"

def entry_message(p):
    return (
        f"🚨 <b>GATE.IO 선물 시그널 | {asset_name(p['asset'])}</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📌 종목: <b>{p['symbol']}</b>\n"
        f"🧭 방향: <b>{'🟢 LONG' if p['direction']=='LONG' else '🔴 SHORT'}</b>\n"
        f"💵 진입 기준가: <code>{fmt(p['entry'])}</code>\n"
        f"🛑 초기 손절: <code>{fmt(p['sl'])}</code>\n"
        f"🎯 TP1: <code>{fmt(p['tp1'])}</code> | TP2: <code>{fmt(p['tp2'])}</code>\n"
        f"🏁 TP3: <code>{fmt(p['tp3'])}</code>\n"
        f"📊 점수: <b>{p['score']}/6</b> · RSI {p['rsi']:.1f} · "
        f"ADX {p['adx']:.1f} · 거래량 {p['volume_ratio']:.2f}x\n"
        f"🪙 BTC 필터: {p['btc_note']}\n"
        f"📈 24시간 변동: {p['change24']:+.2f}%\n"
        f"⚠️ 시그널 추적용이며 실제 주문·체결은 하지 않습니다.\n"
        f"🕒 {p['created']}\n"
        f"<a href=\"{tv_link(p['symbol'])}\">Gate.io 차트 열기</a>"
    )

def event_message(p, title, price, detail=""):
    return (
        f"📣 <b>{title}</b>\n━━━━━━━━━━━━━━━━\n"
        f"종목: <b>{p['symbol']}</b> · {p['direction']}\n"
        f"확인 가격: <code>{fmt(price)}</code>\n"
        f"진입: <code>{fmt(p['entry'])}</code>\n"
        f"손절선: <code>{fmt(p['sl'])}</code>\n"
        f"{detail}\n"
        f"⚠️ 시그널 추적 기준이며 실제 체결 손익과 다를 수 있습니다.\n"
        f"<a href=\"{tv_link(p['symbol'])}\">Gate.io 차트 열기</a>"
    )

def close_position(state, sym, reason, price):
    p = state["positions"].pop(sym)
    stats = state.setdefault("stats", {
        "tp3": 0, "tp2": 0, "tp1_be": 0, "sl": 0, "time": 0
    })
    if reason == "TP3":
        stats["tp3"] += 1
    elif reason == "TP2_BE":
        stats["tp2"] += 1
    elif reason == "TP1_BE":
        stats["tp1_be"] += 1
    elif reason == "SL":
        stats["sl"] += 1
    elif reason == "TIME":
        stats["time"] += 1

    state["cooldowns"][sym] = int(time.time()) + COOLDOWN_MIN * 60
    telegram(event_message(
        p, f"시그널 종료: {reason}", price,
        f"종료 분류: <b>{reason}</b>"
    ))
    log("signal_exit", symbol=sym, reason=reason, price=price, position=p)
    send_stats(state)

def send_stats(state):
    s = state.setdefault("stats", {
        "tp3": 0, "tp2": 0, "tp1_be": 0, "sl": 0, "time": 0
    })
    total = sum(int(s.get(k, 0)) for k in (
        "tp3", "tp2", "tp1_be", "sl", "time"
    ))
    wins = int(s.get("tp3", 0)) + int(s.get("tp2", 0))
    losses = int(s.get("sl", 0))
    decided = wins + losses
    rate = wins / decided * 100 if decided else 0
    telegram(
        f"📊 <b>GATE.IO 종료 통계</b>\n━━━━━━━━━━━━━━━━\n"
        f"총 종료 시그널: <code>{total}</code>\n"
        f"TP3 종료: <code>{s.get('tp3',0)}</code>\n"
        f"TP2 이후 본전/보호 종료: <code>{s.get('tp2',0)}</code>\n"
        f"TP1 이후 본전 종료: <code>{s.get('tp1_be',0)}</code>\n"
        f"초기 손절 종료: <code>{s.get('sl',0)}</code>\n"
        f"시간 종료: <code>{s.get('time',0)}</code>\n"
        f"승률(TP2/TP3 승, 초기 손절 패): <b>{rate:.1f}%</b> "
        f"({wins}승/{losses}패; 본전·시간종료 제외)\n"
        f"현재 추적 중: <code>{len(state.get('positions',{}))}</code>\n"
        f"※ 모의 추적 통계이며 실현 수익률이 아닙니다."
    )

def monitor_positions(state, tickers):
    for sym in list(state["positions"].keys()):
        p = state["positions"].get(sym)
        if not p:
            continue
        try:
            rows = candles(sym, "15m", 5)
            if not rows:
                continue
            c = rows[-1]
            if c["t"] <= int(p.get("last_monitor_bar", 0)):
                continue
            p["last_monitor_bar"] = c["t"]
            long = p["direction"] == "LONG"
            hit_sl = c["l"] <= p["sl"] if long else c["h"] >= p["sl"]
            hit_tp3 = c["h"] >= p["tp3"] if long else c["l"] <= p["tp3"]
            hit_tp2 = c["h"] >= p["tp2"] if long else c["l"] <= p["tp2"]
            hit_tp1 = c["h"] >= p["tp1"] if long else c["l"] <= p["tp1"]

            # 한 캔들에서 손절과 목표가가 함께 닿으면 보수적으로 손절 우선.
            if hit_sl:
                close_position(state, sym, "SL", p["sl"])
                continue
            if hit_tp3:
                close_position(state, sym, "TP3", p["tp3"])
                continue

            if hit_tp2 and not p["tp2_hit"]:
                p["tp2_hit"] = True
                p["sl"] = p["entry"]
                telegram(event_message(
                    p, "TP2 도달 · 손절선을 진입가로 이동", p["tp2"]
                ))
                log("tp2", symbol=sym, price=p["tp2"])
            elif hit_tp1 and not p["tp1_hit"]:
                p["tp1_hit"] = True
                p["sl"] = p["entry"]
                telegram(event_message(
                    p, "TP1 도달 · 손절선을 진입가로 이동", p["tp1"]
                ))
                log("tp1", symbol=sym, price=p["tp1"])

            crossed_entry = c["l"] <= p["entry"] <= c["h"]
            if (p["tp1_hit"] or p["tp2_hit"]) and crossed_entry:
                reason = "TP2_BE" if p["tp2_hit"] else "TP1_BE"
                close_position(state, sym, reason, p["entry"])
                continue

            created = datetime.fromisoformat(p["created"]).timestamp()
            if utcnow().timestamp() - created > 36 * 3600:
                close_position(state, sym, "TIME", c["c"])
                continue

            state["positions"][sym] = p
        except Exception as e:
            print("Monitor error", sym, e)
            log("monitor_error", symbol=sym, error=str(e))
    save_json(STATE_FILE, state)

def main():
    print(f"GATE.IO MULTI-ASSET SIGNAL BOT v{VERSION} | {stamp()}")
    state = load_json(STATE_FILE, {})
    if not isinstance(state, dict):
        raise RuntimeError("Invalid gate_state.json")

    state.setdefault("positions", {})
    state.setdefault("cooldowns", {})
    state.setdefault("last_bars", {})
    state.setdefault("stats", {
        "tp3": 0, "tp2": 0, "tp1_be": 0, "sl": 0, "time": 0
    })
    save_json(STATE_FILE, state)

    contracts, ticker_map = market_data()
    btc = btc_regime(ticker_map)
    print(
        f"Contracts to scan: {len(contracts)} | BTC trend={btc['trend']} "
        f"24h={btc['change24']:.2f}% 15m={btc['m15']:.2f}% "
        f"crash={btc['crash']}"
    )

    monitor_positions(state, ticker_map)
    state = load_json(STATE_FILE, state)
    state.setdefault("positions", {})
    state.setdefault("cooldowns", {})
    state.setdefault("last_bars", {})

    if len(state["positions"]) >= MAX_ACTIVE:
        print("Active position limit reached; no new signals.")
        return

    ranked = []
    for contract in contracts:
        sym = contract["name"]
        if sym in state["positions"]:
            continue
        if int(state["cooldowns"].get(sym, 0)) > int(time.time()):
            continue
        ticker = ticker_map.get(sym, {})
        if not ticker:
            continue
        try:
            info = analyze(contract, ticker, btc)
            if not info:
                continue
            if int(state["last_bars"].get(sym, 0)) == int(info["bar"]):
                continue
            ranked.append(info)
        except Exception as e:
            print("Analyze error", sym, e)
            log("analyze_error", symbol=sym, error=str(e))

    ranked.sort(
        key=lambda x: (x["score"], x["volume_ratio"], x["adx"]),
        reverse=True
    )
    slots = max(0, MAX_ACTIVE - len(state["positions"]))
    for p in ranked[:slots]:
        state["positions"][p["symbol"]] = p
        state["last_bars"][p["symbol"]] = int(p["bar"])
        save_json(STATE_FILE, state)
        telegram(entry_message(p))
        log(
            "signal_entry",
            symbol=p["symbol"],
            asset=p["asset"],
            direction=p["direction"],
            score=p["score"],
            entry=p["entry"]
        )
        print("SIGNAL", p["symbol"], p["direction"], p["score"])

    save_json(STATE_FILE, state)
    print(f"Qualified={len(ranked)} | Active={len(state['positions'])}")

if __name__ == "__main__":
    main()
