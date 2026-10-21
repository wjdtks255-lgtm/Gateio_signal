from datetime import datetime, timedelta, timezone
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List
import requests
import yfinance as yf
import pandas as pd
import numpy as np

# --------------------------------------------------------------------------- #
#   1️⃣ 기본 설정 및 상수 (금 선물 전용)
# --------------------------------------------------------------------------- #
TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID") or "").strip()

BASE_DIR = Path.cwd()
STATE_FILE = BASE_DIR / "signal_state.json"
KST = timezone(timedelta(hours=9))

SYMBOL = "GC=F"  # 금 선물 티커
TV_URL = "https://www.tradingview.com/chart/?symbol=COMEX%3AGC1%21"

# --------------------------------------------------------------------------- #
#   2️⃣ 유틸리티 및 안전한 파일 I/O (3분할 통계 필드 포함)
# --------------------------------------------------------------------------- #
class NpEncoder(json.JSONEncoder):
    def default(self, obj: Any) -> Any:
        if isinstance(obj, (np.integer, np.int64)):
            return int(obj)
        if isinstance(obj, (np.floating, np.float64)):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        if isinstance(obj, (np.bool_, bool)):
            return bool(obj)
        return super().default(obj)

def now() -> datetime:
    return datetime.now(KST)

def ts() -> str:
    return now().strftime("%Y-%m-%d %H:%M:%S")

def load_state() -> Dict[str, Any]:
    default_state = {
        "position": None,
        "last_signal_time": 0,
        "total_trades": 0,  # 총 거래 횟수
        "wins": 0,          # 익절 성공 (TP3 달성)
        "breakevens": 0,    # 본전 마감 (TP1 도달 후 진입가 방어)
        "losses": 0         # 일반 손절 (초기 SL 이탈)
    }
    if not STATE_FILE.exists():
        return default_state
    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                return default_state
            for k, v in default_state.items():
                data.setdefault(k, v)
            return data
    except Exception as e:
        print(f"[{ts()}] State Load Error: {e}")
        return default_state

