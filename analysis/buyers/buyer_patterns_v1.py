import json, sys, bisect, statistics as st, time
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
D = sys.argv[1]
JST = timezone(timedelta(hours=9))
WD = "月火水木金土日"
sales = json.load(open(f"{D}/sales.json"))
buyers = json.load(open(f"{D}/buyers.json"))
prices = json.load(open(f"{D}/prices.json"))
lst = json.load(open(f"{D}/listings.json"))["events"]
def series(n):
    o = sorted((c[0], c[4]) for c in prices[n]["ohlcv"]); return [x[0] for x in o], [x[1] for x in o]
dT, dP = series("DUST"); mT, mP = series("WMON")
def px(T, P, ts):
    i = bisect.bisect_right(T, ts) - 1; return P[max(i, 0)] if T and ts >= T[0] - 4*3600 else None
def status(s):
    L = s.get("lock_at_sale") or {}; pw = s.get("power_at_sale")
    if "amt" not in L or pw is None:
        L = (s.get("now") or {}).get("lock") or {}; pw = (s.get("now") or {}).get("power")
    w = [int(x) for x in L.get("words", [])]
    amt = L.get("amt")
    fut = [x for x in w if s["ts"] + 3600 < x < 4_000_000_000]
    if fut: return amt, "期限付", (max(fut) - s["ts"]) / 86400
    if amt and pw is not None and pw >= amt * 0.9999 and (not w or w[-1] != 0): return amt, "無期限", None
    if pw == 0: return amt, "切れ", None
    return amt, "不明", None
for s in sales:
    amt, stt, days = status(s); s["dust"], s["st"], s["days"] = amt, stt, days
    d, m = px(dT, dP, s["ts"]), px(mT, mP, s["ts"])
    s["disc"] = (1 - s["amount"] * m / (amt * d)) * 100 if (d and m and amt and s["currency"] in ("MON", "WMON")) else None
    s["jst"] = datetime.fromtimestamp(s["ts"], JST)
sales.sort(key=lambda s: s["ts"])
# listing -> sale delay
lby = defaultdict(list)
for e in lst: lby[e["token_id"]].append(e)
for s in sales:
    prev = [e for e in lby.get(s["token_id"], []) if e["ts"] <= s["ts"] and e["maker"] == s["seller"]]
    s["wait_h"] = (s["ts"] - max(e["ts"] for e in prev)) / 3600 if prev else None
now = max(s["ts"] for s in sales)
print("期間", sales[0]["jst"].strftime("%m/%d"), "〜", sales[-1]["jst"].strftime("%m/%d %H:%M"), "成約", len(sales))
cnt = Counter(s["buyer"] for s in sales)
recent_cut = now - 14*86400
cntr = Counter(s["buyer"] for s in sales if s["ts"] >= recent_cut)
print("直近14日 上位", [(b[:8], n) for b, n in cntr.most_common(10)])
def q(v, p):
    v = sorted(v); return v[min(len(v)-1, int(p*(len(v)-1)+0.5))] if v else None
