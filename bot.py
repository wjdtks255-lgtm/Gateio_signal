from datetime import datetime, timedelta, timezone
import json
import os
import random
import time
import requests

TOKEN = os.getenv("GATE_TELEGRAM_TOKEN", "").strip()
CHAT_ID = (os.getenv("GATE_TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID") or "").strip()
STATE_FILE = "gate_state.json"
KST = timezone(timedelta(hours=9))

GATE_FUTURES_TICKERS = "https://fx-api.gateio.ws/api/v4/futures/usdt/tickers"
GATE_FUTURES_CANDLES = "https://fx-api.gateio.ws/api/v4/futures/usdt/candlesticks"
GATE_FUTURES_STATS = "https://fx-api.gateio.ws/api/v4/futures/usdt/contract_stats"

MAX_SLOTS = 8
COOLDOWN = 120
SL_PENALTY_HOURS = 12
MAX_HOLDING_HOURS = 24


def now():
    return datetime.now(KST)


def load():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        s = {}
    s.setdefault("positions", {})
    s.setdefault("signals", {})
    s.setdefault("sl_penalties", {})
    
    stats = s.setdefault("stats", {})
    stats.setdefault("wins", 0)
    stats.setdefault("breakeven", 0)
    stats.setdefault("losses", 0)
    return s


