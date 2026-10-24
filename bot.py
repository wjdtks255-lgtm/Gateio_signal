
import os,json,requests
from datetime import datetime,timezone,timedelta
from pathlib import Path

BASE="https://api.gateio.ws/api/v4"
TOKEN=(os.getenv("GATE_TELEGRAM_TOKEN") or os.getenv("TELEGRAM_TOKEN") or "").strip()
CHAT=(os.getenv("GATE_TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID") or "").strip()
STATE=Path("gate_state.json")
LOG=Path("gate_log.json")
KST=timezone(timedelta(hours=9))
MAX_ACTIVE=10
MAX_SCAN=90
MIN_TURNOVER=500000
MIN_SCORE=6
COOLDOWN=60
STOP_ATR=1.4
TP_R=(1.2,2.0,3.0)
S=requests.Session()

def now():
    return datetime.now(timezone.utc)

def parsed(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z","+00:00")).astimezone(timezone.utc)
    except Exception:
        return now()

def elapsed(s):
    sec=max(0,int((now()-parsed(s)).total_seconds()))
    d,sec=divmod(sec,86400)
    h,sec=divmod(sec,3600)
    m=sec//60
    if d:return f"{d}일 {h}시간 {m}분"
    if h:return f"{h}시간 {m}분"
    return f"{m}분"

def kst(s):
    return parsed(s).astimezone(KST).strftime("%Y-%m-%d %H:%M:%S KST")

def load(path,default):
    try:return json.loads(path.read_text(encoding="utf-8"))
    except Exception:return default

def save(st):
    STATE.write_text(json.dumps(st,ensure_ascii=False,indent=2),encoding="utf-8")

def log(event,data=None):
    rows=load(LOG,[])
    rows.append({"time":now().isoformat(),"event":event,"data":data or {}})
    LOG.write_text(json.dumps(rows[-500:],ensure_ascii=False,indent=2),encoding="utf-8")

def tg(text):
    if not TOKEN or not CHAT:
        print("Telegram secrets missing")
        return False
    try:
        r=requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
          json={"chat_id":CHAT,"text":text,"disable_web_page_preview":True},timeout=15)
        print("Telegram:",r.status_code)
        return r.ok
    except Exception as e:
        print("Telegram error:",e)
        return False

def api(path,params=None):
    r=S.get(BASE+path,params=params,timeout=15)
    r.raise_for_status()
    return r.json()

def num(v,default=0):
    try:return float(v)
    except Exception:return default

def pct(a,b):
    return (b-a)/a*100 if a else 0

def candles(sym,interval="15m",limit=100):
    raw=api("/futures/usdt/candlesticks",
            {"contract":sym,"interval":interval,"limit":limit})
    out=[]
    for x in raw:
        try:
            out.append({"t":int(x[0]),"v":num(x[1]),"c":num(x[2]),
                        "h":num(x[3]),"l":num(x[4]),"o":num(x[5])})
        except Exception:pass
    return sorted(out,key=lambda x:x["t"])

def ema(v,n):
    if len(v)<n:return []
    a=2/(n+1)
    out=[sum(v[:n])/n]
    for x in v[n:]:out.append(x*a+out[-1]*(1-a))
    return out

def rsi(v,n=14):
    if len(v)<n+1:return 50
    d=[v[i]-v[i-1] for i in range(1,len(v))][-n:]
    g=sum(max(x,0) for x in d)/n
    l=sum(max(-x,0) for x in d)/n
    return 100 if l==0 else 100-100/(1+g/l)

def atr(cs,n=14):
    if len(cs)<n+1:return 0
    tr=[]
    for i in range(1,len(cs)):
        c,p=cs[i],cs[i-1]
        tr.append(max(c["h"]-c["l"],abs(c["h"]-p["c"]),abs(c["l"]-p["c"])))
    return sum(tr[-n:])/n

