import os,json,time,math,requests
from pathlib import Path
from datetime import datetime,timezone,timedelta

BASE="https://fx-api.gateio.ws/api/v4/futures/usdt"
STATE=Path("gate_state.json")
TOKEN=os.getenv("GATE_TELEGRAM_TOKEN") or os.getenv("TELEGRAM_TOKEN","")
CHAT=os.getenv("GATE_TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID","")
KST=timezone(timedelta(hours=9))

MAX_SLOTS=8
MIN_TURNOVER=3_000_000  # 알트코인 기회 포착을 위해 최소 거래대금을 300만 USDT로 유연하게 조정
MIN_SCORE=8
MIN_ADX=16
MAX_RISK_PCT=0.035
MAX_ENTRY_DISTANCE_ATR=1.8
MAX_ENTRY_SLIPPAGE_ATR=0.45
COOLDOWN=7200
SL_COOLDOWN=43200
MAX_HOLD=86400
TP_R=(1.0,1.8,2.8)
INTERVAL=900
S=requests.Session()
S.headers.update({"Accept":"application/json","User-Agent":"GateSignalBot/2.0"})

def now():
    return int(time.time())

def api(path,params=None):
    r=S.get(BASE+path,params=params,timeout=15)
    r.raise_for_status()
    return r.json()

def load_state():
    try:
        x=json.loads(STATE.read_text())
        if not isinstance(x,dict): raise ValueError()
    except Exception:
        x={}
    x.setdefault("active",[])
    x.setdefault("history",[])
    x.setdefault("cooldown",{})
    x.setdefault("last_signal",{})
    return x

def save_state(st):
    tmp=STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st,ensure_ascii=False,separators=(",",":")))
    tmp.replace(STATE)

def tg(msg):
    if not TOKEN or not CHAT:
        print("ERROR: Telegram secrets missing")
        return False
    try:
        r=requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id":CHAT,"text":msg,"parse_mode":"HTML",
                  "disable_web_page_preview":True},timeout=15)
        print("Telegram:",r.status_code,r.text[:250])
        return r.ok and r.json().get("ok",False)
    except Exception as e:
        print("Telegram error:",e)
        return False

def num(x,default=0.0):
    try:
        y=float(x)
        return y if math.isfinite(y) else default
    except Exception:
        return default

def candles(symbol,interval,limit=100):
    rows=api("/candlesticks",{
        "contract":symbol,"interval":interval,"limit":limit})
    sec={"15m":900,"1h":3600}[interval]
    out=[]
    for x in rows:
        t=int(x["t"])
        if t+sec>now():
            continue
        out.append({"t":t,"o":num(x.get("o")),"h":num(x.get("h")),
                    "l":num(x.get("l")),"c":num(x.get("c")),
                    "v":num(x.get("v")),"sum":num(x.get("sum"))})
    return sorted(out,key=lambda x:x["t"])

def ema(a,n):
    if len(a)<n: return []
    k=2/(n+1)
    e=sum(a[:n])/n
    out=[None]*(n-1)+[e]
    for x in a[n:]:
        e=x*k+e*(1-k)
        out.append(e)
    return out

def rsi(a,n=14):
    if len(a)<n+1: return 50
    d=[a[i]-a[i-1] for i in range(1,len(a))]
    g=sum(max(x,0) for x in d[:n])/n
    l=sum(max(-x,0) for x in d[:n])/n
    for x in d[n:]:
        g=(g*(n-1)+max(x,0))/n
        l=(l*(n-1)+max(-x,0))/n
    if l==0: return 100 if g>0 else 50
    return 100-100/(1+g/l)

def atr(cs,n=14):
    if len(cs)<n+1: return 0
    tr=[]
    for i in range(1,len(cs)):
        c,p=cs[i],cs[i-1]
        tr.append(max(c["h"]-c["l"],abs(c["h"]-p["c"]),
                      abs(c["l"]-p["c"])))
    v=sum(tr[:n])/n
    for x in tr[n:]:
        v=(v*(n-1)+x)/n
    return v

