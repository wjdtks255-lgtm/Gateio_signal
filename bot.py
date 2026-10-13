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
    print(f"Telegram API Response: {r.status_code} - {r.text}")
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


def analyze_market():
  state = load_state()
  contracts = get_randomized_contracts()
  if not contracts:
    return

  contracts = contracts[:150]
  last_signals = state.get("signals", {})
  current_time = time.time()
  signals_list = []

  for contract in contracts:
    if len(signals_list) >= 8:
      break

    candles = fetch_candles(contract, interval="1h", limit=50)
    if not candles or len(candles) < 20:
      continue

    closes, highs, lows, volumes = parse_candles(candles)
    if len(closes) < 20:
      continue

    current_price = closes[-1]
    rsi = calculate_rsi(closes, 14)
    ema20 = sum(closes[-20:]) / 20

    signal_type = "LONG" if current_price >= ema20 else "SHORT"
    atr = sum([highs[i] - lows[i] for i in range(-14, 0)]) / 14
    if atr == 0:
      atr = current_price * 0.01

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
    signals_list.append({
        "slot": len(signals_list) + 1,
        "coin": coin_name,
        "type": signal_type,
        "price": current_price,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "sl": sl,
        "rsi": rsi,
    })
    last_signals[contract] = {"time": current_time, "type": signal_type}

  if not signals_list:
    print("No signals generated.")
    return

  # 8개 슬롯을 하나의 완벽한 종합 메시지로 패키징
  msg = (
      f"🚨 *Gate.io Futures Signals (Slot 1 ~ 8)* 🚨\n"
      f"⏱️ *시간*: `{now().strftime('%Y-%m-%d %H:%M:%S')} (KST)`\n"
      f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
  )

  for s in signals_list:
    emoji = "🟢" if s["type"] == "LONG" else "🔴"
    msg += (
        f"📌 *[Slot {s['slot']}/8] {s['coin']}/USDT* {emoji} **{s['type']}**\n"
        f"  • 진입가: `{s['price']:,.4f}`\n"
        f"  • TP1: `{s['tp1']:,.4f}` | TP2: `{s['tp2']:,.4f}` | TP3:"
        f" `{s['tp3']:,.4f}`\n"
        f"  • 손절(SL): `{s['sl']:,.4f}` | RSI: `{s['rsi']:.1f}`\n\n"
    )

  if send_telegram(msg):
    print("Successfully sent 8 slots summary message!")

  state["signals"] = last_signals
  save_state(state)


if __name__ == "__main__":
  analyze_market()
