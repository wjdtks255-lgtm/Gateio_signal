#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gate.io USDT 선물 롱/숏 시그널 봇 (텔레그램 알림 전용, 자동 주문 없음)
- 수익형 본전 방어(Profit-Lock Breakeven) 로직 적용 버전
"""

import csv
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

import requests

# ──────────────────────────── 설정 ────────────────────────────
TELEGRAM_TOKEN = os.getenv("GATE_TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("GATE_TELEGRAM_CHAT_ID")

STATE_FILE = "gate_state.json"
TRADES_FILE = "trades.csv"
BASE_URL = "https://api.gateio.ws/api/v4/futures/usdt"

MAX_SLOTS = 8

# 진입 조건
MIN_CHANGE_PCT = 4.5          # 24시간 변동률 기준(%)
MIN_QUOTE_VOLUME = 500_000    # 24시간 거래대금 최소값(USDT)[span_0](start_span)[span_0](end_span)
RSI_PERIOD = 14
ATR_PERIOD = 14
BODY_RATIO_MIN = 0.35         # 직전 완성봉 몸통 / 전체 길이[span_1](start_span)[span_1](end_span)
LEVERAGED_TOKEN_RE = re.compile(r"\d+[LS]_USDT$")  # 3L, 5S 같은 레버리지 토큰 제외[span_2](start_span)[span_2](end_span)

# 목표가/손절가 (ATR 배수)
ATR_SL = 1.5
ATR_TP_MULTS = (1.0, 2.5, 4.0)   # TP1을 1.0R로 당겨 승률 및 본전 방어 속도 극대화
MAX_SL_PCT = 6.0              # 손절 거리(%)가 이 값을 넘는 종목은 신호 제외[span_3](start_span)[span_3](end_span)
MAX_PER_THEME = 1             # 같은 테마에서 같은 방향으로 동시에 가질 수 있는 최대 슬롯 수[span_4](start_span)[span_4](end_span)

# 비용/리스크 가정 및 수익형 본전 방어 오프셋
FEE_ROUNDTRIP = 0.001         # 왕복 수수료+슬리피지 가정 0.1%[span_5](start_span)[span_5](end_span)
PROFIT_LOCK_ATR = 0.2         # TP1 달성 시 스탑로스를 진입가보다 이만큼 유리한 방향(+0.2 ATR)으로 설정
RISK_PER_TRADE_PCT = 1.5      # 권장 증거금 계산용: 손절 시 계좌 손실 목표(%)[span_6](start_span)[span_6](end_span)
MAX_MARGIN_LOSS = 0.5         # 손절 시 증거금 손실이 이 비율을 넘지 않도록 레버리지 상한[span_7](start_span)[span_7](end_span)

# 보유 시간 규칙
ROTATE_HOURS = 18             # 이 시간 이상 + 수익이면 슬롯 회전 청산[span_8](start_span)[span_8](end_span)
ROTATE_MIN_PROFIT_PCT = 0.5   # 회전 청산 최소 수익(%, 레버리지 미반영 가격 기준)[span_9](start_span)[span_9](end_span)
TIME_STOP_HOURS = 24          # 이 시간 이상이면 손익 무관 청산[span_10](start_span)[span_10](end_span)
COOLDOWN_HOURS = 3            # 청산 후 같은 종목 재진입 금지 시간[span_11](start_span)[span_11](end_span)

# 결과 분류: 수수료 반영 R이 ±이 값 이내면 무승부
DRAW_BAND_R = 0.1

# 실행 시간 관리 (GitHub Actions timeout 대비)
MAX_CANDIDATES = 40
SCAN_BUDGET_SEC = 150

# 알림 옵션
FAIL_ALERT_THRESHOLD = 3      # API 연속 실패 N회째에 오류 알림[span_12](start_span)[span_12](end_span)
DAILY_REPORT_ENABLED = True
DAILY_REPORT_HOUR_UTC = 0     # UTC 00:00 = 한국시간 09:00 이후 첫 실행 때 하루 1회[span_13](start_span)[span_13](end_span)

CSV_FIELDS = [
    "closed_at", "symbol", "direction", "category", "leverage",
    "entry_time", "entry_price", "exit_price", "exit_reason",
    "holding_hours", "gross_r", "net_r", "mfe_r", "mae_r",
]

REASON_LABEL = {
    "SL": "손절",
    "PROFIT_LOCK": "수익 방어",  # 본전 대신 약수익 구간 청산
    "TP1_PROTECT": "TP1 방어",
    "TP3": "TP3",
    "ROTATE": "회전",
    "TIME_STOP": "타임스탑",
}

NUM_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣"]


# ──────────────────────────── 공통 유틸 ────────────────────────────
def utcnow():
    return datetime.now(timezone.utc)


def parse_iso(s, default=None):
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return default


def fmt_price(p):
    if p >= 1000:
        return f"{p:.2f}"
    if p >= 1:
        return f"{p:.4f}"
    if p >= 0.01:
        return f"{p:.5f}"
    return f"{p:.8f}"


def fmt_hours(hours):
    h = int(hours)
    m = int((hours - h) * 60)
    return f"{h}시간 {m:02d}분"


def dir_tag(direction):
    return "🟢 롱" if direction == "LONG" else "🔴 숏"


def chart_link(symbol):
    return f"https://www.gate.io/futures/USDT/{symbol}"


def pct_move(direction, entry, price):
    if entry <= 0:
        return 0.0
    if direction == "LONG":
        return (price - entry) / entry * 100
    return (entry - price) / entry * 100


def r_move(direction, entry, price, risk):
    if risk <= 0:
        return 0.0
    if direction == "LONG":
        return (price - entry) / risk
    return (entry - price) / risk


def send_telegram_message(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials missing.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message[:4000],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code != 200:
            print(f"Failed to send telegram message: {response.text}")
            return False
        return True
    except Exception as e:
        print(f"Error sending telegram message: {e}")
        return False


# ──────────────────────────── 상태 저장/불러오기 ────────────────────────────
def default_state():
    return {
        "active_slots": {},
        "stats": {
            "wins": 0, "draws": 0, "losses": 0, "total": 0,
            "r_count": 0, "sum_r": 0.0, "win_r": 0.0, "loss_r": 0.0,
            "recent_r": [],
        },
        "closed_symbols": {},
        "fail_count": 0,
        "last_report_date": "",
    }


def load_state():
    state = default_state()
    if not os.path.exists(STATE_FILE):
        return state
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"Error loading state file: {e}")
        try:
            os.replace(STATE_FILE, STATE_FILE + ".corrupt")
        except Exception:
            pass
        return state

    if not isinstance(data, dict):
        return state
    if isinstance(data.get("active_slots"), dict):
        state["active_slots"] = data["active_slots"]
    if isinstance(data.get("stats"), dict):
        state["stats"].update(data["stats"])
    closed = data.get("closed_symbols", {})
    if isinstance(closed, list):
        state["closed_symbols"] = {sym: utcnow().isoformat() for sym in closed}
    elif isinstance(closed, dict):
        state["closed_symbols"] = closed
    try:
        state["fail_count"] = int(data.get("fail_count", 0))
    except Exception:
        state["fail_count"] = 0
    state["last_report_date"] = str(data.get("last_report_date", ""))
    return state


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def append_trade_csv(row):
    exists = os.path.exists(TRADES_FILE) and os.path.getsize(TRADES_FILE) > 0
    with open(TRADES_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if not exists:
            writer.writeheader()
        writer.writerow(row)


# ──────────────────────────── Gate.io API ────────────────────────────
def api_get(path, params=None):
    try:
        response = requests.get(f"{BASE_URL}{path}", params=params, timeout=10)
        if response.status_code == 200:
            return response.json()
        print(f"API error {response.status_code} {path}: {response.text[:200]}")
    except Exception as e:
        print(f"API request failed {path}: {e}")
    return None


def fetch_tickers():
    data = api_get("/tickers")
    return data if isinstance(data, list) else []


def parse_candles(raw):
    out = []
    for c in raw or []:
        try:
            out.append({
                "t": int(c["t"]),
                "o": float(c["o"]),
                "h": float(c["h"]),
                "l": float(c["l"]),
                "c": float(c["c"]),
            })
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda x: x["t"])
    return out


def fetch_completed_5m(symbol, from_ts, now_ts):
    if from_ts >= now_ts:
        return []
    raw = api_get("/candlesticks", {
        "contract": symbol, "interval": "5m",
        "from": int(from_ts), "to": int(now_ts),
    })
    return [c for c in parse_candles(raw)
            if c["t"] >= from_ts and c["t"] + 300 <= now_ts]


# ──────────────────────────── 지표 ────────────────────────────
def wilder_rsi(closes, period=RSI_PERIOD):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calc_atr(candles, period=ATR_PERIOD):
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        high, low, prev_close = candles[i]["h"], candles[i]["l"], candles[i - 1]["c"]
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return sum(trs[-period:]) / period


def analyze_signal(symbol, direction, now_ts):
    raw = api_get("/candlesticks", {"contract": symbol, "interval": "1h", "limit": 60})
    candles = [c for c in parse_candles(raw) if c["t"] + 3600 <= now_ts]
    if len(candles) < 20:
        return None

    last = candles[-1]
    total_range = last["h"] - last["l"]
    if total_range <= 0:
        return None
    body_ratio = abs(last["c"] - last["o"]) / total_range
    if body_ratio < BODY_RATIO_MIN:
        return None
    if direction == "LONG" and not last["c"] > last["o"]:
        return None
    if direction == "SHORT" and not last["c"] < last["o"]:
        return None

    rsi = wilder_rsi([c["c"] for c in candles])
    if rsi is None:
        return None
    if direction == "LONG" and rsi < 50:
        return None
    if direction == "SHORT" and rsi > 50:
        return None

    atr = calc_atr(candles)
    if not atr or atr <= 0:
        return None
    return {"rsi": rsi, "atr": atr, "body_ratio": body_ratio}


# ──────────────────────────── 분류/레버리지/테마 ────────────────────────────
STOCK_BASES = {"SKHYNIX", "TSLA", "AAPL", "NVDA", "MSFT", "AMZN", "META", "NFLX",
               "GOOGL", "BABA", "AMD", "INTC", "PLTR", "COIN", "MSTR",
               "SOXL", "SOXS", "TQQQ", "SQQQ"}
INDEX_BASES = {"SPX", "NAS", "NDX", "DJI", "US30", "US100", "NIKKEI", "VIX", "DAX"}
COMMODITY_BASES = {"XAU", "XAUT", "XAG", "XAGT", "OIL", "USOIL", "UKOIL", "GOLD",
                   "SILVER", "CRUDE", "BRENT", "COPPER"}
FOREX_BASES = {"EUR", "GBP", "JPY", "AUD", "CHF", "NZD", "CAD"}


def get_asset_category(symbol):
    base = symbol.upper().split("_")[0]
    if base in STOCK_BASES:
        return "주식"
    if base in INDEX_BASES:
        return "지수"
    if base in COMMODITY_BASES:
        return "원자재"
    if base in FOREX_BASES:
        return "외환"
    return "코인"


THEMES = {
    "반도체": {"SOXL": 1, "SOXS": -1, "SKHYNIX": 1, "NVDA": 1, "AMD": 1, "INTC": 1,
             "MU": 1, "TSM": 1},
    "미국지수": {"NAS": 1, "NDX": 1, "SPX": 1, "US100": 1, "US30": 1, "DJI": 1,
             "TQQQ": 1, "SQQQ": -1},
    "금": {"XAU": 1, "XAUT": 1, "GOLD": 1, "PAXG": 1},
}


def theme_exposure(symbol, direction):
    base = symbol.upper().split("_")[0]
    for theme, members in THEMES.items():
        if base in members:
            return theme, members[base] * (1 if direction == "LONG" else -1)
    return None, 0


def count_theme_slots(active, theme, sign):
    count = 0
    for sym, info in active.items():
        t, s = theme_exposure(sym, info.get("direction", "LONG"))
        if t == theme and s == sign:
            count += 1
    return count


def calculate_recommended_leverage(category, change_24h):
    abs_change = abs(change_24h)
    if category == "코인":
        return 5 if abs_change > 10.0 else (7 if abs_change > 6.0 else 10)
    return {"주식": 8, "원자재": 12, "외환": 20, "지수": 15}.get(category, 10)


# ──────────────────────────── 통계 (무승부 분리 반영) ────────────────────────────
def record_stats(stats, net_r, outcome):
    stats["total"] += 1
    stats[{"win": "wins", "draw": "draws", "loss": "losses"}[outcome]] += 1
    stats["r_count"] += 1
    stats["sum_r"] += net_r
    if net_r > 0:
        stats["win_r"] += net_r
    else:
        stats["loss_r"] += -net_r
    stats["recent_r"] = (stats.get("recent_r", []) + [round(net_r, 3)])[-50:]


def build_stats_line(stats):
    total = stats.get("total", 0)
    wins, draws, losses = stats.get("wins", 0), stats.get("draws", 0), stats.get("losses", 0)
    wr_all = round(wins / total * 100, 1) if total else 0.0
    
    # 무승부 제외 순수 승패 기준 승률
    decisive = wins + losses
    wr_dec = round(wins / decisive * 100, 1) if decisive else 0.0
    
    lines = [
        "📈 <b>[누적 통계]</b>",
        f"<code>{total}전 {wins}승 {draws}무 {losses}패</code>",
        f"<code>순수 승률(무승부 제외) {wr_dec}% ({wins}승 {losses}패) | 전체 포함 {wr_all}%</code>",
    ]
    n = stats.get("r_count", 0)
    if n > 0:
        avg_r = stats["sum_r"] / n
        pf = f"{stats['win_r'] / stats['loss_r']:.2f}" if stats.get("loss_r", 0) > 0 else "-"
        recent = stats.get("recent_r", [])[-20:]
        lines.append(
            f"<code>평균 R {avg_r:+.2f} | PF {pf} | 최근 {len(recent)}전 {sum(recent):+.1f}R"
            f" (R 기록 {n}전)</code>"
        )
    else:
        lines.append("<code>R 통계: 기록 대기 중 (청산이 쌓이면 표시)</code>")
    return "\n".join(lines)


# ──────────────────────────── 포지션 관리 ────────────────────────────
def migrate_slot(info):
    entry = float(info.get("entry_price", 0) or 0)
    atr = float(info.get("atr", info.get("risk", entry) / ATR_SL))
    if "risk" not in info:
        info["risk"] = abs(float(info.get("tp1", entry)) - entry)
    info.setdefault("direction", "LONG")
    info.setdefault("leverage", 10)
    info.setdefault("tp1_reached", False)
    info.setdefault("tp2_reached", False)
    info.setdefault("max_fav", entry)
    info.setdefault("max_adv", entry)
    if "last_candle_ts" not in info:
        dt = parse_iso(info.get("entry_time", ""), utcnow())
        info["last_candle_ts"] = int(dt.timestamp()) // 300 * 300
        
    # 수익형 본전 방어 타겟 가격 계산 (진입가 + 0.2 ATR)
    profit_lock_target = entry + (atr * PROFIT_LOCK_ATR) if info["direction"] == "LONG" else entry - (atr * PROFIT_LOCK_ATR)

    if info["direction"] == "LONG":
        if info["tp1_reached"]:
            info["stop_loss"] = max(info["stop_loss"], profit_lock_target)
        if info["tp2_reached"]:
            info["stop_loss"] = max(info["stop_loss"], info["tp1"])
    else:
        if info["tp1_reached"]:
            info["stop_loss"] = min(info["stop_loss"], profit_lock_target)
        if info["tp2_reached"]:
            info["stop_loss"] = min(info["stop_loss"], info["tp1"])


def step(info, high, low):
    direction, entry = info["direction"], info["entry_price"]
    atr = float(info.get("atr", info.get("risk", entry) / ATR_SL))
    profit_lock_target = entry + (atr * PROFIT_LOCK_ATR) if direction == "LONG" else entry - (atr * PROFIT_LOCK_ATR)
    
    events = []
    if direction == "LONG":
        info["max_fav"] = max(info["max_fav"], high)
        info["max_adv"] = min(info["max_adv"], low)
        if low <= info["stop_loss"]:
            return events, ("STOP", info["stop_loss"])
        if high >= info["tp3"]:
            return events, ("TP3", info["tp3"])
        if high >= info["tp1"] and not info["tp1_reached"]:
            info["tp1_reached"] = True
            info["stop_loss"] = max(info["stop_loss"], profit_lock_target)
            events.append("TP1")
        if high >= info["tp2"] and not info["tp2_reached"]:
            info["tp2_reached"] = True
            info["stop_loss"] = max(info["stop_loss"], info["tp1"])
            events.append("TP2")
    else:
        info["max_fav"] = min(info["max_fav"], low)
        info["max_adv"] = max(info["max_adv"], high)
        if high >= info["stop_loss"]:
            return events, ("STOP", info["stop_loss"])
        if low <= info["tp3"]:
            return events, ("TP3", info["tp3"])
        if low <= info["tp1"] and not info["tp1_reached"]:
            info["tp1_reached"] = True
            info["stop_loss"] = min(info["stop_loss"], profit_lock_target)
            events.append("TP1")
        if low <= info["tp2"] and not info["tp2_reached"]:
            info["tp2_reached"] = True
            info["stop_loss"] = min(info["stop_loss"], info["tp1"])
            events.append("TP2")
    return events, None


def stop_reason(info):
    if info.get("tp2_reached"):
        return "TP1_PROTECT"
    if info.get("tp1_reached"):
        return "PROFIT_LOCK"  # 본전 대신 약수익 방어선 체결
    return "SL"


def finalize_trade(state, symbol, info, reason, exit_price, now):
    direction, entry, risk = info["direction"], info["entry_price"], info["risk"]
    gross_r = r_move(direction, entry, exit_price, risk)
    net_r = gross_r - (FEE_ROUNDTRIP * entry / risk)
    if net_r > DRAW_BAND_R:
        outcome = "win"
    elif net_r < -DRAW_BAND_R:
        outcome = "loss"
    else:
        outcome = "draw"

    entry_dt = parse_iso(info.get("entry_time", ""), now)
    hours = max((now - entry_dt).total_seconds() / 3600, 0)
    mfe_r = r_move(direction, entry, info["max_fav"], risk)
    mae_r = r_move(direction, entry, info["max_adv"], risk)

    record_stats(state["stats"], net_r, outcome)
    append_trade_csv({
        "closed_at": now.isoformat(),
        "symbol": symbol,
        "direction": direction,
        "category": get_asset_category(symbol),
        "leverage": info.get("leverage", ""),
        "entry_time": info.get("entry_time", ""),
        "entry_price": entry,
        "exit_price": exit_price,
        "exit_reason": reason,
        "holding_hours": round(hours, 2),
        "gross_r": round(gross_r, 3),
        "net_r": round(net_r, 3),
        "mfe_r": round(mfe_r, 3),
        "mae_r": round(mae_r, 3),
    })
    state["closed_symbols"][symbol] = now.isoformat()
    del state["active_slots"][symbol]
    return {"reason": reason, "exit_price": exit_price, "net_r": net_r,
            "outcome": outcome, "hours": hours}


def build_event_message(symbol, info, event):
    direction, entry = info["direction"], info["entry_price"]
    lev = info.get("leverage", 1)
    level = info["tp1"] if event == "TP1" else info["tp2"]
    pct = pct_move(direction, entry, level)
    if event == "TP1":
        title = "🎯 <b>TP1 도달 (수익형 본전 방어선 확보)</b>"
        action = f"🔒 스탑을 약수익 구간(<code>+{PROFIT_LOCK_ATR} ATR</code>)으로 이동\n   손실 차단 및 소액 수익 확보 완료"
    else:
        title = "🎯🎯 <b>TP2 도달</b>"
        action = (f"🔒 스탑을 TP1(<code>{fmt_price(info['tp1'])}</code>)으로 이동\n"
                  f"   안전한 수익 확보, TP3(<code>{fmt_price(info['tp3'])}</code>) 목표로 홀딩")
    return (
        f"{title} | {dir_tag(direction)}\n"
        f"<a href='{chart_link(symbol)}'>[{get_asset_category(symbol)}] {symbol}</a>\n\n"
        f"진입 <code>{fmt_price(entry)}</code> → <code>{fmt_price(level)}</code>\n"
        f"수익 <code>{pct:+.2f}%</code> ({lev}배: <code>{pct * lev:+.1f}%</code>)\n\n"
        f"{action}"
    )


def build_close_message(symbol, info, trade):
    direction, entry = info["direction"], info["entry_price"]
    lev = info.get("leverage", 1)
    exit_price = trade["exit_price"]
    pct = pct_move(direction, entry, exit_price)
    reason = trade["reason"]
    head = {
        "SL": ("🛑 <b>손절 청산</b>", ""),
        "PROFIT_LOCK": ("💰 <b>수익 방어 청산 (Profit-Lock)</b>", "TP1 도달 후 되돌아와 약수익 스탑 체결\n"),
        "TP1_PROTECT": ("✅ <b>수익 청산 (TP1 방어)</b>", "TP2 도달 후 스탑(TP1)에서 체결\n"),
        "TP3": ("🏆 <b>TP3 최종 목표 도달</b>", ""),
        "ROTATE": ("🔄 <b>회전 청산</b>", "장기 보유 수익 확보, 슬롯 회전\n"),
        "TIME_STOP": ("⏳ <b>타임스탑 청산</b>", f"{TIME_STOP_HOURS}시간 초과, 슬롯 회전을 위해 청산\n"),
    }[reason]
    outcome_label = {"win": "승", "draw": "무", "loss": "패"}[trade["outcome"]]
    return (
        f"{head[0]} | {dir_tag(direction)}\n"
        f"<a href='{chart_link(symbol)}'>[{get_asset_category(symbol)}] {symbol}</a>\n\n"
        f"진입 <code>{fmt_price(entry)}</code> → 청산 <code>{fmt_price(exit_price)}</code>\n"
        f"{head[1]}"
        f"가격 변동 <code>{pct:+.2f}%</code> ({lev}배: <code>{pct * lev:+.1f}%</code>)\n\n"
        f"결과 <code>{trade['net_r']:+.2f}R</code> (수수료 포함) | <b>{outcome_label}</b>\n"
        f"보유 <code>{fmt_hours(trade['hours'])}</code>"
    )


def manage_positions(state, tickers, now):
    active = state["active_slots"]
    now_ts = int(now.timestamp())
    changed = False
    alerts = []

    for symbol, info in list(active.items()):
        try:
            migrate_slot(info)
            entry, risk = info["entry_price"], info["risk"]
            if entry <= 0 or risk <= 0:
                continue

            if symbol not in tickers:
                del active[symbol]
                alerts.append(
                    f"🧹 <b>슬롯 정리</b>\n<code>{symbol}</code>은(는) 선물 시장에 없는 종목이라 "
                    "통계 없이 슬롯에서 제거했습니다."
                )
                changed = True
                continue

            close = None
            for candle in fetch_completed_5m(symbol, info["last_candle_ts"] + 300, now_ts):
                events, close = step(info, candle["h"], candle["l"])
                for ev in events:
                    alerts.append(build_event_message(symbol, info, ev))
                    changed = True
                info["last_candle_ts"] = candle["t"]
                if close:
                    break

            price = tickers.get(symbol)
            if not close and price:
                events, close = step(info, price, price)
                for ev in events:
                    alerts.append(build_event_message(symbol, info, ev))
                    changed = True

            if close:
                kind, exit_price = close
                reason = stop_reason(info) if kind == "STOP" else "TP3"
                trade = finalize_trade(state, symbol, info, reason, exit_price, now)
                alerts.append(build_close_message(symbol, info, trade))
                changed = True
                continue

            entry_dt = parse_iso(info.get("entry_time", ""), now)
            holding_h = (now - entry_dt).total_seconds() / 3600
            if holding_h >= TIME_STOP_HOURS:
                if price:
                    trade = finalize_trade(state, symbol, info, "TIME_STOP", price, now)
                    alerts.append(build_close_message(symbol, info, trade))
                else:
                    del active[symbol]
                    alerts.append(
                        f"⚠️ <b>슬롯 정리</b>\n<code>{symbol}</code> 가격 조회가 불가능해 "
                        f"{fmt_hours(holding_h)} 보유 포지션을 통계 없이 제거했습니다."
                    )
                changed = True
            elif holding_h >= ROTATE_HOURS and price:
                if pct_move(info["direction"], entry, price) >= ROTATE_MIN_PROFIT_PCT:
                    trade = finalize_trade(state, symbol, info, "ROTATE", price, now)
                    alerts.append(build_close_message(symbol, info, trade))
                    changed = True
        except Exception as e:
            print(f"Error managing {symbol}: {e}")

    for alert in alerts:
        send_telegram_message(alert)
    return changed


def identify_new_signals(state, raw_tickers, now, start_time):
    active = state["active_slots"]
    closed = state["closed_symbols"]
    now_ts = int(now.timestamp())
    changed = False

    candidates = []
    for t in raw_tickers:
        symbol = t.get("contract", "")
        if not symbol.endswith("_USDT") or LEVERAGED_TOKEN_RE.search(symbol):
            continue
        if symbol in active or symbol in closed:
            continue
        try:
            last_price = float(t.get("last", 0))
            change = float(t.get("change_percentage", 0))
            volume = float(t.get("volume_24h_quote") or t.get("volume_24h_settle") or 0)
        except (TypeError, ValueError):
            continue
        if last_price <= 0 or volume < MIN_QUOTE_VOLUME or abs(change) < MIN_CHANGE_PCT:
            continue
        candidates.append((volume, symbol, last_price, change))

    candidates.sort(reverse=True)
    for volume, symbol, last_price, change in candidates[:MAX_CANDIDATES]:
        if len(active) >= MAX_SLOTS:
            break
        if time.time() - start_time > SCAN_BUDGET_SEC:
            print("스캔 시간 예산 초과, 남은 후보는 다음 실행으로 넘김")
            break
        try:
            direction = "LONG" if change > 0 else "SHORT"
            theme, theme_sign = theme_exposure(symbol, direction)
            if theme and count_theme_slots(active, theme, theme_sign) >= MAX_PER_THEME:
                continue
            analysis = analyze_signal(symbol, direction, now_ts)
            if not analysis:
                continue

            atr = analysis["atr"]
            risk = atr * ATR_SL
            sign = 1 if direction == "LONG" else -1
            tp1, tp2, tp3 = (last_price + sign * atr * m for m in ATR_TP_MULTS)
            sl = last_price - sign * risk
            if sl <= 0 or tp1 <= 0 or (direction == "SHORT" and tp3 <= 0):
                continue

            sl_frac = risk / last_price
            if sl_frac * 100 > MAX_SL_PCT:
                print(f"{symbol} 손절 거리 {sl_frac * 100:.1f}% > {MAX_SL_PCT}%, 제외")
                continue

            category = get_asset_category(symbol)
            leverage = calculate_recommended_leverage(category, change)
            leverage = max(1, min(leverage, int(MAX_MARGIN_LOSS / sl_frac) or 1))

            notional_frac = (RISK_PER_TRADE_PCT / 100) / sl_frac
            margin_frac = min(notional_frac / leverage, 1.0)
            actual_loss_pct = margin_frac * leverage * sl_frac * 100

            active[symbol] = {
                "direction": direction,
                "entry_price": last_price,
                "entry_time": now.isoformat(),
                "leverage": leverage,
                "risk": risk,
                "atr": atr,  # 수익형 본전 방어 계산용 저장
                "tp1": tp1, "tp2": tp2, "tp3": tp3,
                "stop_loss": sl,
                "initial_stop": sl,
                "tp1_reached": False,
                "tp2_reached": False,
                "max_fav": last_price,
                "max_adv": last_price,
                "last_candle_ts": now_ts // 300 * 300,
            }
            changed = True

            def pct_of(price):
                return pct_move(direction, last_price, price)

            msg = (
                f"🚀 <b>신규 시그널 (수익 방어형)</b> | {dir_tag(direction)}\n"
                f"<a href='{chart_link(symbol)}'>[{category}] {symbol}</a>\n\n"
                f"진입가 <code>{fmt_price(last_price)}</code>\n"
                f"레버리지 <code>{leverage}배</code>\n\n"
                "🎯 <b>목표가 (수익형 본전 방어 적용)</b>\n"
                f"TP1 <code>{fmt_price(tp1)} ({pct_of(tp1):+.2f}%)</code> → 도달 시 스탑을 <b>+{PROFIT_LOCK_ATR} ATR</b>로 락\n"
                f"TP2 <code>{fmt_price(tp2)} ({pct_of(tp2):+.2f}%)</code> → 2.5R\n"
                f"TP3 <code>{fmt_price(tp3)} ({pct_of(tp3):+.2f}%)</code> → 4.0R\n"
                f"🛑 SL <code>{fmt_price(sl)} ({pct_of(sl):+.2f}%)</code> → -1.5R\n\n"
                f"⚠️ 손절 시 증거금 손실 <code>-{sl_frac * leverage * 100:.1f}%</code> ({leverage}배 기준)\n"
                f"💰 권장 증거금: 계좌의 약 <code>{margin_frac * 100:.1f}%</code>\n\n"
                "📌 <b>진입 근거</b>\n"
                f"• 24시간 변동 <code>{change:+.1f}%</code> / 거래대금 <code>{volume / 1e6:.1f}M USDT</code>\n"
                f"• RSI(14) <code>{analysis['rsi']:.1f}</code> / 직전 1시간봉 몸통 <code>{analysis['body_ratio'] * 100:.0f}%</code>\n"
                f"• ATR <code>{atr / last_price * 100:.2f}%</code>"
            )
            send_telegram_message(msg)
        except Exception as e:
            print(f"Error processing candidate {symbol}: {e}")
            continue
    return changed


def build_slot_status_message(state, tickers):
    active = state["active_slots"]
    symbols = list(active.keys())
    longs = sum(1 for s in symbols if active[s].get("direction") == "LONG")
    shorts = len(symbols) - longs
    lines = [f"<b>📊 슬롯 현황 ({len(symbols)} / {MAX_SLOTS}) | 🟢 롱 {longs} / 🔴 숏 {shorts}</b>\n"]

    for i in range(MAX_SLOTS):
        icon = NUM_EMOJI[i] if i < len(NUM_EMOJI) else f"{i + 1}."
        if i >= len(symbols):
            lines.append(f"{icon} (빈 슬롯)")
            continue
        sym = symbols[i]
        info = active[sym]
        migrate_slot(info)
        direction, entry = info["direction"], info["entry_price"]
        lev = info.get("leverage", 1)
        price = tickers.get(sym, entry)
        pct = pct_move(direction, entry, price)
        entry_dt = parse_iso(info.get("entry_time", ""), utcnow())
        hold = fmt_hours(max((utcnow() - entry_dt).total_seconds() / 3600, 0))
        icon_dir = "🟢" if direction == "LONG" else "🔴"

        def dist(level):
            return abs(level - price) / price * 100 if price > 0 else 0.0

        if info["tp2_reached"]:
            status = f"🎯🎯 TP2 도달 | 스탑 TP1 방어 | TP3까지 {dist(info['tp3']):.1f}%"
        elif info["tp1_reached"]:
            status = f"🎯 TP1 도달 | 스탑 수익 방어(+{PROFIT_LOCK_ATR}ATR) | TP2까지 {dist(info['tp2']):.1f}%"
        else:
            status = f"📈 홀딩 | TP1까지 {dist(info['tp1']):.1f}% / SL까지 {dist(info['stop_loss']):.1f}%"

        lines.append(
            f"{icon} {icon_dir} <a href='{chart_link(sym)}'>[{get_asset_category(sym)}] {sym}</a>\n"
            f"   <code>{pct:+.2f}% ({lev}배 {pct * lev:+.1f}%) | 보유 {hold}</code>\n"
            f"   <code>{status}</code>"
        )
    lines.append("\n" + build_stats_line(state["stats"]))
    return "\n".join(lines)


def build_daily_report(now, stats):
    since = now - timedelta(hours=24)
    rows = []
    if os.path.exists(TRADES_FILE):
        with open(TRADES_FILE, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                dt = parse_iso(row.get("closed_at", ""))
                if dt and dt >= since:
                    rows.append(row)

    lines = [f"📋 <b>일일 리포트 (수익 방어 분석)</b> | {now.strftime('%m/%d')}\n"]
    if not rows:
        lines.append("최근 24시간 청산 거래가 없습니다.")
    else:
        nets = [float(r["net_r"]) for r in rows]
        wins = sum(1 for n in nets if n > DRAW_BAND_R)
        losses = sum(1 for n in nets if n < -DRAW_BAND_R)
        draws = len(nets) - wins - losses
        lines.append(f"최근 24시간 <code>{len(nets)}전 {wins}승 {draws}무 {losses}패 | 합계 {sum(nets):+.2f}R</code>\n")

        lines.append("<b>청산 유형별</b>")
        for key, label in REASON_LABEL.items():
            vals = [float(r["net_r"]) for r in rows if r["exit_reason"] == key]
            if vals:
                lines.append(f"• {label} <code>{len(vals)}건 평균 {sum(vals) / len(vals):+.2f}R</code>")

        by_dir = {"LONG": 0.0, "SHORT": 0.0}
        for r in rows:
            by_dir[r["direction"]] = by_dir.get(r["direction"], 0.0) + float(r["net_r"])
        lines.append(f"\n방향별 <code>롱 {by_dir['LONG']:+.2f}R / 숏 {by_dir['SHORT']:+.2f}R</code>")

    lines.append("\n" + build_stats_line(stats))
    return "\n".join(lines)


def maybe_send_daily_report(state, now):
    if not DAILY_REPORT_ENABLED:
        return False
    today = now.strftime("%Y-%m-%d")
    if now.hour < DAILY_REPORT_HOUR_UTC or state.get("last_report_date") == today:
        return False
    send_telegram_message(build_daily_report(now, state["stats"]))
    state["last_report_date"] = today
    return True


def run_bot():
    start_time = time.time()
    print("Bot 시작과 함께 수익 방어 로직 실행...")
    state = load_state()
    now = utcnow()

    raw_tickers = fetch_tickers()
    if not raw_tickers:
        print("티커 정보 불러오기 실패")
        state["fail_count"] += 1
        n = state["fail_count"]
        if n == FAIL_ALERT_THRESHOLD or (n > FAIL_ALERT_THRESHOLD and (n - FAIL_ALERT_THRESHOLD) % 12 == 0):
            send_telegram_message(
                "⚠️ <b>봇 오류 경고</b>\n"
                f"Gate.io 시세 조회가 {n}회 연속 실패했습니다.\n"
                "포지션 TP/SL 감시가 중단된 상태입니다."
            )
        save_state(state)
        return
    state["fail_count"] = 0

    tickers = {}
    for t in raw_tickers:
        try:
            tickers[t["contract"]] = float(t.get("last", 0))
        except (KeyError, TypeError, ValueError):
            continue

    cooldown = timedelta(hours=COOLDOWN_HOURS)
    state["closed_symbols"] = {
        sym: ts for sym, ts in state["closed_symbols"].items()
        if (now - parse_iso(ts, now - cooldown * 2)) < cooldown
    }

    changed_positions = manage_positions(state, tickers, now)

    changed_new = False
    if len(state["active_slots"]) < MAX_SLOTS:
        changed_new = identify_new_signals(state, raw_tickers, now, start_time)

    if changed_positions or changed_new:
        send_telegram_message(build_slot_status_message(state, tickers))
        print("Slot status updated and sent to Telegram.")
    else:
        print("No slot changes. Status message skipped.")

    maybe_send_daily_report(state, now)
    save_state(state)
    print("Bot 사이클 종료")


if __name__ == "__main__":
    run_bot()