def adx(cs,n=14):
    if len(cs)<n*2+2: return 0
    trs=[]; plus=[]; minus=[]
    for i in range(1,len(cs)):
        c,p=cs[i],cs[i-1]
        up=c["h"]-p["h"]
        dn=p["l"]-c["l"]
        plus.append(up if up>dn and up>0 else 0)
        minus.append(dn if dn>up and dn>0 else 0)
        trs.append(max(c["h"]-c["l"],abs(c["h"]-p["c"]),
                       abs(c["l"]-p["c"])))
    tr=sum(trs[:n]); pd=sum(plus[:n]); md=sum(minus[:n])
    dx=[]
    for i in range(n,len(trs)):
        if i>n:
            tr=tr-tr/n+trs[i]
            pd=pd-pd/n+plus[i]
            md=md-md/n+minus[i]
        pdi=100*pd/tr if tr else 0
        mdi=100*md/tr if tr else 0
        dx.append(100*abs(pdi-mdi)/(pdi+mdi) if pdi+mdi else 0)
    if len(dx)<n: return sum(dx)/len(dx) if dx else 0
    val=sum(dx[:n])/n
    for x in dx[n:]:
        val=(val*(n-1)+x)/n
    return val

def tickers():
    data=api("/tickers")
    good=[]
    # 개수 제한 없이 거래대금 조건을 만족하는 모든 USDT 선물 마켓 (알트코인, 원자재, 지수/주식) 전수 스캔
    for x in data:
        sym=x.get("contract","")
        if not sym.endswith("_USDT"): continue
        if x.get("in_delisting") is True: continue
        
        vol=num(x.get("volume_24h_quote",x.get("volume_24h_usd",0)))
        price=num(x.get("last"))
        change=num(x.get("change_percentage"))
        if price<=0 or vol<MIN_TURNOVER: continue
        x["_vol"]=vol
        x["_chg"]=change
        good.append(x)
        
    # 거래대금순 정렬하여 반환 (개수 자르기 없이 전체 반환)
    good.sort(key=lambda x:x["_vol"], reverse=True)
    return good

def trend(cs):
    closes=[x["c"] for x in cs]
    e20,e50=ema(closes,20),ema(closes,50)
    if not e50 or e20[-1] is None: return 0
    if closes[-1]>e20[-1]>e50[-1]: return 1
    if closes[-1]<e20[-1]<e50[-1]: return -1
    return 0

def analyze(symbol,btc_trend):
    m=candles(symbol,"15m",100)
    h=candles(symbol,"1h",100)
    if len(m)<65 or len(h)<60: return None
    close=[x["c"] for x in m]
    e20,e50=ema(close,20),ema(close,50)
    if not e50 or e20[-1] is None: return None

    c=m[-1]; prev=m[-2]
    price = c["c"]
    a=atr(m)
    dx=adx(m)
    rr=rsi(close)
    if price<=0 or a<=0: return None

    if dx<MIN_ADX: return None

    t1=trend(h)
    t15=trend(m)
    if not t1 or t1!=t15: return None
    long=t1==1
    direction="LONG" if long else "SHORT"

    prior=m[-21:-1]
    if len(prior)<20: return None
    high=max(x["h"] for x in prior)
    low=min(x["l"] for x in prior)
    vr_base=sum(x["v"] for x in m[-11:-1])/10
    vr=c["v"]/vr_base if vr_base>0 else 0

    near=abs(c["c"]-e20[-1])<=0.85*a
    if long:
        pullback=near and prev["l"]<=e20[-2] and c["c"]>e20[-1] and c["c"]>c["o"]
        breakout=c["c"]>high and vr>=1.2
        rsi_ok=48<=rr<=68
        candle_ok=(c["c"]-c["l"])/max(c["h"]-c["l"],1e-12)>=0.65
        btc_ok=btc_trend>=0
    else:
        pullback=near and prev["h"]>=e20[-2] and c["c"]<e20[-1] and c["c"]<c["o"]
        breakout=c["c"]<low and vr>=1.2
        rsi_ok=32<=rr<=52
        candle_ok=(c["h"]-c["c"])/max(c["h"]-c["l"],1e-12)>=0.65
        btc_ok=btc_trend<=0

    if not (pullback or breakout): return None

    score=0
    reasons=[]
    score+=2; reasons.append("1H·15M 추세 일치")
    reasons.append("눌림 후 반등" if long else "저항 되돌림 후 하락")
    score+=2
    if rsi_ok: score+=1; reasons.append(f"RSI {rr:.1f}")
    if dx>=20: score+=1; reasons.append(f"ADX {dx:.1f}")
    if vr>=1.2: score+=1; reasons.append(f"거래량 {vr:.2f}배")
    if candle_ok: score+=1; reasons.append("캔들 종가 위치 양호")
    if btc_ok: score+=1; reasons.append("BTC 방향 필터 통과")
    if not rsi_ok or not btc_ok or score<MIN_SCORE: return None

    risk=max(a*1.4,price*0.008)
    if risk/price>MAX_RISK_PCT: return None
    sl=price-risk if long else price+risk
    targets=[price+(1 if long else -1)*risk*r for r in TP_R]
    
    risk_pct = risk / price
    rec_lev = max(2, min(20, round(0.15 / risk_pct)))

    return {"symbol":symbol,"side":direction,"entry":price,"sl":sl,
            "tp":targets,"risk":risk,"score":score,"rsi":rr,
            "adx":dx,"vr":vr,"reasons":reasons,"bar":c["t"],"leverage":rec_lev}

