import os,json,time,math,requests
from pathlib import Path
from datetime import datetime,timezone,timedelta

BASE="https://api.gateio.ws/api/v4"
TOKEN=os.getenv("GATE_TELEGRAM_TOKEN") or os.getenv("TELEGRAM_TOKEN","")
CHAT=os.getenv("GATE_TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID","")
STATE=Path("gate_state.json")
MAX_ACTIVE=int(os.getenv("MAX_ACTIVE","10"))
MAX_SCAN=int(os.getenv("MAX_SCAN","90"))
MIN_TURNOVER=float(os.getenv("MIN_TURNOVER","500000"))
MIN_SCORE=int(os.getenv("MIN_SCORE","6"))
COOLDOWN=int(os.getenv("COOLDOWN","60"))
STOP_ATR=float(os.getenv("STOP_ATR","1.4"))
TP_R=(1.2,2.0,3.0)
TZ=timezone(timedelta(hours=9))
S=requests.Session()
S.headers.update({"Accept":"application/json","User-Agent":"GateSignalBot/2.0"})

def now():
    return datetime.now(TZ)

def stamp():
    return now().isoformat(timespec="seconds")

def num(v,d=0.0):
    try:
        x=float(v)
        return x if math.isfinite(x) else d
    except (TypeError,ValueError):
        return d

def api(path,params=None):
    r=S.get(BASE+path,params=params,timeout=15)
    r.raise_for_status()
    return r.json()

def send(msg):
    if not TOKEN or not CHAT:
        print("TELEGRAM CONFIG MISSING")
        return False
    try:
        r=requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id":CHAT,"text":msg,"disable_web_page_preview":True},
            timeout=15)
        print("TG",r.status_code,r.text[:150])
        return r.ok and r.json().get("ok",False)
    except Exception as e:
        print("TG ERROR",e)
        return False

