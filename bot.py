from datetime import datetime, timedelta, timezone
import json, os, time, math
import requests

TOKEN = os.getenv("GATE_TELEGRAM_TOKEN", "").strip()
CHAT = (os.getenv("GATE_TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID") or "").strip()
STATE = "gate_state.json"
KST = timezone(timedelta(hours=9))
BASE = "https://fx-api.gateio.ws/api/v4/futures/usdt"
MAX_SLOTS = 8
MAX_SCAN = 70
MIN_TURNOVER = 50000
COOLDOWN = 120 * 60
SL_COOLDOWN = 12 * 3600
MAX_HOLD = 24 * 3600
MIN_SCORE = 7
MIN_ADX = 18
MAX_ATR_PCT = 0.06
MAX_RISK_PCT = 0.035
MAX_ENTRY_DISTANCE_ATR = 1.6
TP1_R, TP2_R, TP3_R = 1.0, 1.8, 2.8

S = requests.Session()
S.headers.update({"Accept": "application/json", "User-Agent": "GateSignalBot/2.0"})


def now():
    return datetime.now(KST)


def api(path, params=None, timeout=15):
    r = S.get(BASE + path, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def tg(msg):
    if not TOKEN or not CHAT:
        print("TELEGRAM CONFIG ERROR")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT, "text": msg, "parse_mode": "Markdown",
                  "disable_web_page_preview": True}, timeout=15)
        # 💡 연속 알림 폭탄 방지를 위한 미세 딜레이 (1.5초)
        time.sleep(1.5)
        if r.ok and r.json().get("ok"):
            return True
        print("TG ERROR:", r.text[:250])
    except Exception as e:
        print("TG ERROR:", str(e)[:150])
    return False