def save(s):
    with open(STATE_FILE + ".tmp", "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
    os.replace(STATE_FILE + ".tmp", STATE_FILE)


def tg(msg):
    if not TOKEN or not CHAT_ID:
        print("TG CONFIG ERROR")
        return False
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = {
        "chat_id": CHAT_ID,
        "text": msg,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    for n in range(3):
        try:
            r = requests.post(url, json=data, timeout=20)
            if r.status_code == 200:
                return True
        except Exception as e:
            print("TG ERROR:", e)
        time.sleep(1.5)
    return False


def calculate_ema(prices, period):
    if not prices or len(prices) < period:
        return None
    multiplier = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for price in prices[period:]:
        ema = (price - ema) * multiplier + ema
    return ema


def calculate_rsi(prices, period=14):
    if not prices or len(prices) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(prices)):
        diff = prices[i] - prices[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def get_candles(contract, limit=60):
    try:
        url = f"{GATE_FUTURES_CANDLES}?contract={contract}&interval=15m&limit={limit}"
        r = requests.get(url, timeout=5)
        if r.status_code == 200:
            data = r.json()
            if data and isinstance(data, list):
                highs = [float(item.get("high", item.get("h", 0))) for item in data]
                lows = [float(item.get("low", item.get("l", 0))) for item in data]
                closes = [float(item.get("close", item.get("c", 0))) for item in data]
                volumes = [float(item.get("sum", item.get("v", 0))) for item in data]
                timestamps = [int(item.get("t", 0)) for item in data]
                return highs, lows, closes, volumes, timestamps
    except Exception:
        pass
    return None, None, None, None, None


def get_open_interest_growth(contract):
    try:
        url = f"{GATE_FUTURES_STATS}?contract={contract}&interval=15m&limit=3"
        r = requests.get(url, timeout=5)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list) and len(data) >= 2:
                oi_prev = float(data[-2].get("open_interest", 0))
                oi_curr = float(data[-1].get("open_interest", 0))
                if oi_prev > 0:
                    return ((oi_curr - oi_prev) / oi_prev) * 100
    except Exception:
        pass
    return 0.0


def analyze_market_candidates():
    valid_candidates = []
    try:
        r = requests.get(GATE_FUTURES_TICKERS, timeout=15)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, dict):
                data = data.get("data", [])
            random.shuffle(data)
            
            for item in data[:60]:
                contract = item.get("contract") or item.get("name")
                if not contract or item.get("in_delisting", False):
                    continue
                if "MATIC" in contract:
                    continue
                
                try:
                    last_price = float(item.get("last", 0))
                    funding_rate = float(item.get("funding_rate", 0))
                    if not (0.000001 < last_price <= 100.0):
                        continue

                    highs, lows, closes, volumes, timestamps = get_candles(contract, limit=60)
                    if not closes or len(closes) < 50:
                        continue

                    ema20 = calculate_ema(closes, 20)
                    ema50 = calculate_ema(closes, 50)
                    rsi = calculate_rsi(closes, 14)
                    
                    if not ema20 or not ema50:
                        continue

                    avg_vol = sum(volumes[-11:-1]) / 10 if len(volumes) >= 11 else volumes[-1]
                    curr_vol = volumes[-1]
                    oi_growth = get_open_interest_growth(contract)

                    direction = None
                    strategy_tag = ""
                    strategy_desc = ""

                    # [전략 A] 추세 & 모멘텀형
                    if not direction:
                        if ema20 > ema50 and 50 <= rsi <= 65 and curr_vol > avg_vol * 1.5:
                            direction = "LONG"
                            strategy_tag = "추세돌파형 (LONG)"
                            strategy_desc = "EMA 정배열 및 RSI 상승 동력 속에서 거래량이 평균 대비 1.5배 이상 폭발하며 정직한 추세 지속 예상"
                        elif ema20 < ema50 and 35 <= rsi <= 50 and curr_vol > avg_vol * 1.5:
                            direction = "SHORT"
                            strategy_tag = "추세하락형 (SHORT)"
                            strategy_desc = "EMA 역배열 및 하방 압력 속에서 거래량 동반 매도세가 유입되어 하락 추세 가속화 예상"

                    # [전략 B] 미결제약정(OI) 수급 급증형
                    if not direction and oi_growth >= 2.0:
                        if ema20 > ema50 and rsi >= 45:
                            direction = "LONG"
                            strategy_tag = "OI수급유입형 (LONG)"
                            strategy_desc = f"미결제약정이 최근 15분간 +{oi_growth:.2f}% 급증하며 고래들의 적극적인 매수 자금 유입 포착"
                        elif ema20 < ema50 and rsi <= 55:
                            direction = "SHORT"
                            strategy_tag = "OI수급유입형 (SHORT)"
                            strategy_desc = f"미결제약정이 최근 15분간 +{oi_growth:.2f}% 급증하며 하방 포지션으로 거대 수급 쏠림 포착"

                    # [전략 C] 펀딩비 쏠림 스퀴즈 역매매형
                    if not direction:
                        if funding_rate >= 0.0005:
                            direction = "SHORT"
                            strategy_tag = "펀딩비과열역매매 (SHORT)"
                            strategy_desc = f"롱 포지션 과열로 펀딩비가 극단적 양수({funding_rate:.4f})로 치달아, 하방 스퀴즈(연쇄 청산) 유도 확률 극대화"
                        elif funding_rate <= -0.0005:
                            direction = "LONG"
                            strategy_tag = "펀딩비과열역매매 (LONG)"
                            strategy_desc = f"숏 포지션 과열로 펀딩비가 극단적 음수({funding_rate:.4f})로 치달아, 상방 스퀴즈(연쇄 청산) 유도 확률 극대화"

                    if direction:
                        risk = max(last_price * 0.015, last_price * 0.005)
                        lev = 5 if abs(last_price) > 0 else 7

                        if direction == "LONG":
                            sl = last_price - risk
                            tp1 = last_price + risk * 1.2
                            tp2 = last_price + risk * 2.2
                            tp3 = last_price + risk * 3.8
                            pos_text = "롱 (LONG) 🟢"
                        else:
                            sl = last_price + risk
                            tp1 = last_price - risk * 1.2
                            tp2 = last_price - risk * 2.2
                            tp3 = last_price - risk * 3.8
                            pos_text = "숏 (SHORT) 🔴"

                        valid_candidates.append({
                            "symbol": contract.replace("_USDT", "").replace("USDT", ""),
                            "contract": contract,
                            "direction": direction,
                            "price": last_price,
                            "sl": sl,
                            "tp1": tp1,
                            "tp2": tp2,
                            "tp3": tp3,
                            "lev": lev,
                            "pos_text": pos_text,
                            "strategy": strategy_tag,
                            "strategy_desc": strategy_desc,
                        })
                except Exception:
                    pass
    except Exception as e:
        print(f"Error fetching Gate.io tickers: {e}")

    return valid_candidates


def fmt(x):
    if x >= 1000:
        return f"{x:,.2f}"
    if x >= 1:
        return f"{x:.4f}"
    if x >= 0.01:
        return f"{x:.5f}"
    return f"{x:.8f}".rstrip("0").rstrip(".")