def adx(cs,n=14):
    if len(cs)<n+2:return 0
    plus=[];minus=[];trs=[]
    for i in range(1,len(cs)):
        c,p=cs[i],cs[i-1]
        u=c["h"]-p["h"];d=p["l"]-c["l"]
        plus.append(max(u,0) if u>d else 0)
        minus.append(max(d,0) if d>u else 0)
        trs.append(max(c["h"]-c["l"],abs(c["h"]-p["c"]),abs(c["l"]-p["c"])))
    t=sum(trs[-n:]) or 1
    p=100*sum(plus[-n:])/t;m=100*sum(minus[-n:])/t
    return 100*abs(p-m)/(p+m) if p+m else 0

def category(sym):
    s=sym.upper()
    if any(x in s for x in ("PAXG","XAU","XAG","GOLD","SILVER")):
        return "금속·원자재"
    if any(x in s for x in ("SPY","QQQ","NVDA","TSLA","AAPL","MSFT","COIN","NBIS","CRWV","UNITREE")):
        return "미국 주식·지수"
    return "암호화폐"

def market():
    out=[]
    for x in api("/futures/usdt/tickers"):
        sym=x.get("contract","")
        price=num(x.get("last"))
        turnover=num(x.get("volume_24h_quote") or x.get("volume_24h_settle") or x.get("volume_24h_usd"))
        if sym.endswith("_USDT") and price>0 and turnover>=MIN_TURNOVER:
            out.append((sym,price,turnover))
    return sorted(out,key=lambda x:x[2],reverse=True)[:MAX_SCAN]

def btc_trend():
    try:
        c=candles("BTC_USDT","1h",80)
        v=[x["c"] for x in c[:-1]]
        a=ema(v,20);b=ema(v,50)
        return "up" if a[-1]>b[-1] else "down" if a[-1]<b[-1] else "flat"
    except Exception:return "flat"

def find_signal(sym,price,turnover,btc):
    try:
        m=candles(sym,"15m",100)[:-1]
        h=candles(sym,"1h",80)[:-1]
        if len(m)<55 or len(h)<55:return None
        v=[x["c"] for x in m];hv=[x["c"] for x in h]
        e20=ema(v,20);e50=ema(v,50);he20=ema(hv,20);he50=ema(hv,50)
        a=atr(m);r=rsi(v);ad=adx(m)
        if not a or a/price>.08:return None
        vb=sum(x["v"] for x in m[-21:-1])/20
        vr=m[-1]["v"]/vb if vb else 0
        L=SCORE_L=0;SHORT=0
        L+=e20[-1]>e50[-1];SHORT+=e20[-1]<e50[-1]
        L+=he20[-1]>he50[-1];SHORT+=he20[-1]<he50[-1]
        L+=r>=53;SHORT+=r<=47
        L+=ad>=18;SHORT+=ad>=18
        L+=vr>=1.2;SHORT+=vr>=1.2
        c=m[-1];pos=(c["c"]-c["l"])/max(c["h"]-c["l"],1e-12)
        L+=pos>=.68;SHORT+=pos<=.32
        direction="LONG" if L>SHORT else "SHORT" if SHORT>L else ""
        score=max(L,SHORT)
        if not direction or score<MIN_SCORE:return None
        if abs(price-e20[-1])>a*.45:return None
        if category(sym)=="암호화폐":
            if direction=="LONG" and btc=="down":return None
            if direction=="SHORT" and btc=="up":return None
        risk=a*STOP_ATR
        sign=1 if direction=="LONG" else -1
        sl=price-sign*risk
        t1=price+sign*risk*TP_R[0]
        t2=price+sign*risk*TP_R[1]
        t3=price+sign*risk*TP_R[2]
        ap=a/price*100
        lev=3 if ap>=3 else 5 if ap>=2 else 8 if ap>=1 else 10
        return {"symbol":sym,"category":category(sym),"direction":direction,
          "entry":price,"sl":sl,"tp1":t1,"tp2":t2,"tp3":t3,
          "leverage":lev,"score":score,"max_score":6,"rsi":r,"adx":ad,
          "volume_ratio":vr,"bar_time":datetime.fromtimestamp(m[-1]["t"],timezone.utc).isoformat(),
          "entry_time":now().isoformat(),"status":"추적 중","tp1_hit":False,
          "tp2_hit":False,"breakeven":False,"last_price":price,"turnover":turnover}
    except Exception as e:
        print("Signal scan error",sym,e)
        return None

