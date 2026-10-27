import os
import json
import requests
from datetime import datetime, timezone, timedelta

TELEGRAM_TOKEN = os.getenv("GATE_TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("GATE_TELEGRAM_CHAT_ID")
STATE_FILE = "gate_state.json"
MAX_SLOTS = 8
MAX_HOLDING_HOURS = 18  # 18시간 이상 미달성 시 수익권이면 슬롯 회전(청산)

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
    slot_changed = False
    alerts_to_send = []

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
        entry_time = datetime.fromisoformat(info.get("entry_time", now_utc.isoformat()))
        
        chart_link = f"https://www.gate.io/trade/{symbol}"
        category = get_asset_category(symbol)
        display_name = f"[{category}] {symbol}"

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

        # 💡 [추가] 장기 체류(18시간 이상)하면서 수익권(+0.5% 이상)인 경우 슬롯 회전을 위해 강제 익절 청산
        holding_hours = (now_utc - entry_time).total_seconds() / 3600
        if holding_hours >= MAX_HOLDING_HOURS and current_profit_pct >= 0.5:
            msg = (
                "<b>⏳ [시간 제한 회전 청산 - 익절 마감]</b>\n"
                f"• 방향: <code>{'🟢 롱 포지션' if direction=='LONG' else '🔴 숏 포지션'}</code>\n"
                f"• 종목: <a href='{chart_link}'>{display_name}</a>\n"
                f"• 보유 시간: <code>{int(holding_hours)}시간 초과</code>\n"
                f"• 청산 가격: <code>{current_price}</code> (<code>{current_profit_pct:+.2f}%</code>)\n"
                "• 결과: <code>승리 (장기 체류 수익 확보 회전)</code>"
            )
            alerts_to_send.append(msg)
            stats["wins"] += 1
            stats["total"] += 1
            closed_symbols[symbol] = now_utc.isoformat()
            del active_slots[symbol]
            slot_changed = True
            continue

        if is_stopped:
            if tp2_reached:
                msg = (
                    "<b>🎯 [트레일링 스탑 도달 - TP1 수익 확보 청산]</b>\n"
                    f"• 방향: <code>{'🟢 롱 포지션' if direction=='LONG' else '🔴 숏 포지션'}</code>\n"
                    f"• 종목: <a href='{chart_link}'>{display_name}</a>\n"
                    f"• 진입가: <code>{entry_price}</code>\n"
                    f"• 청산 가격: <code>{current_price}</code> (<code>{current_profit_pct:+.2f}%</code>, TP1 가격 방어)\n"
                    "• 결과: <code>승리 (TP1 수익 확보 익절 마감)</code>"
                )
                alerts_to_send.append(msg)
                stats["wins"] += 1
            elif tp1_reached:
                msg = (
                    "<b>🏳️ [본전 청산 - 무승부]</b>\n"
                    f"• 방향: <code>{'🟢 롱 포지션' if direction=='LONG' else '🔴 숏 포지션'}</code>\n"
                    f"• 종목: <a href='{chart_link}'>{display_name}</a>\n"
                    f"• 진입가: <code>{entry_price}</code>\n"
                    f"• 청산 가격: <code>{current_price}</code> (<code>{current_profit_pct:+.2f}%</code>, 본전 스탑)\n"
                    "• 결과: <code>무 (본전 마감)</code>"
                )
                alerts_to_send.append(msg)
                stats["draws"] += 1
            else:
                msg = (
                    "<b>🛑 [손절가(SL) 도달]</b>\n"
                    f"• 방향: <code>{'🟢 롱 포지션' if direction=='LONG' else '🔴 숏 포지션'}</code>\n"
                    f"• 종목: <a href='{chart_link}'>{display_name}</a>\n"
                    f"• 진입가: <code>{entry_price}</code>\n"
                    f"• 도달 가격: <code>{current_price}</code> (<code>{current_profit_pct:+.2f}%</code>, 손절 마감)\n"
                    "• 결과: <code>패배 (손실 마감)</code>"
                )
                alerts_to_send.append(msg)
                stats["losses"] += 1
            stats["total"] += 1
            closed_symbols[symbol] = now_utc.isoformat()
            del active_slots[symbol]
            slot_changed = True
            continue

        if is_tp3:
            msg = (
                "<b>🎯🎯🎯 [TP3 최종 목표가 도달 - 포지션 회수]</b>\n"
                f"• 방향: <code>{'🟢 롱 포지션' if direction=='LONG' else '🔴 숏 포지션'}</code>\n"
                f"• 종목: <a href='{chart_link}'>{display_name}</a>\n"
                f"• 진입가: <code>{entry_price}</code>\n"
                f"• 도달 가격: <code>{current_price}</code> (<code>{current_profit_pct:+.2f}%</code>)\n"
                "• 결과: <code>승리 (TP3 익절 마감)</code>"
            )
            alerts_to_send.append(msg)
            stats["wins"] += 1
            stats["total"] += 1
            closed_symbols[symbol] = now_utc.isoformat()
            del active_slots[symbol]
            slot_changed = True
            continue

        if is_tp2 and not tp2_reached:
            info["tp2_reached"] = True
            info["stop_loss"] = tp1
            slot_changed = True
            msg = (
                "<b>🎯🎯 [TP2 2차 목표가 도달]</b>\n"
                f"• 방향: <code>{'🟢 롱 포지션' if direction=='LONG' else '🔴 숏 포지션'}</code>\n"
                f"• 종목: <a href='{chart_link}'>{display_name}</a>\n"
                f"• 진입가: <code>{entry_price}</code>\n"
                f"• 도달 가격: <code>{current_price}</code> (<code>{current_profit_pct:+.2f}%</code>)\n"
                "• 조치: <code>스탑로스를 TP1 가격으로 상향 (TP3 최종 목표가를 향해 홀딩 유지)</code>"
            )
            alerts_to_send.append(msg)

        if is_tp1 and not tp1_reached:
            info["tp1_reached"] = True
            info["stop_loss"] = entry_price
            slot_changed = True
            msg = (
                "<b>🎯 [TP1 1차 목표가 도달]</b>\n"
                f"• 방향: <code>{'🟢 롱 포지션' if direction=='LONG' else '🔴 숏 포지션'}</code>\n"
                f"• 종목: <a href='{chart_link}'>{display_name}</a>\n"
                f"• 진입가: <code>{entry_price}</code>\n"
                f"• 도달 가격: <code>{current_price}</code> (<code>{current_profit_pct:+.2f}%</code>)\n"
                "• 조치: <code>본전 스탑(Break-even) 전환 완료 (손실 원천 차단 및 홀딩 유지)</code>"
            )
            alerts_to_send.append(msg)

    for alert in alerts_to_send:
        send_telegram_message(alert)

    return slot_changed

def identify_new_signals(tickers, active_slots, closed_symbols, now_utc):
    slot_changed = False
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

            if direction == "LONG":
                tp1_pct = ((tp1 - last_price) / last_price) * 100
                tp2_pct = ((tp2 - last_price) / last_price) * 100
                tp3_pct = ((tp3 - last_price) / last_price) * 100
                sl_pct = ((sl - last_price) / last_price) * 100
            else:
                tp1_pct = ((last_price - tp1) / last_price) * 100
                tp2_pct = ((last_price - tp2) / last_price) * 100
                tp3_pct = ((last_price - tp3) / last_price) * 100
                sl_pct = ((last_price - sl) / last_price) * 100

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
            slot_changed = True

            chart_link = f"https://www.gate.io/trade/{symbol}"
            display_name = f"[{category}] {symbol}"
            dir_tag = "🟢 롱 포지션" if direction == "LONG" else "🔴 숏 포지션"

            msg = (
                "<b>🚀 [Master 6.0 신규 시그널 포착 (ATR 필터 적용)]</b>\n"
                f"• 방향: <code>{dir_tag}</code>\n"
                f"• 종목: <a href='{chart_link}'>{display_name}</a>\n"
                f"• 진입가: <code>{last_price}</code>\n"
                f"• 추천 레버리지: <code>{leverage}배</code> (변동성 분석)\n"
                "• 탐색 이유: <code>1시간봉 완성 캔들 모멘텀 및 RSI 필터 통과</code>\n\n"
                "<b>🎯 ATR 가변형 목표가 및 손절가</b>\n"
                f"• TP1: <code>{tp1:.4f} ({tp1_pct:+.2f}%)</code>\n"
                f"• TP2: <code>{tp2:.4f} ({tp2_pct:+.2f}%)</code>\n"
                f"• TP3: <code>{tp3:.4f} ({tp3_pct:+.2f}%)</code>\n"
                f"• 손절가 SL: <code>{sl:.4f} ({sl_pct:+.2f}%)</code>"
            )
            send_telegram_message(msg)
            if len(active_slots) >= MAX_SLOTS:
                break
        except Exception:
            continue
    return slot_changed

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

    # 1. 포지션 관리 및 청산 체크 (시간 제한 회전 청산 포함)
    changed_positions = manage_positions(active_slots, tickers_dict, stats, closed_symbols)

    # 2. 신규 시그널 탐색
    changed_new = False
    if len(active_slots) < MAX_SLOTS:
        changed_new = identify_new_signals(raw_tickers, active_slots, closed_symbols, now_utc)

    # 3. 슬롯 현황판은 포지션 추가/청산 등의 변화가 있을 때만 전송
    if changed_positions or changed_new:
        slot_status_msg = build_slot_status_message(active_slots, tickers_dict, stats)
        send_telegram_message(slot_status_msg)
        print("Slot status updated and sent to Telegram.")
    else:
        print("No slot changes. Status message skipped.")

    save_state(state)
    print("Bot 사이클 종료")

if __name__ == "__main__":
    run_bot()