def load():
    try:
        with open(STATE, encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        s = {}
    s.setdefault("positions", {})
    s.setdefault("signals", {})
    s.setdefault("sl_penalties", {})
    s.setdefault("stats", {})
    for k in ("tp3_end", "tp2_end", "tp1_end", "sl_end", "timeouts"):
        s["stats"].setdefault(k, 0)
    return s


def save(s):
    with open(STATE + ".tmp", "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(STATE + ".tmp", STATE)


def ema(a, n):
    if len(a) < n:
        return None
    v = sum(a[:n]) / n
    for x in a[n:]:
        v += (2 / (n + 1)) * (x - v)
    return v


def rsi(a, n=14):
    if len(a) < n + 1:
        return 50.0
    d = [a[i] - a[i - 1] for i in range(1, len(a))]
    g = sum(max(x, 0) for x in d[:n]) / n
    l = sum(max(-x, 0) for x in d[:n]) / n
    for x in d[n:]:
        g = (g * (n - 1) + max(x, 0)) / n
        l = (l * (n - 1) + max(-x, 0)) / n
    return 100.0 if l == 0 else 100 - 100 / (1 + g / l)


def candles(contract, interval, limit=80):
    try:
        d = api("/candlesticks", {
            "contract": contract, "interval": interval, "limit": limit
        })
        out = []
        for x in d:
            t = int(x.get("t", 0))
            o = float(x.get("o", 0) or 0)
            h = float(x.get("h", 0) or 0)
            l = float(x.get("l", 0) or 0)
            c = float(x.get("c", 0) or 0)
            v = float(x.get("sum", 0) or 0)
            if t > 0 and min(o, h, l, c) > 0 and h >= l:
                out.append((t, o, h, l, c, v))
        out.sort(key=lambda z: z[0])
        sec = {"15m": 900, "1h": 3600}.get(interval, 900)
        cutoff = int(time.time())
        return [x for x in out if x[0] + sec <= cutoff]
    except Exception as e:
        print("CANDLE ERROR", contract, str(e)[:100])
        return []


def atr(data, n=14):
    if len(data) < n + 1:
        return 0.0
    tr = []
    for i in range(1, len(data)):
        prev = data[i - 1][4]
        _, _, h, l, _, _ = data[i]
        tr.append(max(h - l, abs(h - prev), abs(l - prev)))
    return sum(tr[-n:]) / n


def adx(data, n=14):
    if len(data) < n * 2 + 2:
        return 0.0
    trs, plus, minus = [], [], []
    for i in range(1, len(data)):
        _, _, h, l, c, _ = data[i]
        prev_h, prev_l, prev_c = data[i - 1][2], data[i - 1][3], data[i - 1][4]
        trs.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
        up, down = h - prev_h, prev_l - l
        plus.append(up if up > down and up > 0 else 0.0)
        minus.append(down if down > up and down > 0 else 0.0)
    dx = []
    for i in range(len(trs) - n + 1):
        tr = sum(trs[i:i+n])
        if tr <= 0:
            continue
        p = 100 * sum(plus[i:i+n]) / tr
        m = 100 * sum(minus[i:i+n]) / tr
        dx.append(100 * abs(p-m) / (p+m) if p+m else 0.0)
    return sum(dx[-n:]) / min(n, len(dx)) if dx else 0.0


def turnover(x):
    for k in ("volume_24h_usd", "volume_24h_quote",
              "volume_24h_usdt", "volume_24h"):
        try:
            v = float(x.get(k, 0) or 0)
            if v > 0:
                return v
        except (ValueError, TypeError):
            pass
    return 0.0


def get_tickers():
    d = api("/tickers")
    return d if isinstance(d, list) else d.get("data", [])


def btc_direction():
    d = candles("BTC_USDT", "1h", 80)
    if len(d) < 55:
        return "NEUTRAL"
    c = [x[4] for x in d]
    e20, e50 = ema(c, 20), ema(c, 50)
    if not e20 or not e50:
        return "NEUTRAL"
    if e20 > e50 and c[-1] > e20:
        return "BULL"
    if e20 < e50 and c[-1] < e20:
        return "BEAR"
    return "NEUTRAL"


def choose_pool(tickers):
    valid = []
    for x in tickers:
        contract = x.get("contract", "")
        if not contract.endswith("_USDT") or contract == "BTC_USDT":
            continue
        if str(x.get("in_delisting", "false")).lower() == "true":
            continue
        try:
            price = float(x.get("last", 0) or 0)
            change = float(x.get("change_percentage", 0) or 0)
        except (ValueError, TypeError):
            continue
        if price <= 0:
            continue
        tv = turnover(x)
        if tv > 0 and tv < MIN_TURNOVER:
            continue
        valid.append({
            "contract": contract, "price": price,
            "change": change, "turnover": tv
        })

    liquid = sorted(valid, key=lambda x: x["turnover"], reverse=True)[:35]
    movers = sorted(valid, key=lambda x: abs(x["change"]), reverse=True)[:35]
    pool = {x["contract"]: x for x in liquid + movers}
    result = list(pool.values())
    result.sort(key=lambda x: (
        (1 if x["turnover"] >= MIN_TURNOVER else 0),
        math.log10(max(x["turnover"], 1)),
        min(abs(x["change"]), 100)
    ), reverse=True)
    return result[:MAX_SCAN]


def analyze(x, btc):
    contract = x["contract"]
    m15 = candles(contract, "15m", 80)
    h1 = candles(contract, "1h", 80)
    if len(m15) < 60 or len(h1) < 55:
        return None

    c = [z[4] for z in m15]
    o, h, l = [z[1] for z in m15], [z[2] for z in m15], [z[3] for z in m15]
    v = [z[5] for z in m15]
    c1 = [z[4] for z in h1]
    e20, e50 = ema(c, 20), ema(c, 50)
    e1, e2 = ema(c1, 20), ema(c1, 50)
    a, ax, rr = atr(m15), adx(m15), rsi(c)
    if not e20 or not e50 or not e1 or not e2 or a <= 0:
        return None

    p, op, high, low = c[-1], o[-1], h[-1], l[-1]
    if p <= 0 or a / p > MAX_ATR_PCT or ax < MIN_ADX:
        return None

    avg_turn = sum(v[-11:-1]) / 10 if len(v) >= 11 else 0
    if avg_turn < 10000:
        return None
    vr = v[-1] / avg_turn if avg_turn > 0 else 0
    if vr <= 0:
        return None

    up1 = e1 > e2 and c1[-1] > e1
    dn1 = e1 < e2 and c1[-1] < e1
    up15 = e20 > e50 and p > e20
    dn15 = e20 < e50 and p < e20

    prior_hi = max(h[-21:-1])
    prior_lo = min(l[-21:-1])
    buffer = max(a * 0.12, p * 0.0005)
    breakout_up = p > prior_hi + buffer and vr >= 1.15
    breakout_dn = p < prior_lo - buffer and vr >= 1.15

    close_pos = (p - low) / max(high - low, 1e-12)
    pullback_long = (
        up15 and l[-1] <= e20 + a * 0.20 and p > e20
        and p > op and close_pos >= 0.62
        and c[-2] <= e20 + a * 0.25
    )
    pullback_short = (
        dn15 and high >= e20 - a * 0.20 and p < e20
        and p < op and close_pos <= 0.38
        and c[-2] >= e20 - a * 0.25
    )

    candidates = []
    for direction in ("LONG", "SHORT"):
        long = direction == "LONG"
        aligned = (up1 and up15) if long else (dn1 and dn15)
        structure = (breakout_up or pullback_long) if long else (breakout_dn or pullback_short)
        if not aligned or not structure:
            continue
        if long and btc == "BEAR":
            continue
        if not long and btc == "BULL":
            continue

        score, reasons = 0, []
        if (up1 if long else dn1):
            score += 2
            reasons.append("1시간 추세 일치")
        if (up15 if long else dn15):
            score += 2
            reasons.append("15분 추세 일치")
        if (breakout_up if long else breakout_dn):
            score += 2
            reasons.append("거래량 동반 돌파")
        elif (pullback_long if long else pullback_short):
            score += 2
            reasons.append("반전 확인 눌림목")
        if (50 <= rr <= 67) if long else (33 <= rr <= 50):
            score += 1
            reasons.append(f"RSI {rr:.1f}")
        if vr >= 1.3:
            score += 1
            reasons.append(f"거래량 {vr:.1f}배")
        if ax >= 23:
            score += 1
            reasons.append(f"ADX {ax:.1f}")
        if btc == ("BULL" if long else "BEAR"):
            score += 1
            reasons.append("BTC 추세 동조")

        if rr > 72 if long else rr < 28:
            continue
        if score < MIN_SCORE:
            continue
        if abs(p - e20) > a * MAX_ENTRY_DISTANCE_ATR:
            continue

        risk = max(a * 1.4, p * 0.008)
        if risk / p > MAX_RISK_PCT:
            continue

        sl = p - risk if long else p + risk
        sign = 1 if long else -1
        candidates.append({
            "symbol": contract[:-5], "contract": contract,
            "direction": direction, "price": p, "risk": risk,
            "sl": sl, "tp1": p + sign * risk * TP1_R,
            "tp2": p + sign * risk * TP2_R,
            "tp3": p + sign * risk * TP3_R,
            "score": score, "reasons": reasons,
            "rsi": rr, "adx": ax, "atr": a,
            "turnover": x["turnover"],
            "rank": score + min(vr, 3) * 0.3 + min(ax, 35) * 0.02
        })
    return max(candidates, key=lambda z: z["rank"]) if candidates else None


def fmt(x):
    if x >= 1000:
        return f"{x:,.2f}"
    if x >= 1:
        return f"{x:.4f}"
    if x >= 0.01:
        return f"{x:.6f}"
    return f"{x:.8f}".rstrip("0").rstrip(".")


def signal_msg(s, slot):
    d = "🟢 LONG 매수" if s["direction"] == "LONG" else "🔴 SHORT 매도"
    return (
        "🚨 *GATE.IO FUTURES SIGNAL*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📍 슬롯: `{slot}/{MAX_SLOTS}`\n"
        f"🪙 종목: `{s['symbol']}_USDT`\n"
        f"📈 방향: *{d}*\n"
        f"⭐ 신호 점수: `{s['score']}/10`\n"
        f"💰 기준 진입가: `{fmt(s['price'])}` USDT\n"
        f"🛑 손절가: `{fmt(s['sl'])}`\n"
        f"🎯 TP1: `{fmt(s['tp1'])}`\n"
        f"🎯 TP2: `{fmt(s['tp2'])}`\n"
        f"🎯 TP3: `{fmt(s['tp3'])}`\n"
        f"📊 RSI: `{s['rsi']:.1f}` | ADX: `{s['adx']:.1f}`\n"
        f"🔎 근거: {', '.join(s['reasons'])}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "⚠️ 신호 기준가 기반 모의 추적이며 실제 체결가는 다를 수 있습니다.\n"
        f"⏱️ `{now():%Y-%m-%d %H:%M:%S} KST`"
    )


def result_msg(p, kind):
    text = {
        "TP1": "🎯 TP1 도달 — 추세 관찰 중",
        "TP2": "🎯 TP2 도달 — 남은 시그널 추적 중",
        "TP3_END": "🏆 TP3 도달 — 시그널 최종 종료",
        "TP2_END": "🎯 TP2 도달 후 본전 스탑 종료",
        "TP1_END": "🟡 TP1 도달 후 본전 스탑 종료",
        "SL_END": "🛑 초기 손절가 도달 — 시그널 종료",
        "TIME_END": "⏱️ 최대 보유 시간 초과 — 시그널 종료"
    }.get(kind, kind)
    return f"📊 *포지션 추적 알림*\n`{p.get('symbol','?')}_USDT` | `{p.get('direction','?')}`\n{text}"


def status_msg(positions):
    if not positions:
        return "📊 *현재 활성 시그널 없음*\n모든 슬롯이 대기 중입니다."
    lines = ["📊 *현재 활성 시그널*", "━━━━━━━━━━━━━━━━━━"]
    for i, (sym, p) in enumerate(positions.items(), 1):
        hit = p.get("max_tp_hit", 0)
        status = "TP2 도달" if hit == 2 else "TP1 도달" if hit == 1 else "추적 중"
        d = "🟢 LONG" if p.get("direction") == "LONG" else "🔴 SHORT"
        lines.append(f"`{i}. {sym}_USDT` {d}\n진입: `{fmt(p.get('entry',0))}` | {status}")
    lines.append(f"⏱️ `{now():%m-%d %H:%M} KST`")
    return "\n".join(lines)


def get_stats_report(stats):
    tp3, tp2 = stats["tp3_end"], stats["tp2_end"]
    tp1, sl, tout = stats["tp1_end"], stats["sl_end"], stats["timeouts"]
    wins = tp1 + tp2 + tp3
    total = wins + sl
    total_all = total + tout
    rate = wins / total * 100 if total else 0
    return (
        "📊 *시그널 누적 성과 통계*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"총 종료 시그널: `{total_all}건`\n\n"
        f"🎯 TP3 종료: `{tp3}건`\n"
        f"🎯 TP2 이후 종료: `{tp2}건`\n"
        f"🎯 TP1 이후 본전 종료: `{tp1}건`\n"
        f"🛑 초기 손절 종료: `{sl}건`\n"
        f"⏱️ 시간 종료: `{tout}건`\n\n"
        f"🏆 분류상 승률: `{rate:.1f}%`\n"
        f"(TP 종료·본전 종료 `{wins}건` / 초기 손절 `{sl}건`)\n"
        "※ 신호 기준가 기반 모의 추적 통계이며 실제 계좌 수익률이 아닙니다."
    )


def monitor(s):
    changed = False
    stats = s["stats"]
    for key, p in list(s["positions"].items()):
        if not isinstance(p, dict):
            s["positions"].pop(key, None)
            changed = True
            continue

        if time.time() - p.get("entry_time", time.time()) > MAX_HOLD:
            tg(result_msg(p, "TIME_END"))
            stats["timeouts"] += 1
            s["positions"].pop(key, None)
            changed = True
            continue

        data = candles(key + "_USDT", "15m", 10)
        if not data:
            continue
        bars = [z for z in data
                if z[0] > p.get("entry_bar", 0)
                and z[0] > p.get("last_bar", 0)]
        if not bars:
            continue

        for bar in bars:
            t, op, high, low, close, vol = bar
            long = p["direction"] == "LONG"
            sl = p["sl"]
            stop_hit = low <= sl if long else high >= sl
            tp1_hit = high >= p["tp1"] if long else low <= p["tp1"]
            tp2_hit = high >= p["tp2"] if long else low <= p["tp2"]
            tp3_hit = high >= p["tp3"] if long else low <= p["tp3"]

            if stop_hit:
                hit = p.get("max_tp_hit", 0)
                if hit >= 2:
                    stats["tp2_end"] += 1
                    tg(result_msg(p, "TP2_END"))
                elif hit == 1:
                    stats["tp1_end"] += 1
                    tg(result_msg(p, "TP1_END"))
                else:
                    stats["sl_end"] += 1
                    s["sl_penalties"][key] = time.time()
                    tg(result_msg(p, "SL_END"))
                s["positions"].pop(key, None)
                changed = True
                break

            if tp3_hit:
                stats["tp3_end"] += 1
                tg(result_msg(p, "TP3_END"))
                s["positions"].pop(key, None)
                changed = True
                break

            if tp2_hit and p.get("max_tp_hit", 0) < 2:
                if p.get("max_tp_hit", 0) < 1:
                    p["tp1_bar_time"] = t
                p["max_tp_hit"] = 2
                p["sl"] = p["entry"]
                p["be_adjusted"] = True
                tg(result_msg(p, "TP2"))
                changed = True
            elif tp1_hit and p.get("max_tp_hit", 0) < 1:
                p["max_tp_hit"] = 1
                p["tp1_bar_time"] = t
                tg(result_msg(p, "TP1"))
                changed = True

            if (p.get("max_tp_hit", 0) >= 1
                    and not p.get("be_adjusted", False)
                    and t > p.get("tp1_bar_time", 0)):
                p["sl"] = p["entry"]
                p["be_adjusted"] = True
                changed = True

            p["last_bar"] = t
        else:
            p["last_bar"] = bars[-1][0]
            changed = True
    return changed


def main():
    print("GATE.IO SIGNAL BOT — FILTERED ENTRY")
    if not TOKEN or not CHAT:
        print("Missing GATE_TELEGRAM_TOKEN or GATE_TELEGRAM_CHAT_ID")
        return

    s = load()
    position_changed = monitor(s)
    save(s)

    try:
        tickers = get_tickers()
    except Exception as e:
        print("TICKER ERROR:", str(e)[:200])
        if position_changed:
            tg(get_stats_report(s["stats"]))
        return

    pool = choose_pool(tickers)
    btc = btc_direction()
    print("TICKERS:", len(tickers), "POOL:", len(pool), "BTC:", btc)

    candidates = []
    for x in pool:
        try:
            c = analyze(x, btc)
            if c:
                candidates.append(c)
        except Exception as e:
            print("ANALYSIS ERROR", x["contract"], str(e)[:100])
        time.sleep(0.04)

    candidates.sort(key=lambda x: x["rank"], reverse=True)
    print("QUALIFIED:", len(candidates))

    sent = 0
    available = max(0, MAX_SLOTS - len(s["positions"]))
    t = time.time()

    for c in candidates:
        if sent >= available:
            break
        key = c["symbol"]
        if key in s["positions"]:
            continue
        if t - float(s["sl_penalties"].get(key, 0) or 0) < SL_COOLDOWN:
            continue
        sid = key + ":" + c["direction"]
        if t - float(s["signals"].get(sid, 0) or 0) < COOLDOWN:
            continue

        entrybar = int(time.time() // 900 * 900)
        slot = len(s["positions"]) + 1
        if not tg(signal_msg(c, slot)):
            continue

        s["signals"][sid] = t
        s["positions"][key] = {
            "symbol": key, "direction": c["direction"],
            "entry": c["price"], "sl": c["sl"],
            "tp1": c["tp1"], "tp2": c["tp2"], "tp3": c["tp3"],
            "max_tp_hit": 0, "be_adjusted": False, "tp1_bar_time": 0,
            "entry_time": t, "entry_bar": entrybar, "last_bar": entrybar
        }
        save(s)
        sent += 1
        print("SIGNAL:", key, c["direction"], c["score"])

    if position_changed or sent:
        tg(status_msg(s["positions"]))
        tg(get_stats_report(s["stats"]))

    save(s)
    print("DONE. SENT:", sent)


if __name__ == "__main__":
    main()
