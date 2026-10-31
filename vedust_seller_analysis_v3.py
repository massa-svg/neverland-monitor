# veDUST 大口売り手 v3: Neverland への預け入れ額と、ロックを配った金庫(マルチシグ)の持ち主。データ集め専用
import json, os, time
from datetime import datetime, timezone
import requests
from web3 import Web3

SELLER = "0xf4db2e9d49817ee4d1b89c214a0dd76b603f9c33"
SAFE   = "0xb83a6637c87e6a7192b3ada845c0745f815e9006"
SAFE_EXECUTOR = "0x4aa43a4870432b2bbc30530fed9e62607deaa881"
COMPARE = ["0x4bd4449ab218c06989fb6ac7830e1d551436b429"]       # 比較用（常連の買い手）
AAVE_ORACLE = "0x94bbA11004B9877d13bb5E1aE29319b6f7bDEdD4"
OS_API = "https://api.opensea.io/api/v2"
OUT = os.path.join("analysis", "data", "seller")
RPC_URLS = [u for u in [os.environ.get("MONAD_RPC_URL", "").strip(),
                        "https://rpc.monad.xyz", "https://rpc1.monad.xyz", "https://monad.drpc.org"] if u]
os.makedirs(OUT, exist_ok=True)
w3 = None
for u in RPC_URLS:
    try:
        w3 = Web3(Web3.HTTPProvider(u, request_kwargs={"timeout": 40}))
        w3.eth.chain_id
        break
    except Exception:
        w3 = None


def call(to, sig, types=(), vals=()):
    from eth_abi import encode
    data = bytes(Web3.keccak(text=sig))[:4] + (encode(list(types), list(vals)) if types else b"")
    return bytes(w3.eth.call({"to": Web3.to_checksum_address(to), "data": "0x" + data.hex()}))


def words(raw):
    return [int.from_bytes(raw[i:i + 32], "big") for i in range(0, len(raw), 32)]


def addr(w):
    return "0x" + w.to_bytes(32, "big")[-20:].hex()


def sym(a):
    try:
        from eth_abi import decode
        return decode(["string"], call(a, "symbol()"))[0]
    except Exception:
        return ""


def dec(a):
    try:
        return words(call(a, "decimals()"))[0]
    except Exception:
        return 18


res = {"run_utc": datetime.now(timezone.utc).isoformat()}
# 1. 預け入れ・借り入れ
try:
    provider = addr(words(call(AAVE_ORACLE, "ADDRESSES_PROVIDER()"))[0])
    pool = addr(words(call(provider, "getPool()"))[0])
    raw = call(pool, "getReservesList()")
    w = words(raw)
    n = w[1]
    assets = [addr(x) for x in w[2:2 + n]]
    res["pool"] = pool
    reserves = []
    who = [SELLER] + COMPARE
    for a in assets:
        rd = words(call(pool, "getReserveData(address)", ["address"], [Web3.to_checksum_address(a)]))
        atoken, vdebt = addr(rd[8]), addr(rd[10])
        d = dec(a)
        px = words(call(AAVE_ORACLE, "getAssetPrice(address)", ["address"], [Web3.to_checksum_address(a)]))[0] / 1e8
        r = {"asset": a, "symbol": sym(a), "price_usd": px, "aToken": atoken, "vDebt": vdebt,
             "total_supply": words(call(atoken, "totalSupply()"))[0] / 10 ** d,
             "total_debt": words(call(vdebt, "totalSupply()"))[0] / 10 ** d}
        for x in who:
            r[f"supply_{x[:8]}"] = words(call(atoken, "balanceOf(address)", ["address"], [Web3.to_checksum_address(x)]))[0] / 10 ** d
            r[f"debt_{x[:8]}"] = words(call(vdebt, "balanceOf(address)", ["address"], [Web3.to_checksum_address(x)]))[0] / 10 ** d
        reserves.append(r)
        print("reserve", r["symbol"], flush=True)
    res["reserves"] = reserves
    for x in who:
        try:
            ud = words(call(pool, "getUserAccountData(address)", ["address"], [Web3.to_checksum_address(x)]))
            res[f"account_{x[:8]}"] = {"collateral_usd": ud[0] / 1e8, "debt_usd": ud[1] / 1e8, "health": ud[5] / 1e18}
        except Exception as e:
            res[f"account_{x[:8]}"] = {"err": str(e)[:150]}
except Exception as e:
    res["pool_err"] = str(e)[:300]

# 2. 金庫（マルチシグ）
safe = {}
for name, sig in (("owners", "getOwners()"), ("threshold", "getThreshold()"), ("nonce", "nonce()")):
    try:
        raw = call(SAFE, sig)
        w = words(raw)
        safe[name] = [addr(x) for x in w[2:2 + w[1]]] if name == "owners" else w[0]
    except Exception as e:
        safe[name] = "ERR " + str(e)[:100]
safe["executor_nonce"] = w3.eth.get_transaction_count(Web3.to_checksum_address(SAFE_EXECUTOR))
safe["seller_is_owner"] = SELLER in [o.lower() for o in safe.get("owners", []) if isinstance(safe.get("owners"), list)]
try:
    key = os.environ.get("OPENSEA_API_KEY", "").strip() or json.load(open("state/neverland_market.json"))["os_key"]["key"]
    ev, cur = [], None
    for _ in range(40):
        p = {"chain": "monad", "after": int(time.time() - 120 * 86400), "limit": 50}
        if cur:
            p["next"] = cur
        r = requests.get(f"{OS_API}/events/accounts/{SAFE}", params=p, timeout=40,
                         headers={"X-API-KEY": key, "accept": "application/json"})
        if r.status_code == 429:
            time.sleep(10)
            continue
        d = r.json()
        ev += d.get("asset_events", [])
        cur = d.get("next")
        if not cur:
            break
    safe["os_events"] = [{"type": e.get("event_type"), "ts": e.get("event_timestamp"),
                          "token_id": (e.get("nft") or {}).get("identifier"),
                          "from": e.get("from_address"), "to": e.get("to_address"), "tx": e.get("transaction")} for e in ev]
except Exception as e:
    safe["os_err"] = str(e)[:200]
res["safe"] = safe
with open(os.path.join(OUT, "deposits_safe.json"), "w") as f:
    json.dump(res, f, indent=1)
print("完了")
