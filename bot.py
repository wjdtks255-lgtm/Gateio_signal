import os,json,time,math,requests
from pathlib import Path
from datetime import datetime,timezone,timedelta

BASE="https://fx-api.gateio.ws/api/v4/futures/usdt"
STATE=Path("gate_state.json")
TOKEN=os.getenv("GATE_TELEGRAM_TOKEN") or os.getenv("TELEGRAM_TOKEN","")
CHAT=os.getenv("GATE_TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID","")
KST=timezone(timedelta(hours=9))

MAX_SLOTS=8
MAX_SCAN=70
MIN_TURNOVER=5_000_000
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
    vr={x["contract"]:i for i,x in enumerate(
        sorted(good,key=lambda z:z["_vol"],reverse=True))}
    cr={x["contract"]:i for i,x in enumerate(
        sorted(good,
