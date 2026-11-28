#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gate.io USDT 선물 자동매매 봇
- 진입 판단은 bot.py 의 시그널 로직(analyze_signal)을 그대로 사용하고, 그중 '확실한' 시그널만 실제 주문.
- 최대 동시 포지션 3개. 조건을 만족하는 시그널이 없으면 진입하지 않음(빈 자리를 억지로 채우지 않음).
- 거래 1건 손절 시 계좌의 RISK_PCT(기본 3%) 손실이 되도록 수량 계산.
- 손절·익절은 거래소 조건부 주문(price_orders)으로 걸어 두므로 봇이 잠시 꺼져도 손절은 거래소에서 실행됨.
- 관리: TP1 50% 분할익절 → 스탑 본절+0.15ATR → 15M EMA21 트레일링, TP2 도달 시 스탑 +1.5ATR,
        TP3 전량 익절, 18시간 후 수익 중이면 회전 종료, 24시간 타임스탑.

실행 전 필수:
- Gate.io 선물 계정을 '단방향(one-way) 포지션 모드'로 설정
- API 키는 선물 거래 권한만, 출금 권한 없이 발급
- 기본은 테스트넷. 실거래는 GATE_LIVE=1 을 명시해야만 동작
"""

import hashlib
import hmac
import json
import logging
import math
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

os.environ.setdefault("LOG_FILE", "auto_trader.log")
import requests  # noqa: E402

import bot  # noqa: E402  (시그널 로직 재사용)

# ───────────────────────────── 설정 ─────────────────────────────
LIVE = os.getenv("GATE_LIVE", "0") == "1"
DRY_RUN = os.getenv("DRY_RUN", "0") == "1"          # 1 이면 주문을 보내지 않고 로그만 남김
API_KEY = os.getenv("GATE_API_KEY", "").strip()
API_SECRET = os.getenv("GATE_API_SECRET", "").strip()
HOST = "https://api.gateio.ws" if LIVE else "https://fx-api-testnet.gateio.ws"
PREFIX = "/api/v4"
SETTLE = "usdt"

MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", "3"))   # 절대 상한
# 잔고(USDT)에 따라 동시 포지션 수를 자동 조절. "기준:개수" 를 쉼표로 구분, 첫 기준 미만은 1개.
# 기본: 100 미만 1개 / 100~250 미만 2개 / 250 이상 3개 (MAX_POSITIONS 를 넘지 않음)
POSITION_TIERS = os.getenv("POSITION_TIERS") or "100:2,250:3"
RISK_PCT = float(os.getenv("RISK_PCT") or "3")        # 손절 1회당 계좌 손실 % (저장소 변수 RISK_PCT 로 조절)

# '확실한 시그널' 기준 (bot.py 의 기본 진입 조건을 모두 통과한 뒤 추가로 적용)
STRICT_MIN_SCORE = int(os.getenv("STRICT_MIN_SCORE", "4"))        # 품질 점수 6점 만점 중
STRICT_MIN_ADX = float(os.getenv("STRICT_MIN_ADX", "22"))         # 1H 추세 강도
STRICT_MIN_VOLUME = float(os.getenv("STRICT_MIN_VOLUME", "1.2"))  # 15M 거래량 배수
# 추격 진입 제외: 이미 과열된 롱 / 이미 과매도된 숏은 진입하지 않음
# (11/12~11/27 기록: 추격 진입 승률 약 48%, 그 외 약 60%. 11/27 급락 때 손실 대부분이 추격 진입)
CHASE_LONG_RSI15 = float(os.getenv("CHASE_LONG_RSI15", "62"))     # 롱: 15M RSI 이 값 초과면 제외
CHASE_LONG_RSI1H = float(os.getenv("CHASE_LONG_RSI1H", "64"))     # 롱: 1H RSI 이 값 초과면 제외
CHASE_SHORT_RSI15 = float(os.getenv("CHASE_SHORT_RSI15", "38"))   # 숏: 15M RSI 이 값 미만이면 제외
# 같은 방향(롱/숏) 동시 보유 상한: 시장 전체가 한쪽으로 움직일 때 한꺼번에 손절되는 것 방지
MAX_SAME_DIRECTION = int(os.getenv("MAX_SAME_DIRECTION") or "2")
# AI 위원회 찬성(APPROVE)을 필수로 할지: 0 = 사용 안 함(기본), 1 = 항상 필수, auto = AI 키가 있으면 필수
# 기본값 0: 11/12~11/26 기록에서 AI 반대 거래(188건, +51.2R)가 찬성 거래(78건, +13.9R)보다 성과가 좋았음.
# AI 판정 기록은 시그널 봇(ai_trades.csv)이 계속 남기므로 나중에 다시 비교 가능.
REQUIRE_AI = os.getenv("REQUIRE_AI") or "0"

LOOP_SECONDS = 60                 # 포지션 관리 주기
SCAN_EVERY_SECONDS = 300          # 신규 시그널 탐색 주기
STOP_UPDATE_MIN_ATR = 0.1         # 트레일링 스탑을 이만큼(ATR 배수) 이상 개선될 때만 주문 교체
STATE_FILE = Path(os.getenv("AUTO_STATE_FILE", "auto_state.json"))

SESSION = requests.Session()
SESSION.headers.update({"Accept": "application/json", "Content-Type": "application/json"})


# ───────────────────────────── Gate.io API (서명) ─────────────────────────────
class GateError(Exception):
    pass


def _sign(method: str, path: str, query: str, body: str, ts: str) -> str:
    body_hash = hashlib.sha512(body.encode("utf-8")).hexdigest()
    payload = "\n".join([method, path, query, body_hash, ts])
    return hmac.new(API_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha512).hexdigest()


def gate(method: str, path: str, params: Optional[Dict[str, Any]] = None,
         body: Optional[Dict[str, Any]] = None, private: bool = True) -> Any:
    full_path = f"{PREFIX}{path}"
    query = urlencode(params or {})
    body_str = json.dumps(body, separators=(",", ":")) if body is not None else ""
    headers: Dict[str, str] = {}
    if private:
        ts = str(int(time.time()))
        headers = {"KEY": API_KEY, "Timestamp": ts, "SIGN": _sign(method, full_path, query, body_str, ts)}
    url = f"{HOST}{full_path}" + (f"?{query}" if query else "")
    response = SESSION.request(method, url, data=body_str or None, headers=headers, timeout=15)
    if response.status_code >= 400:
        raise GateError(f"{method} {path} {response.status_code}: {response.text[:300]}")
    return response.json() if response.text else None


def get_account() -> Dict[str, Any]:
    return gate("GET", f"/futures/{SETTLE}/accounts")


def get_contract(symbol: str) -> Dict[str, Any]:
    return gate("GET", f"/futures/{SETTLE}/contracts/{symbol}", private=False)


def get_position_size(symbol: str) -> int:
    try:
        pos = gate("GET", f"/futures/{SETTLE}/positions/{symbol}")
        return int(float(pos.get("size", 0) or 0))
    except GateError as exc:
        if "POSITION_NOT_FOUND" in str(exc):
            return 0
        raise


def get_last_price(symbol: str) -> float:
    data = gate("GET", f"/futures/{SETTLE}/tickers", {"contract": symbol}, private=False)
    return bot.safe_float(data[0].get("last")) if data else 0.0


def set_leverage(symbol: str, leverage: int) -> None:
    # 단방향 모드에서 leverage > 0 이면 격리 마진
    if DRY_RUN:
        logging.info("[DRY] leverage %s x%s", symbol, leverage)
        return
    gate("POST", f"/futures/{SETTLE}/positions/{symbol}/leverage", {"leverage": str(leverage)})


def market_order(symbol: str, size: int, reduce_only: bool = False, close: bool = False) -> Dict[str, Any]:
    body: Dict[str, Any] = {"contract": symbol, "size": 0 if close else size, "price": "0", "tif": "ioc",
                            "text": "t-autobot"}
    if close:
        body["close"] = True
    elif reduce_only:
        body["reduce_only"] = True
    if DRY_RUN:
        logging.info("[DRY] market order %s", body)
        return {"id": 0, "fill_price": "0", "size": size}
    return gate("POST", f"/futures/{SETTLE}/orders", body=body)


def place_trigger(symbol: str, trigger_price: float, rule: int, size: int, close: bool, tick: str) -> int:
    """rule: 1 = 가격 >= 트리거, 2 = 가격 <= 트리거. close=True 면 남은 포지션 전량 종료."""
    body = {
        "initial": {"contract": symbol, "size": 0 if close else size, "price": "0", "tif": "ioc",
                    "reduce_only": True, "text": "t-autobot"},
        "trigger": {"strategy_type": 0, "price_type": 0, "price": round_price(trigger_price, tick),
                    "rule": rule, "expiration": 0},
    }
    if close:
        body["initial"]["close"] = True
    if DRY_RUN:
        logging.info("[DRY] trigger %s", body)
        return 0
    result = gate("POST", f"/futures/{SETTLE}/price_orders", body=body)
    return int(result.get("id", 0))


def cancel_trigger(order_id: int) -> None:
    if not order_id or DRY_RUN:
        return
    try:
        gate("DELETE", f"/futures/{SETTLE}/price_orders/{order_id}")
    except GateError as exc:
        logging.info("trigger cancel skipped (%s): %s", order_id, exc)


def cancel_all_triggers(symbol: str) -> None:
    if DRY_RUN:
        return
    try:
        gate("DELETE", f"/futures/{SETTLE}/price_orders", {"contract": symbol})
    except GateError as exc:
        logging.info("trigger cancel-all skipped (%s): %s", symbol, exc)


def round_price(price: float, tick: str) -> str:
    step = float(tick or 0) or 0.0
    if step <= 0:
        return f"{price:.8g}"
    decimals = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return f"{round(round(price / step) * step, decimals):.{decimals}f}"


# ───────────────────────────── 상태 ─────────────────────────────
def load_state() -> Dict[str, Any]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            logging.exception("state load failed; starting fresh")
    return {"positions": {}, "closed": {}, "sl_history": []}


def save_state(state: Dict[str, Any]) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def notify(text: str) -> None:
    tag = "🔴 실거래" if LIVE else "🧪 테스트넷"
    bot.send_telegram_message(f"[{tag}] {text}")


# ───────────────────────────── 확실한 시그널 판정 ─────────────────────────────
def ai_required() -> bool:
    if REQUIRE_AI == "1":
        return True
    if REQUIRE_AI == "0" or not bot.ai_committee:
        return False
    return bool(os.getenv("GEMINI_API_KEY") or os.getenv("GROQ_API_KEY") or os.getenv("OPENROUTER_API_KEY"))


def parse_tiers(text: str) -> List[Tuple[float, int]]:
    tiers = []
    for part in text.split(","):
        try:
            threshold, count = part.split(":")
            tiers.append((float(threshold), int(count)))
        except ValueError:
            logging.warning("POSITION_TIERS 항목을 무시합니다: %r", part)
    return sorted(tiers)


def max_positions_for(equity: float) -> int:
    """잔고 구간에 맞는 최대 동시 포지션 수."""
    count = 1
    for threshold, tier_count in parse_tiers(POSITION_TIERS):
        if equity >= threshold:
            count = tier_count
    return max(1, min(count, MAX_POSITIONS))


def tiers_text() -> str:
    parts, low = [], 0.0
    tiers = parse_tiers(POSITION_TIERS)
    for threshold, count in tiers:
        parts.append(f"{low:g}~{threshold:g}$ {max_positions_for(low)}개")
        low = threshold
    parts.append(f"{low:g}$ 이상 {max_positions_for(low)}개")
    return " / ".join(parts)


def is_chasing(direction: str, analysis: Dict[str, Any]) -> bool:
    """이미 많이 움직인 뒤 따라 들어가는 진입인지."""
    if direction == "LONG":
        return analysis["rsi15"] > CHASE_LONG_RSI15 or analysis["rsi1h"] > CHASE_LONG_RSI1H
    return analysis["rsi15"] < CHASE_SHORT_RSI15


def same_direction_count(state: Dict[str, Any], direction: str) -> int:
    return sum(1 for info in state["positions"].values() if info.get("direction") == direction)


def is_strong(analysis: Dict[str, Any]) -> bool:
    return (analysis["score"] >= STRICT_MIN_SCORE
            and analysis["adx"] >= STRICT_MIN_ADX
            and analysis["volume_surge"] >= STRICT_MIN_VOLUME)


def in_cooldown(state: Dict[str, Any], symbol: str, direction: str, now: datetime) -> bool:
    info = state["closed"].get(symbol)
    if info:
        elapsed = (now - bot.parse_iso(info["time"], now)).total_seconds()
        hours = bot.SL_COOLDOWN_HOURS if info.get("reason") == "SL" else bot.COOLDOWN_HOURS
        if info.get("direction") == direction and elapsed < hours * 3600:
            return True
        if info.get("reason") == "SL" and info.get("direction") != direction and elapsed < bot.SL_OPPOSITE_COOLDOWN_SECONDS:
            return True
    recent_sl = [t for s, t in state["sl_history"]
                 if s == symbol and (now - bot.parse_iso(t, now)).total_seconds() <= 86400]
    return len(recent_sl) >= bot.MAX_DAILY_SL_LOCKOUT


def find_strong_signals(state: Dict[str, Any], now: datetime) -> List[Dict[str, Any]]:
    raw = bot.fetch_tickers()
    now_ts = int(now.timestamp())
    btc = bot.get_hourly_bias("BTC_USDT", now_ts)
    btc_bias = btc["direction"] if btc else "FLAT"
    candidates = []
    for ticker in raw:
        symbol = str(ticker.get("contract", ""))
        if not symbol.endswith("_USDT") or bot.LEVERAGED_TOKEN_RE.search(symbol):
            continue
        if symbol in state["positions"]:
            continue
        price = bot.safe_float(ticker.get("last"))
        change = bot.safe_float(ticker.get("change_percentage"))
        volume = bot.safe_float(ticker.get("volume_24h_quote") or ticker.get("volume_24h_settle"))
        if price <= 0 or volume < bot.MIN_QUOTE_VOLUME:
            continue
        if change >= bot.MIN_CHANGE_PCT:
            direction = "LONG"
        elif change <= -bot.MIN_CHANGE_PCT:
            direction = "SHORT"
        else:
            continue
        if in_cooldown(state, symbol, direction, now):
            continue
        candidates.append((volume, symbol, price, change, direction))

    candidates.sort(reverse=True)
    strong = []
    start = time.time()
    for volume, symbol, price, change, direction in candidates[:bot.MAX_CANDIDATES]:
        if time.time() - start > bot.SCAN_BUDGET_SEC:
            break
        try:
            analysis = bot.analyze_signal(symbol, direction, now_ts, price, btc_bias)
            if not analysis or not is_strong(analysis):
                continue
            if is_chasing(direction, analysis):
                logging.info("chasing entry skipped: %s %s rsi15=%.1f rsi1h=%.1f",
                             symbol, direction, analysis["rsi15"], analysis["rsi1h"])
                continue
            sl_pct = analysis["atr"] * bot.ATR_SL / price * 100.0
            if sl_pct > bot.MAX_SL_PCT:
                continue
            strong.append({"symbol": symbol, "direction": direction, "price": price, "change": change,
                           "volume": volume, "analysis": analysis, "sl_pct": sl_pct, "btc_bias": btc_bias})
        except Exception:
            logging.exception("analysis failed for %s", symbol)
    strong.sort(key=lambda s: (s["analysis"]["score"], s["analysis"]["volume_surge"]), reverse=True)
    return strong


def theme_blocked(state: Dict[str, Any], symbol: str, direction: str) -> bool:
    theme, sign = bot.theme_exposure(symbol, direction)
    if not theme:
        return False
    for held, info in state["positions"].items():
        t, s = bot.theme_exposure(held, info["direction"])
        if t == theme and s == sign:
            return True
    return False


def ai_approves(sig: Dict[str, Any]) -> Tuple[bool, str]:
    a = sig["analysis"]
    sign = 1 if sig["direction"] == "LONG" else -1
    price, atr = sig["price"], a["atr"]
    slot = {"direction": sig["direction"], "entry_price": price,
            "stop_loss": price - sign * atr * bot.ATR_SL,
            "tp1": price + sign * atr * bot.ATR_TP_MULTS[0],
            "tp2": price + sign * atr * bot.ATR_TP_MULTS[1],
            "tp3": price + sign * atr * bot.ATR_TP_MULTS[2]}
    text = bot.ai_committee.review_and_format(
        slot, sig["symbol"], bot.get_asset_category(sig["symbol"]), sig["change"], sig["btc_bias"],
        a, {"stats": {}}, time.time())
    verdict = (slot.get("ai") or {}).get("verdict", "UNAVAILABLE")
    return verdict == "APPROVE", text


# ───────────────────────────── 진입 ─────────────────────────────
def open_position(state: Dict[str, Any], sig: Dict[str, Any], now: datetime, limit: int = MAX_POSITIONS) -> None:
    symbol, direction, a = sig["symbol"], sig["direction"], sig["analysis"]
    sign = 1 if direction == "LONG" else -1
    contract = get_contract(symbol)
    multiplier = bot.safe_float(contract.get("quanto_multiplier"), 1.0) or 1.0
    min_size = int(bot.safe_float(contract.get("order_size_min"), 1.0) or 1)
    tick = str(contract.get("order_price_round", "0"))

    account = get_account()
    if account.get("in_dual_mode"):
        raise GateError("양방향(dual) 포지션 모드입니다. 단방향(one-way) 모드로 바꿔 주세요.")
    equity = bot.safe_float(account.get("total"))
    available = bot.safe_float(account.get("available"))
    price = get_last_price(symbol) or sig["price"]

    # 수량: 손절 시 계좌의 RISK_PCT% 손실
    risk_dist = a["atr"] * bot.ATR_SL
    sl_pct = risk_dist / price * 100.0
    notional = equity * RISK_PCT / 100.0 / (sl_pct / 100.0)
    leverage, _, _ = bot.calculate_reference_leverage(symbol, sl_pct, sig["volume"], sig["change"],
                                                      a.get("atr_ratio", 1.0))
    # 남은 자리 수만큼 가용 증거금을 나눠서, 이 포지션 증거금이 그 몫을 넘지 않게 제한
    free_slots = max(1, limit - len(state["positions"]))
    max_margin = available * 0.95 / free_slots
    if notional / leverage > max_margin:
        notional = max_margin * leverage
    size = int(notional / (price * multiplier))
    if size < min_size:
        logging.info("size too small for %s (%s < %s); skip", symbol, size, min_size)
        return

    set_leverage(symbol, leverage)
    order = market_order(symbol, sign * size)
    fill = bot.safe_float(order.get("fill_price")) or price
    filled = abs(int(bot.safe_float(order.get("size"), size) - bot.safe_float(order.get("left"), 0)))
    if not DRY_RUN:
        filled = abs(get_position_size(symbol))
    if filled <= 0:
        notify(f"⚠️ {symbol} 진입 주문이 체결되지 않았습니다.")
        return

    atr = a["atr"]
    stop = fill - sign * risk_dist
    tp1, tp2, tp3 = (fill + sign * atr * m for m in bot.ATR_TP_MULTS)
    rule_stop, rule_tp = (2, 1) if direction == "LONG" else (1, 2)
    half = filled // 2 if filled // 2 >= min_size else 0
    sl_id = place_trigger(symbol, stop, rule_stop, 0, True, tick)
    tp1_id = place_trigger(symbol, tp1, rule_tp, -sign * half, False, tick) if half else 0
    tp3_id = place_trigger(symbol, tp3, rule_tp, 0, True, tick)

    state["positions"][symbol] = {
        "direction": direction, "entry_price": fill, "entry_time": now.isoformat(),
        "size": filled, "leverage": leverage, "atr": atr, "risk": risk_dist,
        "stop": stop, "tp1": tp1, "tp2": tp2, "tp3": tp3, "tick": tick, "min_size": min_size,
        "tp1_reached": False, "tp2_reached": False,
        "sl_id": sl_id, "tp1_id": tp1_id, "tp3_id": tp3_id,
        "score": a["score"],
    }
    save_state(state)
    notify(
        f"🟢 <b>진입</b> {symbol} {bot.dir_tag(direction)}\n"
        f"체결가 <code>{bot.fmt_price(fill)}</code> | 수량 {filled}계약 | {leverage}배 격리\n"
        f"SL <code>{bot.fmt_price(stop)}</code> (계좌 -{RISK_PCT:g}%)\n"
        f"TP1 <code>{bot.fmt_price(tp1)}</code> · TP2 <code>{bot.fmt_price(tp2)}</code> · TP3 <code>{bot.fmt_price(tp3)}</code>\n"
        f"품질 점수 {a['score']}/{bot.MAX_SCORE} · ADX {a['adx']:.1f} · 거래량 {a['volume_surge']:.2f}배"
    )


# ───────────────────────────── 관리 ─────────────────────────────
def replace_stop(symbol: str, info: Dict[str, Any], new_stop: float) -> None:
    rule = 2 if info["direction"] == "LONG" else 1
    new_id = place_trigger(symbol, new_stop, rule, 0, True, info["tick"])  # 새 스탑을 먼저 걸고
    cancel_trigger(info.get("sl_id", 0))                                    # 기존 스탑 취소
    info["sl_id"] = new_id
    info["stop"] = new_stop


def record_close(state: Dict[str, Any], symbol: str, info: Dict[str, Any], reason: str,
                 price: float, now: datetime) -> None:
    cancel_all_triggers(symbol)
    sign = 1 if info["direction"] == "LONG" else -1
    pct = (price - info["entry_price"]) / info["entry_price"] * 100.0 * sign
    final_reason = reason
    if reason == "STOPPED":
        final_reason = "TRAILING_STOP" if info["tp1_reached"] else "SL"
    state["closed"][symbol] = {"time": now.isoformat(), "direction": info["direction"], "reason": final_reason}
    if final_reason == "SL":
        state["sl_history"].append([symbol, now.isoformat()])
    state["sl_history"] = [x for x in state["sl_history"]
                           if (now - bot.parse_iso(x[1], now)).total_seconds() <= 86400]
    del state["positions"][symbol]
    save_state(state)
    notify(f"⚪ <b>종료</b> {symbol} {bot.dir_tag(info['direction'])} | {final_reason}\n"
           f"진입 <code>{bot.fmt_price(info['entry_price'])}</code> → 현재가 <code>{bot.fmt_price(price)}</code>"
           f" ({pct:+.2f}%, 레버리지 미반영)")


def manage(state: Dict[str, Any], now: datetime) -> None:
    for symbol, info in list(state["positions"].items()):
        try:
            direction = info["direction"]
            sign = 1 if direction == "LONG" else -1
            entry, atr = info["entry_price"], info["atr"]
            price = get_last_price(symbol)
            size = abs(get_position_size(symbol)) if not DRY_RUN else info["size"]

            if size == 0:  # 거래소에서 SL/TP3 가 실행됐거나 수동 종료
                reached_tp3 = (price - info["tp3"]) * sign >= 0
                record_close(state, symbol, info, "TP3" if reached_tp3 else "STOPPED", price, now)
                continue

            def beyond(level: float) -> bool:
                return (price - level) * sign >= 0

            # TP1: 분할익절 체결(수량 감소) 또는 가격 도달
            if not info["tp1_reached"] and (size < info["size"] or beyond(info["tp1"])):
                info["tp1_reached"] = True
                if size >= info["size"] and info.get("tp1_id"):  # 트리거가 아직 안 돌았으면 직접 절반 청산
                    cancel_trigger(info["tp1_id"])
                    half = size // 2
                    if half >= info["min_size"]:
                        market_order(symbol, -sign * half, reduce_only=True)
                replace_stop(symbol, info, entry + sign * atr * bot.PROFIT_LOCK_ATR)
                notify(f"🎯 <b>TP1</b> {symbol} 50% 익절, 스탑을 본절로 이동")

            if info["tp1_reached"]:
                target = info["stop"]
                if not info["tp2_reached"] and beyond(info["tp2"]):
                    info["tp2_reached"] = True
                    target = entry + sign * atr * bot.TP2_LOCK_ATR
                    notify(f"🎯 <b>TP2</b> {symbol} 도달, 스탑을 TP1 가격으로 상향")
                m15 = bot.fetch_completed_candles(symbol, "15m", 900, int(now.timestamp()), limit=50)
                if len(m15) >= 21:
                    ema21 = bot.ema([c["c"] for c in m15], 21)
                    if ema21 and (ema21 - target) * sign > 0 and (price - ema21) * sign > 0:
                        target = ema21
                if (target - info["stop"]) * sign >= atr * STOP_UPDATE_MIN_ATR:
                    replace_stop(symbol, info, target)

            held_h = (now - bot.parse_iso(info["entry_time"], now)).total_seconds() / 3600.0
            pct = (price - entry) / entry * 100.0 * sign
            if held_h >= bot.TIME_STOP_HOURS:
                market_order(symbol, 0, close=True)
                record_close(state, symbol, info, "TIME_STOP", price, now)
                continue
            if held_h >= bot.ROTATE_HOURS and pct >= bot.ROTATE_MIN_PROFIT_PCT:
                market_order(symbol, 0, close=True)
                record_close(state, symbol, info, "ROTATE", price, now)
                continue
            save_state(state)
        except Exception:
            logging.exception("manage failed for %s", symbol)


def scan_and_enter(state: Dict[str, Any], now: datetime) -> None:
    equity = bot.safe_float(get_account().get("total"))
    limit = max_positions_for(equity)
    if state.get("limit") != limit:
        if "limit" in state:
            notify(f"⚖️ 잔고 {equity:,.2f} USDT → 최대 동시 포지션 {state['limit']}개 → <b>{limit}개</b>")
        state["limit"] = limit
    if len(state["positions"]) >= limit:
        return
    signals = find_strong_signals(state, now)
    if signals:
        logging.info("strong signals: %s", [(s["symbol"], s["direction"], s["analysis"]["score"]) for s in signals])
    use_ai = ai_required()
    for sig in signals:
        if len(state["positions"]) >= limit:
            break
        if theme_blocked(state, sig["symbol"], sig["direction"]):
            continue
        if same_direction_count(state, sig["direction"]) >= MAX_SAME_DIRECTION:
            logging.info("same-direction cap: %s %s skipped", sig["symbol"], sig["direction"])
            continue
        if use_ai:
            ok, _ = ai_approves(sig)
            if not ok:
                logging.info("AI not approved: %s %s", sig["symbol"], sig["direction"])
                continue
        try:
            open_position(state, sig, now, limit)
        except Exception as exc:
            logging.exception("entry failed for %s", sig["symbol"])
            notify(f"⚠️ {sig['symbol']} 진입 실패: {str(exc)[:200]}")


def run_once() -> None:
    """GitHub Actions 용: 포지션 관리 + 신규 시그널 탐색을 한 번 하고 종료 (약 5분마다 실행)."""
    if not API_KEY or not API_SECRET:
        raise SystemExit("GATE_API_KEY / GATE_API_SECRET 환경변수를 설정하세요.")
    first_run = not STATE_FILE.exists()
    state = load_state()
    if first_run:
        account = get_account()
        if account.get("in_dual_mode"):
            notify("⚠️ 양방향(dual) 포지션 모드입니다. 단방향(one-way) 모드로 바꿔야 자동매매가 동작합니다.")
            raise SystemExit("dual mode")
        notify(f"🤖 자동매매 시작 (약 5분마다 확인) | 잔고 {bot.safe_float(account.get('total')):.2f} USDT | "
               f"최대 {max_positions_for(bot.safe_float(account.get('total')))}포지션 ({tiers_text()}) · 건당 위험 {RISK_PCT:g}% · 같은 방향 최대 {MAX_SAME_DIRECTION}개 · 추격 진입 제외 · AI 필터 {'ON' if ai_required() else 'OFF'}")
    now = bot.utcnow()
    try:
        manage(state, now)
        scan_and_enter(state, now)
    finally:
        save_state(state)


def main() -> None:
    if not API_KEY or not API_SECRET:
        raise SystemExit("GATE_API_KEY / GATE_API_SECRET 환경변수를 설정하세요.")
    account = get_account()
    if account.get("in_dual_mode"):
        raise SystemExit("양방향(dual) 포지션 모드입니다. Gate.io 에서 단방향(one-way) 모드로 바꾼 뒤 실행하세요.")
    mode = "실거래" if LIVE else "테스트넷"
    logging.info("auto trader start: %s, equity %s USDT, dry_run=%s", mode, account.get("total"), DRY_RUN)
    notify(f"🤖 자동매매 시작 | 잔고 {bot.safe_float(account.get('total')):.2f} USDT | "
           f"최대 {max_positions_for(bot.safe_float(account.get('total')))}포지션 ({tiers_text()}) · 건당 위험 {RISK_PCT:g}% · 같은 방향 최대 {MAX_SAME_DIRECTION}개 · 추격 진입 제외 · AI 필터 {'ON' if ai_required() else 'OFF'}")

    state = load_state()
    last_scan = 0.0
    while True:
        now = bot.utcnow()
        try:
            manage(state, now)
            if time.time() - last_scan >= SCAN_EVERY_SECONDS:
                last_scan = time.time()
                scan_and_enter(state, now)
        except Exception:
            logging.exception("loop error")
        time.sleep(LOOP_SECONDS)


if __name__ == "__main__":
    run_once() if os.getenv("RUN_ONCE") == "1" else main()
