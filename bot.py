import os,json,time,math,requests
from datetime import datetime,timedelta,timezone

TOKEN=os.getenv("GATE_TELEGRAM_TOKEN","").strip()
CHAT=(os.getenv("GATE_TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID") or "").strip()
STATE="gate_state.json"
KST=timezone(timedelta(hours=9))
BASE="https://fx-api.gateio.ws/api/v4/futures/usdt"
MAX_SLOTS=8
MAX_SCAN=70
MIN_TURNOVER=50000
COOLDOWN=120*60
SL_COOLDOWN=12*3600
MAX_HOLD=24*3600
MIN_SCORE=6
S=requests.Session()
S.headers.update({"Accept":"application/json"})

def now():
    return datetime.now(KST)

def api(path,params=None,timeout=12):
    r=S.get(BASE+path,params=params,timeout=timeout)
    r.raise_for_status()
    return r.json()

def tg(msg):
    if not TOKEN or not CHAT:
        print("TELEGRAM CONFIG ERROR")
        return False
    try:
        r=requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id":CHAT,"text":msg,"parse_mode":"Markdown",
                  "disable_web_page_preview":True},timeout=15)
        if r.ok and r.json().get("ok"):
            return True
        print("TG ERROR:",r.text[:300])
    except Exception as e:
        print("TG ERROR:",e)
    return False

def load():
    try:
        with open(STATE,encoding="utf-8") as f:
            s=json.load(f)
    except Exception:
        s={}
    for k,v in {
        "positions":{},"signals":{},"sl_penalties":{},
        "stats":{"wins":0,"breakeven":0,"losses":0,"timeouts":0}
    }.items():
        s.setdefault(k,v)
    for k in ("wins","breakeven","losses","timeouts"):
        s["stats"].setdefault(k,0)
    return s

def save(s):
    with open(STATE+".tmp","w",encoding="utf-8") as f:
        json.dump(s,f,ensure_ascii=False,separators=(",",":"))
    os.replace(STATE+".tmp",STATE)

def ema(a,n):
    if len(a)<n:return None
    v=sum(a[:n])/n
    for x in a[n:]:
        v+=(2/(n+1))*(x-v)
    return v

def rsi(a,n=14):
    if len(a)<n+1:return 50
    d=[a[i]-a[i-1] for i in range(1,len(a))]
    g=sum(max(x,0) for x in d[:n])/n
    l=sum(max(-x,0) for x in d[:n])/n
    for x in d[n:]:
        g=(g*(n-1)+max(x,0))/n
        l=(l*(n-1)+max(-x,0))/n
    return 100 if l==0 else 100-100/(1+g/l)

def candles(contract,interval,limit=70):
    try:
        d=api("/candlesticks",{
            "contract":contract,"interval":interval,"limit":limit
        })
        out=[]
        for x in d:
            t=int(x.get("t",0))
            c=float(x.get("c",x.get("close",0)))
            h=float(x.get("h",x.get("high",0)))
            l=float(x.get("l",x.get("low",0)))
            v=float(x.get("sum",x.get("v",0)))
            if t>0 and c>0 and h>0 and l>0:
                out.append((t,h,l,c,v))
        out.sort(key=lambda z:z[0])
        # Use completed candles only.
        sec={"15m":900,"1h":3600}.get(interval,900)
        cutoff=int(time.time())-sec
        return [x for x in out if x[0]+sec<=cutoff]
    except Exception as e:
        print("CANDLE ERROR",contract,interval,str(e)[:100])
        return []

def atr(data,n=14):
    if len(data)<n+1:return 0
    tr=[]
    for i in range(1,len(data)):
        _,h,l,c,_=data[i]
        prev=data[i-1][3]
        tr.append(max(h-l,abs(h-prev),abs(l-prev)))
    return sum(tr[-n:])/n

def turnover(x):
    for k in ("volume_24h_usd","volume_24h_quote","volume_24h_usdt"):
        try:
            v=float(x.get(k,0) or 0)
            if v>0:return v
        except Exception:
            pass
    # Do not treat contract count as confirmed USD turnover.
    return 0.0