def f(x, fmt="{:.1f}"): return "-" if x is None else fmt.format(x)
targets = [b for b, _ in cntr.most_common(8)] + [b for b, _ in cnt.most_common(6)]
seen=set()
for b in targets:
    if b in seen: continue
    seen.add(b)
    S = [s for s in sales if s["buyer"] == b]; R = [s for s in S if s["ts"] >= recent_cut]
    print(f"\n=== {b}  70日 {len(S)}件 / 直近14日 {len(R)}件  初回 {S[0]['jst']:%m/%d} 最終 {S[-1]['jst']:%m/%d %H:%M}")
    ds = [s["disc"] for s in S if s["disc"] is not None]
    print(" 割引: 最浅", f(min(ds) if ds else None), "10%点", f(q(ds,.1)), "中央", f(q(ds,.5)), "90%点", f(q(ds,.9)), " 割引<=17%:", sum(1 for x in ds if x<=17), " 17-25:", sum(1 for x in ds if 17<x<=25), " 25-30:", sum(1 for x in ds if 25<x<=30), " >30:", sum(1 for x in ds if x>30), " 不明", len(S)-len(ds))
    dsr = [s["disc"] for s in R if s["disc"] is not None]
    if dsr: print(" 直近14日 割引: 最浅", f(min(dsr)), "中央", f(q(dsr,.5)), "<=17%:", sum(1 for x in dsr if x<=17))
    print(" ロック種類", dict(Counter(s["st"] for s in S)), " 期限付の残日数", [round(s["days"]) for s in S if s["days"]][:12])
    print(" DUST量", dict(Counter(int(round((s["dust"] or 0)/500)*500) for s in S).most_common(6)), " MON中央", f(q([s["amount"] for s in S],.5),"{:,.0f}"), " 総MON", f"{sum(s['amount'] for s in S):,.0f}")
    print(" 曜日", " ".join(f"{WD[i]}{n}" for i, n in sorted(Counter(s["jst"].weekday() for s in S).items())))
    hb = Counter(s["jst"].hour//3*3 for s in S)
    print(" 時刻(JST 3h)", " ".join(f"{h:02d}-{h+3:02d}時:{hb[h]}" for h in range(0,24,3)))
    print(" 売り手", [(x[:8], n) for x, n in Counter(s["seller"] for s in S).most_common(4)])
    w = [s["wait_h"] for s in S if s["wait_h"] is not None]
    if w: print(" 出品から購入まで 中央", f(q(w,.5)), "h  25%点", f(q(w,.25)), "h  件", len(w))
    # sessions
    sess=[]; 
    for s in S:
        if sess and s["ts"]-sess[-1][-1]["ts"]<=3600: sess[-1].append(s)
        else: sess.append([s])
    print(" まとめ買い回数", len(sess), " 1回の件数", dict(Counter(len(x) for x in sess)))
    # resale
    info = buyers.get(b, {})
    ev = info.get("events", [])
    sold = [e for e in ev if e["type"]=="sale" and str(e.get("from","")).lower()==b and str(e.get("contract","")).lower().startswith("0xbb4738")]
    tout = [e for e in ev if e["type"]=="transfer" and str(e.get("from","")).lower()==b and str(e.get("contract","")).lower().startswith("0xbb4738")]
    tdest = Counter(str(e.get("to",""))[:8] for e in tout)
    orders = [e for e in ev if e["type"]=="order"]
    print(" 売却", len(sold), " 譲渡(出)", len(tout), dict(tdest.most_common(4)), " オファー/出品イベント", len(orders), " 現保有", len(info.get("holds",[])) if "holds" in info else info.get("holds_err","?")[:40], " MON残", f(info.get("mon_balance"),"{:,.0f}"), " nonce", info.get("nonce"), " code", info.get("code_len"))
# who bought shallow (<=18%) in 70d and 14d
print("\n=== 割引18%以下の成約（無期限）")
sh = [s for s in sales if s["disc"] is not None and s["disc"] <= 18 and s["st"]=="無期限"]
print(" 70日", len(sh), Counter(s["buyer"][:8] for s in sh).most_common(8))
shr=[s for s in sh if s["ts"]>=recent_cut]
print(" 14日", len(shr))
for s in shr: print("  ", s["jst"].strftime("%m/%d(%a) %H:%M"), s["buyer"][:8], f(s["disc"]), s["seller"][:8], f(s["wait_h"]))
print("\n=== 週別 割引帯別成約(無期限)")
wk=defaultdict(Counter)
for s in sales:
    if s["disc"] is None or s["st"]!="無期限": continue
    k=(s["jst"]-timedelta(days=s["jst"].weekday())).strftime("%m/%d")
    b="<=15" if s["disc"]<=15 else "15-20" if s["disc"]<=20 else "20-25" if s["disc"]<=25 else "25-30" if s["disc"]<=30 else ">30"
    wk[k][b]+=1
for k in sorted(wk): print(" ",k, dict(wk[k]))
print("\n=== 直近の出品で15-20%引きのもの：売れたか")
