import os
import json
import time
import math
import requests
import fcntl
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta

# 환경변수 안전 조회 함수들
def getenv_float(key, default):
    try:
        v = float(os.getenv(key, default))
        if v < 0:
            raise ValueError
        return v
    except Exception:
        logging.warning(f"환경변수 {key} 값 오류, 기본값({default}) 적용")
        return float(default)

def getenv_int(key, default):
    try:
        v = int(os.getenv(key, default))
        if v < 0:
            raise ValueError
        return v
    except Exception:
        logging.warning(f"환경변수 {key} 값 오류, 기본값({default}) 적용")
        return int(default)

# 기본 상수 및 환경 변수
BASE = "https://api.gateio.ws/api/v4"
TOKEN = os.getenv("GATE_TELEGRAM_TOKEN") or os.getenv("TELEGRAM_TOKEN", "")
CHAT = os.getenv("GATE_TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID", "")
STATE = Path("gate_state.json")
MAX_ACTIVE = getenv_int("MAX_ACTIVE", 10)
MIN_TURNOVER = getenv_float("MIN_TURNOVER", 500000)
MIN_SCORE = getenv_int("MIN_SCORE", 6)
COOLDOWN = getenv_int("COOLDOWN", 60)
STOP_ATR = getenv_float("STOP_ATR", 1.4)
TP_R = (1.2, 2.0, 3.0)
TZ = timezone(timedelta(hours=9))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    filename="gate_signal_bot.log",
    filemode="a"
)

S = requests.Session()
S.headers.update({"Accept": "application/json", "User-Agent": "GateSignalBot/3.4"})

# 유틸 함수들: now, stamp, num 등 기존 코드 유지 생략 가능

def now():
    return datetime.now(TZ)

def stamp():
    return now().isoformat(timespec="seconds")

def num(v, d=0.0):
    try:
        x = float(v)
        return x if math.isfinite(x) else d
    except Exception:
        return d

# API 호출 함수 (재시도 포함)
def api(path, params=None, retries=3, delay=2):
    for attempt in range(retries):
        try:
            r = S.get(BASE + path, params=params, timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logging.warning(f"API 호출 실패 {attempt+1}회: {e}")
            if attempt < retries - 1:
                time.sleep(delay)
            else:
                raise

# 상태 파일 읽기/쓰기 (파일 락 적용)

def save_json_with_lock(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    with open(path, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        tmp.replace(path)
        fcntl.flock(f, fcntl.LOCK_UN)

def load_state():
    if not STATE.exists():
        return {"positions": {}, "last_signal": {}, "history": []}
    try:
        with open(STATE, encoding="utf-8") as f:
            st = json.load(f)
        st.setdefault("positions", {})
        st.setdefault("last_signal", {})
        st.setdefault("history", [])
        # 슬롯 번호 정비
        used_slots = set()
        for sym, pos in st["positions"].items():
            slot = pos.get("slot")
            if slot is None or slot in used_slots or slot > MAX_ACTIVE:
                for i in range(1, MAX_ACTIVE + 1):
                    if i not in used_slots:
                        pos["slot"] = i
                        used_slots.add(i)
                        break
            else:
                used_slots.add(slot)
        return st
    except Exception as e:
        logging.error(f"상태 파일 로드 실패: {e}")
        raise RuntimeError("상태 파일 손상 또는 읽기 오류 발생")

def save_state(st):
    save_json_with_lock(STATE, st)

# 텔레그램 메시지 전송 (재시도 포함)
def send(msg, retries=3):
    if not TOKEN or not CHAT:
        logging.warning("텔레그램 설정 미완료")
        return False
    for attempt in range(retries):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                json={"chat_id": CHAT, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True},
                timeout=15,
            )
            if r.ok and r.json().get("ok", False):
                logging.info("텔레그램 메시지 전송 성공")
                return True
            time.sleep(1)
        except Exception as e:
            logging.warning(f"텔레그램 전송 실패 {attempt+1}회: {e}")
            time.sleep(1)
    logging.error("텔레그램 메시지 전송 최대 재시도 실패")
    return False

# 이후 주요 기능 함수( candles, ema, rsi, atr, find_signal, monitor, scan, main 등 )는 기존 구현 유지하시면 됩니다.

# 예시로 main 함수
def main():
    logging.info("봇 시작")
    try:
        st = load_state()
    except RuntimeError:
        send("⚠️ 상태 파일 오류 발생, 봇 실행 중단")
        return

    # 기본값 보장 및 슬롯 번호 재할당
    for sym, p in st["positions"].items():
        p.setdefault("entry_time", stamp())
        p.setdefault("tp1_hit", False)
        p.setdefault("tp2_hit", False)
        p.setdefault("tp3_hit", False)
        p.setdefault("status", "ACTIVE")
        p.setdefault("last_price", num(p.get("entry")))
        p.setdefault("sl_pct", 0.0)
        p.setdefault("tp1_pct", 0.0)
        p.setdefault("tp2_pct", 0.0)
        p.setdefault("tp3_pct", 0.0)
        p.setdefault("leverage", 5)
        if "slot" not in p:
            p["slot"] = get_next_slot(st["positions"])

    monitor(st)
    scan(st)
    save_state(st)
    logging.info(f"활성 시그널 수: {len(st['positions'])}")
    print(stats_report(st))

if __name__ == "__main__":
    main()
