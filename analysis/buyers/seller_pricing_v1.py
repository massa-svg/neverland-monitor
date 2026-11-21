import json, bisect, statistics as st
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
JST=timezone(timedelta(hours=9)); D="analysis/data"
L=json.load(open(f"{D}/listings.json")); ev=L["events"]; lk=L["locks_now"]
sales=json.load(open(f"{D}/sales.json")); pr=json.load(open(f"{D}/prices.json"))
def ser(n):
    o=sorted((c[0],c[4]) for c in pr[n]["ohlcv"]); return [x[0] for x in o],[x[1] for x in o]
dT,dP=ser("DUST"); mT,mP=ser("WMON")
def px(T,P,t): i=bisect.bisect_right(T,t)-1; return P[max(i,0)]
amt={}
for t,v in lk.items():
    if v.get("amt"): amt[int(t)]=v["amt"]
for s in sales:
    a=(s.get("lock_at_sale") or {}).get("amt")
    if a: amt[s["token_id"]]=a
    elif s["token_id"] not in amt: amt[s["token_id"]]=(s.get("now") or {}).get("lock",{}).get("amt")
salesby={s["token_id"]:s for s in sales}
def corr(x,y):
    mx,my=st.mean(x),st.mean(y); return sum((a-mx)*(b-my) for a,b in zip(x,y))/((sum((a-mx)**2 for a in x)*sum((b-my)**2 for b in y))**.5)
for M in ["0xf4db2e","0x39388c"]:
    E=sorted([e for e in ev if e["maker"].startswith(M) and e["currency"] in("MON","WMON") and amt.get(e["token_id"])],key=lambda e:e["ts"])
    by=defaultdict(list)
    for e in E:
        a=amt[e["token_id"]]; e["a"]=a; e["dm"]=px(dT,dP,e["ts"])/px(mT,mP,e["ts"])
        e["disc"]=(1-e["amount"]/(a*e["dm"]))*100; e["j"]=datetime.fromtimestamp(e["ts"],JST); by[e["token_id"]].append(e)
    F=[v[0] for v in by.values()]
    print("\n####",M,"トークン",len(F),"ロット",Counter(round(x["a"]) for x in F).most_common(5))
    print(" 初回割引 中央",round(st.median(x["disc"] for x in F),1)," 10/90%点",round(sorted(x["disc"] for x in F)[len(F)//10],1),round(sorted(x["disc"] for x in F)[len(F)*9//10],1))
    for lot in sorted({round(x["a"]) for x in F}):
        g=[x["disc"] for x in F if round(x["a"])==lot]
        if len(g)>=3: print("  ロット",lot,"件",len(g),"中央割引",round(st.median(g),1))
    # hour buckets
    hb=defaultdict(list)
    for x in F: hb[x["j"].hour//6*6].append(x["disc"])
    print(" 時間帯別 中央割引",{f"{h}-{h+6}時":(len(v),round(st.median(v),1)) for h,v in sorted(hb.items())})
    wb=defaultdict(list)
    for x in F: wb["月火水木金土日"[x["j"].weekday()]].append(x["disc"])
    print(" 曜日別",{k:(len(v),round(st.median(v),1)) for k,v in wb.items()})
    # price per DUST vs DUST market price
    per=[x["amount"]/x["a"] for x in F]; dm=[x["dm"] for x in F]
    print(" 1DUSTあたり出品価格(MON) 中央",round(st.median(per),3)," DUST相場(MON) 中央",round(st.median(dm),3)," 相関",round(corr(per,dm),2))
    # gap between listings
    gaps=[(b["ts"]-a["ts"])/3600 for a,b in zip(F,F[1:])]
    print(" 新規出品の間隔h 中央",round(st.median(gaps),1)," 1日あたり",round(len(F)/((F[-1]["ts"]-F[0]["ts"])/86400),1))
    # reactive: disc of next new listing vs whether previous sold fast
    fast=[];slow=[]
    for a,b in zip(F,F[1:]):
        s=salesby.get(a["token_id"])
        sold_before = s and s["seller"].startswith(M) and s["ts"]<=b["ts"]
        (fast if sold_before else slow).append(b["disc"]-a["disc"])
    print(" 直前の出品が次の出品前に売れていた→次の割引の変化 中央",round(st.median(fast),1) if fast else None,len(fast)," 売れ残り中→",round(st.median(slow),1) if slow else None,len(slow))
    # open listings count vs disc
    # relists
    R=[(v[i]["amount"]/v[i-1]["amount"]-1)*100 for v in by.values() for i in range(1,len(v))]
    G=[(v[i]["ts"]-v[i-1]["ts"])/3600 for v in by.values() for i in range(1,len(v))]
    print(" 出し直し",len(R),"回 値下げ幅 中央",round(st.median(R),1),"% 間隔中央",round(st.median(G),1),"h  出し直しありのトークン",sum(1 for v in by.values() if len(v)>1),"/",len(by))
    # sold rate and time
    ss=[salesby[t] for t in by if t in salesby and salesby[t]["seller"].startswith(M)]
    print(" 売れた",len(ss),"/",len(by)," 初回出品から売却まで中央",round(st.median((salesby[t]["ts"]-by[t][0]["ts"])/3600 for t in by if t in salesby and salesby[t]["seller"].startswith(M)),1),"h")
    # price levels quantization
    print(" 出品額の頻出(MON)",Counter(round(e["amount"]) for e in E).most_common(8))
    dd=defaultdict(list)
    for x in F: dd[x["j"].strftime("%m/%d")].append(x)
    for d,v in sorted(dd.items())[-16:]:
        print("  ",d,"件",len(v),"ロット",dict(Counter(round(x['a']) for x in v)),"割引中央",round(st.median(x['disc'] for x in v),1),"出品額中央",round(st.median(x['amount'] for x in v)),"DUST相場",round(st.median(x['dm'] for x in v),3))