def get_tickers():
    d=api("/tickers")
    return d if isinstance(d,list) else d.get("data",[])

def btc_direction():
    d=candles("BTC_USDT","1h",70)
    if len(d)<52:return "NEUTRAL"
    c=[x[3] for x in d]
    e20,e50=ema(c,20),ema(c,50)
    if not e20 or not e50:return "NEUTRAL"
    if e20>e50 and c[-1]>e20:return "BULL"
    if e20<e50 and c[-1]<e20:return "BEAR"
    return "NEUTRAL"

def choose_pool(tickers):
    valid=[]
    for x in tickers:
        c=x.get("contract","")
        if not c.endswith("_USDT") or c=="BTC_USDT":continue
        if x.get("in_delisting",False):continue
        try:
            p=float(x.get("last",0))
            ch=float(x.get("change_percentage",0) or 0)
        except Exception:
            continue
        if p<=0:continue
        tv=turnover(x)
        if tv and tv<MIN_TURNOVER:continue
        valid.append({"contract":c,"price":p,"change":ch,"turnover":tv})
    # Spread deep scans across liquid, moving and less-mainstream contracts.
    liquid=sorted(valid,key=lambda x:x["turnover"],reverse=True)[:25]
    movers=sorted(valid,key=lambda x:abs(x["change"]),reverse=True)[:30]
    middle=sorted(valid,key=lambda x:(
        abs(x["change"])*math.log10(max(x["turnover"],1))
    ),reverse=True)[:40]
    pool={x["contract"]:x for x in liquid+movers+middle}
    return list(pool.values())[:MAX_SCAN]

def analyze(x,btc):
    contract=x["contract"]
    m15=candles(contract,"15m",70)
    h1=candles(contract,"1h",65)
    if len(m15)<55 or len(h1)<52:return None
    c=[z[3] for z in m15]
    h=[z[1] for z in m15]
    l=[z[2] for z in m15]
    v=[z[4] for z in m15]
    c1=[z[3] for z in h1]
    e20,e50=ema(c,20),ema(c,50)
    e1,e2=ema(c1,20),ema(c1,50)
    a=atr(m15)
    if not e20 or not e50 or not e1 or not e2 or a<=0:return None
    p=c[-1]
    rr=rsi(c)
    avg=sum(v[-11:-1])/10 if len(v)>=11 else 0
    vr=v[-1]/avg if avg>0 else 0
    if p<=0 or a/p>0.08:return None

    up1=e1>e2 and c1[-1]>e1
    dn1=e1<e2 and c1[-1]<e1
    up15=e20>e50
    dn15=e20<e50
    # Recent range excluding current completed candle.
    hi=max(h[-21:-1])
    lo=min(l[-21:-1])
    breakout_up=p>hi
    breakout_dn=p<lo
    pullback_long=up15 and p>=e20 and c[-2]<=e20
    pullback_short=dn15 and p<=e20 and c[-2]>=e20

    best=None
    for direction in ("LONG","SHORT"):
        score=0
        reasons=[]
        if direction=="LONG":
            if up1:score+=2;reasons.append("1시간 상승 추세")
            if up15:score+=2;reasons.append("15분 EMA 정배열")
            if breakout_up:score+=2;reasons.append("최근 고점 돌파")
            elif pullback_long:score+=2;reasons.append("상승 추세 눌림 확인")
            if 48<=rr<=68:score+=1;reasons.append(f"RSI {rr:.0f}")
            if vr>=1.3:score+=1;reasons.append(f"거래량 {vr:.1f}배")
            if btc!="BEAR":score+=1;reasons.append("BTC 환경 통과")
            if rr>75:score-=2
        else:
            if dn1:score+=2;reasons.append("1시간 하락 추세")
            if dn15:score+=2;reasons.append("15분 EMA 역배열")
            if breakout_dn:score+=2;reasons.append("최근 저점 이탈")
            elif pullback_short:score+=2;reasons.append("하락 추세 반등 실패")
            if 32<=rr<=52:score+=1;reasons.append(f"RSI {rr:.0f}")
            if vr>=1.3:score+=1;reasons.append(f"거래량 {vr:.1f}배")
            if btc!="BULL":score+=1;reasons.append("BTC 환경 통과")
            if rr<25:score-=2
        # Require a real directional structure, not just volume/funding.
        aligned=(up1 and up15) if direction=="LONG" else (dn1 and dn15)
        structure=(breakout_up or pullback_long) if direction=="LONG" else (breakout_dn or pullback_short)
        if not aligned or not structure or score<MIN_SCORE:continue
        # ATR stop: 1.5 ATR, with a minimum distance of 0.8%.
        risk=max(a*1.5,p*0.008)
        if risk/p>0.035:continue
        # Penalize entries too far from the 15m trend EMA.
        if abs(p-e20)>a*2.0:continue
        rank=score+min(vr,3)*0.4
        candidate={
            "symbol":contract[:-5],"contract":contract,
            "direction":direction,"price":p,"risk":risk,
            "sl":p-risk,
            "tp1":p+risk,"tp2":p+risk*1.8,"tp3":p+risk*2.8,
            "score":score,"reasons":reasons,"rsi":rr,"atr":a,
            "turnover":x["turnover"],"rank":rank
        }
        if direction=="SHORT":
            candidate.update(sl=p+risk,tp1=p-risk,tp2=p-risk*1.8,tp3=p-risk*2.8)
        if best is None or candidate["rank"]>best["rank"]:
            best=candidate
    return best