def fmt(x):
    return f"{x:.8g}"

def send_signal(sig,st):
    sym=sig["symbol"]
    side=sig["side"]
    entry=sig["entry"]
    tps=sig["tp"]
    sl=sig["sl"]
    
    if side == "LONG":
        sl_pct = ((sl - entry) / entry) * 100
    else:
        sl_pct = ((entry - sl) / entry) * 100

    tp_data = []
    for i, tp in enumerate(tps, 1):
        if side == "LONG":
            pct = ((tp - entry) / entry) * 100
        else:
            pct = ((entry - tp) / entry) * 100
        r_mult = TP_R[i-1]
        tp_data.append(f"🎯 TP{i}: <code>{fmt(tp)}</code> (+{pct:.2f}% / {r_mult}R)")

    bar_time_kst = datetime.fromtimestamp(sig['bar'], KST).strftime('%Y-%m-%d %H:%M:%S')
    send_time_kst = datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')

    msg=(
        "🚨 <b>GATE.IO FUTURES SIGNAL</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📌 계약: <b>{sym}</b>\n"
        f"📈 방향: <b>{side}</b>\n"
        f"💵 모의 기준 진입가: <code>{fmt(entry)}</code>\n"
        f"🛑 손절가: <code>{fmt(sl)}</code> ({sl_pct:.2f}%)\n"
        f"{tp_data[0]}\n"
        f"{tp_data[1]}\n"
        f"{tp_data[2]}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"⚡ 추천 레버리지: <b>{sig['leverage']}x</b> (계좌 위험 미반영 참고값)\n"
        f"🧠 점수: <b>{sig['score']}/9</b>\n"
        f"📊 RSI {sig['rsi']:.1f} | ADX {sig['adx']:.1f} | 거래량 {sig['vr']:.2f}배\n"
        "🔎 근거: "+", ".join(sig["reasons"])+"\n"
        f"🕒 신호 기준봉 시각: {bar_time_kst}\n"
        f"🕒 발송 시각: {send_time_kst}\n"
        "⚠️ 시그널 추적용 모의 기준이며 실제 주문은 실행하지 않습니다."
    )
    if not tg(msg): return False
    st["active"].append({
        "symbol":sym,"side":side,"entry":sig["entry"],"sl":sig["sl"],
        "tp":sig["tp"],"risk":sig["risk"],"score":sig["score"],
        "entry_time":now(),"entry_bar":sig["bar"],"last_bar":sig["bar"],
        "max_tp":0,"rsi":sig["rsi"],"adx":sig["adx"],"leverage":sig["leverage"]})
    st["last_signal"][sym]=now()
    save_state(st)
    return True

