from datetime import datetime, timedelta, timezone
import json
import os
import requests

# 환경 변수 로드
TOKEN = os.getenv("GATE_TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("GATE_TELEGRAM_CHAT_ID", "").strip()
KST = timezone(timedelta(hours=9))


def now():
  return datetime.now(KST)


def send_telegram(msg):
  if not TOKEN or not CHAT_ID:
    print(
        f"Telegram Config Error: TOKEN length: {len(TOKEN)}, CHAT_ID length:"
        f" {len(CHAT_ID)}"
    )
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
    print(f"Telegram error exception: {e}")
  return False


def test_signal():
  print(f"Testing Telegram connection with CHAT_ID: {CHAT_ID}")
  msg = (
      f"🚨 *Gate.io Bot 연결 테스트 성공* 🚨\n\n"
      f"📌 *상태*: 정상 작동 중\n"
      f"⏱️ *시간*: `{now().strftime('%Y-%m-%d %H:%M:%S')} (KST)`"
  )

  success = send_telegram(msg)
  if success:
    print("Test message sent successfully!")
  else:
    print("Failed to send test message. Please check Token/Chat ID.")


if __name__ == "__main__":
  test_signal()
