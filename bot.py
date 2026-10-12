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
      for update in results:
        if "channel_post" in update:
          chat = update["channel_post"]["chat"]
          print(
              f"👉 [찾았다!] 채널 이름: {chat.get('title')} / 채널 ID:"
              f" {chat.get('id')}"
          )
        elif "message" in update:
          chat = update["message"]["chat"]
          print(
              f"👉 [찾았다!] 유저/그룹 이름: {chat.get('title', chat.get('username'))}"
              f" / ID: {chat.get('id')}"
          )
    else:
      print("Telegram API Error:", data)
  except Exception as e:
    print("Error:", e)


if __name__ == "__main__":
  get_chat_id()