def fmt(x):
    if x>=1000:return f"{x:,.2f}"
    if x>=1:return f"{x:.4f}"
    if x>=0.01:return f"{x:.6f}"
    return f"{x:.8f}".rstrip("0").rstrip(".")

def signal_msg(s,slot):
    d="🟢 LONG" if s["direction"]=="LONG" else "🔴 SHORT"
    return (
        "🚨 *GATE.IO FUTURES SIGNAL*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📍 슬롯: `{slot}/{MAX_SLOTS}`\n"
        f"🪙 종목: `{s['symbol']}_USDT`\n"
        f"📈 방향: *{d}*\n"
        f"⭐ 신호 점수: `{s['score']}/9`\n"
        f"💰 기준 진입가: `{fmt(s['price'])}` USDT\n"
        f"🛑 손절가: `{fmt(s['sl'])}`\n"
        f"🎯 TP1: `{fmt(s['tp1'])}`\n"
        f"🎯 TP2: `{fmt(s['tp2'])}`\n"
        f"🎯 TP3: `{fmt(s['tp3'])}`\n"
        f"📊 RSI: `{s['rsi']:.1f}` | ATR: `{fmt(s['atr'])}`\n"
        f"🔎 선정 근거: {', '.join(s['reasons'])}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "⚠️ 알림 기준 가격이며 실제 체결가는 다를 수 있습니다.\n"
        "레버리지와 포지션 크기는 손실 한도에 맞춰 직접 결정하세요.\n"
        f"⏱️ `{now():%Y-%m-%d %H:%M:%S} KST`"
    )

def result_msg(p,kind):
    name=p.get("symbol","?")
    d=p.get("direction","?")
    text={
        "TP1":"1차 목표 도달. 본전 스탑 단계로 전환",
        "TP2":"2차 목표 도달",
        "TP3":"최종 목표 도달",
        "SL":"초기 손절 도달",
        "BE":"본전 스탑 도달",
        "TIME":"보유 시간 제한 종료"
    }.get(kind,kind)
    return f"📊 *포지션 추적 알림*\n`{name}_USDT` | `{d}`\n{text}"

