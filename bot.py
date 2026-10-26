import os
import json
import time
import math
import requests
import fcntl
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta

def getenv_float(key, default):
    try:
        v = float(os.getenv(key, default))
        if not math.isfinite(v) or v < 0: raise ValueError
        return v
    except Exception:
        logging.warning(f"환경변수 {key} 값 오류, 기본값({default}) 적용")
        return float(default)

def getenv_int(key, default):
    try:
        v = int(os.getenv(key, default))
        if v < 0: raise ValueError
        return v
    except Exception:
        logging.warning(f"환경변수 {key} 값 오류, 기본값({default}) 적용")
        return int(default)

BASE = "https://api.gateio.ws/api/v4"
TOKEN = os.getenv("GATE_TELEGRAM_TOKEN") or os.getenv("TELEGRAM_TOKEN", "")
CHAT = os.getenv("GATE_TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID", "")
STATE = Path("gate_state.json")

MAX_ACTIVE = getenv_int("MAX_ACTIVE", 8)
MIN_TURNOVER = getenv_float("MIN_TURNOVER", 500000)
MIN_SCORE = getenv_int("MIN_SCORE", 6)
COOLDOWN = getenv_int("COOLDOWN", 60)
STOP_ATR = getenv_float("STOP_ATR", 1.8)

# 💡 [변경 1] TP1을 더 가깝게 당겨서 빠르게 본전 방어선 구축 (기존 2.0, 3.5, 5.0 -> 1.2, 2.5, 4.0)
TP_R = (1.2, 2.5, 4.0)

TZ = timezone(timedelta(hours=9))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s",
                    filename="gate_signal_bot.log", filemode="a")
S = requests.Session()
S.headers.update({"Accept": "application/json", "User-Agent": "GateSignalBot/5.9"})

STOCKS = set("""AAPL NVDA TSLA MSFT AMZN META GOOGL GOOG AMD INTC COIN MSTR PLTR HOOD NFLX ORCL CRM AVGO QCOM MU TSM BABA SPY QQQ IWM DIA TLT XLF XLE GLD SLV""".split())
COMMODITIES = set("""PAXG XAUT GOLD SILVER XAG XAU OIL WTI BRENT NATGAS NG COPPER HG""".split())
INDICES = set("""SPX NDX NASDAQ SP500 US30 US500 US100 DJ30 DXY VIX""".split())
FX = set("""EURUSD GBPUSD USDJPY AUDUSD USDCAD USDCHF NZDUSD EUR GBP JPY AUD CAD CHF NZD""".split())

def asset(sym):
    b = sym.upper().replace("_USDT", "").replace("_USD", "")
    if b in COMMODITIES or any(x in b for x in ("GOLD", "SILVER", "OIL", "XAU", "XAG", "PAXG", "XAUT")): return "원자재"
    if b in INDICES or any(x in b for x in ("SP500", "NASDAQ", "US30", "SPX", "NDX", "DXY", "VIX")): return "지수"
    if (b in FX or any(b.startswith(x) for x in ("EUR", "GBP", "USD", "AUD", "CAD", "CHF", "NZD"))) and "_" in sym: return "외환"
    if b in STOCKS: return "주식"
    if sym.endswith("_USDT"): return "코인"
    return "기타·미확인"

def now(): return datetime.now(TZ)
def stamp(): return now().isoformat(timespec="seconds")

def num(v, d=0.0):
    try:
        x = float(v)
        return x if math.isfinite(x) else d
    except Exception: return d

