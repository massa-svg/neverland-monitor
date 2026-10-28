# veDUST 一掃買いの買い手分析 v1（データ集め専用。売買はしない・秘密鍵不要）
# 出力: analysis/data/ に JSON を保存（GitHub Actions がリポジトリに保存する）
import json, os, time
from datetime import datetime, timezone
import requests
from web3 import Web3

DAYS_SALES    = 70
DAYS_LISTINGS = 35
WATCH_PREFIX  = ["0x974757", "0x4bd444"]   # 気になる買い手（先頭一致）
TOP_BUYERS    = 15
DUST_LOCK = "0xBB4738D05AD1b3Da57a4881baE62Ce9bb1eEeD6C"
DUST      = "0xAD96C3dffCD6374294e2573A7fBBA96097CC8d7c"
WMON      = "0x3bd359C1119dA7Da1D913D1C4D2B7c461115433A"
OS_API    = "https://api.opensea.io/api/v2"
SLUG      = "voting-escrow-dust"
OUT       = os.path.join("analysis", "data")
RPC_URLS  = [u for u in [os.environ.get("MONAD_RPC_URL", "").strip(),
                         "https://rpc.monad.xyz", "https://rpc1.monad.xyz", "https://monad.drpc.org"] if u]
H = {"User-Agent": "Mozilla/5.0 (vedust-analysis)", "accept": "application/json"}
os.makedirs(OUT, exist_ok=True)


def log(*a):
    print(datetime.now(timezone.utc).strftime("%H:%M:%S"), *a, flush=True)


def save(name, obj):
    with open(os.path.join(OUT, name), "w") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    log(f"保存 {name}")


# ---------------- OpenSea ----------------
def api_key():
    k = os.environ.get("OPENSEA_API_KEY", "").strip()
    if k:
        return k
    try:
        d = json.load(open("state/neverland_market.json"))
        if d.get("os_key", {}).get("expires_ts", 0) > time.time() + 3600:
            return d["os_key"]["key"]
    except Exception:
        pass
    d = requests.post(f"{OS_API}/auth/keys", json={}, timeout=30).json()
    return d.get("api_key") or d.get("key") or (d.get("data") or {}).get("api_key")


KEY = api_key()


def os_get(path, params=None):
    for i in range(8):
        r = requests.get(f"{OS_API}{path}", params=params, timeout=40,
                         headers={"X-API-KEY": KEY, "accept": "application/json"})
        if r.status_code == 429:
            time.sleep(min(int(r.headers.get("Retry-After", 5) or 5), 60))
            continue
        if r.status_code >= 500:
            time.sleep(5)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"OpenSea失敗 {path}")


def events(path, params, after_ts, max_pages):
    out, cur = [], None
    for _ in range(max_pages):
        p = dict(params, after=int(after_ts), limit=50)
        if cur:
            p["next"] = cur
        d = os_get(path, p)
        out += d.get("asset_events", [])
        cur = d.get("next")
        if not cur:
            break
    return out


def nft_of(e):
    return e.get("nft") or e.get("asset") or {}


# ---------------- Monad ----------------
def connect():
    for u in RPC_URLS:
        try:
            w3 = Web3(Web3.HTTPProvider(u, request_kwargs={"timeout": 30}))
            w3.eth.chain_id
            log("RPC", u)
            return w3
        except Exception as e:
            log("RPC失敗", u, e)
    raise RuntimeError("RPCに接続できません")


w3 = connect()


def call_words(sig, tid, block="latest"):
    from eth_abi import encode
    data = bytes(Web3.keccak(text=sig))[:4] + encode(["uint256"], [tid])
    raw = bytes(w3.eth.call({"to": Web3.to_checksum_address(DUST_LOCK), "data": "0x" + data.hex()}, block))
    return [int.from_bytes(raw[i:i + 32], "big") for i in range(0, len(raw), 32)]


def lock_state(tid, block="latest"):
    try:
        w = call_words("locked(uint256)", tid, block)
        amt = (w[0] - (1 << 256) if w[0] >= (1 << 255) else w[0]) / 1e18
        return {"amt": amt, "words": [str(x) for x in w[1:]]}
    except Exception as e:
        return {"err": str(e)[:120]}


def power(tid, block="latest"):
    try:
        return call_words("balanceOfNFT(uint256)", tid, block)[0] / 1e18
    except Exception:
        return None


def owner(tid):
    try:
        w = call_words("ownerOf(uint256)", tid)
        return "0x" + w[0].to_bytes(32, "big")[-20:].hex()
    except Exception as e:
        return "ERR:" + str(e)[:80]


# ---------------- 1. 成約 ----------------
now = time.time()
raw_sales = events(f"/events/collection/{SLUG}", {"event_type": "sale"}, now - DAYS_SALES * 86400, 200)
sales = []
for e in raw_sales:
    n, pay = nft_of(e), e.get("payment") or {}
    if str(n.get("contract", DUST_LOCK)).lower() != DUST_LOCK.lower():
        continue
    sales.append({"ts": int(e.get("event_timestamp") or 0), "token_id": int(n.get("identifier")),
                  "amount": int(pay.get("quantity", 0)) / 10 ** int(pay.get("decimals", 18)),
                  "currency": str(pay.get("symbol", "")).upper(),
                  "buyer": str(e.get("buyer", "")).lower(), "seller": str(e.get("seller", "")).lower(),
                  "tx": e.get("transaction") or ""})
