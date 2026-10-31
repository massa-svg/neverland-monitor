# KyberSwap（Monad）の見積もりと取引データ作成が使えるかの確認だけ。取引は送らない
import json, os, requests
API = "https://aggregator-api.kyberswap.com/monad/api/v1"
H = {"X-Client-Id": "neverland-trader", "accept": "application/json", "User-Agent": "Mozilla/5.0"}
NATIVE = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"
USDC = "0x754704Bc059F8C67012fEd69BC8A327a5aafb603"
out = {}
r = requests.get(f"{API}/routes", params={"tokenIn": NATIVE, "tokenOut": USDC, "amountIn": str(1000 * 10 ** 18)}, headers=H, timeout=30)
out["routes_status"] = r.status_code
d = r.json()
out["routes"] = d
try:
    rs = d["data"]["routeSummary"]
    b = requests.post(f"{API}/route/build", json={"routeSummary": rs, "sender": "0x6d7579c965175634d85d5acd85a49907a551093f",
                      "recipient": "0x6d7579c965175634d85d5acd85a49907a551093f", "slippageTolerance": 50}, headers=H, timeout=30)
    out["build_status"] = b.status_code
    bd = b.json()
    if isinstance(bd.get("data"), dict) and "data" in bd["data"]:
        bd["data"]["data"] = bd["data"]["data"][:80] + "..."
    out["build"] = bd
except Exception as e:
    out["err"] = str(e)
os.makedirs("analysis/data/loop", exist_ok=True)
json.dump(out, open("analysis/data/loop/kyber_check.json", "w"), indent=1)
print(json.dumps(out)[:2000])
