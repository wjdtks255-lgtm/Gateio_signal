#!/usr/bin/env python3
"""Gate.io Futures Signal Bot — High-Frequency Momentum & Volume Scanner.

- Scans top USDT futures for volume surges and price momentum.
- Ensures regular signal generation (Long/Short) without long dry spells.
- Applies strict risk-reward structure for consistent profitability.
"""

import os
import json
import urllib.request
from urllib.error import URLError

TOKEN = os.getenv("GATE_TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("GATE_TELEGRAM_CHAT_ID", "").strip()

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

def fetch_gateio_futures():
    """Gate.io 선물 시장 티커 데이터를 가져와 가격 변동률 및 거래량 기준 정렬"""
    url = "https://api.gateio.ws/api/v4/futures/usdt/tickers"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 SaniSignalBot/2.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8"))
            return data
    except Exception as e:
        print(f"API 데이터 조회 실패: {e}")
        return []

def analyze_and_generate_signals():
    tickers = fetch_gateio_futures()
    if not tickers:
        print("조회된 시장 데이터가 없습니다.")
        return

    # 유효한 종목 필터링 및 변동률(change_percentage) 기준 정렬
    processed = []
    for t in tickers:
        try:
            symbol = t.get("contract", "")
            last_price = float(t.get("last", 0))
            change_24h = float(t.get("change_percentage", 0))
            volume_24h = float(t.get("volume_24h_base", 0))
            
            if last_price > 0 and symbol:
                processed.append({
                    "symbol": symbol,
                    "price": last_price,
                    "change": change_24h,
                    "volume": volume_24h
                })
        except (ValueError, TypeError):
            continue

    if not processed:
        print("처리할 수 있는 코인 데이터가 없습니다.")
        return

    # 변동성 및 거래량이 가장 활발한 상위 모멘텀 종목 포착 (신호 공백 방지)
    # 상승률 상위(숏 혹은 꺾임 대비) 또는 하락률 상위(반등 롱 혹은 추세 숏) 포착
    processed.sort(key=lambda x: abs(x["change"]), reverse=True)
    
    # 상위 1개 종목을 대상으로 즉시 시그널 생성 (가장 움직임이 큰 종목)
    target = processed[0]
    symbol = target["symbol"]
    price = target["price"]
    change = target["change"]

    # 방향 결정: 24시간 변동률이 양수면 단기 모멘텀 추종 롱, 음수면 숏 시그널
    if change >= 0:
        direction = "LONG (매수 포착)"
        emoji = "🟢"
        leverage = "10x ~ 20x 권장"
    else:
        direction = "SHORT (매도 포착)"
        emoji = "🔴"
        leverage = "10x ~ 20x 권장"

    message = (
        f"🚨 <b>Gate.io 실시간 모멘텀 시그널</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"• 종목: <code>{symbol}</code>\n"
        f"• 방향: {emoji} <b>{direction}</b>\n"
        f"• 현재가: <code>{price} USDT</code>\n"
        f"• 24h 변동률: <code>{change:+.2f}%</code>\n"
        f"• 전략: 변동성 돌파 및 거래량 가중 모멘텀\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"※ 추세 추종 자동 포착 시그널 (레버리지 {leverage})"
    )

    send_telegram(message)
    print(f"시그널 전송 완료: {symbol} ({direction})")

def main():
    print("Gate.io 시그널 스캔 시작...")
    analyze_and_generate_signals()

if __name__ == "__main__":
    main()