log(f"成約 {len(sales)}件")

# 成約時点のロック量（ブロック指定で過去の状態を読む。読めなければ今の値）
blocks = {}
for i, s in enumerate(sales):
    try:
        if s["tx"] not in blocks:
            blocks[s["tx"]] = w3.eth.get_transaction_receipt(s["tx"])["blockNumber"]
        s["block"] = blocks[s["tx"]]
        s["lock_at_sale"] = lock_state(s["token_id"], s["block"] - 1)
        s["power_at_sale"] = power(s["token_id"], s["block"] - 1)
    except Exception as e:
        s["block_err"] = str(e)[:120]
    if i % 50 == 0:
        log(f"  成約時点のロック {i}/{len(sales)}")
tids = sorted({s["token_id"] for s in sales})
now_state = {}
for i, t in enumerate(tids):
    now_state[t] = {"owner": owner(t), "lock": lock_state(t), "power": power(t)}
    if i % 50 == 0:
        log(f"  現在のロック {i}/{len(tids)}")
for s in sales:
    s["now"] = now_state[s["token_id"]]
save("sales.json", sales)

# ---------------- 2. 出品（供給側の曜日・割引の傾向） ----------------
listings = []
try:
    for e in events(f"/events/collection/{SLUG}", {"event_type": "listing"}, now - DAYS_LISTINGS * 86400, 150):
        n, pay = nft_of(e), e.get("payment") or {}
        try:
            listings.append({"ts": int(e.get("event_timestamp") or 0), "token_id": int(n.get("identifier")),
                             "amount": int(pay.get("quantity", 0)) / 10 ** int(pay.get("decimals", 18)),
                             "currency": str(pay.get("symbol", "")).upper(),
                             "maker": str(e.get("maker", "")).lower()})
        except Exception:
            continue
except Exception as e:
    log("出品イベント失敗", e)
lk = {}
for t in sorted({x["token_id"] for x in listings} - set(now_state)):
    lk[t] = lock_state(t)
save("listings.json", {"events": listings, "locks_now": lk})

# ---------------- 3. 買い手の正体 ----------------
cnt = {}
for s in sales:
    cnt[s["buyer"]] = cnt.get(s["buyer"], 0) + 1
watch = [b for b, _ in sorted(cnt.items(), key=lambda kv: -kv[1])[:TOP_BUYERS]]
watch += [b for b in cnt if any(b.startswith(p) for p in WATCH_PREFIX) and b not in watch]
buyers = {}
for b in watch:
    info = {"buys": cnt[b]}
    try:
        cb = Web3.to_checksum_address(b)
        info["mon_balance"] = w3.eth.get_balance(cb) / 1e18
        info["nonce"] = w3.eth.get_transaction_count(cb)
        info["code_len"] = len(w3.eth.get_code(cb))
    except Exception as e:
        info["rpc_err"] = str(e)[:120]
    try:
        ev = events(f"/events/accounts/{b}", {"chain": "monad"}, now - DAYS_SALES * 86400, 60)
        info["events"] = [{"type": e.get("event_type"), "ts": e.get("event_timestamp"),
                           "contract": nft_of(e).get("contract"), "token_id": nft_of(e).get("identifier"),
                           "from": e.get("from_address") or e.get("seller") or e.get("maker"),
                           "to": e.get("to_address") or e.get("buyer"),
                           "pay": e.get("payment"), "tx": e.get("transaction")} for e in ev]
    except Exception as e:
        info["events_err"] = str(e)[:200]
    try:
        d = os_get(f"/chain/monad/account/{b}/nfts", {"collection": SLUG, "limit": 200})
        info["holds"] = [int(n["identifier"]) for n in d.get("nfts", [])]
    except Exception as e:
        info["holds_err"] = str(e)[:200]
    buyers[b] = info
    log(f"買い手 {b[:10]} 完了")
save("buyers.json", buyers)
hold_locks = {}
for b in buyers.values():
    for t in b.get("holds", []):
        if t not in now_state and t not in hold_locks:
            hold_locks[t] = {"lock": lock_state(t), "power": power(t)}
save("buyer_holdings_locks.json", hold_locks)

# ---------------- 4. 価格の履歴（4時間足） ----------------
prices = {}
for name, tok in (("DUST", DUST), ("WMON", WMON)):
    try:
        for net in ("monad", "monad-mainnet"):
            r = requests.get(f"https://api.geckoterminal.com/api/v2/networks/{net}/tokens/{tok}/pools", headers=H, timeout=30)
            if r.ok and r.json().get("data"):
                pool = r.json()["data"][0]["attributes"]["address"]
                o = requests.get(f"https://api.geckoterminal.com/api/v2/networks/{net}/pools/{pool}/ohlcv/hour",
                                 params={"aggregate": 4, "limit": 1000, "token": tok}, headers=H, timeout=30)
                prices[name] = {"pool": pool, "ohlcv": o.json()["data"]["attributes"]["ohlcv_list"]}
                break
    except Exception as e:
        prices[name] = {"err": str(e)[:200]}
save("prices.json", prices)
save("meta.json", {"run_utc": datetime.now(timezone.utc).isoformat(), "sales": len(sales),
                   "listings": len(listings), "buyers": len(buyers)})
log("完了")
