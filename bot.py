import os
import requests

TOKEN = os.getenv("GATE_TELEGRAM_TOKEN", "").strip()


def get_chat_id():
  if not TOKEN:
    print("TOKEN missing!")
    return

  url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
  try:
    r = requests.get(url, timeout=10)
    print("API Response:", r.status_code, r.text)
    data = r.json()
    if data.get("ok"):
      results = data.get("result", [])
      if not results:
        print(
            "⚠️ 봇이 메시지를 수신하지 못했습니다. 채널에 아무 메시지나 먼저"
            " 보내주세요!"
        )
      for update in results:
        # 채널 메시지인 경우
        if "channel_post" in update:
          chat = update["channel_post"]["chat"]
          print(
              f"👉 채널 이름: {chat.get('title')} / 채널 ID: {chat.get('id')}"
          )
        # 일반 메시지나 그룹 메시지인 경우
        elif "message" in update:
          chat = update["message"]["chat"]
          print(f"👉 유저/그룹 이름: {chat.get('title', chat.get('username'))} / ID: {chat.get('id')}")
    else:
      print("Telegram API Error:", data)
  except Exception as e:
    print("Error:", e)


if __name__ == "__main__":
  get_chat_id()
