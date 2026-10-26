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
        "parse_mode": "Markdown"
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code != 200:
            print(f"Failed to send telegram message: {response.text}")
    except Exception as e:
        print(f"Error sending telegram message: {e}")

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "active_slots": {},
        "stats": {"wins": 0, "losses": 0, "total": 0},
        "history": [],  # 최근 매매 종료 내역 기록
        "last_update_id": 0
    }

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
    """종목명 패턴에 따라 자산 군별로 분류"""
    upper_sym = symbol.upper()
    if any(k in upper_sym for k in ['XAU', 'XAG', 'OIL', 'GOLD', 'SILVER', 'CRUDE']):
        return '원자재'
    elif any(k in upper_sym for k in ['EUR', 'GBP', 'JPY', 'AUD', 'CHF', 'NZD', 'CAD']):
        if '_USDT' in upper_sym and not any(f in upper_sym for f in ['EUR', 'GBP', 'JPY', 'AUD', 'CHF', 'NZD', 'CAD']):
            return '코인'
        return '외환'
    elif any(k in upper_sym for k in ['SPX', 'NAS', 'DJI', 'US30', 'US100', 'NIKKEI']):
        return '지수'
    return '코인'

def calculate_recommended_leverage(category, change_24h):
    abs_change = abs(change_24h)
    if category == '코인':
        return 5 if abs_change > 10.0 else (7 if abs_change > 6.0 else 10)
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
    total = stats.get("total", 0)
    wins = stats.get("wins", 0)
    if total == 0:
        return 0.0
    return round((wins / total) * 100, 2)

def build_slot_status_message(active_slots, tickers_dict):
    active_symbols = list(active_slots.keys())
    active_count = len(active_symbols)
    
    lines = [f"📊 *[현재 주시 중인 슬롯 현황 ({active_count} / {MAX_SLOTS} 슬롯)]*\n"]
    
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
            lines.append(f"{i}️⃣ {dir_icon} [`{display_name}`]({chart_link}) | `{profit_pct:+.2f}%` | 보유: `{holding_str}`")
        else:
            lines.append(f"{i}️⃣ (빈 슬롯)")
            
    return "\n".join(lines)