def target_status(p):
    if p.get("tp2_hit"):return "TP3 대기"
    if p.get("tp1_hit"):return "TP2 대기"
    return "TP1 대기"

def active_report(st,prices=None):
    prices=prices or {}
    pos=st["positions"]
    lines=["📊 현재 활성 시그널", "━━━━━━━━━━━━━━━━━━"]
    if not pos:return "📊 현재 활성 시그널\n활성 시그널 없음"
    for i,(sym,p) in enumerate(pos.items(),1):
        cur=num(prices.get(sym),num(p.get("last_price"),p["entry"]))
        p["last_price"]=cur
        sign=1 if p["direction"]=="LONG" else -1
        ret=sign*pct(p["entry"],cur)
        direction="🟢 LONG" if p["direction"]=="LONG" else "🔴 SHORT"
        opened=p.get("entry_time") or p.get("opened_at") or p.get("bar_time") or now().isoformat()
        lines.extend([
          f"{i}. {sym} ({p.get('category',category(sym))}) {direction} | 레버리지: {p.get('leverage','-')}x",
          f"진입: {p['entry']:.6g} | 현재: {cur:.6g}",
          f"상태: {p.get('status','추적 중')} | 수익률: {ret:+.2f}%",
          f"⏱️ 보유 시간: {elapsed(opened)}",
          f"🎯 진행: {target_status(p)} | 손절가: {p['sl']:.6g}"
        ])
    lines.append("🕒 조회 시각: "+now().astimezone(KST).strftime("%m-%d %H:%M:%S KST"))
    return "\n".join(lines)

def signal_message(p):
    sign=1 if p["direction"]=="LONG" else -1
    d="🟢 LONG" if sign==1 else "🔴 SHORT"
    return (
      "🚨 GATE.IO FUTURES SIGNAL\n━━━━━━━━━━━━━━━━━━\n"
      f"📌 계약: {p['symbol']} ({p['category']})\n📈 방향: {d}\n"
      f"💵 모의 기준 진입가: {p['entry']:.6g}\n"
      f"🛑 손절가: {p['sl']:.6g} ({pct(p['entry'],p['sl']):+.2f}%)\n"
      f"🎯 TP1: {p['tp1']:.6g} ({sign*pct(p['entry'],p['tp1']):+.2f}% / {TP_R[0]}R)\n"
      f"🎯 TP2: {p['tp2']:.6g} ({sign*pct(p['entry'],p['tp2']):+.2f}% / {TP_R[1]}R)\n"
      f"🎯 TP3: {p['tp3']:.6g} ({sign*pct(p['entry'],p['tp3']):+.2f}% / {TP_R[2]}R)\n"
      "━━━━━━━━━━━━━━━━━━\n"
      f"⚡ 추천 레버리지: {p['leverage']}x (참고값)\n"
      f"🧠 점수: {p['score']}/{p['max_score']}\n"
      f"📊 RSI {p['rsi']:.1f} | ADX {p['adx']:.1f} | 거래량 {p['volume_ratio']:.2f}배\n"
      f"🕒 기준봉: {kst(p['bar_time'])}\n"
      f"🕒 발송: {now().astimezone(KST).strftime('%Y-%m-%d %H:%M:%S KST')}\n"
      "⚠️ 모의 시그널이며 실제 주문은 실행하지 않습니다."
    )

def slot_notice(st,action,sym,prices=None):
    return (f"🔄 활성 슬롯 변경\n변경: {action} {sym}\n"
            f"활성 슬롯: {len(st['positions'])}/{MAX_ACTIVE}\n\n"
            +active_report(st,prices))