def msg(s, slot_no):
    return (
        f"🚨 *Gate.io 멀티 전략 수급 시그널* 🚨\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 슬롯 번호: `[ {slot_no} / {MAX_SLOTS} ]`\n"
        f"🪙 종목명: `{s['symbol']} / USDT`\n"
        f"📊 포지션: **{s['pos_text']}**\n"
        f"🔍 감지 전략: `{s['strategy']}`\n"
        f"💰 실시간 진입가: `{fmt(s['price'])}` USDT\n\n"
        f"🎯 *[이 종목의 핵심 매매 기준 및 솔루션]*\n"
        f"  • 선정 이유: `{s['strategy_desc']}`\n\n"
        f"⚙️ 포지션 운용 가이드\n"
        f"  • 권장 레버리지: `{s['lev']}x` (격리 마진)\n"
        f"  • 리스크 관리: `TP1 도달 시 즉시 본전스탑(SL=진입가)`\n\n"
        f"🎯 목표가 (Take Profit)\n"
        f"  • 1차 (TP1): `{fmt(s['tp1'])}`\n"
        f"  • 2차 (TP2): `{fmt(s['tp2'])}`\n"
        f"  • 3차 (TP3): `{fmt(s['tp3'])}` (최종)\n\n"
        f"🛡️ 손절가 (Stop Loss): `{fmt(s['sl'])}`\n"
        f"⏱️ 발생 시간: `{now().strftime('%Y-%m-%d %H:%M:%S')} (KST)`"
    )


def status_msg(positions):
    if not positions:
        return "📊 *[현재 보유 중인 포지션 현황]*\n━━━━━━━━━━━━━━━━━━━━━━\n• 현재 활성화된 포지션이 없습니다. (모든 슬롯 대기 중)"
    
    lines = [
        "📊 *[현재 보유 중인 포지션 현황]*",
        "━━━━━━━━━━━━━━━━━━━━━━"
    ]
    for idx, (sym, p) in enumerate(positions.items(), 1):
        d = p.get("direction", "LONG")
        pos_icon = "🟢 롱" if d == "LONG" else "🔴 숏"
        entry = p.get("entry", 0)
        tp1_status = "✅ 도달(본전스탑)" if p.get("tp1_hit") else "⏳ 대기중"
        lines.append(f"• `[{idx}]` **{sym} / USDT** ({pos_icon})\n  - 진입가: `{fmt(entry)}` | TP1: `{tp1_status}`")
    
    lines.append(f"\n⏱️ 확인 시간: `{now().strftime('%m-%d %H:%M')} (KST)`")
    return "\n".join(lines)


def get_stats_text(stats):
    wins = stats.get("wins", 0)
    breakeven = stats.get("breakeven", 0)
    losses = stats.get("losses", 0)
    total = wins + breakeven + losses
    win_rate = (wins / total * 100) if total > 0 else 0.0
    return (
        f"📈 *[누적 트레이딩 성적 통계]*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• 총 종료 포지션: `{total}건`\n"
        f"• 성공 (TP3 익절): `{wins}건` 🟢\n"
        f"• 본전 방어 (BE스탑): `{breakeven}건` 🟡\n"
        f"• 실패 (손절 마감): `{losses}건` 🔴\n"
        f"• **익절 승률: `{win_rate:.1f}%`**"
    )


def posmsg(p, k):
    sy = p.get("symbol", "UNKNOWN")
    d = p.get("direction", "LONG")
    if k == "TP1":
        return f"🎯 *[TP1 1차 익절 & 본전스탑 완료]*\n🪙 종목: `{sy} / USDT` ({d})\n💰 1차 목표가 도달 성공!\n🛡️ **안전장치 발동:** 손절가가 진입가로 상향 조정되었습니다."
    if k == "TP2":
        return f"🎯 *[TP2 2차 익절 도달]*\n🪙 종목: `{sy} / USDT` ({d})\n💰 2차 목표가 달성 완료!"
    if k == "TP3":
        return f"🎉 *[TP3 최종 익절 달성 마감!]*\n🪙 종목: `{sy} / USDT` ({d})\n🎯 최종 목표가 도달로 포지션 완벽 마감!"
    if k == "TIMEOUT":
        return f"⏱️ *[보유 시간 초과 슬롯 반환]*\n🪙 종목: `{sy} / USDT` ({d})\n⌛ 24시간 동안 목표가 미도달로 슬롯을 회수합니다."
    if k == "BE_STOP":
        return f"🛡️ *[본전스탑(BE) 방어 마감]*\n🪙 종목: `{sy} / USDT` ({d})\n🔒 TP1 달성 후 본전가(진입가) 이탈로 원금을 온전히 지키며 종료합니다."
    return f"🛡️ *[손절(SL) 도달 마감]*\n🪙 종목: `{sy} / USDT` ({d})\n📉 초기 손절가 이탈로 포지션 종료\n🔒 **규칙 적용:** 12시간 동안 해당 종목 재진입이 금지됩니다."