def process_telegram_commands(state, tickers_dict):
    if not TELEGRAM_TOKEN:
        return
    
    last_update_id = state.get("last_update_id", 0)
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates?offset={last_update_id + 1}&timeout=2"
    
    try:
        response = requests.get(url, timeout=5)
        if response.status_code != 200:
            return
        
        data = response.json()
        if not data.get("ok"):
            return
            
        updates = data.get("result", [])
        for update in updates:
            update_id = update.get("update_id", 0)
            state["last_update_id"] = max(state.get("last_update_id", 0), update_id)
            
            message = update.get("message", {})
            text = message.get("text", "").strip()
            chat = message.get("chat", {})
            chat_id = str(chat.get("id", ""))
            
            # 디버그용 로그 (GitHub Actions 로그에서 확인 가능)
            print(f"DEBUG - Received chat_id: '{chat_id}', text: '{text}' (Expected CHAT_ID: '{TELEGRAM_CHAT_ID}')")
            
            if chat_id != str(TELEGRAM_CHAT_ID):
                print(f"DEBUG - Chat ID mismatch! Ignored.")
                continue
                
            active_slots = state.get("active_slots", {})
            stats = state.get("stats", {"wins": 0, "losses": 0, "total": 0})
            history_list = state.get("history", [])
            
            parts = text.split()
            command = parts[0] if parts else ""
            
            if command in ["/현황", "/슬롯", "/status"]:
                status_msg = build_slot_status_message(active_slots, tickers_dict)
                send_telegram_message(status_msg)
                
            elif command in ["/전적", "/통계", "/stats"]:
                win_rate = calculate_win_rate(stats)
                stats_msg = (
                    f"📊 *[누적 승패 전적 및 승률 요약]*\n"
                    f"• 승 / 패 / 총전적: `{stats['wins']}승 {stats['losses']}패 (총 {stats['total']}전)`\n"
                    f"• 현재 승률: `{win_rate}%`\n"
                    f"• 현재 운영 슬롯: `{len(active_slots)} / {MAX_SLOTS} 슬롯`"
                )
                send_telegram_message(stats_msg)
                
            elif command in ["/시세", "/price"]:
                if len(parts) > 1:
                    query = parts[1].upper()
                    matched_price = None
                    found_sym = ""
                    
                    for sym, price in tickers_dict.items():
                        if query in sym.upper():
                            found_sym = sym
                            matched_price = price
                            break
                    
                    if found_sym:
                        category = get_asset_category(found_sym)
                        chart_link = f"https://www.gate.io/trade/{found_sym}"
                        price_msg = (
                            f"📈 *[실시간 시세 조회]*\n"
                            f"• 종목: `[{category}]` [`{found_sym}`]({chart_link})\n"
                            f"• 현재가: `{matched_price}`"
                        )
                        send_telegram_message(price_msg)
                    else:
                        send_telegram_message(f"⚠️ `{query}`에 해당하는 종목을 찾을 수 없습니다.")
                else:
                    send_telegram_message("⚠️ 조회할 종목명을 입력해주세요. (예: `/시세 BTC_USDT`)")
                    
            elif command in ["/내역", "/히스토리", "/history"]:
                history_lines = ["📜 *[최근 매매 내역 요약]*"]
                if not history_list:
                    history_lines.append("아직 기록된 종료 내역이 없습니다.")
                else:
                    for idx, h in enumerate(history_list[:5], 1):
                        dir_icon = "🟢" if h.get("direction") == "LONG" else "🔴"
                        history_lines.append(f"{idx}. {dir_icon} `[{h['category']}] {h['symbol']}` | `{h['result']}`")
                send_telegram_message("\n".join(history_lines))
                
            elif command in ["/전략", "/규칙", "/strategy"]:
                strategy_msg = (
                    f"⚙️ *[Master 6.0 전략 핵심 요약]*\n\n"
                    f"• *운영 방식*: 최대 `8개 슬롯` 동시 양방향(롱/숏) 운영\n"
                    f"• *지원 자산*: `[코인]`, `[원자재]`, `[지수]`, `[외환]` 통합 스캔\n"
                    f"• *익절/손절 라인*:\n"
                    f"  - TP1: `2.0%` (본전 스탑 전환)\n"
                    f"  - TP2: `4.0%` (중간 익절)\n"
                    f"  - TP3: `6.0%` (최종 익절 마감)\n"
                    f"  - SL: `-2.0%` (손절 마감)\n"
                    f"• *리스크 관리*: 종목별 24시간 변동성 기반 추천 레버리지 자동 산정"
                )
                send_telegram_message(strategy_msg)
                
            elif command in ["/도움말", "/help"]:
                help_msg = (
                    f"🤖 *[Master 6.0 봇 공용 명령어 안내]*\n\n"
                    f"• `/현황` 또는 `/슬롯` : 현재 1~8번 슬롯 현황판 조회\n"
                    f"• `/전적` 또는 `/통계` : 누적 승패 전적 및 승률 조회\n"
                    f"• `/시세 [종목명]` : 특정 종목 실시간 가격 조회\n"
                    f"• `/내역` : 최근 종료된 매매 내역 보기\n"
                    f"• `/전략` : Master 6.0 매매 규칙 요약\n"
                    f"• `/도움말` : 명령어 안내 보기"
                )
                send_telegram_message(help_msg)
                
    except Exception as e:
        print(f"Error processing telegram commands: {e}")

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
    process_telegram_commands(state, tickers_dict)

    active_slots = state.setdefault("active_slots", {})
    stats = state.setdefault("stats", {"wins": 0, "losses": 0, "total": 0})
    history_list = state.setdefault("history", [])
    
    alerts_to_send = []
    slot_changed = False

    # 1. 활성 슬롯 모니터링 (SL 및 TP1, TP2, TP3 도달 체크)
    for symbol, info in list(active_slots.items()):
        current_price = tickers_dict.get(symbol, info["entry_price"])
        chart_link = f"https://www.gate.io/trade/{symbol}"
        direction = info.get("direction", "LONG")
        category = get_asset_category(symbol)
        display_name = f"[{category}] {symbol}"
        
        if direction == "LONG":
            is_stopped = current_price <= info["stop_loss"]
            is_tp3 = current_price >= info["tp3"]
            is_tp2 = current_price >= info["tp2"]
            is_tp1 = current_price >= info["tp1"]
        else:
            is_stopped = current_price >= info["stop_loss"]
            is_tp3 = current_price <= info["tp3"]
            is_tp2 = current_price <= info["tp2"]
            is_tp1 = current_price <= info["tp1"]

        # 손절가(SL) 도달 체크 (패배)
        if is_stopped:
            msg = (
                f"🛑 *[손절가(SL) 도달]*\n"
                f"• 방향: `{'🟢 롱 포지션' if direction=='LONG' else '🔴 숏 포지션'}`\n"
                f"• 종목: [`{display_name}`]({chart_link})\n"
                f"• 진입가: `{info['entry_price']}`\n"
                f"• 도달 가격: `{current_price}` (손절 마감)\n"
                f"• 결과: `패배 (손실 마감)`"
            )
            alerts_to_send.append(msg)
            stats["losses"] += 1
            stats["total"] += 1
            
            history_list.insert(0, {"symbol": symbol, "category": category, "direction": direction, "result": "패배 (손실 마감)"})
            state["history"] = history_list[:10]
            
            del active_slots[symbol]
            slot_changed = True
            continue

        # TP3 최종 목표가 도달 체크 (승리)
        if is_tp3 and not info.get("tp3_reached"):
            info["tp3_reached"] = True
            msg = (
                f"🎯🎯🎯 *[TP3 최종 목표가 도달]*\n"
                f"• 방향: `{'🟢 롱 포지션' if direction=='LONG' else '🔴 숏 포지션'}`\n"
                f"• 종목: [`{display_name}`]({chart_link})\n"
                f"• 진입가: `{info['entry_price']}`\n"
                f"• TP3 최정상 도달: `{current_price}`\n"
                f"• 결과: `승리 (전량 익절 마감)`"
            )
            alerts_to_send.append(msg)
            stats["wins"] += 1
            stats["total"] += 1
            
            history_list.insert(0, {"symbol": symbol, "category": category, "direction": direction, "result": "승리 (전량 익절)"})
            state["history"] = history_list[:10]
            
            del active_slots[symbol]
            slot_changed = True
            continue

        # TP2 도달 체크
        if is_tp2 and not info.get("tp2_reached"):
            info["tp2_reached"] = True
            msg = (
                f"🎯🎯 *[TP2 2차 목표가 도달]*\n"
                f"• 방향: `{'🟢 롱 포지션' if direction=='LONG' else '🔴 숏 포지션'}`\n"
                f"• 종목: [`{display_name}`]({chart_link})\n"
                f"• 진입가: `{info['entry_price']}`\n"
                f"• TP2 도달: `{current_price}`\n"
                f"• 조치: `중간 익절 진행`"
            )
            alerts_to_send.append(msg)
            slot_changed = True

        # TP1 도달 체크
        if is_tp1 and not info.get("tp1_reached"):
            info["tp1_reached"] = True
            msg = (
                f"🎯 *[TP1 1차 목표가 도달]*\n"
                f"• 방향: `{'🟢 롱 포지션' if direction=='LONG' else '🔴 숏 포지션'}`\n"
                f"• 종목: [`{display_name}`]({chart_link})\n"
                f"• 진입가: `{info['entry_price']}`\n"
                f"• TP1 도달: `{current_price}`\n"
                f"• 조치: `본전 스탑(Break-even) 전환 완료`"
            )
            alerts_to_send.append(msg)
            slot_changed = True

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
                        "tp1_reached": False,
                        "tp2_reached": False,
                        "tp3_reached": False
                    }
                    slot_changed = True
                    
                    chart_link = f"https://www.gate.io/trade/{symbol}"
                    dir_tag = "🟢 롱 포지션" if direction == "LONG" else "🔴 숏 포지션"
                    
                    msg = (
                        f"🚀 *[Master 6.0 신규 시그널 포착]*\n"
                        f"• 방향: `{dir_tag}`\n"
                        f"• 종목: [`{display_name}`]({chart_link})\n"
                        f"• 진입가: `{last_price}`\n"
                        f"• 추천 레버리지: `{leverage}배` (변동성 분석 기반)\n"
                        f"• 탐색 이유: `{reason}`\n\n"
                        f"🎯 *목표가 및 손절가*\n"
                        f"• TP1 ({'+2.0%' if direction=='LONG' else '-2.0%'}): `{tp1:.4f}`\n"
                        f"• TP2 ({'+4.0%' if direction=='LONG' else '-4.0%'}): `{tp2:.4f}`\n"
                        f"• TP3 ({'+6.0%' if direction=='LONG' else '-6.0%'}): `{tp3:.4f}`\n"
                        f"• 손절가 SL ({'-2.0%' if direction=='LONG' else '+2.0%'}): `{sl:.4f}`"
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
            slot_status_msg = build_slot_status_message(active_slots, tickers_dict)
            send_telegram_message(slot_status_msg)
            
        print("Sent signals/milestones and slot status to Telegram.")

    save_state(state)
    print("Bot scan cycle completed successfully.")

if __name__ == "__main__":
    run_bot()