def close_position(st,p,outcome,price,bar):
    record={"symbol":p["symbol"],"side":p["side"],"outcome":outcome,
            "entry":p["entry"],"exit":price,"score":p["score"],
            "entry_time":p["entry_time"],"exit_time":now(),
            "entry_bar":p["entry_bar"],"exit_bar":bar}
    st["history"].append(record)
    st["history"]=st["history"][-1000:]
    delay=SL_COOLDOWN if outcome=="initial_sl" else COOLDOWN
    st["cooldown"][p["symbol"]]=now()+delay
    st["active"].remove(p)
    labels={"tp3":"TP3 목표 도달","tp2_end":"TP2 이후 본전 종료",
            "tp1_end":"TP1 이후 본전 종료","initial_sl":"초기 손절",
            "timeout":"시간 종료"}
    msg=(f"📊 <b>시그널 종료</b>\n{p['symbol']} {p['side']}\n"
         f"결과: <b>{labels[outcome]}</b>\n"
         f"진입: {fmt(p['entry'])} → 종료 기준: {fmt(price)}")
    tg(msg)
    save_state(st)

def monitor(st):
    closed_any = False
    for p in list(st["active"]):
        try:
            cs=candles(p["symbol"],"15m",100)
            for c in cs:
                if c["t"]<=p["last_bar"]: continue
                long=p["side"]=="LONG"
                
                stop_hit=(c["l"]<=p["sl"]) if long else (c["h"]>=p["sl"])
                
                hits=[]
                for i,tp in enumerate(p["tp"],1):
                    hit=(c["h"]>=tp) if long else (c["l"]<=tp)
                    if hit: hits.append(i)
                
                if stop_hit and not (3 in hits):
                    if p["max_tp"]>=2: outcome="tp2_end"
                    elif p["max_tp"]==1: outcome="tp1_end"
                    else: outcome="initial_sl"
                    close_position(st,p,outcome,p["sl"],c["t"])
                    closed_any = True
                    break

                if 3 in hits:
                    close_position(st,p,"tp3",p["tp"][2],c["t"])
                    closed_any = True
                    break
                if 2 in hits and p["max_tp"]<2:
                    p["max_tp"]=2
                    p["sl"]=p["entry"]
                    tg(f"📊 <b>포지션 추적 알림</b>\n{p['symbol']} {p['side']}\n🎯 <b>TP2 도달</b> — 남은 시그널 추적 중 (본전 스탑 전환)")
                    save_state(st)
                if 1 in hits and p["max_tp"]<1:
                    p["max_tp"]=1
                    p["sl"]=p["entry"]
                    tg(f"📊 <b>포지션 추적 알림</b>\n{p['symbol']} {p['side']}\n🎯 <b>TP1 도달</b> — 추세 관찰 중 (본전 스탑 전환)")
                    save_state(st)

                if stop_hit:
                    if p["max_tp"]>=2: outcome="tp2_end"
                    elif p["max_tp"]==1: outcome="tp1_end"
                    else: outcome="initial_sl"
                    close_position(st,p,outcome,p["sl"],c["t"])
                    closed_any = True
                    break

                p["last_bar"]=c["t"]
                if now()-p["entry_time"]>=MAX_HOLD:
                    close_position(st,p,"timeout",c["c"],c["t"])
                    closed_any = True
                    break
            else:
                if now()-p["entry_time"]>=MAX_HOLD and cs:
                    close_position(st,p,"timeout",cs[-1]["c"],cs[-1]["t"])
                    closed_any = True
                    continue
                save_state(st)
        except Exception as e:
            print("Monitor error",p.get("symbol"),repr(e))
    return closed_any

def get_status_msg(st):
    if not st["active"]:
        return "📊 <b>현재 활성 시그널 없음</b>\n모든 슬롯이 대기 중입니다."
    lines=["📊 <b>현재 활성 시그널</b>", "━━━━━━━━━━━━━━━━━━"]
    for i,p in enumerate(st["active"],1):
        hit=p.get("max_tp",0)
        status="TP2 도달" if hit==2 else "TP1 도달" if hit==1 else "추적 중"
        d="🟢 LONG" if p.get("side")=="LONG" else "🔴 SHORT"
        lev_str = f" | 레버리지: {p.get('leverage', 5)}x" if p.get('leverage') else ""
        
        cur_price = p["entry"]
        try:
            t_data = api("/tickers", {"contract": p["symbol"]})
            if isinstance(t_data, list) and len(t_data) > 0:
                cur_price = num(t_data[0].get("last", p["entry"]))
        except Exception:
            pass
            
        entry = p["entry"]
        if entry > 0:
            if p.get("side") == "LONG":
                pnl_pct = ((cur_price - entry) / entry) * 100
            else:
                pnl_pct = ((entry - cur_price) / entry) * 100
        else:
            pnl_pct = 0.0
            
        sign = "+" if pnl_pct >= 0 else ""
        pnl_str = f"{sign}{pnl_pct:.2f}%"
        
        lines.append(f"<code>{i}. {p['symbol']}</code> {d}{lev_str}\n진입: <code>{fmt(entry)}</code> | 현재: <code>{fmt(cur_price)}</code>\n상태: {status} | 수익률: <b>{pnl_str}</b>")
    
    current_query_time = datetime.now(KST).strftime('%m-%d %H:%M:%S')
    lines.append(f"🕒 현재가 조회 시각: {current_query_time} KST")
    return "\n".join(lines)

