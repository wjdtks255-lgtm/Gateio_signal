from datetime import datetime, timedelta, timezone
import json
import os
import time
import requests

TOKEN = os.getenv("GATE_TELEGRAM_TOKEN", "").strip()
CHAT_ID = (os.getenv("GATE_TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID") or "").strip()
STATE_FILE = "gate_state.json"
KST = timezone(timedelta(hours=9))
TV_URL = "https://scanner.tradingview.com/crypto/scan"

MAX_SLOTS = 5
COOLDOWN = 240
SL_PENALTY_HOURS = 12
MIN_SCORE = 82

SYMBOL_MAP = {
    "1000PEPEUSDT": "PEPEUSDT",
    "1000SHIBUSDT": "SHIBUSDT",
    "1000BONKUSDT": "BONKUSDT",
    "1000FLOKIUSDT": "FLOKIUSDT",
}

COL = [
    "open|15",
    "high|15",
    "low|15",
    "close|15",
    "volume|15",
    "EMA20|15",
    "EMA50|15",
    "EMA100|15",
    "RSI|15",
    "ADX|15",
    "ATR|15",
    "open|60",
    "high|60",
    "low|60",
    "close|60",
    "volume|60",
    "EMA20|60",
    "EMA50|60",
    "EMA100|60",
    "RSI|60",
    "ADX|60",
    "ATR|60",
    "change|15",
    "change|60",
]

HEAD = {
    "User-Agent": "Mozilla/5.0",
    "Origin": "https://www.tradingview.com",
    "Referer": "https://www.tradingview.com/",
    "Content-Type": "application/json",
}


def now():
    return datetime.now(KST)


def load():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        s = {"positions": {}, "signals": {}, "sl_penalties": {}}
    s.setdefault("positions", {})
    s.setdefault("signals", {})
    s.setdefault("sl_penalties", {})
    return s