def api(path, params=None, retries=3, delay=2):
    for attempt in range(retries):
        try:
            r = S.get(BASE + path, params=params, timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logging.warning(f"API 호출 실패 {attempt+1}회: {e}")
            if attempt < retries - 1: time.sleep(delay)
            else: raise

def send(msg, retries=3):
    if not TOKEN or not CHAT:
        logging.warning("텔레그램 설정 미완료")
        return False
    for attempt in range(retries):
        try:
            r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                json={"chat_id": CHAT, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=15)
            if r.ok and r.json().get("ok", False):
                logging.info("텔레그램 메시지 전송 성공")
                return True
            logging.warning(f"텔레그램 응답 실패 {attempt+1}회: HTTP {r.status_code}")
        except Exception as e:
            logging.warning(f"텔레그램 전송 실패 {attempt+1}회: {e}")
        if attempt < retries - 1: time.sleep(1)
    logging.error("텔레그램 메시지 전송 최대 재시도 실패")
    return False

def save_json_with_lock(path, data):
    lock_path = path.with_suffix(path.suffix + ".lock")
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    with open(lock_path, "a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            if tmp.exists(): tmp.unlink()
            fcntl.flock(lock, fcntl.LOCK_UN)

def save_state(st): save_json_with_lock(STATE, st)

def reassign_slots(st):
    ps = st.get("positions", {})
    if not ps: return
    sorted_items = sorted(ps.items(), key=lambda x: x[1].get("entry_time", ""))
    for idx, (sym, p) in enumerate(sorted_items, start=1):
        p["slot"] = idx

def load_state():
    if not STATE.exists(): return {"positions": {}, "last_signal": {}, "history": [], "pending_notifications": []}
    try:
        with open(STATE, encoding="utf-8") as f: st = json.load(f)
        if not isinstance(st, dict): raise ValueError("상태 데이터가 객체가 아닙니다")
        st.setdefault("positions", {})
        st.setdefault("last_signal", {})
        st.setdefault("history", [])
        st.setdefault("pending_notifications", [])
        reassign_slots(st)
        save_state(st)
        return st
    except Exception as e:
        logging.error(f"상태 파일 로드 실패: {e}")
        raise RuntimeError("상태 파일 손상 또는 읽기 오류 발생")

def get_next_slot(positions):
    used = {p.get("slot") for p in positions.values() if isinstance(p, dict)}
    return next((i for i in range(1, MAX_ACTIVE + 1) if i not in used), None)

def elapsed(p):
    try:
        d = datetime.fromisoformat(p["entry_time"])
        if d.tzinfo is None: d = d.replace(tzinfo=TZ)
        sec = max(0, int((now() - d.astimezone(TZ)).total_seconds()))
        days, sec = divmod(sec, 86400)
        hours, sec = divmod(sec, 3600)
        mins = sec // 60
        return f"{days}일 {hours}시간 {mins}분" if days else f"{hours}시간 {mins}분"
    except Exception: return "-"

def pnl(p, price):
    entry = num(p.get("entry"))
    if entry <= 0: return 0
    delta = price - entry
    if p.get("side") == "SHORT": delta = -delta
    return delta / entry * 100

def get_status_overview_msg(st, header_title="🔔 <b>GATE.IO 현재 보유 현황</b>"):
    lines = [header_title, "━━━━━━━━━━━━━━"]
    ps = st.get("positions", {})
    lines.append(f"📋 <b>전체 보유 종목 리스트 ({len(ps)}/{MAX_ACTIVE})</b>")
    if not ps: lines.append("현재 보유 중인 종목이 없습니다.")
    else:
        for sym, p in sorted(ps.items(), key=lambda x: x[1].get("slot", 99)):
            price = num(p.get("last_price"), num(p.get("entry")))
            icon = "🟢" if p.get("side") == "LONG" else "🔴"
            val = pnl(p, price)
            pnl_icon = "📈" if val > 0 else ("📉" if val < 0 else "➖")
            lines.append(f"<b>#{p.get('slot', '?')}</b> {sym} ({asset(sym)}) {icon} {p.get('side')}\n  └ 수익률: <b>{pnl_icon} {val:+.2f}%</b> | 보유: {elapsed(p)}")
    lines.extend(["━━━━━━━━━━━━━━", "※ 시그널 추적 전용 (자동 주문 미실행)"])
    return "\n".join(lines)

def candles(sym, interval, limit=120):
    raw = api("/futures/usdt/candlesticks", {"contract": sym, "interval": interval, "limit": limit})
    out = []
    for x in raw:
        if isinstance(x, dict): q = {k: num(x.get(k)) for k in ("t", "o", "h", "l", "c", "v")}
        elif isinstance(x, (list, tuple)) and len(x) >= 6: q = dict(t=num(x[0]), v=num(x[1]), c=num(x[2]), h=num(x[3]), l=num(x[4]), o=num(x[5]))
        else: continue
        if min(q["o"], q["h"], q["l"], q["c"]) > 0 and q["h"] >= q["l"]: out.append(q)
    return sorted(out, key=lambda x: x["t"])

def ema(a, n):
    if len(a) < n: return None
    e = sum(a[:n]) / n
    k = 2 / (n + 1)
    for x in a[n:]: e = x * k + e * (1-k)
    return e

def rsi(a, n=14):
    if len(a) < n+1: return None
    d = [a[i] - a[i-1] for i in range(1, len(a))]
    g = sum(max(x, 0) for x in d[-n:]) / n
    l = sum(max(-x, 0) for x in d[-n:]) / n
    return 100 if l == 0 else 100 - 100/(1 + g/l)

def atr(cs, n=14):
    if len(cs) < n+1: return None
    tr = []
    for i in range(1, len(cs)):
        c, prev = cs[i], cs[i-1]["c"]
        tr.append(max(c["h"]-c["l"], abs(c["h"]-prev), abs(c["l"]-prev)))
    return sum(tr[-n:]) / n

# 💡 [변경 2] 횡보장 필터링을 위한 ADX(추세 강도) 계산 함수 추가
def adx(cs, n=14):
    if len(cs) < n * 2: return 25.0  # 데이터 부족 시 기본 통과
    try:
        trs, p_dms, m_dms = [], [], []
        for i in range(1, len(cs)):
            c, prev = cs[i], cs[i-1]
            tr = max(c["h"] - c["l"], abs(c["h"] - prev["c"]), abs(c["l"] - prev["c"]))
            p_dm = max(0, c["h"] - prev["h"]) if (c["h"] - prev["h"]) > (prev["l"] - c["l"]) else 0
            m_dm = max(0, prev["l"] - c["l"]) if (prev["l"] - c["l"]) > (c["h"] - prev["h"]) else 0
            trs.append(tr); p_dms.append(p_dm); m_dms.append(m_dm)
        
        atr_val = sum(trs[-n:]) / n
        if atr_val == 0: return 0.0
        p_di = 100 * (sum(p_dms[-n:]) / n) / atr_val
        m_di = 100 * (sum(m_dms[-n:]) / n) / atr_val
        dx = 100 * abs(p_di - m_di) / (p_di + m_di) if (p_di + m_di) > 0 else 0
        return dx
    except Exception:
        return 25.0

def market():
    rows = api("/futures/usdt/tickers")
    out = []
    for x in rows:
        if not x.get("contract", "").endswith("_USDT") or x.get("in_delisting") is True: continue
        price = num(x.get("last"))
        turnover = num(x.get("volume_24h_quote"))
        if turnover <= 0: turnover = num(x.get("volume_24h_usd"))
        if price > 0 and turnover >= MIN_TURNOVER:
            x["_price"], x["_turnover"] = price, turnover
            out.append(x)
    return sorted(out, key=lambda x: x["_turnover"], reverse=True)

def btc_direction():
    try:
        cs = candles("BTC_USDT", "1h", 100)
        a = [x["c"] for x in cs[:-1]]
        e20, e50 = ema(a, 20), ema(a, 50)
        if e20 is None or e50 is None: return "FLAT"
        if a[-1] > e20 > e50: return "LONG"
        if a[-1] < e20 < e50: return "SHORT"
    except Exception as e: logging.error(f"BTC ERROR: {e}")
    return "FLAT"

def find_signal(t, btc):
    sym = t["contract"]
    cs = candles(sym, "15m", 100)
    if len(cs) < 60: return None
    cs = cs[:-1]
    a, v = [x["c"] for x in cs], [x["v"] for x in cs]
    last = cs[-1]
    e9, e21, e50 = ema(a, 9), ema(a, 21), ema(a, 50)
    rv, av = rsi(a), atr(cs)
    
    # 💡 [적용] 힘없는 횡보장(ADX < 20) 필터링 추가
    current_adx = adx(cs)
    if current_adx < 20.0: return None

    if any(x is None for x in (e9, e21, e50, rv, av)) or av <= 0 or e21 <= 0: return None
    avg = sum(v[-21:-1]) / 20 if len(v) >= 21 else 0
    surge = v[-1] / avg if avg > 0 else 0
    if surge < 1.8: return None
    body = abs(last["c"]-last["o"]) >= av*.25
    funding = num(t.get("funding_rate"))
    if abs(last["c"]-e21)/e21 > .04: return None
    lc = [btc=="LONG", e9>e21>e50, 54<=rv<=70, surge>=1.8, last["c"]>e21, last["c"]>last["o"] and body]
    sc = [btc=="SHORT", e9<e21<e50, 30<=rv<=46, surge>=1.8, last["c"]<e21, last["c"]<last["o"] and body]
    side = "LONG" if sum(lc) >= sum(sc) else "SHORT"
    if (btc=="LONG" and side=="SHORT") or (btc=="SHORT" and side=="LONG"): return None
    checks = lc if side=="LONG" else sc
    score = sum(checks)
    if score < MIN_SCORE: return None
    if (side=="LONG" and funding>.003) or (side=="SHORT" and funding<-.003): return None
    entry = num(t.get("_price"), last["c"])
    if entry <= 0: return None
    dist = max(av*STOP_ATR, entry*.002)
    sl = entry-dist if side=="LONG" else entry+dist
    tps = [entry+dist*r if side=="LONG" else entry-dist*r for r in TP_R]
    if side=="LONG":
        sl_pct, tp1_pct, tp2_pct, tp3_pct = (sl-entry)/entry*100, (tps[0]-entry)/entry*100, (tps[1]-entry)/entry*100, (tps[2]-entry)/entry*100
    else:
        sl_pct, tp1_pct, tp2_pct, tp3_pct = (entry-sl)/entry*100, (entry-tps[0])/entry*100, (entry-tps[1])/entry*100, (entry-tps[2])/entry*100
    
    risk_pct = abs(entry-sl)/entry
    rec_lev = max(2, min(20, round(.15/risk_pct))) if risk_pct>0 else 5
    names = ["BTC 추세 일치", "EMA 추세 정렬", "RSI 조건 충족", "거래량 급증(1.8배+)", "EMA21 조건 충족", "캔들 방향/몸통 확인"]
    return {"symbol":sym,"side":side,"entry":entry,"sl":sl,"tp1":tps[0],"tp2":tps[1],"tp3":tps[2],
        "sl_pct":sl_pct,"tp1_pct":tp1_pct,"tp2_pct":tp2_pct,"tp3_pct":tp3_pct,"leverage":rec_lev,
        "rsi":rv,"atr":av,"volume_surge":surge,"score":score,"reasons":[names[i] for i,x in enumerate(checks) if x],
        "entry_time":stamp(),"last_price":entry,"tp1_hit":False,"tp2_hit":False,"tp3_hit":False,"status":"ACTIVE"}

def stats_report(st):
    counts = {"TP3":0,"TP2_BE":0,"TP1_BE":0,"SL":0}
    for x in st.get("history", []):
        if x.get("outcome") in counts: counts[x["outcome"]] += 1
    total = sum(counts.values())
    wins, draws, losses = counts["TP3"]+counts["TP2_BE"], counts["TP1_BE"], counts["SL"]
    decisive = wins+losses
    rate = wins/decisive*100 if decisive else 0
    return ("📈 <b>시그널 추적 통계 리포트</b>\n━━━━━━━━━━━━━━\n"
        f"총 종료 시그널: {total}건\n\n🎯 TP3 달성 (승): {counts['TP3']}건\n"
        f"🎯 TP2 이후 TP1방어 종료 (승): {counts['TP2_BE']}건\n🤝 TP1 본전 종료 (무): {counts['TP1_BE']}건\n"
        f"🛑 초기 손절 종료 (패): {counts['SL']}건\n\n🏆 신호 기준 승률: <b>{rate:.1f}%</b>\n"
        f"(승 {wins}건 / 패 {losses}건 | 무 {draws}건 제외)\n"
        "<i>※ 수수료·레버리지 미반영 기초 자산 가격 변동 기준입니다.</i>")

def queue_notification(st, key, message):
    st.setdefault("pending_notifications", [])
    if not any(x.get("key")==key for x in st["pending_notifications"] if isinstance(x,dict)):
        st["pending_notifications"].append({"key":key,"message":message,"created_at":stamp()})
    st["pending_notifications"] = st["pending_notifications"][-200:]
    save_state(st)

def notify(st, key, message):
    if not send(message): queue_notification(st, key, message)

def retry_pending_notifications(st):
    pending = st.setdefault("pending_notifications", [])
    if not pending: return
    remaining = []
    for item in pending:
        if not isinstance(item,dict) or not item.get("message"): continue
        if not send(item["message"]): remaining.append(item)
    st["pending_notifications"] = remaining[-200:]
    save_state(st)

def close_position(st, sym, reason, price):
    p = st["positions"].get(sym)
    if not p: return
    if reason=="TP3": outcome="TP3"
    elif "SL" in reason or "청산" in reason:
        outcome = "TP2_BE" if p.get("tp2_hit") else ("TP1_BE" if p.get("tp1_hit") else "SL")
    else: outcome="SL"
    record = {"symbol":sym,"side":p.get("side"),"asset":asset(sym),"entry":num(p.get("entry")),"exit":price,
        "entry_time":p.get("entry_time"),"exit_time":stamp(),"duration":elapsed(p),"outcome":outcome,
        "reason":reason,"return_pct":round(pnl(p,price),4)}
    st["history"].append(record)
    st["history"] = st["history"][-5000:]
    del st["positions"][sym]
    
    reassign_slots(st)
    save_state(st)
    
    chart = f"https://www.gate.io/futures/USDT/{sym}"
    labels = {"TP3":"TP3 목표 달성","TP2_BE":"TP2 이후 TP1 가격 방어 종료","TP1_BE":"TP1 본전 종료","SL":"초기 손절"}
    overview = get_status_overview_msg(st,"🔔 <b>GATE.IO 포지션 종료 및 현재 보유 현황</b>")
    msg = (f"🏁 <b>시그널 종료 | {sym} ({asset(sym)}) {p.get('side')}</b>\n결과 분류: <b>{labels.get(outcome,outcome)}</b>\n"
        f"진입: <code>{num(p.get('entry')):.8g}</code> → 종료: <code>{price:.8g}</code>\n"
        f"실현 변동률: <b>{pnl(p,price):+.2f}%</b> (수수료 미반영)\n보유 시간: {elapsed(p)}\n"
        f"🔗 <a href='{chart}'>Gate.io 차트 보기</a>\n\n{overview}")
    notify(st, f"CLOSE:{sym}:{record['exit_time']}", msg)
    notify(st, f"STATS:{record['exit_time']}:{sym}", stats_report(st))

def monitor(st):
    for sym in list(st["positions"]):
        p = st["positions"].get(sym)
        if not p: continue
        try:
            cs = candles(sym,"1m",5)
            if not cs: continue
            c = cs[-1]
            p["last_price"] = c["c"]
            long = p.get("side")=="LONG"
            sl = num(p.get("sl"))
            
            # SL 우선 감지
            if (c["l"]<=sl if long else c["h"]>=sl):
                close_position(st,sym,"SL 또는 본절 청산",sl)
                continue
                
            # TP 우선 감지
            for target,flag in (("tp1","tp1_hit"),("tp2","tp2_hit"),("tp3","tp3_hit")):
                if p.get(flag): continue
                level = num(p.get(target))
                if not (c["h"]>=level if long else c["l"]<=level): continue
                p[flag] = True
                chart = f"https://www.gate.io/futures/USDT/{sym}"
                if target=="tp1":
                    p["sl"] = num(p.get("entry"))
                    save_state(st)
                    msg = (f"🎯 <b>TP1 도달 (시그널 추적) | {sym} ({asset(sym)})</b>\n변동률: {pnl(p,level):+.2f}%\n"
                           f"가상 손절가를 진입가(본전)로 이동 완료\nTP2 대기\n🔗 <a href='{chart}'>Gate.io 차트 보기</a>\n\n"
                           + get_status_overview_msg(st,"🔔 <b>GATE.IO TP1 도달 및 현재 보유 현황</b>"))
                    notify(st,f"TP1:{sym}:{p.get('entry_time')}",msg)
                elif target=="tp2":
                    p["sl"] = num(p.get("tp1"))
                    save_state(st)
                    msg = (f"🎯 <b>TP2 도달 (시그널 추적) | {sym} ({asset(sym)})</b>\n변동률: {pnl(p,level):+.2f}%\n"
                           f"가상 손절가를 TP1 가격으로 이동 (수익 보전)\nTP3 대기\n🔗 <a href='{chart}'>Gate.io 차트 보기</a>\n\n"
                           + get_status_overview_msg(st,"🔔 <b>GATE.IO TP2 도달 및 현재 보유 현황</b>"))
                    notify(st,f"TP2:{sym}:{p.get('entry_time')}",msg)
                    notify(st,f"STATS:TP2:{sym}:{p.get('entry_time')}", stats_report(st))
                else:
                    p["last_price"] = level
                    save_state(st)
                    close_position(st,sym,"TP3",level)
                    break
                break
            if sym in st["positions"]: save_state(st)
        except Exception as e: logging.error(f"MONITOR ERROR {sym}: {e}")

def scan(st):
    if len(st["positions"])>=MAX_ACTIVE:
        logging.info("SLOTS FULL")
        return
    btc = btc_direction()
    if btc=="FLAT":
        logging.info("BTC FILTER: FLAT")
        return
    try: tickers = market()
    except Exception as e:
        logging.error(f"MARKET ERROR: {e}")
        return
    for t in tickers:
        if len(st["positions"])>=MAX_ACTIVE: break
        sym = t["contract"]
        if sym in st["positions"]: continue
        if time.time()-num(st["last_signal"].get(sym)) < COOLDOWN*60: continue
        try:
            sig = find_signal(t,btc)
            if not sig: continue
            
            slot = get_next_slot(st["positions"])
            if slot is None:
                logging.warning("사용 가능한 슬롯 없음")
                break
            sig["slot"] = slot
            st["positions"][sym] = sig
            
            reassign_slots(st)
            st["last_signal"][sym] = time.time()
            save_state(st)
            
            chart = f"https://www.gate.io/futures/USDT/{sym}"
            overview = get_status_overview_msg(st,"🔔 <b>GATE.IO 신규 진입 및 현재 보유 현황</b>")
            msg = (f"🚨 <b>GATE.IO FUTURES SIGNAL</b>\n━━━━━━━━━━━━━━\n📌 슬롯: <b>#{slot}</b> | 계약: <b>{sym}</b> ({asset(sym)})\n"
                f"📈 방향: <b>{sig['side']}</b>\n🕒 발생: {sig['entry_time']}\n💵 진입가: <code>{sig['entry']:.8g}</code>\n"
                f"🛑 가상 손절가: <code>{sig['sl']:.8g}</code> (<b>{sig['sl_pct']:+.2f}%</b>)\n"
                f"🎯 TP1: <code>{sig['tp1']:.8g}</code> (<b>{sig['tp1_pct']:+.2f}%</b>)\n"
                f"🎯 TP2: <code>{sig['tp2']:.8g}</code> (<b>{sig['tp2_pct']:+.2f}%</b>)\n"
                f"🎯 TP3: <code>{sig['tp3']:.8g}</code> (<b>{sig['tp3_pct']:+.2f}%</b>)\n━━━━━━━━━━━━━━\n"
                f"⚡ 추천 레버리지: <b>{sig['leverage']}x</b>\n🧠 점수: {sig['score']}/6 | RSI: {sig['rsi']:.1f} | 거래량: {sig['volume_surge']:.1f}배\n"
                f"🔎 근거: {', '.join(sig['reasons'])}\n🔗 <a href='{chart}'>Gate.io 차트 보기</a>\n\n{overview}")
            notify(st,f"ENTRY:{sym}:{sig['entry_time']}",msg)
            logging.info(f"SIGNAL {sym} {sig['side']} {sig['score']}")
        except Exception as e: logging.error(f"SCAN ERROR {sym}: {e}")

def main():
    logging.info("GATE.IO FUTURES BOT v5.9 시작")
    try: st = load_state()
    except RuntimeError: return
    for p in st["positions"].values():
        p.setdefault("entry_time",stamp())
        p.setdefault("tp1_hit",False); p.setdefault("tp2_hit",False); p.setdefault("tp3_hit",False)
        p.setdefault("status","ACTIVE"); p.setdefault("last_price",num(p.get("entry")))
        p.setdefault("sl_pct",0.0); p.setdefault("tp1_pct",0.0); p.setdefault("tp2_pct",0.0); p.setdefault("tp3_pct",0.0)
        p.setdefault("leverage",5)
    retry_pending_notifications(st)
    monitor(st)
    scan(st)
    save_state(st)
    logging.info(f"ACTIVE: {len(st['positions'])}")

if __name__=="__main__":
    main()
