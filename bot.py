import os,json,time,math,requests
from datetime import datetime,timezone

BASE="https://api.gateio.ws/api/v4"
TOKEN=(os.getenv("GATE_TELEGRAM_TOKEN") or os.getenv("TELEGRAM_TOKEN") or "").strip()
CHAT=(os.getenv("GATE_TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID") or "").strip()
STATE="gate_state.json"; LOG="gate_log.json"
MAX_SCAN=90; MIN_TURNOVER=500000; MAX_ACTIVE=10
MIN_SCORE=6; COOLDOWN=60; MAX_GAP_ATR=.45
MAX_ATR_PCT=.08; STOP_ATR=1.4
TP1_R,TP2_R,TP3_R=1.2,2,3
S=requests.Session()
S.headers.update({"Accept":"application/json","User-Agent":"GateSignalBot/3.0"})

def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def kst(ts=None):
    from datetime import timedelta
    return datetime.fromtimestamp(ts or time.time(),timezone.utc).astimezone(
        timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M:%S KST")

def n(x,d=0):
    try:
        x=float(x)
        return x if math.isfinite(x) else d
    except:
        return d

def load(path,default):
    try:
        with open(path,encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default

def save(path,data):
    tmp=path+".tmp"
    with open(tmp,"w",encoding="utf-8") as f:
        json.dump(data,f,ensure_ascii=False,indent=2,allow_nan=False)
    os.replace(tmp,path)

def log(event,**kw):
    a=load(LOG,[])
    if not isinstance(a,list): a=[]
    a.append({"time":now(),"event":event,**kw})
    save(LOG,a[-1500:])

def api(path,params=None):
    r=S.get(BASE+path,params=params,timeout=20)
    r.raise_for_status()
    return r.json()

def tg(msg):
    if not TOKEN or not CHAT:
        print("ERROR: Telegram secrets missing")
        return False
    for attempt in range(3):
        try:
            r=S.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                json={"chat_id":CHAT,"text":msg,"parse_mode":"HTML",
                      "disable_web_page_preview":True},timeout=15)
            if r.ok and r.json().get("ok"): return True
            print("Telegram error:",r.status_code,r.text[:200])
            if 400<=r.status_code<500: return False
        except Exception as e:
            print("Telegram error:",e)
        time.sleep(attempt+1)
    return False

def fmt(x):
    x=n(x)
    if abs(x)>=1000: return f"{x:,.3f}".rstrip("0").rstrip(".")
    if abs(x)>=1: return f"{x:.5f}".rstrip("0").rstrip(".")
    if abs(x)>=.01: return f"{x:.6f}".rstrip("0").rstrip(".")
    return f"{x:.8f}".rstrip("0").rstrip(".")

def pct(a,b):
    return (b-a)/a*100 if a else 0

def ema(a,p):
    if not a: return []
    k=2/(p+1);out=[a[0]]
    for x in a[1:]: out.append(x*k+out[-1]*(1-k))
    return out

def rsi(a,p=14):
    if len(a)<p+1: return 50
    ds=[a[i]-a[i-1] for i in range(1,len(a))]
    g=sum(max(x,0) for x in ds[:p])/p
    l=sum(max(-x,0) for x in ds[:p])/p
    for x in ds[p:]:
        g=(g*(p-1)+max(x,0))/p
        l=(l*(p-1)+max(-x,0))/p
    return 100 if l==0 else 100-100/(1+g/l)

def atr(c,p=14):
    if len(c)<p+1: return 0
    tr=[]
    for i in range(1,len(c)):
        x,y=c[i],c[i-1]
        tr.append(max(x["h"]-x["l"],abs(x["h"]-y["c"]),abs(x["l"]-y["c"])))
    a=sum(tr[:p])/p
    for x in tr[p:]: a=(a*(p-1)+x)/p
    return a

def adx(c,p=14):
    if len(c)<2*p+2: return 0
    tr=[];pd=[];md=[]
    for i in range(1,len(c)):
        x,y=c[i],c[i-1]
        up=x["h"]-y["h"];dn=y["l"]-x["l"]
        pd.append(up if up>dn and up>0 else 0)
        md.append(dn if dn>up and dn>0 else 0)
        tr.append(max(x["h"]-x["l"],abs(x["h"]-y["c"]),abs(x["l"]-y["c"])))
    dx=[]
    for i in range(p-1,len(tr)):
        z=sum(tr[i-p+1:i+1])
        if z<=0: continue
        a=100*sum(pd[i-p+1:i+1])/z
        b=100*sum(md[i-p+1:i+1])/z
        dx.append(100*abs(a-b)/max(a+b,1e-9))
    tail=dx[-p:]
    return sum(tail)/len(tail) if tail else 0

# ATR 기반 참고 레버리지. 거래소 설정은 변경하지 않음.
def recommend_leverage(price,av):
    if price<=0 or av<=0: return 0,0
    v=av/price*100
    return (3 if v>=3 else 5 if v>=2 else 8 if v>=1 else 10),v

def get_candles(sym,interval,limit=100):
    raw=api("/futures/usdt/candlesticks",
        {"contract":sym,"interval":interval,"limit":limit})
    out=[]
    for x in raw if isinstance(raw,list) else []:
        try:
            out.append({"t":int(x["t"]),"o":n(x["o"]),
                "h":n(x["h"]),"l":n(x["l"]),"c":n(x["c"]),
                "v":n(x.get("sum",x.get("v",0)))})
        except: pass
    out.sort(key=lambda x:x["t"])
    return out[:-1] if len(out)>2 else []

def trend(c):
    a=[x["c"] for x in c]
    if len(a)<55: return "NEUTRAL"
    e20,e50=ema(a,20),ema(a,50)
    if a[-1]>e20[-1]>e50[-1] and e20[-1]>e20[-4]: return "BULL"
    if a[-1]<e20[-1]<e50[-1] and e20[-1]<e20[-4]: return "BEAR"
    return "NEUTRAL"

def asset_type(c):
    t=str(c.get("contract_type") or "").lower()
    name=c.get("name","")
    if t in {"metals","commodities","indices","stocks","forex"}: return t
    if name=="BTC_USDT": return "btc"
    if t in {"crypto","cryptocurrency","coin","coins"}: return "crypto"
    if t in {"","other","unknown"} and name.endswith("_USDT"): return "crypto"
    return "other"

def market():
    cs=api("/futures/usdt/contracts")
    ts=api("/futures/usdt/tickers")
    tm={x.get("contract"):x for x in ts if x.get("contract")}
    out=[]
    for c in cs:
        name=c.get("name","")
        if c.get("status")!="trading" or not name.endswith("_USDT"): continue
        typ=asset_type(c)
        if typ=="other": continue
        t=tm.get(name,{})
        turnover=n(t.get("volume_24h_quote") or t.get("volume_24h_settle") or t.get("volume_24h_usd"))
        if name!="BTC_USDT" and turnover<MIN_TURNOVER: continue
        c["_asset"]=typ;c["_turnover"]=turnover
        out.append(c)
    out.sort(key=lambda x:x["_turnover"],reverse=True)
    btc=next((x for x in out if x["name"]=="BTC_USDT"),None)
    scan=[x for x in out if x["name"]!="BTC_USDT"][:MAX_SCAN]
    if btc: scan.insert(0,btc)
    return scan,tm

def btc_info(tm):
    t=tm.get("BTC_USDT",{})
    ch=n(t.get("change_percentage"));m15=0;tr="NEUTRAL"
    try:
        c=get_candles("BTC_USDT","15m",8)
        if len(c)>1 and c[-2]["c"]: m15=pct(c[-2]["c"],c[-1]["c"])
    except: pass
    try: tr=trend(get_candles("BTC_USDT","1h",90))
    except: pass
    return {"trend":tr,"change24":ch,"m15":m15,
            "crash":ch<=-4 or m15<=-1.5}

def analyze(c,t,btc):
    sym=c["name"];typ=c["_asset"]
    m=get_candles(sym,"15m",110);h=get_candles(sym,"1h",90)
    if len(m)<60 or len(h)<55: return None
    a=[x["c"] for x in m];ha=[x["c"] for x in h]
    e20,e50=ema(a,20),ema(a,50)
    he20,he50=ema(ha,20),ema(ha,50)
    av=atr(m)
    if av<=0 or a[-1]<=0 or av/a[-1]>MAX_ATR_PCT: return None
    strength=adx(m);rv=rsi(a)
    vols=[x["v"] for x in m]
    vr=vols[-1]/max(sum(vols[-21:-1])/max(len(vols[-21:-1]),1),1e-9)
    x=m[-1];rng=max(x["h"]-x["l"],1e-9)
    cp=(x["c"]-x["l"])/rng
    hi=max(z["h"] for z in m[-21:-1]);lo=min(z["l"] for z in m[-21:-1])
    bull=a[-1]>e20[-1]>e50[-1] and e20[-1]>e20[-4]
    bear=a[-1]<e20[-1]<e50[-1] and e20[-1]<e20[-4]
    hb=ha[-1]>he20[-1]>he50[-1] and he20[-1]>he20[-4]
    hs=ha[-1]<he20[-1]<he50[-1] and he20[-1]<he20[-4]
    lc=[bull,hb,48<=rv<=72,strength>=14,vr>=1.05,cp>=.58 or x["c"]>hi]
    sc=[bear,hs,28<=rv<=54,strength>=14,vr>=1.05,cp<=.42 or x["c"]<lo]
    ls=sum(lc);ss=sum(sc);bn="미적용"
    if typ=="crypto":
        rel=n(t.get("change_percentage"))-btc["change24"]
        bn=f"BTC {btc['trend']} / 24h {btc['change24']:+.2f}%"
        if btc["crash"]:
            if rel<3: ls=-99
            else: ls-=1
        elif btc["trend"]=="BEAR" and not(rel>=3 and hb and bull): ls-=1
        elif btc["trend"]=="BULL" and hs and bear: ss-=1
    direction="LONG" if ls>=MIN_SCORE and ls>ss else "SHORT" if ss>=MIN_SCORE and ss>ls else None
    if not direction: return None
    price=n(t.get("last")) or x["c"]
    if abs(price-x["c"])/av>MAX_GAP_ATR: return None
    lev,ap=recommend_leverage(price,av)
    risk=max(STOP_ATR*av,price*.002)
    if direction=="LONG":
        sl=price-risk;tp1=price+TP1_R*risk;tp2=price+TP2_R*risk;tp3=price+TP3_R*risk
    else:
        sl=price+risk;tp1=price-TP1_R*risk;tp2=price-TP2_R*risk;tp3=price-TP3_R*risk
    reasons=[]
    reasons.append("1H·15M 상승 추세 일치" if direction=="LONG" and hb and bull else
                   "1H·15M 하락 추세 일치" if direction=="SHORT" and hs and bear else "추세 조건 확인")
    reasons.append(f"RSI {rv:.1f}")
    reasons.append(f"ADX {strength:.1f}")
    reasons.append(f"거래량 {vr:.2f}배")
    reasons.append("캔들 종가 위치 양호")
    if typ=="crypto": reasons.append("BTC 방향 필터 통과")
    return {"symbol":sym,"asset":typ,"direction":direction,"entry":price,
        "sl":sl,"tp1":tp1,"tp2":tp2,"tp3":tp3,"risk":risk,
        "atr":av,"atr_pct":ap,"leverage":lev,"rsi":rv,"adx":strength,
        "volume_ratio":vr,"score":max(ls,ss),"bar":x["t"],
        "btc_note":bn,"change24":n(t.get("change_percentage")),
        "reasons":reasons,"created":now(),"tp1_hit":False,
        "tp2_hit":False,"last_monitor_bar":0}

def asset_name(t):
    return {"crypto":"암호화폐","btc":"비트코인","metals":"금속",
        "commodities":"원자재","stocks":"주식","indices":"주가지수",
        "forex":"외환"}.get(t,t)

def link(sym):
    return f"https://www.gate.com/futures/USDT/{sym}"

def slot_msg(state,title,detail):
    active=state["positions"]
    names=", ".join(active.keys()) if active else "없음"
    return (f"🧩 <b>종목 슬롯 변경</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"변경: <b>{title}</b>\n{detail}\n"
        f"사용 슬롯: <b>{len(active)}/{MAX_ACTIVE}</b>\n"
        f"남은 슬롯: <b>{MAX_ACTIVE-len(active)}</b>\n"
        f"추적 종목: {names}\n🕒 {kst()}")

def entry_msg(p,state):
    direction="🟢 LONG" if p["direction"]=="LONG" else "🔴 SHORT"
    stop_pct=abs(p["entry"]-p["sl"])/p["entry"]*100
    return (
        f"🚨 <b>GATE.IO FUTURES SIGNAL</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📌 계약: <b>{p['symbol']}</b> ({asset_name(p['asset'])})\n"
        f"📈 방향: <b>{direction}</b>\n"
        f"💵 모의 기준 진입가: <code>{fmt(p['entry'])}</code>\n"
        f"⚡ 추천 레버리지: <b>{p['leverage']}x</b> (ATR 참고값)\n"
        f"🛑 손절가: <code>{fmt(p['sl'])}</code> "
        f"({stop_pct:.2f}%)\n"
        f"🎯 TP1: <code>{fmt(p['tp1'])}</code> "
        f"({abs(p['tp1']-p['entry'])/p['entry']*100:.2f}% / {TP1_R:.1f}R)\n"
        f"🎯 TP2: <code>{fmt(p['tp2'])}</code> "
        f"({abs(p['tp2']-p['entry'])/p['entry']*100:.2f}% / {TP2_R:.1f}R)\n"
        f"🏁 TP3: <code>{fmt(p['tp3'])}</code> "
        f"({abs(p['tp3']-p['entry'])/p['entry']*100:.2f}% / {TP3_R:.1f}R)\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🧠 점수: <b>{p['score']}/6</b>\n"
        f"📊 RSI {p['rsi']:.1f} | ADX {p['adx']:.1f} | 거래량 {p['volume_ratio']:.2f}배\n"
        f"🔎 근거: {', '.join(p['reasons'])}\n"
        f"🪙 BTC 필터: {p['btc_note']}\n"
        f"🕒 기준봉: {kst(p['bar'])}\n"
        f"🕒 발송: {kst()}\n"
        f"🧩 슬롯: {len(state['positions'])}/{MAX_ACTIVE} 사용\n"
        f"⚠️ 레버리지는 변동성 기반 참고값이며 계좌 위험·청산 가격을 반영하지 않습니다.\n"
        f"⚠️ 모의 시그널이며 실제 주문·체결은 실행하지 않습니다.\n"
        f"<a href=\"{link(p['symbol'])}\">Gate.io 차트 열기</a>"
    )

def event_msg(p,title,price,detail=""):
    move=abs(price-p["entry"])/p["entry"]*100 if p["entry"] else 0
    return (
        f"📣 <b>GATE.IO SIGNAL UPDATE</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"📌 계약: <b>{p['symbol']}</b>\n"
        f"📈 방향: <b>{p['direction']}</b>\n"
        f"🔔 변경: <b>{title}</b>\n"
        f"💵 확인 가격: <code>{fmt(price)}</code>\n"
        f"📊 진입 대비 가격 변동: {move:.2f}%\n"
        f"🛑 현재 추적 손절선: <code>{fmt(p['sl'])}</code>\n"
        f"{detail}\n🕒 {kst()}\n"
        f"⚠️ 모의 추적 기준이며 실제 체결 손익과 다를 수 있습니다.\n"
        f"<a href=\"{link(p['symbol'])}\">Gate.io 차트 열기</a>"
    )

def stats_msg(state):
    s=state["stats"]
    total=sum(s.get(k,0) for k in ("tp3","tp2","tp1_be","sl","time"))
    wins=s.get("tp3",0)+s.get("tp2",0)
    loss=s.get("sl",0)
    rate=wins/(wins+loss)*100 if wins+loss else 0
    return (
        f"📊 <b>GATE.IO 종료 통계</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"총 종료 시그널: <code>{total}</code>\n"
        f"🏁 TP3 종료: <code>{s['tp3']}</code>\n"
        f"🎯 TP2 보호 종료: <code>{s['tp2']}</code>\n"
        f"🔄 TP1 이후 본전 종료: <code>{s['tp1_be']}</code>\n"
        f"🛑 초기 손절 종료: <code>{s['sl']}</code>\n"
        f"⏱ 시간 종료: <code>{s['time']}</code>\n"
        f"승률: <b>{rate:.1f}%</b> ({wins}승/{loss}패; 본전·시간종료 제외)\n"
        f"현재 활성 슬롯: <code>{len(state['positions'])}/{MAX_ACTIVE}</code>\n"
        f"※ 모의 추적 통계이며 실현 손익률이 아닙니다."
    )

def close_pos(state,sym,reason,price):
    p=state["positions"].pop(sym)
    state["stats"][reason]+=1
    state["cooldowns"][sym]=int(time.time())+COOLDOWN*60
    save(STATE,state)
    tg(event_msg(p,"시그널 종료: "+reason.upper(),price,
        f"종료 분류: <b>{reason.upper()}</b>"))
    log("exit",symbol=sym,reason=reason,price=price)
    tg(slot_msg(state,"슬롯 비움",f"종료 종목: <b>{sym}</b> · 사유: {reason.upper()}"))
    tg(stats_msg(state))

def monitor(state):
    for sym in list(state["positions"]):
        p=state["positions"].get(sym)
        if not p: continue
        try:
            c=get_candles(sym,"15m",5)
            if not c: continue
            x=c[-1]
            if x["t"]<=p.get("last_monitor_bar",0): continue
            p["last_monitor_bar"]=x["t"]
            long=p["direction"]=="LONG"
            hit_sl=x["l"]<=p["sl"] if long else x["h"]>=p["sl"]
            hit1=x["h"]>=p["tp1"] if long else x["l"]<=p["tp1"]
            hit2=x["h"]>=p["tp2"] if long else x["l"]<=p["tp2"]
            hit3=x["h"]>=p["tp3"] if long else x["l"]<=p["tp3"]
            # 같은 봉에서 손절과 목표가가 모두 닿으면 보수적으로 손절 우선.
            if hit_sl:
                close_pos(state,sym,"sl",p["sl"]);continue
            if hit3:
                close_pos(state,sym,"tp3",p["tp3"]);continue
            if hit2 and not p["tp2_hit"]:
                p["tp2_hit"]=True;p["sl"]=p["entry"]
                save(STATE,state)
                tg(event_msg(p,"TP2 도달 · 손절선을 진입가로 이동",p["tp2"],
                    f"🎯 TP2 도달 · 목표 수익 변동 {TP2_R:.1f}R\n"
                    f"🔒 추적 손절선을 진입가로 이동"))
                log("tp2",symbol=sym,price=p["tp2"])
            elif hit1 and not p["tp1_hit"]:
                p["tp1_hit"]=True;p["sl"]=p["entry"]
                save(STATE,state)
                tg(event_msg(p,"TP1 도달 · 손절선을 진입가로 이동",p["tp1"],
                    f"🎯 TP1 도달 · 목표 수익 변동 {TP1_R:.1f}R\n"
                    f"🔒 추적 손절선을 진입가로 이동"))
                log("tp1",symbol=sym,price=p["tp1"])
            if (p["tp1_hit"] or p["tp2_hit"]) and x["l"]<=p["entry"]<=x["h"]:
                close_pos(state,sym,"tp2" if p["tp2_hit"] else "tp1_be",p["entry"])
                continue
            created=datetime.fromisoformat(p["created"]).timestamp()
            if time.time()-created>36*3600:
                close_pos(state,sym,"time",x["c"]);continue
            state["positions"][sym]=p
        except Exception as e:
            print("Monitor error",sym,e)
            log("monitor_error",symbol=sym,error=str(e))
    save(STATE,state)

def main():
    print("GATE.IO SIGNAL BOT V3",now())
    default={"positions":{},"cooldowns":{},"last_bars":{},
        "stats":{"tp3":0,"tp2":0,"tp1_be":0,"sl":0,"time":0}}
    state=load(STATE,default)
    for k,v in default.items(): state.setdefault(k,v)
    for k,v in default["stats"].items(): state["stats"].setdefault(k,v)
    save(STATE,state)
    contracts,tm=market()
    btc=btc_info(tm)
    print("Scan:",len(contracts),"BTC:",btc)
    monitor(state)
    state=load(STATE,default)
    slots=max(0,MAX_ACTIVE-len(state["positions"]))
    if slots==0:
        print("All slots occupied")
        return
    results=[]
    for c in contracts:
        sym=c["name"]
        if sym in state["positions"]: continue
        if int(state["cooldowns"].get(sym,0))>int(time.time()): continue
        try:
            p=analyze(c,tm.get(sym,{}),btc)
            if p and int(state["last_bars"].get(sym,0))!=p["bar"]:
                results.append(p)
        except Exception as e:
            print("Analyze error",sym,e)
            log("analyze_error",symbol=sym,error=str(e))
    results.sort(key=lambda p:(p["score"],p["volume_ratio"],p["adx"]),reverse=True)
    for p in results[:slots]:
        state["positions"][p["symbol"]]=p
        state["last_bars"][p["symbol"]]=p["bar"]
        save(STATE,state)
        tg(entry_msg(p,state))
        log("entry",symbol=p["symbol"],direction=p["direction"],
            score=p["score"],leverage=p["leverage"],atr_pct=p["atr_pct"])
        tg(slot_msg(state,"신규 종목 등록",
            f"등록 종목: <b>{p['symbol']}</b> · {p['direction']}"))
        print("SIGNAL",p["symbol"],p["direction"],p["score"],p["leverage"])
    save(STATE,state)
    print("Qualified:",len(results),"Active:",len(state["positions"]))

if __name__=="__main__":
    main()