def read_json(path,default):
    if not path.exists():
        return default
    try:
        with open(path,encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        raise RuntimeError(f"{path} 읽기 실패. 파일을 덮어쓰지 않음: {e}")

def save_json(path,data):
    tmp=path.with_suffix(path.suffix+".tmp")
    with open(tmp,"w",encoding="utf-8") as f:
        json.dump(data,f,ensure_ascii=False,indent=2)
    tmp.replace(path)

def load_state():
    st=read_json(STATE,{"positions":{},"last_signal":{},"history":[]})
    if not isinstance(st,dict):
        raise RuntimeError("상태 파일 형식 오류")
    st.setdefault("positions",{})
    st.setdefault("last_signal",{})
    st.setdefault("history",[])
    if not isinstance(st["positions"],dict):
        raise RuntimeError("positions 형식 오류: 기존 파일을 수정하지 마세요.")
    return st

def save_state(st):
    save_json(STATE,st)

def candles(sym,interval,limit=120):
    raw=api("/futures/usdt/candlesticks",{
        "contract":sym,"interval":interval,"limit":limit})
    out=[]
    for x in raw:
        if isinstance(x,dict):
            q={k:num(x.get(k)) for k in ("t","o","h","l","c","v")}
        elif isinstance(x,(list,tuple)) and len(x)>=6:
            q=dict(t=num(x[0]),v=num(x[1]),c=num(x[2]),
                   h=num(x[3]),l=num(x[4]),o=num(x[5]))
        else:
            continue
        if min(q["o"],q["h"],q["l"],q["c"])>0:
            out.append(q)
    return sorted(out,key=lambda x:x["t"])

def ema(a,n):
    if len(a)<n:
        return None
    e=sum(a[:n])/n
    k=2/(n+1)
    for x in a[n:]:
        e=x*k+e*(1-k)
    return e

def rsi(a,n=14):
    if len(a)<n+1:
        return None
    d=[a[i]-a[i-1] for i in range(1,len(a))]
    g=sum(max(x,0) for x in d[-n:])/n
    l=sum(max(-x,0) for x in d[-n:])/n
    return 100 if l==0 else 100-100/(1+g/l)

def atr(cs,n=14):
    if len(cs)<n+1:
        return None
    tr=[]
    for i in range(1,len(cs)):
        c=cs[i]
        prev=cs[i-1]["c"]
        tr.append(max(c["h"]-c["l"],abs(c["h"]-prev),abs(c["l"]-prev)))
    return sum(tr[-n:])/n

def market():
    rows=api("/futures/usdt/tickers")
    out=[]
    for x in rows:
        if not x.get("contract","").endswith("_USDT"):
            continue
        price=num(x.get("last"))
        turnover=num(x.get("volume_24h_quote",x.get("volume_24h_usd",0)))
        if price>0 and turnover>=MIN_TURNOVER:
            x["_price"]=price
            x["_turnover"]=turnover
            out.append(x)
    out.sort(key=lambda x:x["_turnover"],reverse=True)
    return out[:MAX_SCAN]

def btc_direction():
    try:
        cs=candles("BTC_USDT","1h",100)
        a=[x["c"] for x in cs[:-1]]
        e20,e50=ema(a,20),ema(a,50)
        if e20 is None or e50 is None:
            return "FLAT"
        if a[-1]>e20>e50:
            return "LONG"
        if a[-1]<e20<e50:
            return "SHORT"
    except Exception as e:
        print("BTC ERROR",e)
    return "FLAT"

def find_signal(t,btc):
    sym=t["contract"]
    cs=candles(sym,"15m",100)
    if len(cs)<60:
        return None
    cs=cs[:-1]
    a=[x["c"] for x in cs]
    v=[x["v"] for x in cs]
    last=cs[-1]
    e9,e21,e50=ema(a,9),ema(a,21),ema(a,50)
    rv,av=rsi(a),atr(cs)
    if any(x is None for x in (e9,e21,e50,rv,av)) or av<=0:
        return None
    avg=sum(v[-21:-1])/20 if len(v)>=21 else 0
    surge=v[-1]/avg if avg>0 else 0
    body=abs(last["c"]-last["o"])>=av*.15
    funding=num(t.get("funding_rate"))
    lc=[btc=="LONG",e9>e21>e50,52<=rv<=72,
        surge>=1.2,last["c"]>e21,last["c"]>last["o"] and body]
    sc=[btc=="SHORT",e9<e21<e50,28<=rv<=48,
        surge>=1.2,last["c"]<e21,last["c"]<last["o"] and body]
    side="LONG" if sum(lc)>=sum(sc) else "SHORT"
    checks=lc if side=="LONG" else sc
    score=sum(checks)
    if score<MIN_SCORE:
        return None
    if side=="LONG" and funding>.003:
        return None
    if side=="SHORT" and funding<-.003:
        return None
    entry=num(t.get("_price"),last["c"])
    dist=max(av*STOP_ATR,entry*.002)
    sl=entry-dist if side=="LONG" else entry+dist
    tps=[entry+dist*r if side=="LONG" else entry-dist*r for r in TP_R]
    reasons=[]
    names=["BTC 추세 일치","EMA 추세 정렬","RSI 조건 충족",
           "거래량 증가","EMA21 조건 충족","캔들 방향 확인"]
    reasons=[names[i] for i,x in enumerate(checks) if x]
    return {
        "symbol":sym,"side":side,"entry":entry,"sl":sl,
        "tp1":tps[0],"tp2":tps[1],"tp3":tps[2],
        "rsi":rv,"atr":av,"volume_surge":surge,"score":score,
        "reasons":reasons,"entry_time":stamp(),"last_price":entry,
        "tp1_hit":False,"tp2_hit":False,"tp3_hit":False,
        "status":"ACTIVE"
    }

def elapsed(p):
    try:
        d=datetime.fromisoformat(p["entry_time"])
        if d.tzinfo is None:
            d=d.replace(tzinfo=TZ)
        sec=max(0,int((now()-d.astimezone(TZ)).total_seconds()))
        days,sec=divmod(sec,86400)
        hours,sec=divmod(sec,3600)
        mins=sec//60
        return f"{days}일 {hours}시간 {mins}분" if days else f"{hours}시간 {mins}분"
    except Exception:
        return "-"

def pnl(p,price):
    entry=num(p.get("entry"))
    if entry<=0:
        return 0
    delta=price-entry
    if p.get("side")=="SHORT":
        delta=-delta
    return delta/entry*100

def tp_status(p):
    if p.get("tp3_hit"):
        return "TP3 달성"
    if p.get("tp2_hit"):
        return "TP1·TP2 달성 / TP3 대기"
    if p.get("tp1_hit"):
        return "TP1 달성 / TP2 대기"
    return "TP1 대기 / TP2 대기 / TP3 대기"

def active_report(st):
    ps=st["positions"]
    lines=["📊 GATE.IO FUTURES | 활성 시그널",
           f"활성 슬롯: {len(ps)}/{MAX_ACTIVE}"]
    if not ps:
        lines.append("활성 시그널 없음")
    for i,(sym,p) in enumerate(ps.items(),1):
        price=num(p.get("last_price"),num(p.get("entry")))
        lines.extend([
            f"\n#{i} {sym} | {p.get('side')}",
            f"진입 {num(p.get('entry')):.8g} / 현재 {price:.8g}",
            f"변동 {pnl(p,price):+.2f}% | 보유 {elapsed(p)}",
            tp_status(p),
            f"SL {num(p.get('sl')):.8g}",
            f"TP1 {num(p.get('tp1')):.8g} | TP2 {num(p.get('tp2')):.8g} | TP3 {num(p.get('tp3')):.8g}"
        ])
    return "\n".join(lines)

def stats_report(st):
    counts={"TP3":0,"TP2":0,"TP1":0,"SL":0}
    for x in st.get("history",[]):
        if x.get("outcome") in counts:
            counts[x["outcome"]]+=1
    total=sum(counts.values())
    wins=counts["TP3"]+counts["TP2"]+counts["TP1"]
    rate=wins/total*100 if total else 0
    return (
        "📈 종료 시그널 통계\n━━━━━━━━━━━━━━\n"
        f"총 종료: {total}\nTP3 종료: {counts['TP3']}회\n"
        f"TP2 종료: {counts['TP2']}회\nTP1 종료: {counts['TP1']}회\n"
        f"손절 종료: {counts['SL']}회\n승률: {rate:.2f}% ({wins}/{total})"
    )

def close_position(st,sym,reason,price):
    p=st["positions"].get(sym)
    if not p:
        return
    highest=3 if p.get("tp3_hit") else 2 if p.get("tp2_hit") else 1 if p.get("tp1_hit") else 0
    outcome="TP3" if reason=="TP3" else "TP2" if highest==2 else "TP1" if highest==1 else "SL"
    record={
        "symbol":sym,"side":p.get("side"),
        "entry":num(p.get("entry")),"exit":price,
        "entry_time":p.get("entry_time"),"exit_time":stamp(),
        "duration":elapsed(p),"outcome":outcome,"reason":reason,
        "return_pct":round(pnl(p,price),4)
    }
    st["history"].append(record)
    st["history"]=st["history"][-5000:]
    del st["positions"][sym]
    save_state(st)
    send(
        f"🏁 시그널 종료 | {sym} {p.get('side')}\n"
        f"분류: {outcome} | 사유: {reason}\n"
        f"진입: {num(p.get('entry')):.8g} → 종료: {price:.8g}\n"
        f"변동률: {pnl(p,price):+.2f}%\n보유 시간: {elapsed(p)}"
    )
    send(active_report(st))
    send(stats_report(st))

def monitor(st):
    changed_any=False
    for sym in list(st["positions"]):
        p=st["positions"].get(sym)
        if not p:
            continue
        changed=False
        try:
            cs=candles(sym,"1m",5)
            if len(cs)<2:
                continue
            c=cs[-2]
            price=c["c"]
            p["last_price"]=price
            long=p.get("side")=="LONG"
            sl=num(p.get("sl"))
            slhit=c["l"]<=sl if long else c["h"]>=sl
            # 동일 1분봉에서 SL과 TP가 모두 닿으면 보수적으로 SL 우선
            if slhit:
                close_position(st,sym,"SL 또는 본절 청산",price)
                changed_any=True
                continue
            for target,flag in [
                ("tp1","tp1_hit"),("tp2","tp2_hit"),("tp3","tp3_hit")
            ]:
                if p.get(flag):
                    continue
                level=num(p.get(target))
                hit=c["h"]>=level if long else c["l"]<=level
                if not hit:
                    continue
                p[flag]=True
                changed=True
                if target=="tp1":
                    p["sl"]=num(p.get("entry"))
                    send(f"🎯 TP1 도달 | {sym}\n변동: {pnl(p,level):+.2f}%\n손절가를 진입가로 이동\nTP2 대기")
                elif target=="tp2":
                    p["sl"]=num(p.get("entry"))
                    send(f"🎯 TP2 도달 | {sym}\n변동: {pnl(p,level):+.2f}%\nTP3 대기")
                else:
                    p["last_price"]=level
                    save_state(st)
                    close_position(st,sym,"TP3",level)
                    changed_any=True
                    break
            if sym in st["positions"] and changed:
                save_state(st)
                changed_any=True
        except Exception as e:
            print("MONITOR ERROR",sym,str(e)[:200])
    if changed_any:
        send(active_report(st))

def scan(st):
    if len(st["positions"])>=MAX_ACTIVE:
        print("SLOTS FULL",len(st["positions"]))
        return
    btc=btc_direction()
    if btc=="FLAT":
        print("BTC FILTER: FLAT")
        return
    try:
        tickers=market()
    except Exception as e:
        print("MARKET ERROR",e)
        return
    for t in tickers:
        if len(st["positions"])>=MAX_ACTIVE:
            break
        sym=t["contract"]
        if sym in st["positions"]:
            continue
        last=num(st["last_signal"].get(sym))
        if time.time()-last<COOLDOWN*60:
            continue
        try:
            sig=find_signal(t,btc)
            if not sig:
                continue
            st["positions"][sym]=sig
            st["last_signal"][sym]=time.time()
            save_state(st)
            send(
                f"🚨 GATE.IO FUTURES SIGNAL\n━━━━━━━━━━━━━━\n"
                f"{sym} | {sig['side']}\n진입: {sig['entry']:.8g}\n"
                f"SL: {sig['sl']:.8g}\nTP1: {sig['tp1']:.8g}\n"
                f"TP2: {sig['tp2']:.8g}\nTP3: {sig['tp3']:.8g}\n"
                f"점수: {sig['score']}/6\nRSI: {sig['rsi']:.1f} | "
                f"거래량: {sig['volume_surge']:.1f}배\n"
                f"근거: {', '.join(sig['reasons'])}\n"
                "※ 시그널 전용, 자동 주문 미실행"
            )
            print("SIGNAL",sym,sig["side"],sig["score"])
        except Exception as e:
            print("SCAN ERROR",sym,str(e)[:200])

def main():
    print("GATE.IO FUTURES BOT",stamp())
    st=load_state()
    for p in st["positions"].values():
        p.setdefault("entry_time",stamp())
        p.setdefault("tp1_hit",False)
        p.setdefault("tp2_hit",False)
        p.setdefault("tp3_hit",False)
        p.setdefault("status","ACTIVE")
        p.setdefault("last_price",num(p.get("entry")))
    save_state(st)
    monitor(st)
    scan(st)
    print("ACTIVE:",len(st["positions"]))
    print(stats_report(st))

if __name__=="__main__":
    main()