def close_position(st,sym,reason,price):
    p=st["positions"].pop(sym,None)
    if not p:return
    sign=1 if p["direction"]=="LONG" else -1
    ret=sign*pct(p["entry"],price)
    label={"tp3":"TP3 도달","sl":"손절 도달","be":"본전 손절"}.get(reason,reason)
    tg(f"🏁 시그널 종료 | {sym}\n종료 사유: {label}\n진입: {p['entry']:.6g} | 종료: {price:.6g}\n진입 대비: {ret:+.2f}%\n⏱️ 보유 시간: {elapsed(p.get('entry_time',now().isoformat()))}\n⚠️ 모의 기준")
    tg(slot_notice(st,"슬롯 해제",sym))
    log("closed",{"symbol":sym,"reason":reason,"price":price})
    save(st)

def monitor(st,prices):
    changed=False
    for sym,p in list(st["positions"].items()):
        try:
            cs=candles(sym,"1m",4)
            if len(cs)<2:continue
            c=cs[-2];cur=num(prices.get(sym),c["c"]);p["last_price"]=cur
            long=p["direction"]=="LONG"
            slhit=c["l"]<=p["sl"] if long else c["h"]>=p["sl"]
            tp1=c["h"]>=p["tp1"] if long else c["l"]<=p["tp1"]
            tp2=c["h"]>=p["tp2"] if long else c["l"]<=p["tp2"]
            tp3=c["h"]>=p["tp3"] if long else c["l"]<=p["tp3"]
            if slhit:
                close_position(st,sym,"be" if p.get("breakeven") else "sl",p["sl"])
                changed=True
                continue
            if tp3:
                close_position(st,sym,"tp3",p["tp3"])
                changed=True
                continue
            if tp2 and not p.get("tp2_hit"):
                p["tp1_hit"]=p["tp2_hit"]=p["breakeven"]=True
                p["sl"]=p["entry"];p["status"]="TP2 도달"
                tg(f"🎯 TP2 도달 | {sym}\n다음 목표: TP3\n손절가: 진입가로 이동\n⏱️ 보유 시간: {elapsed(p['entry_time'])}")
                tg(active_report(st,prices));changed=True
            elif tp1 and not p.get("tp1_hit"):
                p["tp1_hit"]=p["breakeven"]=True
                p["sl"]=p["entry"];p["status"]="TP1 도달"
                tg(f"🎯 TP1 도달 | {sym}\n다음 목표: TP2\n손절가: 진입가로 이동\n⏱️ 보유 시간: {elapsed(p['entry_time'])}")
                tg(active_report(st,prices));changed=True
        except Exception as e:print("Monitor error",sym,e)
    if changed:save(st)

def main():
    if not TOKEN or not CHAT:
        print("Set TELEGRAM_TOKEN and TELEGRAM_CHAT_ID")
        return
    st=load(STATE,{"positions":{},"last_signal":{}})
    st.setdefault("positions",{});st.setdefault("last_signal",{})
    for sym,p in st["positions"].items():
        if not p.get("entry_time"):
            p["entry_time"]=p.get("opened_at") or p.get("bar_time") or now().isoformat()
        p.setdefault("category",category(sym))
        p.setdefault("status","TP2 도달" if p.get("tp2_hit") else "TP1 도달" if p.get("tp1_hit") else "추적 중")
    mk=market();prices={s:p for s,p,_ in mk}
    monitor(st,prices)
    btc=btc_trend()
    for sym,price,turnover in mk:
        if sym in st["positions"] or len(st["positions"])>=MAX_ACTIVE:continue
        last=st["last_signal"].get(sym)
        if last and (now()-parsed(last)).total_seconds()<COOLDOWN*60:continue
        p=find_signal(sym,price,turnover,btc)
        if not p:continue
        st["positions"][sym]=p;st["last_signal"][sym]=now().isoformat()
        save(st);tg(signal_message(p))
        tg(slot_notice(st,"새 시그널 진입",sym,prices))
        log("opened",{"symbol":sym,"direction":p["direction"],"entry":price})
    save(st)
    print(active_report(st,prices))

if __name__=="__main__":main()