def save_state(state: Dict[str, Any]) -> None:
    tmp_file = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
    try:
        with tmp_file.open("w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2, cls=NpEncoder)
        tmp_file.replace(STATE_FILE)
    except Exception as e:
        print(f"[{ts()}] State Save Error: {e}")

# 독립된 3분할 성적표 메시지 전송 함수
def send_performance_report(state: Dict[str, Any]) -> None:
    total = state.get("total_trades", 0)
    wins = state.get("wins", 0)
    breakevens = state.get("breakevens", 0)
    losses = state.get("losses", 0)
    
    win_rate = (wins / total * 100) if total > 0 else 0.0
    
    report_text = (
        f"📊 *[Trading Performance Report]*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"  • Total Trades: `{total}회`\n"
        f"  • 승 / 본전 / 패: `{wins}승 {breakevens}무 {losses}패`\n"
        f"  • Win Rate: **`{win_rate:.1f}%`**\n"
        f"⏱️ 시간: `{ts()} (KST)`"
    )
    send_telegram(report_text, add_chart_btn=False)

# --------------------------------------------------------------------------- #
#   3️⃣ 텔레그램 알림 시스템
# --------------------------------------------------------------------------- #
def send_telegram(text: str, add_chart_btn: bool = False) -> bool:
    if not TOKEN or not CHAT_ID:
        print(f"[{ts()}] TG CONFIG ERROR: Token or Chat ID missing.")
        return False
    
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    
    if add_chart_btn:
        payload["reply_markup"] = {
            "inline_keyboard": [
                [{"text": "📈 트레이딩뷰 골드 차트 바로가기", "url": TV_URL}]
            ]
        }

    for attempt in range(3):
        try:
            res = requests.post(url, json=payload, timeout=10)
            if res.status_code == 200:
                return True
        except Exception as e:
            print(f"[{ts()}] TG Exception (try {attempt+1}): {e}")
        time.sleep(1)
    return False

# --------------------------------------------------------------------------- #
#   4️⃣ 금 선물 데이터 수집 (1분봉 기준)
# --------------------------------------------------------------------------- #
def fetch_gold_data() -> pd.DataFrame:
    try:
        ticker = yf.Ticker(SYMBOL)
        df = ticker.history(period="1d", interval="1m")
        if df.empty:
            return pd.DataFrame()
        df.columns = [c.lower() for c in df.columns]
        return df
    except Exception as e:
        print(f"[{ts()}] Data Fetch Error: {e}")
        return pd.DataFrame()

def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or len(df) < 20:
        return df
    
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / (loss + 1e-9)
    df['rsi'] = 100 - (100 / (1 + rs))
    
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df['atr'] = tr.rolling(window=14).mean()
    
    return df

def fmt_money(v: Any) -> str:
    try:
        return f"${float(v):,.2f}"
    except Exception:
        return "$0.00"

# --------------------------------------------------------------------------- #
#   5️⃣ 포지션 및 TP/SL 단계별 독립 감지 엔진
# --------------------------------------------------------------------------- #
def process_gold_signal():
    state = load_state()
    df = fetch_gold_data()
    if df.empty:
        print(f"[{ts()}] Failed to fetch gold data.")
        return

    df = calculate_indicators(df)
    latest = df.iloc[-1]
    
    price = float(latest['close'])
    rsi = float(latest['rsi']) if 'rsi' in latest and not np.isnan(latest['rsi']) else 50.0
    atr = float(latest['atr']) if 'atr' in latest and not np.isnan(latest['atr']) else price * 0.005
    
    recent_high = float(df['high'].tail(15).max())
    recent_low = float(df['low'].tail(15).min())

    print(f"[{ts()}] Gold Price: {fmt_money(price)} (15m Min: {fmt_money(recent_low)} / 15m Max: {fmt_money(recent_high)}) | RSI: {rsi:.2f}")

    pos = state.get("position")

    if pos:
        long = pos["direction"] == "LONG"
        hit_list = pos.setdefault("hit", [])

        if long:
            # 1. TP1 도달 체크
            if "TP1" not in hit_list and recent_high >= pos["tp1"]:
                hit_list.append("TP1")
                pos["tp1_hit"] = True
                pos["sl"] = pos["entry"]
                send_telegram(
                    f"🎯 *[골드 선물 LONG - TP1 도달 & 본전스탑]*\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🪙 종목: `Gold Futures (GC=F)`\n"
                    f"💰 도달가: `{fmt_money(recent_high)}`\n"
                    f"🛡️ 조치: 손절가를 진입가(`{fmt_money(pos['entry'])}`)로 수정했습니다.\n"
                    f"⏱️ 시간: `{ts()} (KST)`", 
                    add_chart_btn=True
                )
                save_state(state)
                time.sleep(1)

            # 2. TP2 도달 체크
            if "TP2" not in hit_list and recent_high >= pos["tp2"]:
                hit_list.append("TP2")
                send_telegram(
                    f"🎯 *[골드 선물 LONG TP2 2차 익절 도달]*\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🪙 종목: `Gold Futures (GC=F)`\n"
                    f"💰 도달가: `{fmt_money(recent_high)}`\n"
                    f"⏱️ 시간: `{ts()} (KST)`", 
                    add_chart_btn=True
                )
                save_state(state)
                time.sleep(1)

            # 3. TP3 최종 익절 체크
            if "TP3" not in hit_list and recent_high >= pos["tp3"]:
                hit_list.append("TP3")
                state["total_trades"] = state.get("total_trades", 0) + 1
                state["wins"] = state.get("wins", 0) + 1
                
                send_telegram(
                    f"🎉 *[골드 선물 LONG - TP3 최종 익절 달성]*\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🪙 종목: `Gold Futures (GC=F)`\n"
                    f"💰 최종 도달가: `{fmt_money(recent_high)}`\n"
                    f"⏱️ 시간: `{ts()} (KST)`", 
                    add_chart_btn=True
                )
                time.sleep(1.5)
                send_performance_report(state)
                state["position"] = None
                save_state(state)
                return

            # 4. SL 손절 / 본전 마감 체크
            if recent_low <= pos["sl"]:
                if pos.get("tp1_hit", False):
                    state["total_trades"] = state.get("total_trades", 0) + 1
                    state["breakevens"] = state.get("breakevens", 0) + 1
                    
                    send_telegram(
                        f"🛡️ *[골드 선물 LONG - 본전 마감 (Breakeven)]*\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"🪙 종목: `Gold Futures (GC=F)`\n"
                        f"💰 도달가 (진입가 방어): `{fmt_money(recent_low)}`\n"
                        f"⏱️ 시간: `{ts()} (KST)`", 
                        add_chart_btn=True
                    )
                    time.sleep(1.5)
                    send_performance_report(state)
                else:
                    state["total_trades"] = state.get("total_trades", 0) + 1
                    state["losses"] = state.get("losses", 0) + 1
                    
                    send_telegram(
                        f"🛡️ *[골드 선물 LONG - 손절(SL) 도달]*\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"🪙 종목: `Gold Futures (GC=F)`\n"
                        f"📉 최종 도달가: `{fmt_money(recent_low)}`\n"
                        f"⏱️ 시간: `{ts()} (KST)`", 
                        add_chart_btn=True
                    )
                    time.sleep(1.5)
                    send_performance_report(state)
                
                state["position"] = None
                save_state(state)
                return

        else: # SHORT 포지션
            # 1. TP1 도달 체크
            if "TP1" not in hit_list and recent_low <= pos["tp1"]:
                hit_list.append("TP1")
                pos["tp1_hit"] = True
                pos["sl"] = pos["entry"]
                send_telegram(
                    f"🎯 *[골드 선물 SHORT - TP1 도달 & 본전스탑]*\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🪙 종목: `Gold Futures (GC=F)`\n"
                    f"💰 도달가: `{fmt_money(recent_low)}`\n"
                    f"🛡️ 조치: 손절가를 진입가(`{fmt_money(pos['entry'])}`)로 수정했습니다.\n"
                    f"⏱️ 시간: `{ts()} (KST)`", 
                    add_chart_btn=True
                )
                save_state(state)
                time.sleep(1)

            # 2. TP2 도달 체크
            if "TP2" not in hit_list and recent_low <= pos["tp2"]:
                hit_list.append("TP2")
                send_telegram(
                    f"🎯 *[골드 선물 SHORT TP2 2차 익절 도달]*\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🪙 종목: `Gold Futures (GC=F)`\n"
                    f"💰 도달가: `{fmt_money(recent_low)}`\n"
                    f"⏱️ 시간: `{ts()} (KST)`", 
                    add_chart_btn=True
                )
                save_state(state)
                time.sleep(1)

            # 3. TP3 최종 익절 체크
            if "TP3" not in hit_list and recent_low <= pos["tp3"]:
                hit_list.append("TP3")
                state["total_trades"] = state.get("total_trades", 0) + 1
                state["wins"] = state.get("wins", 0) + 1
                
                send_telegram(
                    f"🎉 *[골드 선물 SHORT - TP3 최종 익절 달성]*\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🪙 종목: `Gold Futures (GC=F)`\n"
                    f"💰 최종 도달가: `{fmt_money(recent_low)}`\n"
                    f"⏱️ 시간: `{ts()} (KST)`", 
                    add_chart_btn=True
                )
                time.sleep(1.5)
                send_performance_report(state)
                state["position"] = None
                save_state(state)
                return

            # 4. SL 손절 / 본전 마감 체크
            if recent_high >= pos["sl"]:
                if pos.get("tp1_hit", False):
                    state["total_trades"] = state.get("total_trades", 0) + 1
                    state["breakevens"] = state.get("breakevens", 0) + 1
                    
                    send_telegram(
                        f"🛡️ *[골드 선물 SHORT - 본전 마감 (Breakeven)]*\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"🪙 종목: `Gold Futures (GC=F)`\n"
                        f"💰 도달가 (진입가 방어): `{fmt_money(recent_high)}`\n"
                        f"⏱️ 시간: `{ts()} (KST)`", 
                        add_chart_btn=True
                    )
                    time.sleep(1.5)
                    send_performance_report(state)
                else:
                    state["total_trades"] = state.get("total_trades", 0) + 1
                    state["losses"] = state.get("losses", 0) + 1
                    
                    send_telegram(
                        f"🛡️ *[골드 선물 SHORT - 손절(SL) 도달]*\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"🪙 종목: `Gold Futures (GC=F)`\n"
                        f"📉 최종 도달가: `{fmt_money(recent_high)}`\n"
                        f"⏱️ 시간: `{ts()} (KST)`", 
                        add_chart_btn=True
                    )
                    time.sleep(1.5)
                    send_performance_report(state)
                
                state["position"] = None
                save_state(state)
                return

        save_state(state)
        return

    # 신규 진입 스캔
    direction = None
    if rsi <= 35:
        direction = "LONG"
    elif rsi >= 65:
        direction = "SHORT"

    if direction:
        risk = atr * 1.5
        if direction == "LONG":
            sl = price - risk
            tp1 = price + risk * 1.2
            tp2 = price + risk * 2.2
            tp3 = price + risk * 3.5
            pos_text = "롱 (LONG) 🟢"
        else:
            sl = price + risk
            tp1 = price - risk * 1.2
            tp2 = price - risk * 2.2
            tp3 = price - risk * 3.5
            pos_text = "숏 (SHORT) 🔴"

        msg = (
            f"🌟 *골드 선물 스마트 시그널* 🌟\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🪙 종목: `Gold Futures (GC=F)`\n"
            f"📊 포지션: **{pos_text}**\n"
            f"💰 진입가: `{fmt_money(price)}`\n"
            f"📊 RSI: `{rsi:.2f}` | ATR: `{atr:.2f}`\n\n"
            f"🎯 목표가 (Take Profit)\n"
            f"  • 1차 (TP1): `{fmt_money(tp1)}` (본전스탑)\n"
            f"  • 2차 (TP2): `{fmt_money(tp2)}`\n"
            f"  • 3차 (TP3): `{fmt_money(tp3)}`\n\n"
            f"🛡️ 손절가 (Stop Loss): `{fmt_money(sl)}`\n"
            f"⏱️ 시간: `{ts()} (KST)`"
        )

        if send_telegram(msg, add_chart_btn=True):
            state["position"] = {
                "direction": direction,
                "entry": price,
                "sl": sl,
                "tp1": tp1,
                "tp2": tp2,
                "tp3": tp3,
                "tp1_hit": False,
                "hit": []
            }
            save_state(state)
            print(f"[{ts()}] Gold Signal Sent successfully.")

def main():
    print(f"===================================")
    print(f" GOLD FUTURES BOT RUNNING")
    print(f"===================================")
    process_gold_signal()

if __name__ == "__main__":
    main()
