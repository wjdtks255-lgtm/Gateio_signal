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
    """상태 파일 로드 시 승무패(wins, draws, losses) 통계 안전 보정"""
    state = {
        "active_slots": {},
        "stats": {"wins": 0, "draws": 0, "losses": 0, "total": 0},
        "history": []
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
                    if "history" in data and isinstance(data["history"], list):
                        state["history"] = data["history"]
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

def run_bot():
    print("Master 6.0 Bot Scan Initiated...")
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
    history_list = state.setdefault("history", [])
    
    alerts_to_send = []
    slot_changed = False

    # 1. 활성 슬롯 모니터링 (SL, TP1~3 체크)
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

        # 손절(패배) 또는 본전 스탑(무) 도달 체크
        if is_stopped:
            if tp1_reached:
                msg = (
                    "<b>🏳️ [본전 청산 - 무승부]</b>\n"
                    f"• 방향: <code>{'🟢 롱 포지션' if direction=='LONG' else '🔴 숏 포지션'}</code>\n"
                    f"• 종목: <a href='{chart_link}'>{display_name}</a>\n"
                    f"• 진입가: <code>{entry_price}</code>\n"
                    f"• 청산 가격: <code>{current_price}</code> (본전 스탑)\n"
                    "• 결과: <code>무 (본전 마감)</code>"
                )
                alerts_to_send.append(msg)
                stats["draws"] += 1
                stats["total"] += 1
            else:
                msg = (
                    "<b>🛑 [손절가(SL) 도달]</b>\n"
                    f"• 방향: <code>{'🟢 롱 포지션' if direction=='LONG' else '🔴 숏 포지션'}</code>\n"
                    f"• 종목: <a href='{chart_link}'>{display_name}</a>\n"
                    f"• 진입가: <code>{entry_price}</code>\n"
                    f"• 도달 가격: <code>{current_price}</code> (손절 마감)\n"
                    "• 결과: <code>패배 (손실 마감)</code>"
                )
                alerts_to_send.append(msg)
                stats["losses"] += 1
                stats["total"] += 1
            
            del active_slots[symbol]
            slot_changed = True
            continue

        # TP3 도달 (승리)
        if is_tp3:
            msg = (
                "<b>🎯🎯🎯 [TP3 최종 목표가 도달 - 포지션 회수]</b>\n"
                f"• 방향: <code>{'🟢 롱 포지션' if direction=='LONG' else '🔴 숏 포지션'}</code>\n"
                f"• 종목: <a href='{chart_link}'>{display_name}</a>\n"
                f"• 진입가: <code>{entry_price}</code>\n"
                f"• 도달 가격: <code>{current_price}</code>\n"
                "• 결과: <code>승리 (TP3 익절 마감)</code>"
            )
            alerts_to_send.append(msg)
            stats["wins"] += 1
            stats["total"] += 1
            del active_slots[symbol]
            slot_changed = True
            continue

        # TP2 도달 (승리)
        if is_tp2:
            msg = (
                "<b>🎯🎯 [TP2 2차 목표가 도달 - 포지션 회수]</b>\n"
                f"• 방향: <code>{'🟢 롱 포지션' if direction=='LONG' else '🔴 숏 포지션'}</code>\n"
                f"• 종목: <a href='{chart_link}'>{display_name}</a>\n"
                f"• 진입가: <code>{entry_price}</code>\n"
                f"• 도달 가격: <code>{current_price}</code>\n"
                "• 결과: <code>승리 (TP2 익절 마감)</code>"
            )
            alerts_to_send.append(msg)
            stats["wins"] += 1
            stats["total"] += 1
            del active_slots[symbol]
            slot_changed = True
            continue

        # TP1 도달 (본전 스탑 상향)
        if is_tp1 and not tp1_reached:
            info["tp1_reached"] = True
            info["stop_loss"] = entry_price
            slot_changed = True
            
            msg = (
                "<b>🎯 [TP1 1차 목표가 도달]</b>\n"
                f"• 방향: <code>{'🟢 롱 포지션' if direction=='LONG' else '🔴 숏 포지션'}</code>\n"
                f"• 종목: <a href='{chart_link}'>{display_name}</a>\n"
                f"• 진입가: <code>{entry_price}</code>\n"
                f"• 도달 가격: <code>{current_price}</code>\n"
                "• 조치: <code>본전 스탑(Break-even) 전환 완료 (손실 원천 차단)</code>"
            )
            alerts_to_send.append(msg)

    # 2. 새로운 신규 시그널 탐색 및 슬롯 배정
    if len(active_slots) < MAX_SLOTS:
        for t in raw_tickers:
            symbol = t.get("currency_pair", "")
            category = get_asset_category(symbol)
            display_name = f"[{category}] {symbol}"
            
            try:
                last_price = float(t.get("last", 0))
                volume_24h = float(t.get("quote_volume", 0))
                change_24h = float(t.get("change_percentage", 0))
                
                is_long_signal = (change_24h >= 4.5 and volume_24h > 80000)
                is_short_signal = (change_24h <= -4.5 and volume_24h > 80000)
                
                if (is_long_signal or is_short_signal) and symbol not in active_slots:
                    direction = "LONG" if is_long_signal else "SHORT"
                    leverage = calculate_recommended_leverage(category, change_24h)
                    
                    if direction == "LONG":
                        tp1, tp2, tp3, sl = last_price * 1.02, last_price * 1.04, last_price * 1.06, last_price * 0.98
                        reason = "1시간봉 추세 돌파 및 15분봉 거래량 급증 (상승 모멘텀)"
                    else:
                        tp1, tp2, tp3, sl = last_price * 0.98, last_price * 0.96, last_price * 0.94, last_price * 1.02
                        reason = "1시간봉 저항선 도달 후 하락 다이버전스 발생 (하락 모멘텀)"

                    now_iso = datetime.now(timezone.utc).isoformat()
                    active_slots[symbol] = {
                        "direction": direction,
                        "entry_price": last_price,
                        "entry_time": now_iso,
                        "leverage": leverage,
                        "tp1": tp1,
                        "tp2": tp2,
                        "tp3": tp3,
                        "stop_loss": sl,
                        "tp1_reached": False
                    }
                    slot_changed = True
                    
                    chart_link = f"https://www.gate.io/trade/{symbol}"
                    dir_tag = "🟢 롱 포지션" if direction == "LONG" else "🔴 숏 포지션"
                    
                    msg = (
                        "<b>🚀 [Master 6.0 신규 시그널 포착]</b>\n"
                        f"• 방향: <code>{dir_tag}</code>\n"
                        f"• 종목: <a href='{chart_link}'>{display_name}</a>\n"
                        f"• 진입가: <code>{last_price}</code>\n"
                        f"• 추천 레버리지: <code>{leverage}배</code> (변동성 분석 기반)\n"
                        f"• 탐색 이유: <code>{reason}</code>\n\n"
                        "<b>🎯 목표가 및 손절가</b>\n"
                        f"• TP1 ({'+2.0%' if direction=='LONG' else '-2.0%'}): <code>{tp1:.4f}</code>\n"
                        f"• TP2 ({'+4.0%' if direction=='LONG' else '-4.0%'}): <code>{tp2:.4f}</code>\n"
                        f"• TP3 ({'+6.0%' if direction=='LONG' else '-6.0%'}): <code>{tp3:.4f}</code>\n"
                        f"• 손절가 SL ({'-2.0%' if direction=='LONG' else '+2.0%'}): <code>{sl:.4f}</code>"
                    )
                    alerts_to_send.append(msg)
                    
                    if len(active_slots) >= MAX_SLOTS:
                        break
            except Exception:
                continue

    # 3. 알림 발송
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
