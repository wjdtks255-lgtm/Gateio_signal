#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Gate.io USDT 선물 롱/숏 시그널 봇
텔레그램 알림 전용 / 자동 주문 없음

최종 전적 규칙:
TP3 = 승
TP2 이후 TP1 방어 종료 = 승
TP1 이후 Profit-Lock 종료 = 무
TP1 이전 SL = 패
ROTATE / TIME_STOP = TP 도달 단계 우선 분류
승률 = 승 / (승 + 패) * 100
"""

import csv
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

import requests


# ============================================================
# 설정
# ============================================================

TELEGRAM_TOKEN = os.getenv("GATE_TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("GATE_TELEGRAM_CHAT_ID")

STATE_FILE = "gate_state.json"
TRADES_FILE = "trades.csv"
BASE_URL = "https://api.gateio.ws/api/v4/futures/usdt"

MAX_SLOTS = 8

MIN_CHANGE_PCT = 4.5
MIN_QUOTE_VOLUME = 500_000

RSI_PERIOD = 14
ATR_PERIOD = 14
BODY_RATIO_MIN = 0.35

LEVERAGED_TOKEN_RE = re.compile(r"\d+[LS]_USDT$")

ATR_SL = 1.5
ATR_TP_MULTS = (1.0, 2.5, 4.0)
MAX_SL_PCT = 6.0
MAX_PER_THEME = 1

FEE_ROUNDTRIP = 0.001
PROFIT_LOCK_ATR = 0.2

RISK_PER_TRADE_PCT = 1.5
MAX_MARGIN_LOSS = 0.5

ROTATE_HOURS = 18
ROTATE_MIN_PROFIT_PCT = 0.5
TIME_STOP_HOURS = 24
COOLDOWN_HOURS = 6

DRAW_BAND_R = 0.05

MAX_CANDIDATES = 40
SCAN_BUDGET_SEC = 150

FAIL_ALERT_THRESHOLD = 3

DAILY_REPORT_ENABLED = True
DAILY_REPORT_HOUR_UTC = 0

CSV_FIELDS = [
    "closed_at", "symbol", "direction", "category", "leverage",
    "entry_time", "entry_price", "exit_price", "exit_reason",
    "holding_hours", "gross_r", "net_r", "mfe_r", "mae_r",
    "leverage_pct", "outcome"
]

REASON_LABEL = {
    "SL": "손절",
    "PROFIT_LOCK": "수익 방어",
    "TP1_PROTECT": "TP1 방어",
    "TP3": "TP3",
    "ROTATE": "회전",
    "TIME_STOP": "타임스탑",
}

NUM_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣"]


# ============================================================
# 공통 유틸
# ============================================================

def utcnow():
    return datetime.now(timezone.utc)


def parse_iso(value, default=None):
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return default


def fmt_price(price):
    if price >= 1000:
        return f"{price:.2f}"
    if price >= 1:
        return f"{price:.4f}"
    if price >= 0.01:
        return f"{price:.5f}"
    return f"{price:.8f}"


def fmt_hours(hours):
    hours = max(0, hours)
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
            print("Telegram error:", response.text)
            return False

        return True

    except Exception as exc:
        print("Telegram exception:", exc)
        return False


# ============================================================
# 상태 저장 및 불러오기
# ============================================================

def default_state():
    return {
        "active_slots": {},
        "stats": {
            "wins": 0,
            "draws": 0,
            "losses": 0,
            "total": 0,
            "r_count": 0,
            "sum_r": 0.0,
            "win_r": 0.0,
            "loss_r": 0.0,
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
    except Exception as exc:
        print("State load error:", exc)

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
        state["closed_symbols"] = {
            symbol: {
                "time": utcnow().isoformat(),
                "direction": "LONG",
                "reason": "SL",
            }
            for symbol in closed
        }

    elif isinstance(closed, dict):
        for symbol, value in closed.items():
            if isinstance(value, str):
                state["closed_symbols"][symbol] = {
                    "time": value,
                    "direction": "LONG",
                    "reason": "SL",
                }
            elif isinstance(value, dict):
                state["closed_symbols"][symbol] = value

    try:
        state["fail_count"] = int(data.get("fail_count", 0))
    except Exception:
        state["fail_count"] = 0

    state["last_report_date"] = str(
        data.get("last_report_date", "")
    )

    return state


def save_state(state):
    tmp = STATE_FILE + ".tmp"

    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

    os.replace(tmp, STATE_FILE)


def append_trade_csv(row):
    exists = (
        os.path.exists(TRADES_FILE)
        and os.path.getsize(TRADES_FILE) > 0
    )

    with open(TRADES_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)

        if not exists:
            writer.writeheader()

        writer.writerow(row)


# ============================================================
# Gate.io API
# ============================================================

def api_get(path, params=None):
    try:
        response = requests.get(
            f"{BASE_URL}{path}",
            params=params,
            timeout=10,
        )

        if response.status_code == 200:
            return response.json()

        print(
            f"API error {response.status_code} "
            f"{path}: {response.text[:200]}"
        )

    except Exception as exc:
        print(f"API request failed {path}: {exc}")

    return None


def fetch_tickers():
    data = api_get("/tickers")
    return data if isinstance(data, list) else []


def parse_candles(raw):
    result = []

    for candle in raw or []:
        try:
            result.append({
                "t": int(candle["t"]),
                "o": float(candle["o"]),
                "h": float(candle["h"]),
                "l": float(candle["l"]),
                "c": float(candle["c"]),
            })
        except (KeyError, TypeError, ValueError):
            continue

    result.sort(key=lambda x: x["t"])
    return result


def fetch_completed_5m(symbol, from_ts, now_ts):
    if from_ts >= now_ts:
        return []

    raw = api_get(
        "/candlesticks",
        {
            "contract": symbol,
            "interval": "5m",
            "from": int(from_ts),
            "to": int(now_ts),
        },
    )

    return [
        candle for candle in parse_candles(raw)
        if candle["t"] >= from_ts
        and candle["t"] + 300 <= now_ts
    ]


# ============================================================
# 기술적 지표
# ============================================================

def wilder_rsi(closes, period=RSI_PERIOD):
    if len(closes) < period + 1:
        return None

    gains = []
    losses = []

    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (
            avg_gain * (period - 1) + gains[i]
        ) / period

        avg_loss = (
            avg_loss * (period - 1) + losses[i]
        ) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calc_atr(candles, period=ATR_PERIOD):
    if len(candles) < period + 1:
        return None

    trs = []

    for i in range(1, len(candles)):
        high = candles[i]["h"]
        low = candles[i]["l"]
        prev_close = candles[i - 1]["c"]

        trs.append(
            max(
                high - low,
                abs(high - prev_close),
                abs(low - prev_close),
            )
        )

    return sum(trs[-period:]) / period


def calc_ma(closes, period=50):
    if len(closes) < period:
        return sum(closes) / len(closes) if closes else None

    return sum(closes[-period:]) / period


def check_order_block_retest(candles, direction, current_price):
    if len(candles) < 5:
        return None, None

    last = candles[-1]

    for i in range(
        len(candles) - 2,
        max(0, len(candles) - 6),
        -1,
    ):
        candle = candles[i]

        if direction == "LONG":
            if candle["o"] > candle["c"]:
                zone_low = candle["l"]
                zone_high = candle["o"]

                if (
                    zone_low * 0.99
                    <= current_price
                    <= zone_high * 1.02
                ):
                    return zone_low, zone_high

        else:
            if candle["c"] > candle["o"]:
                zone_low = candle["o"]
                zone_high = candle["h"]

                if (
                    zone_low * 0.98
                    <= current_price
                    <= zone_high * 1.01
                ):
                    return zone_low, zone_high

    return last["l"], last["h"]


def analyze_signal(symbol, direction, now_ts, current_price):
    raw = api_get(
        "/candlesticks",
        {
            "contract": symbol,
            "interval": "1h",
            "limit": 60,
        },
    )

    candles = [
        c for c in parse_candles(raw)
        if c["t"] + 3600 <= now_ts
    ]

    if len(candles) < 50:
        return None

    closes = [c["c"] for c in candles]

    ma50 = calc_ma(closes, 50)

    if ma50:
        if direction == "LONG" and current_price < ma50:
            return None

        if direction == "SHORT" and current_price > ma50:
            return None

    last = candles[-1]

    total_range = last["h"] - last["l"]

    if total_range <= 0:
        return None

    body_ratio = abs(last["c"] - last["o"]) / total_range

    if body_ratio < BODY_RATIO_MIN:
        return None

    if direction == "LONG" and last["c"] <= last["o"]:
        return None

    if direction == "SHORT" and last["c"] >= last["o"]:
        return None

    rsi = wilder_rsi(closes)

    if rsi is None:
        return None

    if direction == "LONG" and rsi < 50:
        return None

    if direction == "SHORT" and rsi > 50:
        return None

    atr = calc_atr(candles)

    if not atr or atr <= 0:
        return None

    zone_low, zone_high = check_order_block_retest(
        candles,
        direction,
        current_price,
    )

    if zone_low is None or zone_high is None:
        return None

    return {
        "rsi": rsi,
        "atr": atr,
        "body_ratio": body_ratio,
        "ob_low": zone_low,
        "ob_high": zone_high,
    }


# ============================================================
# 종목 분류 및 테마
# ============================================================

STOCK_BASES = {
    "SKHYNIX", "TSLA", "AAPL", "NVDA", "MSFT", "AMZN",
    "META", "NFLX", "GOOGL", "BABA", "AMD", "INTC",
    "PLTR", "COIN", "MSTR", "SOXL", "SOXS", "TQQQ", "SQQQ",
}

INDEX_BASES = {
    "SPX", "NAS", "NDX", "DJI", "US30", "US100",
    "NIKKEI", "VIX", "DAX",
}

THEMES = {
    "반도체": {
        "SOXL": 1, "SOXS": -1, "SKHYNIX": 1,
        "NVDA": 1, "AMD": 1, "INTC": 1,
        "MU": 1, "TSM": 1,
    },
    "미국지수": {
        "NAS": 1, "NDX": 1, "SPX": 1,
        "US100": 1, "US30": 1, "DJI": 1,
        "TQQQ": 1, "SQQQ": -1,
    },
}


def get_asset_category(symbol):
    base = symbol.upper().split("_")[0]

    if base in STOCK_BASES:
        return "주식"

    if base in INDEX_BASES:
        return "지수"

    return "코인"


def theme_exposure(symbol, direction):
    base = symbol.upper().split("_")[0]

    for theme, members in THEMES.items():
        if base in members:
            sign = members[base] * (
                1 if direction == "LONG" else -1
            )
            return theme, sign

    return None, 0


def count_theme_slots(active, theme, sign):
    count = 0

    for symbol, info in active.items():
        current_theme, current_sign = theme_exposure(
            symbol,
            info.get("direction", "LONG"),
        )

        if current_theme == theme and current_sign == sign:
            count += 1

    return count


def calculate_dynamic_leverage(
    category,
    change_24h,
    atr,
    current_price,
):
    abs_change = abs(change_24h)

    if category == "코인":
        base_lev = (
            5 if abs_change > 10
            else 7 if abs_change > 6
            else 10
        )
    else:
        base_lev = 8 if category == "주식" else 12

    if current_price > 0 and atr > 0:
        atr_pct = atr / current_price * 100

        if atr_pct > 3.5:
            base_lev = max(3, base_lev - 3)
        elif atr_pct > 2.0:
            base_lev = max(4, base_lev - 2)

    return base_lev


# ============================================================
# 전적 통계
# ============================================================

def record_stats(stats, net_r, outcome):
    stats["total"] += 1

    key = {
        "win": "wins",
        "draw": "draws",
        "loss": "losses",
    }[outcome]

    stats[key] += 1
    stats["r_count"] += 1
    stats["sum_r"] += net_r

    if net_r > 0:
        stats["win_r"] += net_r
    elif net_r < 0:
        stats["loss_r"] += abs(net_r)

    stats["recent_r"] = (
        stats.get("recent_r", []) + [round(net_r, 3)]
    )[-50:]


def classify_trade(info, reason, net_r):
    """
    최종 시그널 종료 결과 분류.

    TP3:
        무조건 승리

    TP1_PROTECT:
        TP2 도달 후 TP1 방어선 종료이므로 승리

    PROFIT_LOCK:
        TP1 도달 후 Profit-Lock 종료이므로 무승부

    SL:
        TP1 이전 최초 손절이므로 패배

    ROTATE / TIME_STOP:
        TP2 도달 여부 우선
        TP1만 도달했다면 무승부
        TP 미도달이면 net R 기준
    """

    if reason == "TP3":
        return "win"

    if reason == "TP1_PROTECT":
        return "win"

    if reason == "PROFIT_LOCK":
        return "draw"

    if reason == "SL":
        return "loss"

    if reason in ("ROTATE", "TIME_STOP"):

        if info.get("tp2_reached"):
            return "win"

        if info.get("tp1_reached"):
            return "draw"

        if net_r > DRAW_BAND_R:
            return "win"

        if net_r < -DRAW_BAND_R:
            return "loss"

        return "draw"

    if net_r > DRAW_BAND_R:
        return "win"

    if net_r < -DRAW_BAND_R:
        return "loss"

    return "draw"


def build_stats_line(stats):
    total = stats.get("total", 0)

    wins = stats.get("wins", 0)
    draws = stats.get("draws", 0)
    losses = stats.get("losses", 0)

    decisive = wins + losses

    win_rate = (
        wins / decisive * 100
        if decisive > 0 else 0.0
    )

    lines = [
        "📈 <b>[누적 통계]</b>",
        f"<code>{total}전 {wins}승 {draws}무 {losses}패</code>",
        (
            f"<code>승률 {win_rate:.1f}% "
            f"({wins}승 {losses}패 / 무승부 제외)</code>"
        ),
    ]

    n = stats.get("r_count", 0)

    if n > 0:
        avg_r = stats["sum_r"] / n

        loss_r = stats.get("loss_r", 0)
        pf = (
            f"{stats['win_r'] / loss_r:.2f}"
            if loss_r > 0 else "-"
        )

        recent = stats.get("recent_r", [])[-20:]

        lines.append(
            f"<code>평균 R {avg_r:+.2f} | PF {pf} | "
            f"최근 {len(recent)}전 {sum(recent):+.1f}R</code>"
        )
    else:
        lines.append("<code>R 통계: 기록 대기 중</code>")

    return "\n".join(lines)


# ============================================================
# 포지션 관리
# ============================================================

def migrate_slot(info):
    entry = float(info.get("entry_price", 0) or 0)

    atr = float(
        info.get(
            "atr",
            info.get("risk", entry) / ATR_SL,
        )
    )

    if "risk" not in info:
        info["risk"] = abs(
            float(info.get("tp1", entry)) - entry
        )

    info.setdefault("direction", "LONG")
    info.setdefault("leverage", 10)
    info.setdefault("tp1_reached", False)
    info.setdefault("tp2_reached", False)
    info.setdefault("max_fav", entry)
    info.setdefault("max_adv", entry)

    if "last_candle_ts" not in info:
        dt = parse_iso(
            info.get("entry_time", ""),
            utcnow(),
        )
        info["last_candle_ts"] = (
            int(dt.timestamp()) // 300 * 300
        )

    info.setdefault("stop_loss", info.get("initial_stop", entry))
    info.setdefault("tp1", entry)
    info.setdefault("tp2", entry)
    info.setdefault("tp3", entry)

    profit_lock_target = (
        entry + atr * PROFIT_LOCK_ATR
        if info["direction"] == "LONG"
        else entry - atr * PROFIT_LOCK_ATR
    )

    if info["direction"] == "LONG":
        if info["tp1_reached"]:
            info["stop_loss"] = max(
                info["stop_loss"],
                profit_lock_target,
            )

        if info["tp2_reached"]:
            info["stop_loss"] = max(
                info["stop_loss"],
                info["tp1"],
            )

    else:
        if info["tp1_reached"]:
            info["stop_loss"] = min(
                info["stop_loss"],
                profit_lock_target,
            )

        if info["tp2_reached"]:
            info["stop_loss"] = min(
                info["stop_loss"],
                info["tp1"],
            )


def step(info, high, low):
    direction = info["direction"]
    entry = info["entry_price"]

    atr = float(
        info.get(
            "atr",
            info.get("risk", entry) / ATR_SL,
        )
    )

    profit_lock_target = (
        entry + atr * PROFIT_LOCK_ATR
        if direction == "LONG"
        else entry - atr * PROFIT_LOCK_ATR
    )

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

            info["stop_loss"] = max(
                info["stop_loss"],
                profit_lock_target,
            )

            events.append("TP1")

        if high >= info["tp2"] and not info["tp2_reached"]:
            info["tp2_reached"] = True

            info["stop_loss"] = max(
                info["stop_loss"],
                info["tp1"],
            )

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

            info["stop_loss"] = min(
                info["stop_loss"],
                profit_lock_target,
            )

            events.append("TP1")

        if low <= info["tp2"] and not info["tp2_reached"]:
            info["tp2_reached"] = True

            info["stop_loss"] = min(
                info["stop_loss"],
                info["tp1"],
            )

            events.append("TP2")

    return events, None


def stop_reason(info):
    if info.get("tp2_reached"):
        return "TP1_PROTECT"

    if info.get("tp1_reached"):
        return "PROFIT_LOCK"

    return "SL"


def finalize_trade(state, symbol, info, reason, exit_price, now):
    direction = info["direction"]
    entry = float(info["entry_price"])
    risk = float(info["risk"])

    gross_r = r_move(
        direction,
        entry,
        exit_price,
        risk,
    )

    fee_r = (
        FEE_ROUNDTRIP * entry / risk
        if risk > 0 else 0
    )

    net_r = gross_r - fee_r

    # 핵심 수정: 종료 사유 및 TP 도달 이력을 반영
    outcome = classify_trade(
        info,
        reason,
        net_r,
    )

    leverage = float(info.get("leverage", 1))

    raw_pct = pct_move(
        direction,
        entry,
        exit_price,
    )

    leverage_pct = round(raw_pct * leverage, 2)

    entry_dt = parse_iso(
        info.get("entry_time", ""),
        now,
    )

    hours = max(
        (now - entry_dt).total_seconds() / 3600,
        0,
    )

    mfe_r = r_move(
        direction,
        entry,
        info["max_fav"],
        risk,
    )

    mae_r = r_move(
        direction,
        entry,
        info["max_adv"],
        risk,
    )

    record_stats(
        state["stats"],
        net_r,
        outcome,
    )

    append_trade_csv({
        "closed_at": now.isoformat(),
        "symbol": symbol,
        "direction": direction,
        "category": get_asset_category(symbol),
        "leverage": leverage,
        "entry_time": info.get("entry_time", ""),
        "entry_price": entry,
        "exit_price": exit_price,
        "exit_reason": reason,
        "holding_hours": round(hours, 2),
        "gross_r": round(gross_r, 3),
        "net_r": round(net_r, 3),
        "mfe_r": round(mfe_r, 3),
        "mae_r": round(mae_r, 3),
        "leverage_pct": leverage_pct,
        "outcome": outcome,
    })

    state["closed_symbols"][symbol] = {
        "time": now.isoformat(),
        "direction": direction,
        "reason": reason,
    }

    del state["active_slots"][symbol]

    return {
        "reason": reason,
        "exit_price": exit_price,
        "net_r": net_r,
        "outcome": outcome,
        "hours": hours,
        "leverage_pct": leverage_pct,
    }


# ============================================================
# 텔레그램 메시지
# ============================================================

def build_event_message(symbol, info, event):
    direction = info["direction"]
    entry = info["entry_price"]
    leverage = info.get("leverage", 1)

    level = (
        info["tp1"]
        if event == "TP1"
        else info["tp2"]
    )

    pct = pct_move(direction, entry, level)

    atr = float(
        info.get(
            "atr",
            info.get("risk", entry) / ATR_SL,
        )
    )

    sign = 1 if direction == "LONG" else -1

    profit_lock_price = (
        entry + sign * atr * PROFIT_LOCK_ATR
        if event == "TP1"
        else info["tp1"]
    )

    if event == "TP1":
        title = "🎯 <b>TP1 도달 (수익형 본전 방어선 확보)</b>"

        action = (
            f"🔒 스탑을 약수익 구간 "
            f"(<code>{fmt_price(profit_lock_price)}</code> / "
            f"<code>+{PROFIT_LOCK_ATR} ATR</code>)으로 이동"
        )

    else:
        title = "🎯🎯 <b>TP2 도달</b>"

        action = (
            f"🔒 스탑을 TP1 "
            f"(<code>{fmt_price(info['tp1'])}</code>)으로 이동"
        )

    return (
        f"{title} | {dir_tag(direction)}\n"
        f"<a href='{chart_link(symbol)}'>"
        f"[{get_asset_category(symbol)}] {symbol}</a>\n\n"
        f"진입 <code>{fmt_price(entry)}</code> → "
        f"<code>{fmt_price(level)}</code>\n"
        f"수익 <code>{pct:+.2f}%</code> "
        f"({leverage}배: <code>{pct * leverage:+.1f}%</code>)\n\n"
        f"{action}"
    )


def build_close_message(symbol, info, trade):
    direction = info["direction"]
    entry = info["entry_price"]
    leverage = info.get("leverage", 1)
    exit_price = trade["exit_price"]

    pct = pct_move(
        direction,
        entry,
        exit_price,
    )

    reason = trade["reason"]

    head = {
        "SL": (
            "🛑 <b>손절 청산</b>",
            "",
        ),
        "PROFIT_LOCK": (
            "💰 <b>수익 방어 청산 (Profit-Lock)</b>",
            "TP1 도달 후 되돌아와 약수익 스탑 체결\n",
        ),
        "TP1_PROTECT": (
            "✅ <b>수익 청산 (TP1 방어)</b>",
            "TP2 도달 후 스탑(TP1)에서 체결\n",
        ),
        "TP3": (
            "🏆 <b>TP3 최종 목표 도달</b>",
            "",
        ),
        "ROTATE": (
            "🔄 <b>회전 청산</b>",
            "장기 보유 수익 확보, 슬롯 회전\n",
        ),
        "TIME_STOP": (
            "⏳ <b>타임스탑 청산</b>",
            f"{TIME_STOP_HOURS}시간 초과 청산\n",
        ),
    }.get(reason, ("📌 <b>포지션 종료</b>", ""))

    outcome_label = {
        "win": "승",
        "draw": "무",
        "loss": "패",
    }[trade["outcome"]]

    return (
        f"{head[0]} | {dir_tag(direction)}\n"
        f"<a href='{chart_link(symbol)}'>"
        f"[{get_asset_category(symbol)}] {symbol}</a>\n\n"
        f"진입 <code>{fmt_price(entry)}</code> → "
        f"청산 <code>{fmt_price(exit_price)}</code>\n"
        f"{head[1]}"
        f"가격 변동 <code>{pct:+.2f}%</code> "
        f"({leverage}배: <code>{pct * leverage:+.1f}%</code>)\n\n"
        f"결과 <code>{trade['net_r']:+.2f}R</code> | "
        f"<b>{outcome_label}</b>\n"
        f"보유 <code>{fmt_hours(trade['hours'])}</code>"
    )


# ============================================================
# 기존 포지션 관리
# ============================================================

def manage_positions(state, tickers, now):
    active = state["active_slots"]
    now_ts = int(now.timestamp())

    changed = False
    alerts = []

    for symbol, info in list(active.items()):
        try:
            migrate_slot(info)

            entry = info["entry_price"]
            risk = info["risk"]

            if entry <= 0 or risk <= 0:
                continue

            if symbol not in tickers:
                del active[symbol]

                alerts.append(
                    f"🧹 <b>슬롯 정리</b>\n"
                    f"<code>{symbol}</code> 상장 폐지 또는 조회 불가로 제거됨"
                )

                changed = True
                continue

            close = None

            candles = fetch_completed_5m(
                symbol,
                info["last_candle_ts"] + 300,
                now_ts,
            )

            for candle in candles:
                events, close = step(
                    info,
                    candle["h"],
                    candle["l"],
                )

                for event in events:
                    alerts.append(
                        build_event_message(
                            symbol,
                            info,
                            event,
                        )
                    )
                    changed = True

                info["last_candle_ts"] = candle["t"]

                if close:
                    break

            price = tickers.get(symbol)

            if not close and price:
                events, close = step(
                    info,
                    price,
                    price,
                )

                for event in events:
                    alerts.append(
                        build_event_message(
                            symbol,
                            info,
                            event,
                        )
                    )
                    changed = True

            if close:
                kind, exit_price = close

                reason = (
                    stop_reason(info)
                    if kind == "STOP"
                    else "TP3"
                )

                trade = finalize_trade(
                    state,
                    symbol,
                    info,
                    reason,
                    exit_price,
                    now,
                )

                alerts.append(
                    build_close_message(
                        symbol,
                        info,
                        trade,
                    )
                )

                changed = True
                continue

            entry_dt = parse_iso(
                info.get("entry_time", ""),
                now,
            )

            holding_h = (
                now - entry_dt
            ).total_seconds() / 3600

            if holding_h >= TIME_STOP_HOURS and price:

                trade = finalize_trade(
                    state,
                    symbol,
                    info,
                    "TIME_STOP",
                    price,
                    now,
                )

                alerts.append(
                    build_close_message(
                        symbol,
                        info,
                        trade,
                    )
                )

                changed = True

            elif holding_h >= ROTATE_HOURS and price:

                profit_pct = pct_move(
                    info["direction"],
                    entry,
                    price,
                )

                if profit_pct >= ROTATE_MIN_PROFIT_PCT:

                    trade = finalize_trade(
                        state,
                        symbol,
                        info,
                        "ROTATE",
                        price,
                        now,
                    )

                    alerts.append(
                        build_close_message(
                            symbol,
                            info,
                            trade,
                        )
                    )

                    changed = True

        except Exception as exc:
            print(f"Error managing {symbol}: {exc}")

    for alert in alerts:
        send_telegram_message(alert)

    return changed


# ============================================================
# 신규 시그널 탐색
# ============================================================

def identify_new_signals(state, raw_tickers, now, start_time):
    active = state["active_slots"]
    closed = state["closed_symbols"]

    now_ts = int(now.timestamp())
    changed = False

    candidates = []

    for ticker in raw_tickers:
        try:
            symbol = ticker.get("contract", "")

            if (
                not symbol.endswith("_USDT")
                or LEVERAGED_TOKEN_RE.search(symbol)
            ):
                continue

            last_price = float(ticker.get("last", 0))
            change = float(ticker.get("change_percentage", 0))

            direction = (
                "LONG" if change > 0 else "SHORT"
            )

            if symbol in active:
                continue

            if symbol in closed:
                closed_info = closed[symbol]

                closed_time = parse_iso(
                    closed_info.get("time", ""),
                    now,
                )

                closed_direction = closed_info.get(
                    "direction",
                    "",
                )

                elapsed = (
                    now - closed_time
                ).total_seconds()

                if closed_direction == direction:
                    if elapsed < COOLDOWN_HOURS * 3600:
                        continue

                elif (
                    closed_info.get("reason") == "SL"
                    and elapsed < 1800
                ):
                    continue

            volume = float(
                ticker.get("volume_24h_quote")
                or ticker.get("volume_24h_settle")
                or 0
            )

            if (
                last_price <= 0
                or volume < MIN_QUOTE_VOLUME
                or abs(change) < MIN_CHANGE_PCT
            ):
                continue

            candidates.append((
                volume,
                symbol,
                last_price,
                change,
                direction,
            ))

        except (KeyError, TypeError, ValueError):
            continue

    candidates.sort(reverse=True)

    for volume, symbol, last_price, change, direction in candidates[:MAX_CANDIDATES]:

        if len(active) >= MAX_SLOTS:
            break

        if time.time() - start_time > SCAN_BUDGET_SEC:
            break

        try:
            theme, theme_sign = theme_exposure(
                symbol,
                direction,
            )

            if (
                theme
                and count_theme_slots(
                    active,
                    theme,
                    theme_sign,
                ) >= MAX_PER_THEME
            ):
                continue

            analysis = analyze_signal(
                symbol,
                direction,
                now_ts,
                last_price,
            )

            if not analysis:
                continue

            atr = analysis["atr"]
            risk = atr * ATR_SL

            sign = 1 if direction == "LONG" else -1

            tp1, tp2, tp3 = (
                last_price + sign * atr * multiplier
                for multiplier in ATR_TP_MULTS
            )

            sl = last_price - sign * risk

            if (
                sl <= 0
                or tp1 <= 0
                or (
                    direction == "SHORT"
                    and tp3 <= 0
                )
            ):
                continue

            sl_frac = risk / last_price

            if sl_frac * 100 > MAX_SL_PCT:
                continue

            category = get_asset_category(symbol)

            leverage = calculate_dynamic_leverage(
                category,
                change,
                atr,
                last_price,
            )

            leverage = max(
                1,
                min(
                    leverage,
                    int(MAX_MARGIN_LOSS / sl_frac) or 1,
                ),
            )

            ob_low = analysis["ob_low"]
            ob_high = analysis["ob_high"]

            active[symbol] = {
                "direction": direction,
                "entry_price": last_price,
                "entry_time": now.isoformat(),
                "leverage": leverage,
                "risk": risk,
                "atr": atr,
                "tp1": tp1,
                "tp2": tp2,
                "tp3": tp3,
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
                return pct_move(
                    direction,
                    last_price,
                    price,
                )

            message = (
                f"🛡️ <b>신규 시그널 "
                f"(동적레버리지 & OB리테스트)</b> | "
                f"{dir_tag(direction)}\n"
                f"<a href='{chart_link(symbol)}'>"
                f"[{category}] {symbol}</a>\n\n"
                f"진입가 <code>{fmt_price(last_price)}</code> | "
                f"레버리지 <code>{leverage}배</code>\n\n"
                "🎯 <b>목표가 및 방어선</b>\n"
                f"TP1 <code>{fmt_price(tp1)} "
                f"({pct_of(tp1):+.2f}%)</code> "
                f"→ 도달 시 스탑 +{PROFIT_LOCK_ATR} ATR\n"
                f"TP2 <code>{fmt_price(tp2)} "
                f"({pct_of(tp2):+.2f}%)</code>\n"
                f"TP3 <code>{fmt_price(tp3)} "
                f"({pct_of(tp3):+.2f}%)</code>\n"
                f"🛑 SL <code>{fmt_price(sl)} "
                f"({pct_of(sl):+.2f}%)</code>\n\n"
                "📌 <b>진입 근거 (스마트 머니 & MA50)</b>\n"
                f"• 오더블럭(OB) 존: "
                f"<code>{fmt_price(ob_low)} ~ "
                f"{fmt_price(ob_high)}</code> 리테스트 확인\n"
                f"• 변동 <code>{change:+.1f}%</code> / "
                f"거래대금 <code>{volume / 1e6:.1f}M USDT</code>\n"
                f"• RSI <code>{analysis['rsi']:.1f}</code> / "
                "MA50 추세 정렬 완료"
            )

            send_telegram_message(message)

        except Exception as exc:
            print(
                f"Error processing candidate {symbol}: {exc}"
            )
            continue

    return changed


# ============================================================
# 슬롯 현황 메시지
# ============================================================

def build_slot_status_message(state, tickers):
    active = state["active_slots"]
    symbols = list(active.keys())

    longs = sum(
        1 for symbol in symbols
        if active[symbol].get("direction") == "LONG"
    )

    shorts = len(symbols) - longs

    lines = [
        f"📊 <b>슬롯 현황 "
        f"({len(symbols)} / {MAX_SLOTS}) | "
        f"🟢 롱 {longs} / 🔴 숏 {shorts}</b>\n"
    ]

    for i in range(MAX_SLOTS):

        icon = (
            NUM_EMOJI[i]
            if i < len(NUM_EMOJI)
            else f"{i + 1}."
        )

        if i >= len(symbols):
            lines.append(f"{icon} (빈 슬롯)")
            continue

        symbol = symbols[i]
        info = active[symbol]

        migrate_slot(info)

        direction = info["direction"]
        entry = info["entry_price"]
        leverage = info.get("leverage", 1)

        price = tickers.get(symbol, entry)

        pct = pct_move(
            direction,
            entry,
            price,
        )

        entry_dt = parse_iso(
            info.get("entry_time", ""),
            utcnow(),
        )

        hold = fmt_hours(
            max(
                (utcnow() - entry_dt).total_seconds() / 3600,
                0,
            )
        )

        icon_dir = (
            "🟢" if direction == "LONG" else "🔴"
        )

        def dist(level):
            return (
                abs(level - price) / price * 100
                if price > 0 else 0.0
            )

        if info["tp2_reached"]:

            status = (
                f"🎯🎯 TP2 도달 | 스탑 TP1 방어 | "
                f"TP3까지 {dist(info['tp3']):.1f}%"
            )

        elif info["tp1_reached"]:

            status = (
                f"🎯 TP1 도달 | "
                f"스탑 수익 방어(+{PROFIT_LOCK_ATR}ATR) | "
                f"TP2까지 {dist(info['tp2']):.1f}%"
            )

        else:

            status = (
                f"📈 홀딩 | "
                f"TP1까지 {dist(info['tp1']):.1f}% / "
                f"SL까지 {dist(info['stop_loss']):.1f}%"
            )

        lines.append(
            f"{icon} {icon_dir} "
            f"<b>[{get_asset_category(symbol)}]</b> "
            f"<a href='{chart_link(symbol)}'>{symbol}</a>\n"
            f"   <code>{pct:+.2f}% "
            f"({leverage}배 {pct * leverage:+.1f}%) | "
            f"보유 {hold}</code>\n"
            f"   <code>{status}</code>"
        )

    lines.append(
        "\n" + build_stats_line(state["stats"])
    )

    return "\n".join(lines)


# ============================================================
# 일일 현황 메시지
# ============================================================

def maybe_send_daily_report(state, tickers, now):
    if not DAILY_REPORT_ENABLED:
        return False

    today = now.strftime("%Y-%m-%d")

    if (
        now.hour < DAILY_REPORT_HOUR_UTC
        or state.get("last_report_date") == today
    ):
        return False

    send_telegram_message(
        build_slot_status_message(
            state,
            tickers,
        )
    )

    state["last_report_date"] = today
    return True


# ============================================================
# 메인 실행
# ============================================================

def run_bot():
    start_time = time.time()

    state = load_state()
    now = utcnow()

    raw_tickers = fetch_tickers()

    if not raw_tickers:
        state["fail_count"] += 1

        n = state["fail_count"]

        if (
            n == FAIL_ALERT_THRESHOLD
            or (
                n > FAIL_ALERT_THRESHOLD
                and (n - FAIL_ALERT_THRESHOLD) % 12 == 0
            )
        ):
            send_telegram_message(
                f"⚠️ <b>봇 오류 경고</b>\n"
                f"Gate.io 시세 조회가 {n}회 연속 실패했습니다."
            )

        save_state(state)
        return

    state["fail_count"] = 0

    tickers = {}

    for ticker in raw_tickers:
        try:
            contract = ticker.get("contract")
            last_price = float(ticker.get("last", 0))

            if contract:
                tickers[contract] = last_price

        except (KeyError, TypeError, ValueError):
            continue

    cooldown = timedelta(hours=COOLDOWN_HOURS)

    state["closed_symbols"] = {
        symbol: info
        for symbol, info in state["closed_symbols"].items()
        if (
            now - parse_iso(
                info.get("time", ""),
                now - cooldown * 2,
            )
        ) < cooldown
    }

    changed_positions = manage_positions(
        state,
        tickers,
        now,
    )

    changed_new = False

    if len(state["active_slots"]) < MAX_SLOTS:
        changed_new = identify_new_signals(
            state,
            raw_tickers,
            now,
            start_time,
        )

    if changed_positions or changed_new:
        send_telegram_message(
            build_slot_status_message(
                state,
                tickers,
            )
        )

    maybe_send_daily_report(
        state,
        tickers,
        now,
    )

    save_state(state)


if __name__ == "__main__":
    run_bot()