def save(s):
    with open(STATE_FILE + ".tmp", "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
    os.replace(STATE_FILE + ".tmp", STATE_FILE)


def tg(msg, label="MSG"):
    if not TOKEN or not CHAT_ID:
        print("TG CONFIG ERROR")
        return False
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = {
        "chat_id": CHAT_ID,
        "text": msg,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
        "disable_notification": False,
    }
    for n in range(3):
        try:
            r = requests.post(url, json=data, timeout=20)
            j = r.json() if r.status_code == 200 else {}
            if r.status_code == 200 and j.get("ok") is True:
                time.sleep(1.2)
                return True
            if r.status_code == 429:
                retry_after = int(r.headers.get("Retry-After", 3))
                time.sleep(retry_after)
        except Exception as e:
            print("TG ERROR:", e)
        if n < 2:
            time.sleep(1.5)
    return False


def rows():
    q = {
        "options": {"lang": "en"},
        "markets": ["crypto"],
        "filter": [{"left": "exchange", "operation": "equal", "right": "GATEIO"}],
        "columns": COL,
        "sort": {"sortBy": "volume|15", "sortOrder": "desc"},
        "range": [0, 1000],
    }
    try:
        r = requests.post(TV_URL, json=q, headers=HEAD, timeout=25)
        return r.json().get("data", []) if r.status_code == 200 else []
    except Exception as e:
        print("TV ERROR:", e)
        return []


def v(r, i):
    try:
        return float(r["d"][i])
    except Exception:
        return 0.0


def symbol(r):
    return str(r.get("s", "")).split(":")[-1].replace(".P", "")


def fmt(x):
    if x >= 1000:
        return f"{x:,.2f}"
    if x >= 1:
        return f"{x:.4f}"
    if x >= 0.01:
        return f"{x:.5f}"
    return f"{x:.8f}".rstrip("0").rstrip(".")


def fetch_klines(sym, limit=12):
    try:
        clean_sym = sym.upper().replace(".P", "")
        clean_sym = SYMBOL_MAP.get(clean_sym, clean_sym)
        url = f"https://api.gateio.ws/api/v4/futures/usdt/candlesticks?contract={clean_sym}&interval=15m&limit={limit}"
        r = requests.get(url, timeout=4)
        if r.status_code == 200:
            data = r.json()
            klines = []
            for item in data:
                try:
                    c_open = float(item.get("open", item[5] if len(item) > 5 else 0))
                    c_high = float(item.get("high", item[3] if len(item) > 3 else 0))
                    c_low = float(item.get("low", item[4] if len(item) > 4 else 0))
                    c_close = float(item.get("close", item[2] if len(item) > 2 else 0))
                    klines.append({"open": c_open, "high": c_high, "low": c_low, "close": c_close})
                except Exception:
                    pass
            if klines:
                return klines
    except Exception as e:
        print(f"KLINE FETCH ERROR ({sym}):", e)
    return []


def verify_candle_structure(sym, direction, current_p):
    klines = fetch_klines(sym, limit=12)
    if len(klines) < 8:
        return True, "KLINE_FETCH_SKIP"
    history = klines[:-1]
    curr = klines[-1]
    recent_highs = [k["high"] for k in history[-5:]]
    recent_lows = [k["low"] for k in history[-5:]]
    max_range = (max(recent_highs) - min(recent_lows)) / current_p * 100
    if max_range < 0.25:
        return False, "FLAT_CONSOLIDATION"
    if direction == "LONG":
        has_pullback = any(k["close"] < k["open"] for k in history[-6:-1])
        is_rebound = curr["close"] > curr["open"] and curr["close"] > history[-1]["close"]
        if not has_pullback or not is_rebound:
            return False, "NO_WAVE"
    else:
        has_bounce = any(k["close"] > k["open"] for k in history[-6:-1])
        is_breakdown = curr["close"] < curr["open"] and curr["close"] < history[-1]["low"]
        if not has_bounce or not is_breakdown:
            return False, "NO_WAVE"
    return True, "STRUCTURE_VALIDATED"


def analyze(r, st):
    p, o, h, l = [v(r, i) for i in (3, 0, 1, 2)]
    e20, e50, e100, rs, adx, atr = [v(r, i) for i in (5, 6, 7, 8, 9, 10)]
    ph, e20h, e50h, e100h, rsh, adxh, atrh = [v(r, i) for i in (14, 16, 17, 18, 19, 20, 21)]
    ch15, ch60 = [v(r, i) for i in (22, 23)]

    if min(p, e20, e50, e100, e20h, e50h, e100h, atr, atrh) <= 0:
        return

    atrp = atr / p * 100
    atrph = atrh / ph * 100
    d20 = (p - e20) / e20 * 100

    if atrp < 0.18 or atrp > 3.5:
        return

    bull = e20h > e50h > e100h and e20 > e50 > e100 and ph > e20h
    bear = e20h < e50h < e100h and e20 < e50 < e100 and ph < e20h

    if not bull and not bear:
        return

    direction = "LONG" if bull else "SHORT"

    if direction == "LONG":
        pullback = -0.75 <= d20 <= 0.25
        rsi_ok = 34 <= rs <= 67
    else:
        pullback = -0.25 <= d20 <= 0.75
        rsi_ok = 33 <= rs <= 66

    if not pullback or not rsi_ok or adx < 20:
        return

    sym = symbol(r)
    valid_structure, reason = verify_candle_structure(sym, direction, p)
    if not valid_structure:
        return

    score = 85
    risk = max(atr * 1.20, p * 0.0045)

    if direction == "LONG":
        sl = p - risk
        tp1 = p + risk * 1.5
        tp2 = p + risk * 2.5
        tp3 = p + risk * 4.0
        pos_text = "롱 (LONG) 🟢"
    else:
        sl = p + risk
        tp1 = p - risk * 1.5
        tp2 = p - risk * 2.5
        tp3 = p - risk * 4.0
        pos_text = "숏 (SHORT) 🔴"

    lev = 8 if atrp < 0.8 else (6 if atrp < 1.4 else 5)

    return {
        "symbol": sym,
        "direction": direction,
        "score": score,
        "price": p,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "lev": lev,
        "pos_text": pos_text,
    }


def msg(s, slot_no):
    coin_name = s["symbol"].replace("USDT", "")
    return (
        f"🚨 *Gate.io 소액러 맞춤 트레이딩 시그널* 🚨\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 슬롯 번호: `[ {slot_no} / {MAX_SLOTS} ]`\n"
        f"🪙 종목명: `{coin_name} / USDT`\n"
        f"📊 포지션: **{s['pos_text']}**\n"
        f"💰 실시간 진입가: `{fmt(s['price'])}` USDT\n\n"
        f"💡 소액러 부담 완화 가이드\n"
        f"  • 권장 레버리지: `{s['lev']}x` (격리 마진)\n\n"
        f"🎯 목표가 (Take Profit)\n"
        f"  • 1차 (TP1): `{fmt(s['tp1'])}` (본전 스탑)\n"
        f"  • 2차 (TP2): `{fmt(s['tp2'])}`\n"
        f"  • 3차 (TP3): `{fmt(s['tp3'])}` (최종 익절)\n\n"
        f"🛡️ 손절가 (Stop Loss): `{fmt(s['sl'])}`\n"
        f"⏱️ 발생 시간: `{now().strftime('%Y-%m-%d %H:%M:%S')} (KST)`"
    )


def posmsg(p, k):
    sy = p.get("symbol", "UNKNOWN").replace("USDT", "")
    d = p.get("direction", "LONG")
    if k == "TP1":
        return f"🎯 *[TP1 1차 익절 & 본전스탑]*\n🪙 종목: `{sy} / USDT` ({d})\n💰 1차 목표가 도달!\n🛡️ 손절가를 진입가로 상향 조정."
    if k == "TP2":
        return f"🎯 *[TP2 2차 익절 도달]*\n🪙 종목: `{sy} / USDT` ({d})\n💰 2차 목표가 도달!"
    if k == "TP3":
        return f"🎉 *[TP3 최종 익절 달성 마감!]*\n🪙 종목: `{sy} / USDT` ({d})\n🎯 3차 목표가 달성 완료!"
    return f"🛡️ *[손절(SL) 도달 마감]*\n🪙 종목: `{sy} / USDT` ({d})\n📉 손절가 이탈로 포지션 종료 (12시간 재진입 금지)"


def check(state, rs):
    mp = {symbol(r): r for r in rs}
    for key, p in list(state["positions"].items()):
        if not isinstance(p, dict):
            state["positions"].pop(key, None)
            continue
        p.setdefault("symbol", key)
        r = mp.get(p["symbol"])
        if not r:
            continue
        price = v(r, 3)
        long = p["direction"] == "LONG"

        if long and price >= p["tp3"] or not long and price <= p["tp3"]:
            tg(posmsg(p, "TP3"), f"{p['symbol']} TP3")
            state["positions"].pop(key, None)
            continue

        if long and price <= p["sl"] or not long and price >= p["sl"]:
            tg(posmsg(p, "SL"), f"{p['symbol']} SL")
            state.setdefault("sl_penalties", {})[key] = time.time()
            state["positions"].pop(key, None)
            continue

        if long and price >= p["tp2"] and "TP2" not in p.get("hit", []):
            tg(posmsg(p, "TP2"), f"{p['symbol']} TP2")
            p.setdefault("hit", []).append("TP2")

        if not p.get("tp1_hit", False) and (price >= p["tp1"] if long else price <= p["tp1"]):
            p["tp1_hit"] = True
            p["sl"] = p["entry"]
            tg(posmsg(p, "TP1"), f"{p['symbol']} TP1")


def main():
    rs = rows()
    if not rs:
        return

    state = load()
    check(state, rs)

    candidates = []
    for r in rs:
        try:
            s = analyze(r, None)
            if s:
                candidates.append(s)
        except Exception:
            pass

    current_active_count = len(state["positions"])
    available_slots = MAX_SLOTS - current_active_count

    sent = 0
    t = time.time()
    sl_penalties = state.get("sl_penalties", {})

    if available_slots > 0:
        for s in candidates:
            if sent >= available_slots:
                break
            key = s["symbol"]

            if key in state["positions"] or key in sl_penalties:
                if key in sl_penalties and t - float(sl_penalties[key]) < SL_PENALTY_HOURS * 3600:
                    continue

            sid = f"{key}:{s['direction']}"
            if t - float(state["signals"].get(sid, 0) or 0) < COOLDOWN * 60:
                continue

            current_slot_number = current_active_count + sent + 1
            if not tg(msg(s, current_slot_number), f"{key} {s['direction']}"):
                continue

            state["signals"][sid] = t
            state["positions"][key] = {
                "symbol": key,
                "direction": s["direction"],
                "entry": s["price"],
                "sl": s["sl"],
                "tp1": s["tp1"],
                "tp2": s["tp2"],
                "tp3": s["tp3"],
                "tp1_hit": False,
                "hit": [],
            }
            save(state)
            sent += 1

    save(state)


if __name__ == "__main__":
    main()