def monitor(s):
    changed=False
    stats=s["stats"]
    for key,p in list(s["positions"].items()):
        if not isinstance(p,dict):
            s["positions"].pop(key,None);changed=True;continue
        if time.time()-p.get("entry_time",time.time())>MAX_HOLD:
            tg(result_msg(p,"TIME"))
            stats["timeouts"]+=1
            s["positions"].pop(key,None);changed=True
            continue
        data=candles(key+"_USDT","15m",5)
        if not data:continue
        last=data[-1]
        # Process each closed candle once.
        if last[0]<=p.get("last_bar",0):continue
        # Only candles after the entry candle are safe to evaluate.
        bars=[z for z in data if z[0]>p.get("entry_bar",0)
              and z[0]>p.get("last_bar",0)]
        if not bars:
            p["last_bar"]=last[0];changed=True;continue
        for bar in bars:
            t,high,low,close,_=bar
            long=p["direction"]=="LONG"
            sl=p["sl"]
            # Conservative assumption if SL and target both touched in one bar.
            stop_hit=low<=sl if long else high>=sl
            if stop_hit:
                kind="BE" if p.get("tp1_hit") else "SL"
                tg(result_msg(p,kind))
                stats["breakeven" if kind=="BE" else "losses"]+=1
                if kind=="SL":s["sl_penalties"][key]=time.time()
                s["positions"].pop(key,None);changed=True
                break
            tp1=high>=p["tp1"] if long else low<=p["tp1"]
            tp2=high>=p["tp2"] if long else low<=p["tp2"]
            tp3=high>=p["tp3"] if long else low<=p["tp3"]
            if tp1 and not p.get("tp1_hit"):
                p["tp1_hit"]=True
                p["sl"]=p["entry"]
                tg(result_msg(p,"TP1"));changed=True
            if tp2 and "TP2" not in p.get("hit",[]):
                p.setdefault("hit",[]).append("TP2")
                tg(result_msg(p,"TP2"));changed=True
            if tp3:
                tg(result_msg(p,"TP3"))
                stats["wins"]+=1
                s["positions"].pop(key,None);changed=True
                break
            p["last_bar"]=t
        else:
            p["last_bar"]=bars[-1][0]
            changed=True
    return changed

def main():
    print("GATE.IO LONG/SHORT SCANNER")
    if not TOKEN or not CHAT:
        print("Missing GATE_TELEGRAM_TOKEN or GATE_TELEGRAM_CHAT_ID")
        return
    s=load()
    changed=monitor(s)
    save(s)
    try:
        tickers=get_tickers()
    except Exception as e:
        print("TICKER ERROR:",e)
        return
    pool=choose_pool(tickers)
    print("TICKERS:",len(tickers),"POOL:",len(pool))
    btc=btc_direction()
    print("BTC:",btc)
    candidates=[]
    for x in pool:
        try:
            c=analyze(x,btc)
            if c:candidates.append(c)
        except Exception as e:
            print("ANALYSIS ERROR",x["contract"],str(e)[:100])
        time.sleep(0.05)
    candidates.sort(key=lambda x:x["rank"],reverse=True)
    print("QUALIFIED:",len(candidates))
    sent=0
    available=max(0,MAX_SLOTS-len(s["positions"]))
    t=time.time()
    for c in candidates:
        if sent>=available:break
        key=c["symbol"]
        if key in s["positions"]:continue
        penalty=float(s["sl_penalties"].get(key,0) or 0)
        if t-penalty<SL_COOLDOWN:continue
        sid=key+":"+c["direction"]
        if t-float(s["signals"].get(sid,0) or 0)<COOLDOWN:continue
        entrybar=int(time.time()//900*900)
        if not tg(signal_msg(c,len(s["positions"])+1)):continue
        s["signals"][sid]=t
        s["positions"][key]={
            "symbol":key,"direction":c["direction"],
            "entry":c["price"],"sl":c["sl"],
            "tp1":c["tp1"],"tp2":c["tp2"],"tp3":c["tp3"],
            "tp1_hit":False,"hit":[],"entry_time":t,
            "entry_bar":entrybar,"last_bar":entrybar
        }
        save(s)
        sent+=1
        print("SIGNAL:",key,c["direction"],c["score"])
    if changed or sent:
        total=sum(s["stats"].get(k,0) for k in ("wins","breakeven","losses","timeouts"))
        st=s["stats"]
        tg(
            "📊 *누적 시뮬레이션 통계*\n"
            f"종료: `{total}` | TP3: `{st['wins']}` | "
            f"본전: `{st['breakeven']}` | 손절: `{st['losses']}` | "
            f"시간종료: `{st['timeouts']}`\n"
            "※ 실제 체결·부분 익절·수수료를 반영한 계좌 손익이 아닙니다."
        )
    save(s)
    print("DONE. SENT:",sent)

if __name__=="__main__":
    main()
