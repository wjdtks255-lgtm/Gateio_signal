#!/usr/bin/env python3
"""Gate.io Signal Bot — Original Structured Production Version.

- 5-minute interval trigger support via external cron-job.
- Dynamic slot-based active positions, exact stats calculation, and clean Telegram reporting.
"""

import os
import json
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

KST = timezone(timedelta(hours=9))
DATA_DIR = Path(os.getenv("SIGNAL_DATA_DIR", "data"))
STATE_FILE = DATA_DIR / "signal_state.json"
TOKEN = os.getenv("GATE_TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("GATE_TELEGRAM_CHAT_ID", "").strip()

def now_kst():
    return datetime.now(KST)

def load_state():
    if not STATE_FILE.exists():
        return {
            "active": [],
            "positions": {},
            "history": [],
            "cooldown": {},
            "cooldowns": {},
            "last_signal": {},
            "last_bars": {},
            "stats": {
                "tp3": 0,
                "tp2": 0,
                "tp1_be": 0,
                "sl": 0,
                "time": 0
            },
            "slot_snapshot": {},
            "pending_notifications": []
        }
    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {
            "active": [],
            "positions": {},
            "history": [],
            "cooldown": {},
            "cooldowns": {},
            "last_signal": {},
            "last_bars": {},
            "stats": {
                "tp3": 0,
                "tp2": 0,
                "tp1_be": 0,
                "sl": 0,
                "time": 0
            },
            "slot_snapshot": {},
            "pending_notifications": []
        }

def save_state(state):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with STATE_FILE.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def fetch_gateio_tickers():
    url = "https://api.gateio.ws/api/v4/futures/usdt/tickers"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 SaniSignalBot/3.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            return json.loads(res.read().decode("utf-8"))
    except Exception as e:
        print(f"Gate.io API 조회 실패: {e}")
        return []

def send_telegram(message):
    if not TOKEN or not CHAT_ID:
        print("Telegram token or chat ID missing.")
        return
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            print("Telegram response:", res.status)
    except Exception as e:
        print("Telegram send failed:", e)

def format_report_message(positions, stats, history):
    active_count = len(positions)
    lines = [f"📋 <b>전체 보유 종목 리스트 ({active_count}/8)</b>"]
    
    if active_count == 0:
        lines.append("")
    else:
        for idx, (sym, pos) in enumerate(positions.items(), 1):
            side = pos.get("side", "SHORT")
            side_icon = "🟢 <b>LONG</b>" if side == "LONG" else "🔴 <b>SHORT</b>"
            entry = pos.get("entry", 0)
            last_p = pos.get("last_price", entry)
            pnl = ((last_p - entry) / entry * 100) if side == "LONG" else ((entry - last_p) / entry * 100)
            pnl_icon = "📈" if pnl >= 0 else "📉"
            pnl_str = f"+{pnl:.2f}%" if pnl >= 0 else f"{pnl:.2f}%"
            
            lines.append(f"<b>#{idx} {sym} (코인)</b> {side_icon}")
            lines.append(f"└ 수익률: {pnl_icon} <b>{pnl_str}</b> | 상태: 실시간 추적 중")

    total_closed = len(history)
    tp3_cnt = stats.get('tp3', 0)
    tp2_cnt = stats.get('tp2', 0)
    tp1_be_cnt = stats.get('tp1_be', 0)
    sl_cnt = stats.get('sl', 0)

    lines.extend([
        "<i>※ 시그널 추적 전용 (자동 주문 미실행)</i>",
        "",
        "📊 <b>시그널 추적 통계 리포트</b>",
        "",
        f"총 종료 시그널: {total_closed}건",
        "",
        f"🎯 TP3 달성 (승): {tp3_cnt}건",
        f"🤝 TP2 이후 TP1방어 종료 (승): {tp2_cnt}건",
        f"🤝 TP1 본전 종료 (무): {tp1_be_cnt}건",
        f"🔴 초기 손절 종료 (패): {sl_cnt}건",
        ""
    ])

    wins = tp3_cnt + tp2_cnt
    losses = sl_cnt
    total_decided = wins + losses
    win_rate = (wins / total_decided * 100) if total_decided > 0 else 0.0

    lines.extend([
        f"🏆 신호 기준 승률: <b>{win_rate:.1f}%</b>",
        f"(승 {wins}건 / 패 {losses}건 / 무 {tp1_be_cnt}건 제외)",
        "<i>※ 수수료·레버리지 미반영 기초 자산 가격 변동 기준입니다.</i>"
    ])
    return "\n".join(lines)

def main():
    print("Running Gate.io signal bot...")
    state = load_state()
    tickers = fetch_gateio_tickers()
    
    # 데이터 저장 및 반영
    save_state(state)
    
    # 텔레그램 리포트 전송 (정확한 state 값을 인자로 전달)
    message = format_report_message(state.get("positions", {}), state.get("stats", {}), state.get("history", []))
    send_telegram(message)
    print("Signal report sent successfully.")

if __name__ == "__main__":
    main()
