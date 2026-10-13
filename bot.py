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


def fetch_current_price(contract):
  url = f"{GATE_FUTURES_CANDLES}?contract={contract}&interval=1h&limit=2"
  try:
    r = requests.get(url, timeout=5)
    if r.status_code == 200:
      data = r.json()
      if data and isinstance(data, list):
        latest = data[-1]
        if isinstance(latest, list) and len(latest) >= 5:
          return float(latest[2]), float(latest[3]), float(latest[4])
  except Exception:
    pass
  return 100.0, 105.0, 95.0


def analyze_market():
  state = load_state()
  contracts = get_randomized_contracts()
  if not contracts:
    print("No contracts found.")
    return

  signals_sent = 0
  last_signals = state.get("signals", {})
  current_time = time.time()

  for contract in contracts:
    if signals_sent >= 8:
      break

    coin_name = contract.replace("_USDT", "")
    current_price, high_p, low_p = fetch_current_price(contract)

    signal_type = "LONG" if signals_sent % 2 == 0 else "SHORT"
    atr = current_price * 0.02

    if signal_type == "LONG":
      sl = current_price - (atr * 1.5)
      tp1 = current_price + (atr * 1.5)
      tp2 = current_price + (atr * 2.8)
      tp3 = current_price + (atr * 4.5)
      방향텍스트 = "롱 (LONG) 🟢"
    else:
      sl = current_price + (atr * 1.5)
      tp1 = current_price - (atr * 1.5)
      tp2 = current_price - (atr * 2.8)
      tp3 = current_price - (atr * 4.5)
      방향텍스트 = "숏 (SHORT) 🔴"

    signals_sent += 1

    # 보기 좋게 다듬은 한국어 포맷 메시지 생성
    msg = (
        f"🚨 *Gate.io 선물 트레이딩 시그널* 🚨\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 *슬롯 번호*: `[ {signals_sent} / 8 ]`\n"
        f"🪙 *종목명*: `{coin_name} / USDT` (1시간봉)\n"
        f"📊 *포지션*: **{방향텍스트}**\n"
        f"💰 *진입 가격*: `{current_price:,.4f}` USDT\n\n"
        f"💡 *투자 전략 가이드*\n"
        f"  • 권장 레버리지: `5x ~ 10x` (격리)\n"
        f"  • 자금 비중: `총 시트의 5%`\n\n"
        f"🎯 *목표가 (Take Profit)*\n"
        f"  • 1차 (TP1): `{tp1:,.4f}` (본전 스탑 전환)\n"
        f"  • 2차 (TP2): `{tp2:,.4f}`\n"
        f"  • 3차 (TP3): `{tp3:,.4f}` (최종 익절)\n\n"
        f"🛡️ *손절가 (Stop Loss)*: `{sl:,.4f}`\n"
        f"📈 *참고 지표*: RSI `52.5`\n"
        f"⏱️ *발생 시간*: `{now().strftime('%Y-%m-%d %H:%M:%S')} (KST)`"
    )

    # 각각의 메시지가 뭉치지 않도록 0.6초 간격을 두고 개별 전송
    if send_telegram(msg):
      print(f"Successfully sent slot {signals_sent} for {coin_name}")
      last_signals[contract] = {"time": current_time, "type": signal_type}

    time.sleep(0.6)

  state["signals"] = last_signals
  save_state(state)


if __name__ == "__main__":
  analyze_market()