def check(state):
    t_now = time.time()
    stats = state.setdefault("stats", {"wins": 0, "breakeven": 0, "losses": 0})
    changed = False

    for key, p in list(state["positions"].items()):
        if not isinstance(p, dict):
            state["positions"].pop(key, None)
            changed = True
            continue
        p.setdefault("symbol", key)
        contract = f"{key}_USDT"
        entry_time = p.get("entry_time", 0)
        
        if entry_time > 0 and (t_now - entry_time) > (MAX_HOLDING_HOURS * 3600):
            tg(posmsg(p, "TIMEOUT"))
            state["positions"].pop(key, None)
            changed = True
            continue

        highs, lows, closes, volumes, timestamps = get_candles(contract, limit=15)
        if not closes or not timestamps:
            continue
            
        valid_highs, valid_lows = [], []
        for h, l, t in zip(highs, lows, timestamps):
            if t >= entry_time:
                valid_highs.append(h)
                valid_lows.append(l)
                
        high_p = max(valid_highs) if valid_highs else closes[-1]
        low_p = min(valid_lows) if valid_lows else closes[-1]
        curr_p = closes[-1]
        
        high_p = max(high_p, curr_p)
        low_p = min(low_p, curr_p)

        long = p["direction"] == "LONG"

        if long:
            if high_p >= p["tp3"]:
                stats["wins"] += 1
                tg(posmsg(p, "TP3"))
                tg(get_stats_text(stats))
                state["positions"].pop(key, None)
                changed = True
                continue
            if low_p <= p["sl"]:
                if p.get("tp1_hit", False):
                    stats["breakeven"] += 1
                    tg(posmsg(p, "BE_STOP"))
                else:
                    stats["losses"] += 1
                    tg(posmsg(p, "SL"))
                tg(get_stats_text(stats))
                state.setdefault("sl_penalties", {})[key] = time.time()
                state["positions"].pop(key, None)
                changed = True
                continue
            if high_p >= p["tp2"] and "TP2" not in p.get("hit", []):
                tg(posmsg(p, "TP2"))
                p.setdefault("hit", []).append("TP2")
                changed = True
            if not p.get("tp1_hit", False) and high_p >= p["tp1"]:
                p["tp1_hit"] = True
                p["sl"] = p["entry"]
                tg(posmsg(p, "TP1"))
                changed = True
        else:
            if low_p <= p["tp3"]:
                stats["wins"] += 1
                tg(posmsg(p, "TP3"))
                tg(get_stats_text(stats))
                state["positions"].pop(key, None)
                changed = True
                continue
            if high_p >= p["sl"]:
                if p.get("tp1_hit", False):
                    stats["breakeven"] += 1
                    tg(posmsg(p, "BE_STOP"))
                else:
                    stats["losses"] += 1
                    tg(posmsg(p, "SL"))
                tg(get_stats_text(stats))
                state.setdefault("sl_penalties", {})[key] = time.time()
                state["positions"].pop(key, None)
                changed = True
                continue
            if low_p <= p["tp2"] and "TP2" not in p.get("hit", []):
                tg(posmsg(p, "TP2"))
                p.setdefault("hit", []).append("TP2")
                changed = True
            if not p.get("tp1_hit", False) and low_p <= p["tp1"]:
                p["tp1_hit"] = True
                p["sl"] = p["entry"]
                tg(posmsg(p, "TP1"))
                changed = True
                
    return changed


def main():
    print("==================================================")
    print(" GATE.IO DETAILED STRATEGY REPORT SIGNAL BOT")
    print("==================================================")
    
    state = load()
    
    old_positions_count = len(state["positions"])

    position_changed = check(state)
    save(state)

    candidates = analyze_market_candidates()
    print("Filtered Report Candidates:", len(candidates))

    random.shuffle(candidates)

    current_active_count = len(state["positions"])
    available_slots = MAX_SLOTS - current_active_count
    print(f"ACTIVE SLOTS: {current_active_count}/{MAX_SLOTS} (Available: {available_slots})")

    sent = 0
    t = time.time()
    sl_penalties = state.get("sl_penalties", {})

    if available_slots > 0 and candidates:
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
            if not tg(msg(s, current_slot_number)):
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
                "entry_time": int(time.time()),
            }
            save(state)
            sent += 1
            print("DETAILED REPORT SIGNAL SENT (Slot):", key)

    new_positions_count = len(state["positions"])
    if position_changed or (sent > 0) or (new_positions_count != old_positions_count):
        tg(status_msg(state["positions"]))

    save(state)
    print("DONE. Sent:", sent)


if __name__ == "__main__":
    main()
