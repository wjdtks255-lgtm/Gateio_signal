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
GATE_FUTURES_TICKERS = "https://fx-api.gateio.ws/api/v4/futures/usdt/tickers"


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


def get_market_data():
  try:
    r = requests.get(GATE_FUTURES_TICKERS, timeout=10)
    if r.status_code == 200:
      data = r.json()
      valid_tickers = {}
      for item in data:
        contract = item.get("name")
        if not contract or item.get("in_delisting", False):
          continue
        try:
          last_price = float(item.get("last", 0))
          if last_price > 0:
            valid_tickers[contract] = last_price
        except Exception:
          pass
      return valid_tickers
  except Exception as e:
    print(f"Error fetching tickers: {e}")
  return {}


def track_and_manage_positions(state, current_prices):
  active = state.get("active_positions", {})
  to_remove = []

  for contract, pos in list(active.items()):
    if contract not in current_prices:
      continue

    current_price = current_prices[contract]
    coin_name = contract.replace("_USDT", "")
    signal_type = pos["type"]
    entry = pos["entry"]
    tp1, tp2, tp3, sl = pos["tp1"], pos["tp2"], pos["tp3"], pos["sl"]
    hit = pos.get("hit", [])

    if signal_type == "LONG":
      # 손절 체크
      if current_price <= sl:
        send_telegram(
            f"🛡️ *[손절(SL) 도달 알림]*\n"
            f"🪙 종목: `{coin_name} / USDT` (LONG)\n"
            f"📉 손절가 `{sl:,.4f}` 이탈로 포지션 종료\n"
            f"⏱️ `{now().strftime('%Y-%m-%d %H:%M:%S')} (KST)`"
        )
        to_remove.append(contract)
        continue
      # TP3 최종 익절 체크
      if current_price >= tp3 and "TP3" not in hit:
        send_telegram(
            f"🎉 *[TP3 최종 익절 달성!]*\n"
            f"🪙 종목: `{coin_name} / USDT` (LONG)\n"
            f"🎯 3차 목표가 `{tp3:,.4f}` 도달 완료!\n"
            f"⏱️ `{now().strftime('%Y-%m-%d %H:%M:%S')} (KST)`"
        )
        to_remove.append(contract)
        continue
      # TP2 익절 체크
      if current_price >= tp2 and "TP2" not in hit:
        send_telegram(
            f"🎯 *[TP2 2차 익절 도달]*\n"
            f"🪙 종목: `{coin_name} / USDT` (LONG)\n"
            f"💰 목표가 `{tp2:,.4f}` 도달!"
        )
        pos["hit"].append("TP2")
      # TP1 익절 체크 및 본전 스탑 전환
      if current_price >= tp1 and "TP1" not in hit:
        send_telegram(
            f"🎯 *[TP1 1차 익절 & 본전스탑 전환]*\n"
            f"🪙 종목: `{coin_name} / USDT` (LONG)\n"
            f"💰 목표가 `{tp1:,.4f}` 도달!\n"
            f"🛡️ *손절가를 진입가 고정(`{entry:,.4f}`)으로 상향합니다.*"
        )
        pos["sl"] = entry  # 본전 스탑으로 변경
        pos["hit"].append("TP1")

    elif signal_type == "SHORT":
      # 손절 체크
      if current_price >= sl:
        send_telegram(
            f"🛡️ *[손절(SL) 도달 알림]*\n"
            f"🪙 종목: `{coin_name} / USDT` (SHORT)\n"
            f"📈 손절가 `{sl:,.4f}` 이탈로 포지션 종료\n"
            f"⏱️ `{now().strftime('%Y-%m-%d %H:%M:%S')} (KST)`"
        )
        to_remove.append(contract)
        continue
      # TP3 최종 익절 체크
      if current_price <= tp3 and "TP3" not in hit:
        send_telegram(
            f"🎉 *[TP3 최종 익절 달성!]*\n"
            f"🪙 종목: `{coin_name} / USDT` (SHORT)\n"
            f"🎯 3차 목표가 `{tp3:,.4f}` 도달 완료!\n"
            f"⏱️ `{now().strftime('%Y-%m-%d %H:%M:%S')} (KST)`"
        )
        to_remove.append(contract)
        continue
      # TP2 익절 체크
      if current_price <= tp2 and "TP2" not in hit:
        send_telegram(
            f"🎯 *[TP2 2차 익절 도달]*\n"
            f"🪙 종목: `{coin_name} / USDT` (SHORT)\n"
            f"💰 목표가 `{tp2:,.4f}` 도달!"
        )
        pos["hit"].append("TP2")
      # TP1 익절 체크 및 본전 스탑 전환
      if current_price <= tp1 and "TP1" not in hit:
        send_telegram(
            f"🎯 *[TP1 1차 익절 & 본전스탑 전환]*\n"
            f"🪙 종목: `{coin_name} / USDT` (SHORT)\n"
            f"💰 목표가 `{tp1:,.4f}` 도달!\n"
            f"🛡️ *손절가를 진입가 고정(`{entry:,.4f}`)으로 하향합니다.*"
        )
        pos["sl"] = entry  # 본전 스탑으로 변경
        pos["hit"].append("TP1")

  for c in to_remove:
    del active[c]
  state["active_positions"] = active


def analyze_market():
  state = load_state()
  current_prices = get_market_data()
  if not current_prices:
    print("No market data found.")
    return

  # 1단계: 기존에 열려있는 포지션들의 가격 움직임 실시간 추적 및 관리
  track_and_manage_positions(state, current_prices)

  # 2단계: 새로운 신호 발송 (필요 시 기존 포지션이 비었을 때 등 새 슬롯 생성)
  tickers_list = [{"name": k, "price": v} for k, v in current_prices.items()]
  random.shuffle(tickers_list)

  signals_sent = 0
  last_signals = state.get("signals", {})
  active_positions = state.get("active_positions", {})
  current_time = time.time()

  for t in tickers_list:
    if signals_sent >= 8:
      break

    contract = t["name"]
    if contract in active_positions:
      continue  # 이미 추적 중인 포지션은 중복 신호 방지

    current_price = t["price"]
    coin_name = contract.replace("_USDT", "")

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

    msg = (
        f"🚨 *Gate.io 선물 트레이딩 시그널* 🚨\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 *슬롯 번호*: `[ {signals_sent} / 8 ]`\n"
        f"🪙 *종목명*: `{coin_name} / USDT`\n"
        f"📊 *포지션*: **{방향텍스트}**\n"
        f"💰 *실시간 진입가*: `{current_price:,.4f}` USDT\n\n"
        f"💡 *소액러 맞춤 가이드*\n"
        f"  • 권장 레버리지: `5x ~ 10x` (**격리 마진
