import os
import json
import requests
from datetime import datetime, timezone, timedelta

TELEGRAM_TOKEN = os.getenv("GATE_TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("GATE_TELEGRAM_CHAT_ID")
STATE_FILE = "gate_state.json"
MAX_SLOTS = 8

def send_telegram_message(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials missing.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code != 200:
            print(f"Failed to send telegram message: {response.text}")
    except Exception as e:
        print(f"Error sending telegram message: {e}")

def load_state():
    state = {
        "active_slots": {},
        "stats": {"wins": 0, "draws": 0, "losses": 0, "total": 0},
        "closed_symbols": {}
    }
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    state["active_slots"] = data.get("active_slots", {})
                    state["stats"] = data.get("stats", {"wins": 0, "draws": 0, "losses": 0, "total": 0})
                    closed = data.get("closed_symbols", {})
                    if isinstance(closed, list):
                        state["closed_symbols"] = {sym: datetime.now(timezone.utc).isoformat() for sym in closed}
                    elif isinstance(closed, dict):
                        state["closed_symbols"] = closed
        except Exception as e:
            print(f"Error loading state file: {e}")
    return state

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def fetch_gateio_tickers():
    url = "https://api.gateio.ws/api/v4/spot/tickers"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            return response.json()
    except Exception as e:
        print(f"Error fetching Gate.io tickers: {e}")
    return []

def fetch_candles(symbol, interval="1h", limit=20):
    url = "https://api.gateio.ws/api/v4/spot/candlesticks"
    params = {"currency_pair": symbol, "interval": interval, "limit": limit}
    try:
        response = requests.get(url, params=params, timeout=10)
        if response.status_code == 200:
            return response.json()
    except Exception as e:
        print(f"Error fetching candles for {symbol}: {e}")
    return []

def calculate_rsi(symbol, interval="1h", period=14):
    candles = fetch_candles(symbol, interval, period + 1)
    if not candles or len(candles) < period + 1:
        return None
    closes = [float(c[2]) for c in candles]
    gains = 0.0
    losses = 0.0
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        if diff > 0:
            gains += diff
        else:
            losses -= diff
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def fetch_atr_and_validate_breakout(symbol, direction):
    candles = fetch_candles(symbol, interval="1h", limit=20)
    if not candles or len(candles) < 15:
        return None, False

    parsed = []
    for c in candles:
        try:
            parsed.append({
                "close": float(c[2]),
                "high": float(c[3]),
                "low": float(c[4]),
                "open": float(c[5])
            })
        except Exception:
            continue

    if len(parsed) < 15:
        return None, False

    latest = parsed[-2]
    body = abs(latest["close"] - latest["open"])
    total_range = latest["high"] - latest["low"]
    if total_range == 0:
        return None, False

    is_strong_body = (body / total_range) >= 0.35
    if direction == "LONG":
        is_valid_breakout = (latest["close"] > latest["open"]) and is_strong_body
    else:
        is_valid_breakout = (latest["close"] < latest["open"]) and is_strong_body

    if not is_valid_breakout:
        return None, False

    tr_list = []
    for i in range(1, len(parsed)):
        high = parsed[i]["high"]
        low = parsed[i]["low"]
        prev_close = parsed[i-1]["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        tr_list.append(tr)

    atr = sum(tr_list[-14:]) / 14.0
    return atr, True

def get_asset_category(symbol):
    upper_sym = symbol.upper()
    stock_keywords = ['SKHYNIX', 'TSLA', 'AAPL', 'NVDA', 'MSFT', 'AMZN', 'META', 'NFLX', 'GOOGL', 'BABA', 'AMD', 'INTC', 'PLTR']
    if any(k in upper_sym for k in stock_keywords):
        return '주식'
    index_keywords = ['SPX', 'NAS', 'DJI', 'US30', 'US100', 'NIKKEI', 'VIX', 'DAX']
    if any(k in upper_sym for k in index_keywords):
        return '지수'
    commodity_keywords = ['XAU', 'XAG', 'OIL', 'GOLD', 'SILVER', 'CRUDE', 'BRENT', 'COPPER', 'GAS']
    if any(k in upper_sym for k in commodity_keywords):
        return '원자재'
    forex_keywords = ['EUR', 'GBP', 'JPY', 'AUD', 'CHF', 'NZD', 'CAD']
    if any(k in upper_sym for k in forex_keywords):
        if not upper_sym.endswith('_USDT') or any(fx in upper_sym[:4] for fx in forex_keywords):
            return '외환'
    return '코인'

def calculate_recommended_leverage(category, change_24h):
    abs_change = abs(change_24h)
    if category == '코인':
        return 5 if abs_change > 10.0 else (7 if abs_change > 6.0 else 10)
    elif category == '주식':
        return 8
    elif category == '원자재':
        return 12
    elif category == '외환':
        return 20
    elif category == '지수':
        return 15
    return 10

def build_slot_status_message(active_slots, tickers_dict, stats):
    active_symbols = list(active_slots.keys())
    active_count = len(active_symbols)
    lines = [f"<b>📊 [현재 주시 중인 슬롯 현황 ({active_count} / {MAX_SLOTS} 슬롯)]</b>\n"]
    for i in range(1, MAX_SLOTS + 1):
        if i <= active_count:
            sym = active_symbols[i-1]
            info = active_slots[sym]
            entry_price = info.get("entry_price", 0)
            direction = info.get("direction", "LONG")
            current_price = tickers_dict.get(sym, entry_price)
            profit_pct = 0.0
            if entry_price > 0:
                if direction == "LONG":
                    profit_pct = ((current_price - entry_price) / entry_price) * 100
                else:
                    profit_pct = ((entry_price - current_price) / entry_price) * 100
            holding_str = format_holding_time(info.get("entry_time"))
            chart_link = f"https://www.gate.io/trade/{sym}"
            category = get_asset_category(sym)
            dir_icon = "🟢" if direction == "LONG" else "🔴"
            display_name = f"[{category}] {sym}"
            if info.get("tp2_reached", False):
                status_badge = "🎯 TP2 도달 (TP1 방어중)"
            elif info.get("tp1_reached", False):
                status_badge = "🎯 TP1 도달 (본전 방어중)"
            else:
                status_badge = "📈 홀딩 진행중"
            lines.append(f"{i}️⃣ {dir_icon} <a href='{chart_link}'>{display_name}</a> | <code>{profit_pct:+.2f}%</code> | 보유: <code>{holding_str}</code>\n    └ 상태: <code>{status_badge}</code>")
        else:
            lines.append(f"{i}️⃣ (빈 슬롯)")
    win_rate = calculate_win_rate(stats)
    lines.append(f"\n📈 <b>[누적 통계]</b> <code>{stats['wins']}승 {stats['draws']}무 {stats['losses']}패 (총 {stats['total']}전) | 승률: {win_rate}%</code>")
    return "\n".join(lines)

def format_holding_time(entry_iso):
    try:
        entry_time = datetime.fromisoformat(entry_iso)
        now = datetime.now(timezone.utc)
        diff = now - entry_time
        hours = int(diff.total_seconds() // 3600)
        minutes = int((diff.total_seconds() % 3600) // 60)
        return f"{hours}시간 {minutes:02d}분"
    except Exception:
        return "0시간 00분"

def calculate_win_rate(stats):
    wins = stats.get("wins", 0)
    losses = stats.get("losses", 0)
    decisive_total = wins + losses
    if decisive_total == 0:
        return 0.0
    return round((wins / decisive_total) * 100, 2)

def manage_positions(active_slots, tickers_dict, stats, closed_symbols):
    now_utc = datetime.now(timezone.utc)
    for symbol, info in list(active_slots.items()):
        current_price = tickers_dict.get(symbol, info["entry_price"])
        direction = info.get("direction", "LONG")
        entry_price = info["entry_price"]
        tp1 = info["tp1"]
        tp2 = info["tp2"]
        tp3 = info["tp3"]
        stop_loss = info["stop_loss"]
        tp1_reached = info.get("tp1_reached", False)
        tp2_reached = info.get("tp2_reached", False)

        if entry_price <= 0:
            continue

        if direction == "LONG":
            current_profit_pct = ((current_price - entry_price) / entry_price) * 100
            is_stopped = current_price <= stop_loss
            is_tp1 = current_price >= tp1
            is_tp2 = current_price >= tp2
            is_tp3 = current_price >= tp3
        else:
            current_profit_pct = ((entry_price - current_price) / entry_price) * 100
            is_stopped = current_price >= stop_loss
            is_tp1 = current_price <= tp1
            is_tp2 = current_price <= tp2
            is_tp3 = current_price <= tp3

        alerts_to_send = []

        if is_tp1 and not tp1_reached:
            info["tp1_reached"] = True
            # TP1 도달 후 손절가를 TP1보다 살짝 여유 있게 조정 (15% ATR 범위)
            atr_gap = abs(tp1 - entry_price) * 0.15
            if direction == "LONG":
                info["stop_loss"] = tp1 - atr_gap
            else:
                info["stop_loss"] = tp1 + atr_gap
            alerts_to_send.append(f"<b>🎯 [TP1 1차 목표가 도달]</b> • 종목: {symbol} • 손절가 조정 완료")

        if is_tp2 and not tp2_reached:
            info["tp2_reached"] = True
            info["stop_loss"] = tp1
            alerts_to_send.append(f"<b>🎯🎯 [TP2 2차 목표가 도달]</b> • 종목: {symbol} • 스탑로스 TP1 가격으로 상향 조정")

        if is_tp3:
            alerts_to_send.append(f"<b>🎯🎯🎯 [TP3 최종 목표가 도달]</b> • 종목: {symbol}")
            stats["wins"] += 1
            stats["total"] += 1
            closed_symbols[symbol] = now_utc.isoformat()
            del active_slots[symbol]

        elif is_stopped:
            if tp2_reached:
                alerts_to_send.append(f"<b>🎯 [트레일링 스탑 도달 - TP1 수익 확보 청산]</b> • 종목: {symbol}")
                stats["wins"] += 1
            elif tp1_reached:
                alerts_to_send.append(f"<b>🏳️ [본전 청산 - 무승부]</b> • 종목: {symbol}")
                stats["draws"] += 1
            else:
                alerts_to_send.append(f"<b>🛑 [손절가 도달]</b> • 종목: {symbol}")
                stats["losses"] += 1
            stats["total"] += 1
            closed_symbols[symbol] = now_utc.isoformat()
            del active_slots[symbol]

        for alert in alerts_to_send:
            send_telegram_message(alert)

def identify_new_signals(tickers, active_slots, closed_symbols, now_utc):
    for t in tickers:
        symbol = t.get("currency_pair", "")
        if symbol in active_slots or symbol in closed_symbols:
            continue
        try:
            last_price = float(t.get("last", 0))
            volume_24h = float(t.get("quote_volume", 0))
            change_24h = float(t.get("change_percentage", 0))
            is_long_candidate = (change_24h >= 4.5 and volume_24h > 80000)
            is_short_candidate = (change_24h <= -4.5 and volume_24h > 80000)
            if not (is_long_candidate or is_short_candidate):
                continue
            rsi = calculate_rsi(symbol)
            if rsi is None:
                continue
            if is_long_candidate and rsi < 50:
                continue
            if is_short_candidate and rsi > 50:
                continue
            direction = "LONG" if is_long_candidate else "SHORT"
            atr, valid = fetch_atr_and_validate_breakout(symbol, direction)
            if not valid or not atr:
                continue
            category = get_asset_category(symbol)
            leverage = calculate_recommended_leverage(category, change_24h)
            if direction == "LONG":
                tp1 = last_price + (atr * 1.5)
                tp2 = last_price + (atr * 3.0)
                tp3 = last_price + (atr * 4.5)
                sl = last_price - (atr * 1.5)
            else:
                tp1 = last_price - (atr * 1.5)
                tp2 = last_price - (atr * 3.0)
                tp3 = last_price - (atr * 4.5)
                sl = last_price + (atr * 1.5)
            active_slots[symbol] = {
                "direction": direction,
                "entry_price": last_price,
                "entry_time": now_utc.isoformat(),
                "leverage": leverage,
                "tp1": tp1,
                "tp2": tp2,
                "tp3": tp3,
                "stop_loss": sl,
                "tp1_reached": False,
                "tp2_reached": False,
            }
            msg = (
                f"<b>🚀 신규 시그널 포착</b>\n종목: {symbol}\n진입가: {last_price}\n방향: {direction}\n레버리지: {leverage}배\n"
                f"TP1: {tp1:.4f}, TP2: {tp2:.4f}, TP3: {tp3:.4f}, SL: {sl:.4f}"
            )
            send_telegram_message(msg)
            if len(active_slots) >= MAX_SLOTS:
                break
        except Exception:
            continue

def run_bot():
    print("Bot 시작...")
    raw_tickers = fetch_gateio_tickers()
    if not raw_tickers:
        print("티커 정보 불러오기 실패")
        return
    tickers_dict = {t["currency_pair"]: float(t.get("last", 0)) for t in raw_tickers if "currency_pair" in t}
    state = load_state()
    active_slots = state["active_slots"]
    stats = state["stats"]
    closed_symbols = state.setdefault("closed_symbols", {})
    now_utc = datetime.now(timezone.utc)
    expired_symbols = [sym for sym, t_str in closed_symbols.items()
                       if (now_utc - datetime.fromisoformat(t_str)) >= timedelta(hours=3)]
    for sym in expired_symbols:
        del closed_symbols[sym]

    manage_positions(active_slots, tickers_dict, stats, closed_symbols)

    if len(active_slots) < MAX_SLOTS:
        identify_new_signals(raw_tickers, active_slots, closed_symbols, now_utc)

    slot_status_msg = build_slot_status_message(active_slots, tickers_dict, stats)
    send_telegram_message(slot_status_msg)

    save_state(state)
    print("Bot 사이클 종료")

if __name__ == "__main__":
    run_bot()
