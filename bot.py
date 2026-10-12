from datetime import datetime, timedelta, timezone
import json
import os
import random
import time
import requests

# 환경 변수 로드
TOKEN = os.getenv("GATE_TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("GATE_TELEGRAM_CHAT_ID", "").strip()
STATE_FILE = "gate_state.json"
KST = timezone(timedelta(hours=9))

# Gate.io 선물 API 엔드포인트
GATE_FUTURES_CONTRACTS = (
    "https://fx-api.gateio.ws/api/v4/futures/usdt/contracts"
)
GATE_FUTURES_CANDLES = "https://fx-api.gateio.ws/api/v4/futures/usdt/candlesticks"


def now():
  return datetime.now(KST)


def load_state():
  try:
    with open(STATE_FILE, encoding="utf-8") as f:
      return json.load(f)
  except Exception:
    return {"signals": {}, "active_positions": {}}


def save_state(s):
  with open(STATE_FILE + ".tmp", "w", encoding="utf-8") as f:
    json.dump(s, f, ensure_ascii=False, indent=2)
  os.replace(STATE_FILE + ".tmp", STATE_FILE)


def send_telegram(msg):
  if not TOKEN or not CHAT_ID:
    print("Telegram Config Error: Token or Chat ID missing.")
    return False
  url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
  data = {
      "chat_id": CHAT_ID,
      "text": msg,
      "parse_mode": "Markdown",
      "disable_web_page_preview": True,
  }
  try:
    r = requests.post(url, json=data, timeout=15)
    return r.status_code == 200
  except Exception as e:
    print(f"Telegram error: {e}")
  return False


def get_randomized_contracts():
  try:
    r = requests.get(GATE_FUTURES_CONTRACTS, timeout=10)
    if r.status_code == 200:
      data = r.json()
      contracts = [
          item["name"] for item in data if not item.get("in_delisting", False)
      ]
      random.shuffle(contracts)
      return contracts
  except Exception as e:
    print(f"Error fetching contracts: {e}")
  return []


def fetch_candles(contract, interval="1h", limit=50):
  url = f"{GATE_FUTURES_CANDLES}?contract={contract}&interval={interval}&limit={limit}"
  try:
    r = requests.get(url, timeout=5)
    if r.status_code == 200:
      return r.json()
  except Exception:
    pass
  return []


def parse_candles(candles):
  closes, highs, lows, volumes = [], [], [], []
  for c in candles:
    if isinstance(c, list) and len(c) >= 5:
      try:
        closes.append(float(c[2]))
        highs.append(float(c[3]))
        lows.append(float(c[4]))
        volumes.append(float(c[1]))
      except Exception:
        pass
  return closes, highs, lows, volumes


def calculate_rsi(closes, period=14):
  if len(closes) < period + 1:
    return 50.0
  gains, losses = 0.0, 0.0
  for i in range(1, period + 1):
    diff = closes[-i] - closes[-i - 1]
    if diff >= 0:
      gains += diff
    else:
      losses -= diff
  avg_gain, avg_loss = gains / period, losses / period
  if avg_loss == 0:
    return 100.0
  return 100 - (100 / (1 + (avg_gain / avg_loss)))


def track_positions(state):
  active = state.get("active_positions", {})
  to_remove = []

  for contract, pos in list(active.items()):
    candles = fetch_candles(contract, interval="5m", limit=3)
    if not candles:
      continue

    _, highs, lows, _ = parse_candles(candles)
    if not highs or not lows:
      continue

    high, low = highs[-1], lows[-1]
    coin_name = contract.replace("_USDT", "")

    signal_type = pos["type"]
    entry = pos["entry"]
    tp1, tp2, tp3, sl = pos["tp1"], pos["tp2"], pos["tp3"], pos["sl"]
    hit = pos.get("hit", [])

    if signal_type == "LONG":
      if low <= sl:
        send_telegram(
            f"🛡️ *[손절 알림]* `{coin_name}/USDT` (LONG)\n\n손절가"
            f" `{sl:,.4f}` 도달로 포지션 종료"
        )
        to_remove.append(contract)
        continue
      if high >= tp3 and "TP3" not in hit:
        send_telegram(
            f"🎉 *[TP3 최종 익절]* `{coin_name}/USDT` (LONG)\n\n최종 목표가"
            f" `{tp3:,.4f}` 달성완료! 포지션 종료"
        )
        to_remove.append(contract)
        continue
      if high >= tp2 and "TP2" not in hit:
        send_telegram(
            f"🎯 *[TP2 익절 달성]* `{coin_name}/USDT` (LONG)\n\n2차 목표가"
            f" `{tp2:,.4f}` 도달!"
        )
        pos["hit"].append("TP2")
      if high >= tp1 and "TP1" not in hit:
        send_telegram(
            f"🎯 *[TP1 익절 달성]* `{coin_name}/USDT` (LONG)\n\n1차 목표가"
            f" `{tp1:,.4f}` 도달! (손절가를 본전 `{entry:,.4f}`로 상향)"
        )
        pos["sl"] = entry
        pos["hit"].append("TP1")

    elif signal_type == "SHORT":
      if high >= sl:
        send_telegram(
            f"🛡️ *[손절 알림]* `{coin_name}/USDT` (SHORT)\n\n손절가"
            f" `{sl:,.4f}` 도달로 포지션 종료"
        )
        to_remove.append(contract)
        continue
      if low <= tp3 and "TP3" not in hit:
        send_telegram(
            f"🎉 *[TP3 최종 익절]* `{coin_name}/USDT` (SHORT)\n\n최종 목표가"
            f" `{tp3:,.4f}` 달성완료! 포지션 종료"
        )
        to_remove.append(contract)
        continue
      if low <= tp2 and "TP2" not in hit:
        send_telegram(
            f"🎯 *[TP2 익절 달성]* `{coin_name}/USDT` (SHORT)\n\n2차 목표가"
            f" `{tp2:,.4f}` 도달!"
        )
        pos["hit"].append("TP2")
      if low <= tp1 and "TP1" not in hit:
        send_telegram(
            f"🎯 *[TP1 익절 달성]* `{coin_name}/USDT` (SHORT)\n\n1차 목표가"
            f" `{tp1:,.4f}` 도달! (손절가를 본전 `{entry:,.4f}`로 하향)"
        )
        pos["sl"] = entry
        pos["hit"].append("TP1")

  for c in to_remove:
    del active[c]
  state["active_positions"] = active


def analyze_market():
  state = load_state()
  track_positions(state)

  contracts = get_randomized_contracts()
  if not contracts:
    return

  last_signals = state.get("signals", {})
  active_positions = state.get("active_positions", {})
  current_time = time.time()

  for contract in contracts:
    if contract in active_positions:
      continue
    if current_time - last_signals.get(contract, {}).get("time", 0) < 14400:
      continue

    candles = fetch_candles(contract, interval="1h", limit=50)
    if not candles or len(candles) < 30:
      continue

    closes, highs, lows, volumes = parse_candles(candles)
    if len(closes) < 30:
      continue

    current_price = closes[-1]

    # 최소 거래대금 필터 완화
    avg_vol = sum(volumes[-20:]) / 20
    if avg_vol * current_price < 30000:
      continue

    ema20 = sum(closes[-20:]) / 20
    ema50 = sum(closes[-50:]) / 50 if len(closes) >= 50 else ema20
    rsi = calculate_rsi(closes, 14)

    sma20 = ema20
    std_dev = (sum((x - sma20) ** 2 for x in closes[-20:]) / 20) ** 0.5
    upper_band = sma20 + (2 * std_dev)
    lower_band = sma20 - (2 * std_dev)

    # 거래량 돌파 조건 완화 (1.3배 -> 1.1배)
    is_high_volume = volumes[-1] > (avg_vol * 1.1)

    signal_type = None
    # 롱 조건 완화 (RSI 범위 넓힘, 볼린저 밴드 조건 유연화)
    if (
        current_price >= ema50 * 0.98
        and (30 <= rsi <= 60 or current_price <= lower_band * 1.02)
        and is_high_volume
    ):
      signal_type = "LONG"
    # 숏 조건 완화
    elif (
        current_price <= ema50 * 1.02
        and (40 <= rsi <= 70 or current_price >= upper_band * 0.98)
        and is_high_volume
    ):
      signal_type = "SHORT"

    if signal_type:
      atr = sum([highs[i] - lows[i] for i in range(-14, 0)]) / 14

      if signal_type == "LONG":
        sl = current_price - (atr * 1.5)
        tp1 = current_price + (atr * 1.5)
        tp2 = current_price + (atr * 2.8)
        tp3 = current_price + (atr * 4.5)
      else:
        sl = current_price + (atr * 1.5)
        tp1 = current_price - (atr * 1.5)
        tp2 = current_price - (atr * 2.8)
        tp3 = current_price - (atr * 4.5)

      coin_name = contract.replace("_USDT", "")
      msg = (
          f"🚨 *Gate.io Futures Signal* 🚨\n\n"
          f"📌 *종목*: `{coin_name}/USDT` (1시간봉)\n"
          f"📊 *방향*: **{signal_type}** {'🟢' if signal_type == 'LONG' else '🔴'}\n"
          f"💰 *진입가*: `{current_price:,.4f}`\n\n"
          f"📌 *단일 포지션 권장 가이드*\n"
          f"  • *추천 레버리지*: `5x ~ 10x` (격리 마진)\n"
          f"  • *자금 비중*: `총 자산의 5%`\n\n"
          f"🎯 *익절 라인*\n"
          f"  • *TP1*: `{tp1:,.4f}` (1차 익절 & 본전스탑)\n"
          f"  • *TP2*: `{tp2:,.4f}` (2차 익절)\n"
          f"  • *TP3*: `{tp3:,.4f}` (최종 익절)\n\n"
          f"🛡️ *손절가(SL)*: `{sl:,.4f}`\n"
          f"📈 *보조지표*: RSI `({rsi:.1f})`, 거래량 돌파\n"
          f"⏱️ *시간*: `{now().strftime('%Y-%m-%d %H:%M:%S')} (KST)`"
      )

      if send_telegram(msg):
        last_signals[contract] = {"time": current_time, "type": signal_type}
        active_positions[contract] = {
            "type": signal_type,
            "entry": current_price,
            "tp1": tp1,
            "tp2": tp2,
            "tp3": tp3,
            "sl": sl,
            "hit": [],
        }
      time.sleep(0.3)

  state["signals"] = last_signals
  state["active_positions"] = active_positions
  save_state(state)


if __name__ == "__main__":
  analyze_market()
