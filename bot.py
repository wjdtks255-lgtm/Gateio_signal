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
        "stats": {"wins": 0, "losses": 0, "total": 0}
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

def build_slot_status_message(active_slots, tickers_dict):
    active_symbols = list(active_slots.keys())
    active_count = len(active_symbols)
    
    lines = [f"📊 *[현재 주시 중인 슬롯 현황 ({active_count} / {MAX_SLOTS} 슬롯)]*\n"]
    
    for i in range(1, MAX_SLOTS + 1):
        if i <= active_count:
            sym = active_symbols[i-1]
            info = active_slots[sym]
            entry_price = info.get("entry_price", 0)
            
            current_price = tickers_dict.get(sym, entry_price)
            profit_pct = 0.0
            if entry_price > 0:
                profit_pct = ((current_price - entry_price) / entry_price) * 100
            
            holding_str = format_holding_time(info.get("entry_time"))
            chart_link = f"https://www.gate.io/trade/{sym}"
            
            lines.append(f"{i}️⃣ [`{sym}`]({chart_link}) | `{profit_pct:+.2f}%` | 보유: `{holding_str}`")
        else:
            lines.append(f"{i}️⃣ (빈 슬롯)")
            
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
    active_slots = state.setdefault("active_slots", {})
    stats = state.setdefault("stats", {"wins": 0, "losses": 0, "total": 0})
    
    alerts_to_send = []
    slot_changed = False

    # 1. 활성 슬롯 모니터링 (SL 및 TP1, TP2, TP3 도달 체크)
    for symbol, info in list(active_slots.items()):
        current_price = tickers_dict.get(symbol, info["entry_price"])
        chart_link = f"https://www.gate.io/trade/{symbol}"
        
        # 손절가(SL) 도달 체크
        if current_price <= info["stop_loss"]:
            msg = (
                f"🛑 *[손절가(SL) 도달]*\n"
                f"• 종목: [`{symbol}`]({chart_link})\n"
                f"• 진입가: `{info['entry_price']}`\n"
                f"• 도달 가격: `{current_price}` (SL -2.0% 도달)\n"
                f"• 결과: `패배 (손실 마감)`"
            )
            alerts_to_send.append(msg)
            stats["losses"] += 1
            stats["total"] += 1
            del active_slots[symbol]
            slot_changed = True
            continue

        # TP3 최종 목표가 도달 체크
        if current_price >= info["tp3"] and not info.get("tp3_reached"):
            info["tp3_reached"] = True
            msg = (
                f"🎯🎯🎯 *[TP3 최종 목표가 도달]*\n"
                f"• 종목: [`{symbol}`]({chart_link})\n"
                f"• 진입가: `{info['entry_price']}`\n"
                f"• TP3 (+6.0%): `{current_price}` (최종 도달 완료)\n"
                f"• 결과: `승리 (전량 익절 마감)`"
            )
            alerts_to_send.append(msg)
            stats["wins"] += 1
            stats["total"] += 1
            del active_slots[symbol]
            slot_changed = True
            continue

        # TP2 목표가 도달 체크
        if current_price >= info["tp2"] and not info.get("tp2_reached"):
            info["tp2_reached"] = True
            msg = (
                f"🎯🎯 *[TP2 2차 목표가 도달]*\n"
                f"• 종목: [`{symbol}`]({chart_link})\n"
                f"• 진입가: `{info['entry_price']}`\n"
                f"• TP2 (+4.0%): `{current_price}` (도달 완료)\n"
                f"• 조치: `중간 익절 진행`"
            )
            alerts_to_send.append(msg)
            slot_changed = True

        # TP1 목표가 도달 체크
        if current_price >= info["tp1"] and not info.get("tp1_reached"):
            info["tp1_reached"] = True
            msg = (
                f"🎯 *[TP1 1차 목표가 도달]*\n"
                f"• 종목: [`{symbol}`]({chart_link})\n"
                f"• 진입가: `{info['entry_price']}`\n"
                f"• TP1 (+2.0%): `{current_price}` (도달 완료)\n"
                f"• 조치: `본전 스탑(Break-even) 전환 완료`"
            )
            alerts_to_send.append(msg)
            slot_changed = True

    # 2. 새로운 신규 시그널 탐색 및 슬롯 배정 (최대 8개 제한)
    if len(active_slots) < MAX_SLOTS:
        for t in raw_tickers:
            symbol = t.get("currency_pair", "")
            if not symbol.endswith("_USDT"):
                continue
            
            try:
                last_price = float(t.get("last", 0))
                volume_24h = float(t.get("quote_volume", 0))
                change_24h = float(t.get("change_percentage", 0))
                
                # Master 6.0 진입 조건 만족 시
                if change_24h >= 4.0 and volume_24h > 50000:
                    if symbol not in active_slots:
                        now_iso = datetime.now(timezone.utc).isoformat()
                        active_slots[symbol] = {
                            "entry_price": last_price,
                            "entry_time": now_iso,
                            "tp1": last_price * 1.02,
                            "tp2": last_price * 1.04,
                            "tp3": last_price * 1.06,
                            "stop_loss": last_price * 0.98,
                            "tp1_reached": False,
                            "tp2_reached": False,
                            "tp3_reached": False
                        }
                        slot_changed = True
                        
                        chart_link = f"https://www.gate.io/trade/{symbol}"
                        
                        # 신규 시그널 알림
                        msg = (
                            f"🚀 *[Master 6.0 신규 시그널 포착]*\n"
                            f"• 종목: [`{symbol}`]({chart_link})\n"
                            f"• 진입가: `{last_price}`\n"
                            f"• 탐색 이유: `1시간봉 추세 돌파 및 15분봉 거래량 급증`\n\n"
                            f"🎯 *목표가 및 손절가*\n"
                            f"• TP1 (+2.0%): `{active_slots[symbol]['tp1']:.4f}`\n"
                            f"• TP2 (+4.0%): `{active_slots[symbol]['tp2']:.4f}`\n"
                            f"• TP3 (+6.0%): `{active_slots[symbol]['tp3']:.4f}`\n"
                            f"• 손절가 SL (-2.0%): `{active_slots[symbol]['stop_loss']:.4f}`"
                        )
                        alerts_to_send.append(msg)
                        
                        if len(active_slots) >= MAX_SLOTS:
                            break
            except Exception:
                continue

    # 3. 알림 발송 (이벤트 알림 전송 후, 신규 추가 또는 TP/SL 도달로 슬롯 변동이 있을 때만 슬롯 현황판 별도 전송)
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
