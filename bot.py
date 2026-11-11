#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gate.io USDT Futures Signal Bot - versioned paper-signal tracker
- Telegram signal-only bot; it does NOT place orders.
- 1h trend + 15m pullback/reclaim entry confirmation.
- Net-R based win/draw/loss accounting.
- Conservative 5m stop/target monitoring.
- Preserves existing active slots and mixed legacy CSV rows.

Important:
This is a rule-based signal tracker, not a profit guarantee or an order executor.
New strategy versions are reported separately. Historical rows without an entry
version or indicator snapshots cannot validate the current entry filters.
"""

import csv
import json
import logging
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

# ───────────────────────────── Configuration ─────────────────────────────
TELEGRAM_TOKEN = os.getenv("GATE_TELEGRAM_TOKEN") or os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("GATE_TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID")

BASE_URL = "https://api.gateio.ws/api/v4/futures/usdt"
STATE_FILE = Path(os.getenv("STATE_FILE", "gate_state.json"))
TRADES_FILE = Path(os.getenv("TRADES_FILE", "trades.csv"))
LOG_FILE = Path(os.getenv("LOG_FILE", "gate_signal_bot.log"))

MAX_SLOTS = 5
MAX_CRYPTO_SLOTS = 3
MAX_CANDIDATES = 40
SCAN_BUDGET_SEC = 150
MIN_CHANGE_PCT = 1.5
MIN_QUOTE_VOLUME = 200_000.0
LEVERAGED_TOKEN_RE = re.compile(r"\d+[LS]_USDT$")

# Entry quality filters. More selective than the previous 24h-momentum-only scan.
STRATEGY_VERSION = 3
ADX_MIN_1H = 17.0
BODY_RATIO_MIN = 0.35
MIN_VOLUME_SURGE = 1.05
MAX_EMA_DISTANCE_ATR = 1.25
# The uploaded sample showed weaker LONG expectancy than SHORT expectancy.
# LONG therefore uses a tighter RSI band and a higher confirmation score.
MIN_SCORE_LONG = 8
MIN_SCORE_SHORT = 7
RSI_LONG_15M = (53.0, 67.0)
RSI_SHORT_15M = (32.0, 50.0)
RSI_LONG_1H = (50.0, 68.0)
RSI_SHORT_1H = (30.0, 54.0)
BTC_FILTER_ENABLED = True

# Risk/exit structure. ATR target distances preserve the former basic geometry.
ATR_SL = 1.5
ATR_TP_MULTS = (1.0, 2.5, 4.0)
PROFIT_LOCK_ATR = 0.5       # after TP1: protect +0.5 ATR
TP2_LOCK_ATR = 1.0          # after TP2: protect +1.0 ATR (TP1 price), not entry
MAX_SL_PCT = 7.0
FEE_ROUNDTRIP = 0.001       # assumed round-trip trading cost; adjust to actual fee tier
DRAW_BAND_R = 0.05           # -0.05R to +0.05R is a draw

# Display-only leverage reference; it is not account-risk sizing.
MAX_REFERENCE_LEVERAGE = 5
MIN_REFERENCE_LEVERAGE = 1

ROTATE_HOURS = 18
ROTATE_MIN_PROFIT_PCT = 0.5
TIME_STOP_HOURS = 24
COOLDOWN_HOURS = 2
SL_OPPOSITE_COOLDOWN_SECONDS = 900
MAX_PER_THEME = 1

DAILY_REPORT_ENABLED = True
DAILY_REPORT_HOUR_UTC = 0
FAIL_ALERT_THRESHOLD = 3
MISSING_TICKER_ALERT_AFTER = 3

BASE_CSV_FIELDS = [
    "closed_at", "symbol", "direction", "category", "leverage",
    "entry_time", "entry_price", "exit_price", "exit_reason",
    "holding_hours", "gross_r", "net_r", "mfe_r", "mae_r",
    "leverage_pct", "outcome",
]
ENTRY_AUDIT_FIELDS = [
    "strategy_version", "score", "required_score", "rsi_1h", "rsi_15m",
    "adx_1h", "volume_surge",
]
CSV_FIELDS = BASE_CSV_FIELDS
FULL_CSV_FIELDS = BASE_CSV_FIELDS + ENTRY_AUDIT_FIELDS
AUDIT_CSV_FIELDS = ["closed_at", "symbol"] + ENTRY_AUDIT_FIELDS
AUDIT_FILE = Path(os.getenv(
    "AUDIT_FILE",
    str(TRADES_FILE.with_name(f"{TRADES_FILE.stem}_audit.csv")),
))
LEGACY_CSV_FIELDS = BASE_CSV_FIELDS[:-1]

NUM_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]

STOCK_BASES = {
    "SKHYNIX", "TSLA", "AAPL", "NVDA", "MSFT", "AMZN", "META", "NFLX",
    "GOOGL", "BABA", "AMD", "INTC", "PLTR", "COIN", "MSTR", "CRCL",
    "SOXL", "SOXS", "TQQQ", "SQQQ", "GOOG", "AVGO", "MU", "TSM",
    "SMCI", "ORCL", "AMAT", "QCOM", "ARM", "HOOD", "SNDK", "NBIS",
}
INDEX_BASES = {
    "SPX", "NAS", "NDX", "DJI", "US30", "US100", "NIKKEI", "VIX", "DAX",
}
COMMODITY_BASES = {
    "CL", "GC", "SI", "HG", "NG", "XAU", "XAG", "WTI", "BRENT",
    "GOLD", "SILVER", "OIL", "NATGAS",
}

SESSION = requests.Session()
SESSION.headers.update({
    "Accept": "application/json",
    "User-Agent": "GateSignalBot/5.0",
})

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()],
)

# ───────────────────────────── Common helpers ─────────────────────────────
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(value: str, default: Optional[datetime] = None) -> datetime:
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return default or utcnow()


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def fmt_price(price: float) -> str:
    if price >= 1000:
        return f"{price:.2f}"
    if price >= 1:
        return f"{price:.4f}"
    if price >= 0.01:
        return f"{price:.5f}"
    return f"{price:.8f}"


def fmt_hours(hours: float) -> str:
    hours = max(0.0, hours)
    whole = int(hours)
    minutes = int((hours - whole) * 60)
    return f"{whole}시간 {minutes:02d}분"


def dir_tag(direction: str) -> str:
    return "🟢 롱" if direction == "LONG" else "🔴 숏"


def chart_link(symbol: str) -> str:
    return f"https://www.gate.com/futures/USDT/{symbol}"


def pct_move(direction: str, entry: float, price: float) -> float:
    if entry <= 0:
        return 0.0
    if direction == "LONG":
        return (price - entry) / entry * 100.0
    return (entry - price) / entry * 100.0


def r_move(direction: str, entry: float, price: float, risk: float) -> float:
    if risk <= 0:
        return 0.0
    if direction == "LONG":
        return (price - entry) / risk
    return (entry - price) / risk


def send_telegram_message(message: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("Telegram credentials are missing; message was not sent.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message[:4000],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        response = SESSION.post(url, json=payload, timeout=12)
        if response.status_code != 200:
            logging.error("Telegram send failed: %s %s", response.status_code, response.text[:300])
            return False
        return True
    except requests.RequestException as exc:
        logging.error("Telegram request failed: %s", exc)
        return False

# ───────────────────────────── State and CSV ──────────────────────────────
def default_state() -> Dict[str, Any]:
    return {
        "active_slots": {},
        "stats": {
            "wins": 0, "draws": 0, "losses": 0, "total": 0,
            "r_count": 0, "sum_r": 0.0, "win_r": 0.0,
            "loss_r": 0.0, "recent_r": [], "positive_count": 0,
        },
        "closed_symbols": {},
        "fail_count": 0,
        "last_report_date": "",
        # Retained for compatibility; v3 reports are filtered by strategy_version.
        "csv_stats_offset": None,
        "strategy_version": STRATEGY_VERSION,
    }


def load_state() -> Dict[str, Any]:
    state = default_state()
    if not STATE_FILE.exists():
        return state
    try:
        with STATE_FILE.open("r", encoding="utf-8") as handle:
            saved = json.load(handle)
        if not isinstance(saved, dict):
            return state
        for key in ("active_slots", "closed_symbols"):
            if isinstance(saved.get(key), dict):
                state[key] = saved[key]
        if isinstance(saved.get("stats"), dict):
            state["stats"].update(saved["stats"])
        # Normalize old state formats where closed_symbols was a list or string map.
        old_closed = saved.get("closed_symbols", {})
        normalized_closed = {}
        if isinstance(old_closed, list):
            for symbol in old_closed:
                normalized_closed[str(symbol)] = {
                    "time": utcnow().isoformat(), "direction": "LONG", "reason": "SL"
                }
        elif isinstance(old_closed, dict):
            for symbol, value in old_closed.items():
                if isinstance(value, str):
                    normalized_closed[str(symbol)] = {
                        "time": value, "direction": "LONG", "reason": "SL"
                    }
                elif isinstance(value, dict):
                    normalized_closed[str(symbol)] = value
        state["closed_symbols"] = normalized_closed
        for key in ("fail_count", "last_report_date", "csv_stats_offset", "strategy_version"):
            if key in saved:
                state[key] = saved[key]
    except Exception as exc:
        logging.exception("Unable to load state file: %s", exc)
        try:
            os.replace(STATE_FILE, STATE_FILE.with_suffix(STATE_FILE.suffix + ".corrupt"))
        except OSError:
            pass
    return state


def save_state(state: Dict[str, Any]) -> None:
    tmp_path = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
    os.replace(tmp_path, STATE_FILE)


def append_trade_csv(row: Dict[str, Any]) -> None:
    exists = TRADES_FILE.exists() and TRADES_FILE.stat().st_size > 0
    with TRADES_FILE.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerow({field: row.get(field, "") for field in CSV_FIELDS})

    audit_exists = AUDIT_FILE.exists() and AUDIT_FILE.stat().st_size > 0
    try:
        with AUDIT_FILE.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=AUDIT_CSV_FIELDS, extrasaction="ignore")
            if not audit_exists:
                writer.writeheader()
            writer.writerow({field: row.get(field, "") for field in AUDIT_CSV_FIELDS})
    except OSError:
        # Preserve the closed trade in the primary CSV and make the audit failure visible.
        logging.exception("Unable to append strategy audit row to %s", AUDIT_FILE)


def read_trade_csv() -> List[Dict[str, Any]]:
    """Read current audit rows plus headered/headerless 15/16-column legacy rows."""
    if not TRADES_FILE.exists():
        return []
    records: List[Dict[str, Any]] = []
    with TRADES_FILE.open("r", newline="", encoding="utf-8-sig") as handle:
        for raw in csv.reader(handle):
            if not raw or not any(str(value).strip() for value in raw):
                continue
            if raw[0].strip().lower() == "closed_at":
                continue
            if len(raw) == 15:
                record = dict(zip(LEGACY_CSV_FIELDS, raw))
            elif len(raw) == len(BASE_CSV_FIELDS):
                record = dict(zip(BASE_CSV_FIELDS, raw))
            elif len(raw) == len(FULL_CSV_FIELDS):
                record = dict(zip(FULL_CSV_FIELDS, raw))
            else:
                logging.warning("Skipping malformed trade CSV row with %d columns", len(raw))
                continue
            for field in FULL_CSV_FIELDS:
                record.setdefault(field, "")
            try:
                record["net_r"] = float(record["net_r"])
                record["gross_r"] = safe_float(record.get("gross_r"))
                record["mfe_r"] = safe_float(record.get("mfe_r"))
                record["mae_r"] = safe_float(record.get("mae_r"))
                record["holding_hours"] = safe_float(record.get("holding_hours"))
                record["leverage_pct"] = safe_float(record.get("leverage_pct"))
            except (TypeError, ValueError):
                logging.warning("Skipping malformed numeric trade row: %s", record.get("symbol", "?"))
                continue
            records.append(record)

    if AUDIT_FILE.exists():
        audit_by_trade: Dict[Tuple[str, str], Dict[str, str]] = {}
        try:
            with AUDIT_FILE.open("r", newline="", encoding="utf-8-sig") as handle:
                for audit in csv.DictReader(handle):
                    key = (str(audit.get("closed_at", "")), str(audit.get("symbol", "")))
                    if key[0] and key[1]:
                        audit_by_trade[key] = audit
        except OSError:
            logging.exception("Unable to read strategy audit file %s", AUDIT_FILE)
        for record in records:
            audit = audit_by_trade.get((str(record.get("closed_at", "")),
                                        str(record.get("symbol", ""))))
            if audit:
                for field in ENTRY_AUDIT_FIELDS:
                    if audit.get(field, "") != "":
                        record[field] = audit[field]
    return records


def classify_trade(net_r: float) -> str:
    """Outcome is based on realized net R, not the exit-reason label."""
    if net_r > DRAW_BAND_R:
        return "win"
    if net_r < -DRAW_BAND_R:
        return "loss"
    return "draw"


def calculate_stats(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    stats = {
        "wins": 0, "draws": 0, "losses": 0, "total": 0,
        "r_count": 0, "sum_r": 0.0, "win_r": 0.0,
        "loss_r": 0.0, "recent_r": [], "positive_count": 0,
        "directional": {},
    }
    for record in records:
        net_r = safe_float(record.get("net_r"))
        outcome = classify_trade(net_r)
        stats["total"] += 1
        stats[{"win": "wins", "draw": "draws", "loss": "losses"}[outcome]] += 1
        stats["r_count"] += 1
        stats["sum_r"] += net_r
        if net_r > 0:
            stats["positive_count"] += 1
            stats["win_r"] += net_r
        elif net_r < 0:
            stats["loss_r"] += abs(net_r)
        stats["recent_r"].append(round(net_r, 3))
    stats["recent_r"] = stats["recent_r"][-50:]
    for side in ("LONG", "SHORT"):
        subset = [record for record in records if record.get("direction") == side]
        side_wins = sum(1 for record in subset if classify_trade(safe_float(record.get("net_r"))) == "win")
        side_draws = sum(1 for record in subset if classify_trade(safe_float(record.get("net_r"))) == "draw")
        side_losses = sum(1 for record in subset if classify_trade(safe_float(record.get("net_r"))) == "loss")
        side_profit = sum(max(0.0, safe_float(record.get("net_r"))) for record in subset)
        side_loss = sum(abs(min(0.0, safe_float(record.get("net_r")))) for record in subset)
        side_total = len(subset)
        decisive = side_wins + side_losses
        stats["directional"][side] = {
            "total": side_total,
            "wins": side_wins,
            "draws": side_draws,
            "losses": side_losses,
            "win_rate": side_wins / decisive * 100.0 if decisive else 0.0,
            "avg_r": sum(safe_float(record.get("net_r")) for record in subset) / side_total if side_total else 0.0,
            "pf": side_profit / side_loss if side_loss else 0.0,
        }
    return stats


def sync_stats_from_csv(state: Dict[str, Any]) -> Dict[str, Any]:
    records = read_trade_csv()
    current_records = [
        record for record in records
        if int(safe_float(record.get("strategy_version"), 0)) == STRATEGY_VERSION
    ]
    stats = calculate_stats(current_records)
    stats["strategy_version"] = STRATEGY_VERSION
    stats["csv_rows"] = len(records)
    stats["excluded_other_versions"] = len(records) - len(current_records)
    stats["first_closed_at"] = current_records[0].get("closed_at", "") if current_records else ""
    stats["last_closed_at"] = current_records[-1].get("closed_at", "") if current_records else ""
    state["stats"] = stats
    return stats


def build_stats_line(stats: Dict[str, Any]) -> str:
    total = int(stats.get("total", 0))
    wins = int(stats.get("wins", 0))
    draws = int(stats.get("draws", 0))
    losses = int(stats.get("losses", 0))
    decisive = wins + losses
    win_rate = wins / decisive * 100.0 if decisive else 0.0
    avg_r = stats.get("sum_r", 0.0) / total if total else 0.0
    loss_r = stats.get("loss_r", 0.0)
    pf = stats.get("win_r", 0.0) / loss_r if loss_r > 0 else 0.0
    recent = stats.get("recent_r", [])[-20:]
    recent_sum = sum(recent)
    positive_rate = stats.get("positive_count", wins) / total * 100.0 if total else 0.0
    directional = stats.get("directional", {})
    long_stats = directional.get("LONG", {})
    short_stats = directional.get("SHORT", {})
    long_line = (
        f"롱 {long_stats.get('win_rate', 0.0):.1f}% / PF {long_stats.get('pf', 0.0):.2f} "
        f"/ 평균 {long_stats.get('avg_r', 0.0):+.2f}R"
    )
    short_line = (
        f"숏 {short_stats.get('win_rate', 0.0):.1f}% / PF {short_stats.get('pf', 0.0):.2f} "
        f"/ 평균 {short_stats.get('avg_r', 0.0):+.2f}R"
    )
    first_closed = str(stats.get("first_closed_at", ""))[:10]
    last_closed = str(stats.get("last_closed_at", ""))[:10]
    period = f"{first_closed}~{last_closed} UTC" if first_closed else "신규 버전 거래 대기"
    return "\n".join([
        f"📈 <b>[전략 v{int(stats.get('strategy_version', STRATEGY_VERSION))} | 순손익 R]</b>",
        f"<code>집계 {period} | CSV {int(stats.get('csv_rows', total))}건 중 다른 버전 {int(stats.get('excluded_other_versions', 0))}건 제외</code>",
        f"<code>{total}전 {wins}승 {draws}무 {losses}패</code>",
        f"<code>승률 {win_rate:.1f}% (순 R +0.05 초과 / -0.05 미만, 무 제외)</code>",
        f"<code>순플러스 종료 {positive_rate:.1f}% | 평균 R {avg_r:+.2f} | PF {pf:.2f}</code>",
        f"<code>{long_line}</code>",
        f"<code>{short_line}</code>",
        f"<code>최근 {len(recent)}전 {recent_sum:+.1f}R</code>",
    ])

# ───────────────────────────── Gate API ──────────────────────────────────
def api_get(path: str, params: Optional[Dict[str, Any]] = None) -> Optional[Any]:
    try:
        response = SESSION.get(f"{BASE_URL}{path}", params=params, timeout=12)
        if response.status_code == 200:
            return response.json()
        logging.warning("Gate API %s %s: %s", response.status_code, path, response.text[:250])
    except requests.RequestException as exc:
        logging.warning("Gate API request failed %s: %s", path, exc)
    return None


def fetch_tickers() -> List[Dict[str, Any]]:
    data = api_get("/tickers")
    return data if isinstance(data, list) else []


def parse_candles(raw: Any) -> List[Dict[str, float]]:
    candles: List[Dict[str, float]] = []
    for item in raw or []:
        try:
            candles.append({
                "t": int(item["t"]),
                "o": float(item["o"]),
                "h": float(item["h"]),
                "l": float(item["l"]),
                "c": float(item["c"]),
                "v": float(item.get("v", 0) or 0),
            })
        except (KeyError, TypeError, ValueError):
            continue
    candles.sort(key=lambda candle: candle["t"])
    return candles


def fetch_completed_candles(symbol: str, interval: str, seconds: int,
                            now_ts: int, limit: int = 100) -> List[Dict[str, float]]:
    raw = api_get("/candlesticks", {
        "contract": symbol,
        "interval": interval,
        "limit": limit,
    })
    return [candle for candle in parse_candles(raw)
            if candle["t"] + seconds <= now_ts]


def fetch_completed_5m(symbol: str, from_ts: int, now_ts: int) -> List[Dict[str, float]]:
    if from_ts >= now_ts:
        return []
    raw = api_get("/candlesticks", {
        "contract": symbol,
        "interval": "5m",
        "from": int(from_ts),
        "to": int(now_ts),
    })
    return [candle for candle in parse_candles(raw)
            if candle["t"] >= from_ts and candle["t"] + 300 <= now_ts]

# ───────────────────────────── Indicators ─────────────────────────────────
def ema_series(values: List[float], period: int) -> List[float]:
    if not values:
        return []
    if len(values) < period:
        seed = sum(values) / len(values)
        return [seed for _ in values]
    result: List[float] = [float("nan")] * (period - 1)
    ema = sum(values[:period]) / period
    result.append(ema)
    multiplier = 2.0 / (period + 1.0)
    for value in values[period:]:
        ema = (value - ema) * multiplier + ema
        result.append(ema)
    return result


def ema(values: List[float], period: int) -> Optional[float]:
    series = ema_series(values, period)
    return series[-1] if series and math.isfinite(series[-1]) else None


def rsi_wilder(values: List[float], period: int = 14) -> Optional[float]:
    if len(values) < period + 1:
        return None
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for index in range(period, len(changes)):
        avg_gain = (avg_gain * (period - 1) + gains[index]) / period
        avg_loss = (avg_loss * (period - 1) + losses[index]) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def atr_value(candles: List[Dict[str, float]], period: int = 14) -> Optional[float]:
    if len(candles) < period + 1:
        return None
    true_ranges = []
    for index in range(1, len(candles)):
        current = candles[index]
        previous_close = candles[index - 1]["c"]
        true_ranges.append(max(
            current["h"] - current["l"],
            abs(current["h"] - previous_close),
            abs(current["l"] - previous_close),
        ))
    if len(true_ranges) < period:
        return None
    value = sum(true_ranges[:period]) / period
    for tr in true_ranges[period:]:
        value = (value * (period - 1) + tr) / period
    return value


def adx_value(candles: List[Dict[str, float]], period: int = 14) -> Optional[float]:
    """Wilder-style ADX with smoothed TR and directional movement."""
    if len(candles) < period * 2 + 1:
        return None
    tr_values: List[float] = []
    plus_values: List[float] = []
    minus_values: List[float] = []
    for index in range(1, len(candles)):
        current = candles[index]
        previous = candles[index - 1]
        up_move = current["h"] - previous["h"]
        down_move = previous["l"] - current["l"]
        tr = max(
            current["h"] - current["l"],
            abs(current["h"] - previous["c"]),
            abs(current["l"] - previous["c"]),
        )
        tr_values.append(tr)
        plus_values.append(up_move if up_move > down_move and up_move > 0 else 0.0)
        minus_values.append(down_move if down_move > up_move and down_move > 0 else 0.0)

    smoothed_tr = sum(tr_values[:period])
    smoothed_plus = sum(plus_values[:period])
    smoothed_minus = sum(minus_values[:period])
    dx_values: List[float] = []

    for index in range(period, len(tr_values)):
        smoothed_tr = smoothed_tr - (smoothed_tr / period) + tr_values[index]
        smoothed_plus = smoothed_plus - (smoothed_plus / period) + plus_values[index]
        smoothed_minus = smoothed_minus - (smoothed_minus / period) + minus_values[index]
        if smoothed_tr <= 0:
            continue
        plus_di = 100.0 * smoothed_plus / smoothed_tr
        minus_di = 100.0 * smoothed_minus / smoothed_tr
        denominator = plus_di + minus_di
        dx_values.append(100.0 * abs(plus_di - minus_di) / denominator if denominator else 0.0)

    if len(dx_values) < period:
        return None
    adx = sum(dx_values[:period]) / period
    for dx in dx_values[period:]:
        adx = ((adx * (period - 1)) + dx) / period
    return adx


def candle_body_ratio(candle: Dict[str, float]) -> float:
    total_range = candle["h"] - candle["l"]
    if total_range <= 0:
        return 0.0
    return abs(candle["c"] - candle["o"]) / total_range


def close_position(candle: Dict[str, float]) -> float:
    total_range = candle["h"] - candle["l"]
    if total_range <= 0:
        return 0.5
    return (candle["c"] - candle["l"]) / total_range


def volume_surge(candles: List[Dict[str, float]], lookback: int = 20) -> float:
    if len(candles) < lookback + 1:
        return 0.0
    previous = [candle["v"] for candle in candles[-(lookback + 1):-1]]
    average = sum(previous) / len(previous) if previous else 0.0
    return candles[-1]["v"] / average if average > 0 else 0.0

# ───────────────────────────── Asset and theme ────────────────────────────
def get_asset_category(symbol: str) -> str:
    base = symbol.upper().split("_")[0]
    if base in STOCK_BASES:
        return "주식"
    if base in INDEX_BASES:
        return "지수"
    if base in COMMODITY_BASES:
        return "원자재"
    return "코인"


THEMES = {
    "반도체": {"SOXL": 1, "SOXS": -1, "SKHYNIX": 1, "NVDA": 1, "AMD": 1,
              "INTC": 1, "MU": 1, "TSM": 1, "AVGO": 1, "AMAT": 1},
    "미국지수": {"NAS": 1, "NDX": 1, "SPX": 1, "US100": 1, "US30": 1,
                "DJI": 1, "TQQQ": 1, "SQQQ": -1},
}


def theme_exposure(symbol: str, direction: str) -> Tuple[Optional[str], int]:
    base = symbol.upper().split("_")[0]
    for theme, members in THEMES.items():
        if base in members:
            return theme, members[base] * (1 if direction == "LONG" else -1)
    return None, 0


def count_theme_slots(active: Dict[str, Any], theme: str, sign: int) -> int:
    count = 0
    for symbol, info in active.items():
        current_theme, current_sign = theme_exposure(symbol, info.get("direction", "LONG"))
        if current_theme == theme and current_sign == sign:
            count += 1
    return count


def calculate_reference_leverage(category: str, change_24h: float,
                                  atr: float, current_price: float) -> int:
    """Conservative display-only leverage suggestion; not account risk sizing."""
    base = 4 if category == "코인" else 3
    if abs(change_24h) >= 8:
        base -= 1
    if current_price > 0 and atr > 0:
        atr_pct = atr / current_price * 100.0
        if atr_pct >= 3.5:
            base -= 2
        elif atr_pct >= 2.0:
            base -= 1
    return max(MIN_REFERENCE_LEVERAGE, min(base, MAX_REFERENCE_LEVERAGE))

# ───────────────────────────── Signal analysis ────────────────────────────
def get_hourly_bias(symbol: str, now_ts: int) -> Optional[Dict[str, Any]]:
    candles = fetch_completed_candles(symbol, "1h", 3600, now_ts, limit=100)
    if len(candles) < 60:
        return None
    closes = [candle["c"] for candle in candles]
    ema20_now = ema(closes, 20)
    ema50_now = ema(closes, 50)
    ema20_prev = ema(closes[:-5], 20)
    adx = adx_value(candles, 14)
    rsi = rsi_wilder(closes, 14)
    if None in (ema20_now, ema50_now, ema20_prev, adx, rsi):
        return None
    slope_pct = (ema20_now - ema20_prev) / ema20_prev * 100.0 if ema20_prev else 0.0
    last_close = closes[-1]
    if last_close > ema20_now > ema50_now and slope_pct > 0.03:
        direction = "LONG"
    elif last_close < ema20_now < ema50_now and slope_pct < -0.03:
        direction = "SHORT"
    else:
        direction = "FLAT"
    return {
        "direction": direction,
        "ema20": ema20_now,
        "ema50": ema50_now,
        "adx": adx,
        "rsi": rsi,
        "slope_pct": slope_pct,
        "candles": candles,
    }


def analyze_signal(symbol: str, direction: str, now_ts: int,
                   current_price: float, btc_bias: str) -> Optional[Dict[str, Any]]:
    hourly = get_hourly_bias(symbol, now_ts)
    if not hourly or hourly["direction"] != direction:
        return None

    h1_candles = hourly["candles"]
    h1_closes = [candle["c"] for candle in h1_candles]
    h1_rsi = hourly["rsi"]
    if direction == "LONG":
        if not (RSI_LONG_1H[0] <= h1_rsi <= RSI_LONG_1H[1]):
            return None
    else:
        if not (RSI_SHORT_1H[0] <= h1_rsi <= RSI_SHORT_1H[1]):
            return None
    if hourly["adx"] < ADX_MIN_1H:
        return None

    category = get_asset_category(symbol)
    if BTC_FILTER_ENABLED and category == "코인" and symbol != "BTC_USDT":
        if btc_bias not in ("FLAT", direction):
            return None

    m15 = fetch_completed_candles(symbol, "15m", 900, now_ts, limit=100)
    if len(m15) < 45:
        return None
    closes = [candle["c"] for candle in m15]
    ema9_series = ema_series(closes, 9)
    ema21_series = ema_series(closes, 21)
    ema9_now = ema9_series[-1]
    ema21_now = ema21_series[-1]
    if not math.isfinite(ema9_now) or not math.isfinite(ema21_now):
        return None

    atr15 = atr_value(m15, 14)
    rsi15 = rsi_wilder(closes, 14)
    if atr15 is None or atr15 <= 0 or rsi15 is None:
        return None

    last = m15[-1]
    previous = m15[-2]
    body_ratio = candle_body_ratio(last)
    close_pos = close_position(last)
    surge = volume_surge(m15)
    ema_distance_atr = abs(current_price - ema21_now) / atr15

    if ema_distance_atr > MAX_EMA_DISTANCE_ATR:
        return None
    if body_ratio < BODY_RATIO_MIN:
        return None

    # Require a recent EMA21 pullback followed by a completed 15m reclaim candle.
    pullback_bars = m15[-6:-1]
    pullback_seen = False
    for index, candle in enumerate(pullback_bars, start=len(m15) - 6):
        e21 = ema21_series[index]
        if not math.isfinite(e21):
            continue
        if direction == "LONG" and candle["l"] <= e21 + atr15 * 0.25:
            pullback_seen = True
            break
        if direction == "SHORT" and candle["h"] >= e21 - atr15 * 0.25:
            pullback_seen = True
            break

    if direction == "LONG":
        if not (ema9_now > ema21_now and last["c"] > ema9_now > ema21_now):
            return None
        if not (last["c"] > last["o"] and close_pos >= 0.62):
            return None
        if not (RSI_LONG_15M[0] <= rsi15 <= RSI_LONG_15M[1]):
            return None
        if not pullback_seen:
            return None
        if current_price <= ema9_now:
            return None
        if last["c"] <= previous["c"]:
            return None
    else:
        if not (ema9_now < ema21_now and last["c"] < ema9_now < ema21_now):
            return None
        if not (last["c"] < last["o"] and close_pos <= 0.38):
            return None
        if not (RSI_SHORT_15M[0] <= rsi15 <= RSI_SHORT_15M[1]):
            return None
        if not pullback_seen:
            return None
        if current_price >= ema9_now:
            return None
        if last["c"] >= previous["c"]:
            return None

    # Quality score makes the remaining confirmation strength visible in the alert.
    score = 0
    reasons: List[str] = []
    score += 2
    reasons.append("1H EMA20/50 추세 정렬")
    if abs(hourly["slope_pct"]) >= 0.08:
        score += 1
        reasons.append(f"1H EMA20 기울기 {hourly['slope_pct']:+.2f}%")
    if hourly["adx"] >= 20:
        score += 1
        reasons.append(f"1H ADX {hourly['adx']:.1f}")
    if pullback_seen:
        score += 2
        reasons.append("15M EMA21 눌림 후 재돌파")
    if surge >= MIN_VOLUME_SURGE:
        score += 1
        reasons.append(f"거래량 증가 {surge:.2f}배")
    if body_ratio >= 0.50:
        score += 1
        reasons.append(f"15M 캔들 몸통 {body_ratio:.2f}")
    if category != "코인" or symbol == "BTC_USDT" or btc_bias == direction:
        score += 1
        reasons.append("BTC 방향 필터 통과")
    required_score = MIN_SCORE_LONG if direction == "LONG" else MIN_SCORE_SHORT
    if score < required_score:
        return None

    atr1h = atr_value(h1_candles, 14)
    if atr1h is None or atr1h <= 0:
        return None

    return {
        "score": score,
        "required_score": required_score,
        "reasons": reasons,
        "atr": atr1h,
        "atr15": atr15,
        "rsi15": rsi15,
        "rsi1h": h1_rsi,
        "adx": hourly["adx"],
        "volume_surge": surge,
        "body_ratio": body_ratio,
        "ema20_1h": hourly["ema20"],
        "ema50_1h": hourly["ema50"],
        "ema_distance_atr": ema_distance_atr,
        "ob_low": min(candle["l"] for candle in pullback_bars),
        "ob_high": max(candle["h"] for candle in pullback_bars),
    }

# ───────────────────────────── Position management ───────────────────────
def migrate_slot(info: Dict[str, Any]) -> None:
    entry = safe_float(info.get("entry_price"))
    if entry <= 0:
        return
    direction = info.get("direction", "LONG")
    info.setdefault("direction", direction)
    info.setdefault("leverage", 3)
    atr = safe_float(info.get("atr"))
    if atr <= 0:
        old_risk = safe_float(info.get("risk"))
        atr = old_risk / ATR_SL if old_risk > 0 else entry * 0.01
        info["atr"] = atr
    if safe_float(info.get("risk")) <= 0:
        info["risk"] = atr * ATR_SL
    risk = safe_float(info.get("risk"))
    sign = 1 if direction == "LONG" else -1
    info.setdefault("tp1", entry + sign * atr * ATR_TP_MULTS[0])
    info.setdefault("tp2", entry + sign * atr * ATR_TP_MULTS[1])
    info.setdefault("tp3", entry + sign * atr * ATR_TP_MULTS[2])
    info.setdefault("initial_stop", entry - sign * risk)
    info.setdefault("stop_loss", info.get("initial_stop", entry - sign * risk))
    info.setdefault("tp1_reached", False)
    info.setdefault("tp2_reached", False)
    info.setdefault("max_fav", entry)
    info.setdefault("max_adv", entry)
    info.setdefault("missing_ticker_count", 0)
    info.setdefault("missing_ticker_alerted", False)
    info.setdefault("strategy_version", 1)
    info.setdefault("entry_time", utcnow().isoformat())
    if "last_candle_ts" not in info:
        dt = parse_iso(info.get("entry_time", ""), utcnow())
        info["last_candle_ts"] = int(dt.timestamp()) // 300 * 300

    tp1_lock = entry + sign * atr * PROFIT_LOCK_ATR
    info.setdefault("tp1_lock_price", tp1_lock)
    if "tp2_lock_price" not in info:
        # Existing live slots keep their old break-even protection.
        if int(info.get("strategy_version", 1)) < 2:
            info["tp2_lock_price"] = entry
        else:
            info["tp2_lock_price"] = entry + sign * atr * TP2_LOCK_ATR

    # Do not loosen a live stop while migrating an old position.
    if direction == "LONG":
        if info.get("tp1_reached") and not info.get("tp2_reached"):
            info["stop_loss"] = max(safe_float(info["stop_loss"]), tp1_lock)
        elif info.get("tp2_reached"):
            info["stop_loss"] = max(safe_float(info["stop_loss"]), safe_float(info["tp2_lock_price"]))
    else:
        if info.get("tp1_reached") and not info.get("tp2_reached"):
            info["stop_loss"] = min(safe_float(info["stop_loss"]), tp1_lock)
        elif info.get("tp2_reached"):
            info["stop_loss"] = min(safe_float(info["stop_loss"]), safe_float(info["tp2_lock_price"]))


def step_position(info: Dict[str, Any], high: float, low: float) -> Tuple[List[str], Optional[Tuple[str, float]]]:
    direction = info["direction"]
    entry = safe_float(info["entry_price"])
    stop = safe_float(info["stop_loss"])
    tp1 = safe_float(info["tp1"])
    tp2 = safe_float(info["tp2"])
    tp3 = safe_float(info["tp3"])
    events: List[str] = []

    if direction == "LONG":
        info["max_fav"] = max(safe_float(info.get("max_fav"), entry), high)
        info["max_adv"] = min(safe_float(info.get("max_adv"), entry), low)
        # If stop and target are both inside one OHLC candle, assume stop first.
        if low <= stop:
            return events, ("STOP", stop)
        if high >= tp3:
            return events, ("TP3", tp3)
        if high >= tp1 and not info.get("tp1_reached"):
            info["tp1_reached"] = True
            new_stop = safe_float(info.get("tp1_lock_price"), entry)
            info["stop_loss"] = max(stop, new_stop)
            events.append("TP1")
        if high >= tp2 and not info.get("tp2_reached"):
            info["tp2_reached"] = True
            new_stop = safe_float(info.get("tp2_lock_price"), entry)
            info["stop_loss"] = max(safe_float(info["stop_loss"]), new_stop)
            events.append("TP2")
    else:
        info["max_fav"] = min(safe_float(info.get("max_fav"), entry), low)
        info["max_adv"] = max(safe_float(info.get("max_adv"), entry), high)
        if high >= stop:
            return events, ("STOP", stop)
        if low <= tp3:
            return events, ("TP3", tp3)
        if low <= tp1 and not info.get("tp1_reached"):
            info["tp1_reached"] = True
            new_stop = safe_float(info.get("tp1_lock_price"), entry)
            info["stop_loss"] = min(stop, new_stop)
            events.append("TP1")
        if low <= tp2 and not info.get("tp2_reached"):
            info["tp2_reached"] = True
            new_stop = safe_float(info.get("tp2_lock_price"), entry)
            info["stop_loss"] = min(safe_float(info["stop_loss"]), new_stop)
            events.append("TP2")
    return events, None


def stop_reason(info: Dict[str, Any]) -> str:
    if info.get("tp2_reached"):
        return "TP2_PROTECT"
    if info.get("tp1_reached"):
        return "PROFIT_LOCK"
    return "SL"


def finalize_trade(state: Dict[str, Any], symbol: str, info: Dict[str, Any],
                   reason: str, exit_price: float, now: datetime) -> Dict[str, Any]:
    direction = info["direction"]
    entry = safe_float(info["entry_price"])
    risk = safe_float(info["risk"])
    gross_r = r_move(direction, entry, exit_price, risk)
    fee_r = (FEE_ROUNDTRIP * entry / risk) if risk > 0 else 0.0
    net_r = gross_r - fee_r
    outcome = classify_trade(net_r)
    leverage = safe_float(info.get("leverage"), 1.0)
    raw_pct = pct_move(direction, entry, exit_price)
    leverage_pct = round(raw_pct * leverage, 2)
    entry_dt = parse_iso(info.get("entry_time", ""), now)
    holding_hours = max((now - entry_dt).total_seconds() / 3600.0, 0.0)
    mfe_r = r_move(direction, entry, safe_float(info.get("max_fav"), entry), risk)
    mae_r = r_move(direction, entry, safe_float(info.get("max_adv"), entry), risk)

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
        "holding_hours": round(holding_hours, 2),
        "gross_r": round(gross_r, 3),
        "net_r": round(net_r, 3),
        "mfe_r": round(mfe_r, 3),
        "mae_r": round(mae_r, 3),
        "leverage_pct": leverage_pct,
        "outcome": outcome,
        "strategy_version": int(safe_float(info.get("strategy_version"), 1)),
        "score": int(safe_float(info.get("score"))),
        "required_score": int(safe_float(info.get("required_score"))),
        "rsi_1h": round(safe_float(info.get("rsi_1h")), 2),
        "rsi_15m": round(safe_float(info.get("rsi_15m")), 2),
        "adx_1h": round(safe_float(info.get("adx_1h")), 2),
        "volume_surge": round(safe_float(info.get("volume_surge")), 3),
    })

    state["closed_symbols"][symbol] = {
        "time": now.isoformat(),
        "direction": direction,
        "reason": reason,
    }
    state["active_slots"].pop(symbol, None)
    return {
        "reason": reason,
        "exit_price": exit_price,
        "gross_r": gross_r,
        "net_r": net_r,
        "outcome": outcome,
        "hours": holding_hours,
        "leverage_pct": leverage_pct,
    }


def build_event_message(symbol: str, info: Dict[str, Any], event: str) -> str:
    direction = info["direction"]
    entry = safe_float(info["entry_price"])
    leverage = safe_float(info.get("leverage"), 1.0)
    target = safe_float(info["tp1"] if event == "TP1" else info["tp2"])
    pct = pct_move(direction, entry, target)
    stop = safe_float(info.get("stop_loss"))
    if event == "TP1":
        title = "🎯 <b>TP1 도달</b>"
        detail = f"수익 방어선: <code>{fmt_price(stop)}</code>"
    else:
        title = "🎯🎯 <b>TP2 도달</b>"
        detail = f"수익 보호 스탑: <code>{fmt_price(stop)}</code> (TP1 가격 부근)"
    return (
        f"{title} | {dir_tag(direction)}\n"
        f"<a href='{chart_link(symbol)}'>[{get_asset_category(symbol)}] {symbol}</a>\n\n"
        f"진입 <code>{fmt_price(entry)}</code> → 목표 <code>{fmt_price(target)}</code>\n"
        f"가격 변동 <code>{pct:+.2f}%</code> ({leverage:g}배 참고값: <code>{pct * leverage:+.1f}%</code>)\n"
        f"🔒 {detail}"
    )


def build_close_message(symbol: str, info: Dict[str, Any], trade: Dict[str, Any]) -> str:
    direction = info["direction"]
    entry = safe_float(info["entry_price"])
    exit_price = safe_float(trade["exit_price"])
    leverage = safe_float(info.get("leverage"), 1.0)
    price_pct = pct_move(direction, entry, exit_price)
    labels = {
        "SL": "🛑 <b>손절 종료</b>",
        "PROFIT_LOCK": "💰 <b>TP1 이후 수익 방어 종료</b>",
        "TP2_PROTECT": "✅ <b>TP2 이후 수익 보호 종료</b>",
        "TP3": "🏆 <b>TP3 최종 목표 도달</b>",
        "ROTATE": "🔄 <b>회전 종료</b>",
        "TIME_STOP": "⏳ <b>타임스탑 종료</b>",
    }
    result_label = {"win": "승", "draw": "무", "loss": "패"}[trade["outcome"]]
    return (
        f"{labels.get(trade['reason'], '📌 <b>포지션 종료</b>')} | {dir_tag(direction)}\n"
        f"<a href='{chart_link(symbol)}'>[{get_asset_category(symbol)}] {symbol}</a>\n\n"
        f"진입 <code>{fmt_price(entry)}</code> → 종료 <code>{fmt_price(exit_price)}</code>\n"
        f"가격 변동 <code>{price_pct:+.2f}%</code> ({leverage:g}배 참고값: <code>{price_pct * leverage:+.1f}%</code>)\n"
        f"순손익 <code>{trade['net_r']:+.2f}R</code> | 결과 <b>{result_label}</b>\n"
        f"보유 <code>{fmt_hours(trade['hours'])}</code>"
    )


def manage_positions(state: Dict[str, Any], tickers: Dict[str, float], now: datetime) -> bool:
    active = state["active_slots"]
    now_ts = int(now.timestamp())
    changed = False
    alerts: List[str] = []

    for symbol, info in list(active.items()):
        try:
            migrate_slot(info)
            entry = safe_float(info.get("entry_price"))
            risk = safe_float(info.get("risk"))
            if entry <= 0 or risk <= 0:
                logging.error("Invalid active position retained for review: %s", symbol)
                continue

            if symbol not in tickers or tickers[symbol] <= 0:
                info["missing_ticker_count"] = int(info.get("missing_ticker_count", 0)) + 1
                if (info["missing_ticker_count"] >= MISSING_TICKER_ALERT_AFTER
                        and not info.get("missing_ticker_alerted")):
                    alerts.append(
                        f"⚠️ <b>시세 조회 지연</b>\n<code>{symbol}</code> 시세가 연속 조회되지 않습니다. "
                        "슬롯은 임의 삭제하지 않았습니다. 거래소 상장/데이터 상태를 확인하세요."
                    )
                    info["missing_ticker_alerted"] = True
                changed = True
                continue
            else:
                info["missing_ticker_count"] = 0
                info["missing_ticker_alerted"] = False

            close_event = None
            next_candle_from = int(info.get("last_candle_ts", now_ts // 300 * 300)) + 300
            for candle in fetch_completed_5m(symbol, next_candle_from, now_ts):
                events, close_event = step_position(info, candle["h"], candle["l"])
                for event in events:
                    alerts.append(build_event_message(symbol, info, event))
                    changed = True
                info["last_candle_ts"] = candle["t"]
                if close_event:
                    break

            current_price = tickers[symbol]
            if not close_event:
                events, close_event = step_position(info, current_price, current_price)
                for event in events:
                    alerts.append(build_event_message(symbol, info, event))
                    changed = True

            if close_event:
                kind, exit_price = close_event
                reason = stop_reason(info) if kind == "STOP" else "TP3"
                trade = finalize_trade(state, symbol, info, reason, exit_price, now)
                alerts.append(build_close_message(symbol, info, trade))
                changed = True
                continue

            entry_dt = parse_iso(info.get("entry_time", ""), now)
            holding_hours = (now - entry_dt).total_seconds() / 3600.0
            if holding_hours >= TIME_STOP_HOURS:
                trade = finalize_trade(state, symbol, info, "TIME_STOP", current_price, now)
                alerts.append(build_close_message(symbol, info, trade))
                changed = True
            elif holding_hours >= ROTATE_HOURS:
                profit_pct = pct_move(info["direction"], entry, current_price)
                if profit_pct >= ROTATE_MIN_PROFIT_PCT:
                    trade = finalize_trade(state, symbol, info, "ROTATE", current_price, now)
                    alerts.append(build_close_message(symbol, info, trade))
                    changed = True
        except Exception:
            logging.exception("Position manager failed for %s", symbol)

    for alert in alerts:
        send_telegram_message(alert)
    return changed

# ───────────────────────────── New signal scan ───────────────────────────
def identify_new_signals(state: Dict[str, Any], raw_tickers: List[Dict[str, Any]],
                         now: datetime, start_time: float, btc_bias: str) -> bool:
    active = state["active_slots"]
    closed = state["closed_symbols"]
    now_ts = int(now.timestamp())
    changed = False
    candidates: List[Tuple[float, str, float, float, str]] = []

    for ticker in raw_tickers:
        try:
            symbol = str(ticker.get("contract", ""))
            if not symbol.endswith("_USDT") or LEVERAGED_TOKEN_RE.search(symbol):
                continue
            if symbol in active:
                continue
            price = safe_float(ticker.get("last"))
            change = safe_float(ticker.get("change_percentage"))
            volume = safe_float(ticker.get("volume_24h_quote") or ticker.get("volume_24h_settle"))
            if price <= 0 or volume < MIN_QUOTE_VOLUME:
                continue
            if change >= MIN_CHANGE_PCT:
                direction = "LONG"
            elif change <= -MIN_CHANGE_PCT:
                direction = "SHORT"
            else:
                continue

            closed_info = closed.get(symbol)
            if isinstance(closed_info, dict):
                closed_at = parse_iso(closed_info.get("time", ""), now)
                elapsed = (now - closed_at).total_seconds()
                previous_direction = closed_info.get("direction", "")
                if previous_direction == direction and elapsed < COOLDOWN_HOURS * 3600:
                    continue
                if (closed_info.get("reason") == "SL"
                        and previous_direction != direction
                        and elapsed < SL_OPPOSITE_COOLDOWN_SECONDS):
                    continue
            candidates.append((volume, symbol, price, change, direction))
        except Exception:
            continue

    # Keep the API workload bounded by liquidity, then choose among valid setups
    # by confirmation score instead of letting scan order fill the slots.
    candidates.sort(key=lambda item: item[0], reverse=True)
    qualified: List[Dict[str, Any]] = []
    for volume, symbol, price, change, direction in candidates[:MAX_CANDIDATES]:
        if time.time() - start_time > SCAN_BUDGET_SEC:
            logging.info("Scan budget exceeded; remaining candidates deferred.")
            break
        try:
            theme, theme_sign = theme_exposure(symbol, direction)
            if theme and count_theme_slots(active, theme, theme_sign) >= MAX_PER_THEME:
                continue

            analysis = analyze_signal(symbol, direction, now_ts, price, btc_bias)
            if not analysis:
                continue

            atr = analysis["atr"]
            risk = atr * ATR_SL
            sign = 1 if direction == "LONG" else -1
            tp1, tp2, tp3 = (price + sign * atr * multiplier for multiplier in ATR_TP_MULTS)
            stop = price - sign * risk
            if stop <= 0 or tp1 <= 0 or tp2 <= 0 or tp3 <= 0:
                continue
            if risk / price * 100.0 > MAX_SL_PCT:
                continue

            category = get_asset_category(symbol)
            qualified.append({
                "volume": volume,
                "symbol": symbol,
                "price": price,
                "change": change,
                "direction": direction,
                "analysis": analysis,
                "risk": risk,
                "stop": stop,
                "targets": (tp1, tp2, tp3),
                "category": category,
            })
        except Exception:
            logging.exception("Candidate analysis failed for %s", symbol)

    qualified.sort(
        key=lambda item: (item["analysis"]["score"], item["volume"]),
        reverse=True,
    )
    crypto_slots = sum(
        1 for symbol in active if get_asset_category(symbol) == "코인"
    )

    for item in qualified:
        if len(active) >= MAX_SLOTS:
            break
        symbol = item["symbol"]
        direction = item["direction"]
        price = item["price"]
        change = item["change"]
        analysis = item["analysis"]
        risk = item["risk"]
        stop = item["stop"]
        tp1, tp2, tp3 = item["targets"]
        category = item["category"]

        if category == "코인" and crypto_slots >= MAX_CRYPTO_SLOTS:
            continue
        theme, theme_sign = theme_exposure(symbol, direction)
        if theme and count_theme_slots(active, theme, theme_sign) >= MAX_PER_THEME:
            continue

        try:
            atr = analysis["atr"]
            sign = 1 if direction == "LONG" else -1
            leverage = calculate_reference_leverage(category, change, atr, price)
            entry_time = now.isoformat()
            active[symbol] = {
                "strategy_version": STRATEGY_VERSION,
                "direction": direction,
                "entry_price": price,
                "entry_time": entry_time,
                "leverage": leverage,
                "risk": risk,
                "atr": atr,
                "tp1": tp1,
                "tp2": tp2,
                "tp3": tp3,
                "stop_loss": stop,
                "initial_stop": stop,
                "tp1_lock_price": price + sign * atr * PROFIT_LOCK_ATR,
                "tp2_lock_price": price + sign * atr * TP2_LOCK_ATR,
                "tp1_reached": False,
                "tp2_reached": False,
                "max_fav": price,
                "max_adv": price,
                "last_candle_ts": now_ts // 300 * 300,
                "score": analysis["score"],
                "required_score": analysis["required_score"],
                "rsi_1h": analysis["rsi1h"],
                "rsi_15m": analysis["rsi15"],
                "adx_1h": analysis["adx"],
                "volume_surge": analysis["volume_surge"],
                "reasons": analysis["reasons"],
                "missing_ticker_count": 0,
                "missing_ticker_alerted": False,
            }
            changed = True
            if category == "코인":
                crypto_slots += 1

            def target_pct(target: float) -> float:
                return pct_move(direction, price, target)

            reasons_text = "\n".join(f"• {reason}" for reason in analysis["reasons"])
            message = (
                f"🛡️ <b>신규 시그널 | 품질 점수 {analysis['score']}/9 (기준 {analysis['required_score']})</b> | {dir_tag(direction)}\n"
                f"<a href='{chart_link(symbol)}'>[{category}] {symbol}</a>\n\n"
                f"진입가 <code>{fmt_price(price)}</code> | 참고 레버리지 <code>{leverage}배</code>\n"
                f"🎯 TP1 <code>{fmt_price(tp1)} ({target_pct(tp1):+.2f}%)</code>\n"
                f"🎯 TP2 <code>{fmt_price(tp2)} ({target_pct(tp2):+.2f}%)</code>\n"
                f"🏆 TP3 <code>{fmt_price(tp3)} ({target_pct(tp3):+.2f}%)</code>\n"
                f"🛑 SL <code>{fmt_price(stop)} ({target_pct(stop):+.2f}%)</code>\n\n"
                f"📌 <b>진입 근거</b>\n{reasons_text}\n"
                f"• 1H ADX <code>{analysis['adx']:.1f}</code> / 1H RSI <code>{analysis['rsi1h']:.1f}</code>\n"
                f"• 15M RSI <code>{analysis['rsi15']:.1f}</code> / 거래량 <code>{analysis['volume_surge']:.2f}배</code>\n"
                f"• EMA21 거리 <code>{analysis['ema_distance_atr']:.2f} ATR</code>\n\n"
                "⚠️ 시그널·가상 추적 전용입니다. 자동 주문과 부분청산은 실행하지 않습니다. "
                "참고 레버리지는 계좌 위험률이나 주문 수량을 계산하지 않습니다."
            )
            send_telegram_message(message)
            logging.info("New signal: %s %s score=%s", symbol, direction, analysis["score"])
        except Exception:
            logging.exception("Candidate activation failed for %s", symbol)
    return changed

# ───────────────────────────── Status and main ────────────────────────────
def build_slot_status_message(state: Dict[str, Any], tickers: Dict[str, float],
                               stats: Dict[str, Any]) -> str:
    active = state["active_slots"]
    symbols = list(active.keys())
    longs = sum(1 for symbol in symbols if active[symbol].get("direction") == "LONG")
    shorts = len(symbols) - longs
    lines = [f"📊 <b>슬롯 현황 ({len(symbols)} / {MAX_SLOTS}) | 🟢 롱 {longs} / 🔴 숏 {shorts}</b>\n"]

    for index in range(MAX_SLOTS):
        icon = NUM_EMOJI[index] if index < len(NUM_EMOJI) else f"{index + 1}."
        if index >= len(symbols):
            lines.append(f"{icon} (빈 슬롯)")
            continue
        symbol = symbols[index]
        info = active[symbol]
        migrate_slot(info)
        direction = info["direction"]
        entry = safe_float(info["entry_price"])
        leverage = safe_float(info.get("leverage"), 1.0)
        price = tickers.get(symbol, entry)
        pct = pct_move(direction, entry, price)
        entry_dt = parse_iso(info.get("entry_time", ""), utcnow())
        holding = fmt_hours(max((utcnow() - entry_dt).total_seconds() / 3600.0, 0.0))
        if info.get("tp2_reached"):
            status = f"🎯🎯 TP2 도달 | 보호 스탑 {fmt_price(safe_float(info['stop_loss']))}"
        elif info.get("tp1_reached"):
            status = f"🎯 TP1 도달 | 수익 방어 {fmt_price(safe_float(info['stop_loss']))}"
        else:
            status = "📈 홀딩"
        lines.append(
            f"{icon} {dir_tag(direction)} <b>[{get_asset_category(symbol)}]</b> "
            f"<a href='{chart_link(symbol)}'>{symbol}</a>\n"
            f"   <code>{pct:+.2f}% ({leverage:g}배 참고값 {pct * leverage:+.1f}%) | 보유 {holding}</code>\n"
            f"   <code>{status}</code>"
        )
    lines.append("\n" + build_stats_line(stats))
    return "\n".join(lines)


def maybe_send_daily_report(state: Dict[str, Any], tickers: Dict[str, float],
                            now: datetime, stats: Dict[str, Any]) -> bool:
    if not DAILY_REPORT_ENABLED:
        return False
    today = now.strftime("%Y-%m-%d")
    if now.hour < DAILY_REPORT_HOUR_UTC or state.get("last_report_date") == today:
        return False
    send_telegram_message(build_slot_status_message(state, tickers, stats))
    state["last_report_date"] = today
    return True


def run_bot() -> None:
    start_time = time.time()
    state = load_state()
    now = utcnow()
    raw_tickers = fetch_tickers()
    if not raw_tickers:
        state["fail_count"] = int(state.get("fail_count", 0)) + 1
        failures = state["fail_count"]
        if failures == FAIL_ALERT_THRESHOLD or (
            failures > FAIL_ALERT_THRESHOLD and (failures - FAIL_ALERT_THRESHOLD) % 12 == 0
        ):
            send_telegram_message(
                f"⚠️ <b>봇 오류 경고</b>\nGate.io 시세 조회가 {failures}회 연속 실패했습니다."
            )
        sync_stats_from_csv(state)
        save_state(state)
        return

    state["fail_count"] = 0
    tickers: Dict[str, float] = {}
    for ticker in raw_tickers:
        symbol = ticker.get("contract")
        price = safe_float(ticker.get("last"))
        if symbol and price > 0:
            tickers[str(symbol)] = price

    cooldown = timedelta(hours=COOLDOWN_HOURS)
    state["closed_symbols"] = {
        symbol: info for symbol, info in state["closed_symbols"].items()
        if (now - parse_iso(info.get("time", ""), now - cooldown * 2)) < cooldown
    }

    changed_positions = manage_positions(state, tickers, now)
    changed_new = False
    if len(state["active_slots"]) < MAX_SLOTS:
        btc = get_hourly_bias("BTC_USDT", int(now.timestamp()))
        btc_bias = btc["direction"] if btc else "FLAT"
        changed_new = identify_new_signals(state, raw_tickers, now, start_time, btc_bias)

    stats = sync_stats_from_csv(state)
    if changed_positions or changed_new:
        send_telegram_message(build_slot_status_message(state, tickers, stats))
    maybe_send_daily_report(state, tickers, now, stats)
    save_state(state)


if __name__ == "__main__":
    run_bot()