def report(st):
    h=st["history"]
    counts={k:sum(x.get("outcome")==k for x in h) for k in
            ("tp3","tp2_end","tp1_end","initial_sl","timeout")}
    total=len(h)
    
    wins=counts["tp3"]+counts["tp2_end"]
    draws=counts["tp1_end"]
    losses=counts["initial_sl"]+counts["timeout"]
    
    decisive_total = total - draws
    rate = 100 * wins / decisive_total if decisive_total else 0
    
    return (f"📊 <b>목표 도달 후 추적 종료 분류 통계</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"총 종료 시그널: {total}건\n\n"
            f"🎯 TP3 종료 (승): {counts['tp3']}건\n"
            f"🎯 TP2 이후 종료 (승): {counts['tp2_end']}건\n"
            f"🤝 1차 본전 종료 (무): {counts['tp1_end']}건\n"
            f"🛑 초기 손절 종료 (패): {counts['initial_sl']}건\n"
            f"⏱️ 시간 종료 (패): {counts['timeout']}건\n\n"
            f"🏆 승률 (승/패 기준): {rate:.1f}%\n"
            f"(승 {wins}건 / 패 {losses}건 | 무 {draws}건 제외)\n"
            f"현재 추적 중: {len(st['active'])}건\n"
            "※ 실현 계좌 수익률이 아닌 모의 시그널 목표 도달·종료 기록입니다.")

def main():
    if not TOKEN or not CHAT:
        raise RuntimeError("GATE_TELEGRAM_TOKEN 및 GATE_TELEGRAM_CHAT_ID Secrets를 확인하세요.")
    st=load_state()
    print("Gate.io signal bot start",datetime.now(KST).isoformat())
    
    position_closed = monitor(st)

    if len(st["active"])>=MAX_SLOTS:
        if position_closed:
            tg(get_status_msg(st))
            tg(report(st))
        save_state(st)
        return

    try:
        btc=candles("BTC_USDT","1h",80)
        btc_trend=trend(btc)
        if not btc_trend:
            print("BTC trend unclear; skip new entries")
            if position_closed:
                tg(get_status_msg(st))
                tg(report(st))
            save_state(st)
            return
            
        pool=tickers()
        print("Candidates (All Markets):",len(pool),"BTC trend:",btc_trend)
        results=[]
        for t in pool:
            sym=t["contract"]
            if any(p["symbol"]==sym for p in st["active"]): continue
            if int(st["cooldown"].get(sym,0))>now(): continue
            try:
                sig=analyze(sym,btc_trend)
                if sig: results.append(sig)
            except Exception as e:
                print("Analyze error",sym,repr(e))
            time.sleep(0.02)  # 전수 스캔 속도 최적화

        results.sort(key=lambda x:(x["score"],x["adx"],x["vr"]),reverse=True)
        
        new_sent = 0
        for sig in results:
            if len(st["active"])>=MAX_SLOTS: break
            if send_signal(sig,st):
                new_sent += 1
                
        if new_sent > 0 or position_closed:
            print("Sending status and stats update due to new signal or position closure.")
            tg(get_status_msg(st))
            tg(report(st))
            
        save_state(st)
        print("Qualified:",len(results),"Active:",len(st["active"]), "New Sent:", new_sent)
    except Exception as e:
        print("Scan error:",repr(e))
        if position_closed:
            tg(get_status_msg(st))
            tg(report(st))
        raise

if __name__=="__main__":
    main()
