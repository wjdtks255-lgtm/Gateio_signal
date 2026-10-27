import os
import json
import requests
from datetime import datetime, timezone, timedelta

TELEGRAM_TOKEN = os.getenv("GATE_TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("GATE_TELEGRAM_CHAT_ID")
STATE_FILE = "gate_state.json"
MAX_SLOTS = 8  # 최대 동시 운영 슬롯 8개

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
    """상태 파일 로드 시 승무패 및 3시간 쿨다운(closed_symbols) 안전 보정"""
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
                    if "active_slots" in data and isinstance(data["active_slots"], dict):
                        state["active_slots"] = data["active_slots"]
                    if "stats" in data and isinstance(data["stats"], dict):
                        state["stats"]["wins"] = data["stats"].get("wins", 0)
                        state["stats"]["draws"] = data["stats"].get("draws", 0)
                        state["stats"]["losses"] = data["stats"].get("losses", 0)
                        state["stats"]["total"] = data["stats"].get("total", 0)
                    if "closed_symbols" in data:
                        if isinstance(data["closed_symbols"], dict):
                            state["closed_symbols"] = data["closed_symbols"]
                        elif isinstance(data["closed_symbols"], list):
                            state["closed_symbols"] = {sym: datetime.now(timezone.utc).isoformat() for sym in data["closed_symbols"]}
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

def get_asset_category(symbol):
    """종목명 패턴에 따라 주식, 지수, 원자재, 외환, 코인 군을 확실하게 분류"""
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
            
            lines.append(f"{i}️⃣ {dir_icon} <a href='{chart_link}'>{display_name}</a> | <code>{profit_pct:+.2f}%</code> | 보유: <code>{holding_str}</code>")
        else:
            lines.append(f"{i}️⃣ (빈 슬롯)")
            
    win_rate = calculate_win_rate(stats)
    lines.append(f"\n📈 <b>[누적 통계]</b> <code>{stats['wins']}승 {stats['draws']}무 {stats['losses']}패 (총 {stats['total']}전) | 승률: {win_rate}%</code>")
            
    return "\n".join(lines)

def fetch_atr_and_validate_breakout(symbol, direction):
    url = "https://api.gateio.ws/api/v4/spot/candlesticks"
    params = {"currency_pair": symbol, "interval": "1h", "limit": 20}
    try:
        response = requests.get(url, params=params, timeout=10)
        if response.status_code == 200:
            candles = response.json()
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
    except Exception as e:
        print(f"Error in ATR/Breakout validation for {symbol}: {e}")
    return None, False

def run_bot():
    print("Master 6.0 Bot Scan Initiated (ATR & Breakout Filter Enabled)...")
    raw_tickers = fetch_gateio_tickers()
    if not raw_tickers:
        print("Failed to fetch tickers.")
        return

    tickers_dict = {}
    for t in raw_tickers:
        sym = t.get("currency_pair", "")
        try:
            tickers_dict[sym] = float(t.get("last", 0))
        except Exception:
            pass

    state = load_state()
    active_slots = state["active_slots"]
    stats = state["stats"]
    closed_symbols = state.setdefault("closed_symbols", {})
    
    now_utc = datetime.now(timezone.utc)

    expired_symbols = []
    for sym, closed_time_str in closed_symbols.items():
        try:
            closed_time = datetime.fromisoformat(closed_time_str)
            if now_utc - closed_time >= timedelta(hours=3):
                expired_symbols.append(sym)
        except Exception:
            expired_symbols.append(sym)
            
    for sym in expired_symbols:
        del closed_symbols[sym]

    alerts_to_send = []
    slot_changed = False

    for symbol, info in list(active_slots.items()):
        current_price = tickers_dict.get(symbol, info["entry_price"])
        chart_link = f"https://www.gate.io/trade/{symbol}"
        direction = info.get("direction", "LONG")
        category = get_asset_category(symbol)
        display_name = f"[{category}] {symbol}"
        entry_price = info["entry_price"]
        tp1 = info["tp1"]
        tp2 = info["tp2"]
        tp3 = info["tp3"]
        stop_loss = info["stop_loss"]
        tp1_reached = info.get("tp1_reached", False)
        tp2_reached = info.get("tp2_reached", False)
        
        if entry_price > 0:
            if direction == "LONG":
                current_profit_pct = ((current_price - entry_price) / entry_price) * 100
            else:
                current_profit_pct = ((entry_price - current_price) / entry_price) * 100
        else:
            current_profit_pct = 0.0

        if direction == "LONG":
            is_stopped = current_price <= stop_loss
            is_tp3 = current_price >= tp3
            is_tp2 = current_price >= tp2
            is_tp1 = current_price >= tp1
        else:
            is_stopped = current_price >= stop_loss
            is_tp3 = current_price <= tp3
            is_tp2 = current_price <= tp2
            is_tp1 = current_price <= tp1

        # 1. 스탑로스(트레일링 스탑 포함) 도달 체크
        if is_stopped:
            if tp2_reached:
                # TP2까지 갔다가 밀려서 TP1 가격(스탑로스)에 도달한 경우 -> TP1 수익 확보 승리
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
                stats["total"] += 1
            elif tp1_reached:
                # TP1 갔다가 밀려서 본전에 도달한 경우 -> 무(본전)
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
                stats["total"] += 1
            else:
                # 초기 손절가 도달 -> 패배
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

        # 2. TP3 최종 목표가 도달 (포지션 최종 회수 및 승리)
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

        # 3. TP2 도달 (종료하지 않고 스탑로스를 TP1 가격으로 상향한 뒤 홀딩 유지)
        if is_tp2 and not tp2_reached:
            info["tp2_reached"] = True
            info["stop_loss"] = tp1  # 스탑로스를 TP1 가격으로 상향 트레일링
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

        # 4. TP1 도달 (본전 스탑 상향 및 홀딩 유지)
        if is_tp1 and not tp1_reached:
            info["tp1_reached"] = True
            info["stop_loss"] = entry_price  # 본전으로 스탑로스 상향
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

    # 5. 새로운 신규 시그널 탐색
    if len(active_slots) < MAX_SLOTS:
        for t in raw_tickers:
            symbol = t.get("currency_pair", "")
            
            if symbol in active_slots or symbol in closed_symbols:
                continue
                
            category = get_asset_category(symbol)
            display_name = f"[{category}] {symbol}"
            
            try:
                last_price = float(t.get("last", 0))
                volume_24h = float(t.get("quote_volume", 0))
                change_24h = float(t.get("change_percentage", 0))
                
                is_long_candidate = (change_24h >= 4.5 and volume_24h > 80000)
                is_short_candidate = (change_24h <= -4.5 and volume_24h > 80000)
                
                if is_long_candidate or is_short_candidate:
                    direction = "LONG" if is_long_candidate else "SHORT"
                    
                    atr, is_valid_signal = fetch_atr_and_validate_breakout(symbol, direction)
                    if not is_valid_signal or not atr:
                        continue
                    
                    leverage = calculate_recommended_leverage(category, change_24h)
                    
                    if direction == "LONG":
                        tp1 = last_price + (atr * 1.5)
                        tp2 = last_price + (atr * 3.0)
                        tp3 = last_price + (atr * 4.5)
                        sl = last_price - (atr * 1.5)
                        reason = "1시간봉 완성 캔들 강한 돌파 확인 (ATR 가변형 모멘텀)"
                    else:
                        tp1 = last_price - (atr * 1.5)
                        tp2 = last_price - (atr * 3.0)
                        tp3 = last_price - (atr * 4.5)
                        sl = last_price + (atr * 1.5)
                        reason = "1시간봉 완성 캔들 하방 돌파 확인 (ATR 가변형 모멘텀)"

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
                        "tp2_reached": False
                    }
                    slot_changed = True
                    
                    chart_link = f"https://www.gate.io/trade/{symbol}"
                    dir_tag = "🟢 롱 포지션" if direction == "LONG" else "🔴 숏 포지션"
                    
                    msg = (
                        "<b>🚀 [Master 6.0 신규 시그널 포착 (ATR 필터 적용)]</b>\n"
                        f"• 방향: <code>{dir_tag}</code>\n"
                        f"• 종목: <a href='{chart_link}'>{display_name}</a>\n"
                        f"• 진입가: <code>{last_price}</code>\n"
                        f"• 추천 레버리지: <code>{leverage}배</code> (변동성 분석)\n"
                        f"• 탐색 이유: <code>{reason}</code>\n\n"
                        "<b>🎯 ATR 가변형 목표가 및 손절가</b>\n"
                        f"• TP1 (+): <code>{tp1:.4f}</code>\n"
                        f"• TP2 (+): <code>{tp2:.4f}</code>\n"
                        f"• TP3 (+): <code>{tp3:.4f}</code>\n"
                        f"• 손절가 SL (-): <code>{sl:.4f}</code>"
                    )
                    alerts_to_send.append(msg)
                    
                    if len(active_slots) >= MAX_SLOTS:
                        break
            except Exception:
                continue

    # 6. 알림 발송
    if alerts_to_send:
        for alert in alerts_to_send:
            send_telegram_message(alert)
        
        if slot_changed:
            slot_status_msg = build_slot_status_message(active_slots, tickers_dict, stats)
            send_telegram_message(slot_status_msg)
            
        print("Sent signals/milestones and slot status to Telegram.")

    save_state(state)
    print("Bot scan cycle completed successfully.")

if __name__ == "__main__":
    run_bot()
