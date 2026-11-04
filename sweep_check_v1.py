# veDUST 一掃チェック v1（データ集め専用。売買はしない・秘密鍵不要）
# 直近の成約（何%引きで、誰が、いつ買ったか）と、今の安い出品の並びを analysis/sweep/ に書き出す
import json, os, time
from datetime import datetime, timezone, timedelta
import requests
from web3 import Web3

HOURS     = 96
ME        = "0x6d7579c965175634d85d5acd85a49907a551093f"
DUST_LOCK = "0xBB4738D05AD1b3Da57a4881baE62Ce9bb1eEeD6C"
DUST      = "0xAD96C3dffCD6374294e2573A7fBBA96097CC8d7c"
WMON      = "0x3bd359C1119dA7Da1D913D1C4D2B7c461115433A"
OS_API    = "https://api.opensea.io/api/v2"
SLUG      = "voting-escrow-dust"
OUT       = os.path.join("analysis", "sweep")
JST       = timezone(timedelta(hours=9))
RPC_URLS  = [u for u in [os.environ.get("MONAD_RPC_URL", "").strip(),
                         "https://rpc.monad.xyz", "https://rpc1.monad.xyz", "https://monad.drpc.org"] if u]
H = {"User-Agent": "Mozilla/5.0 (vedust-analysis)", "accept": "application/json"}
os.makedirs(OUT, exist_ok=True)


def api_key():
    k = os.environ.get("OPENSEA_API_KEY", "").strip()
    if k:
        return k
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


def connect():
    for u in RPC_URLS:
        try:
            w3 = Web3(Web3.HTTPProvider(u, request_kwargs={"timeout": 30}))
            w3.eth.chain_id
            return w3
        except Exception:
            pass
    raise RuntimeError("RPCに接続できません")


w3 = connect()
_amt = {}


def lock_amount(tid):
    if tid not in _amt:
        from eth_abi import encode
        data = bytes(Web3.keccak(text="locked(uint256)"))[:4] + encode(["uint256"], [tid])
        raw = bytes(w3.eth.call({"to": Web3.to_checksum_address(DUST_LOCK), "data": "0x" + data.hex()}))
        a = int.from_bytes(raw[:32], "big")
        _amt[tid] = (a - (1 << 256) if a >= (1 << 255) else a) / 1e18
    return _amt[tid]


def ohlcv(tok):
    for net in ("monad", "monad-mainnet"):
        r = requests.get(f"https://api.geckoterminal.com/api/v2/networks/{net}/tokens/{tok}/pools", headers=H, timeout=30)
        if r.ok and r.json().get("data"):
            pool = r.json()["data"][0]["attributes"]["address"]
            o = requests.get(f"https://api.geckoterminal.com/api/v2/networks/{net}/pools/{pool}/ohlcv/hour",
                             params={"aggregate": 1, "limit": 200, "token": tok}, headers=H, timeout=30)
            return sorted((int(x[0]), float(x[4])) for x in o.json()["data"]["attributes"]["ohlcv_list"])
    raise RuntimeError(f"価格履歴なし {tok}")


DUST_PX, MON_PX = ohlcv(DUST), ohlcv(WMON)


def px_at(series, ts):
    best = series[0][1]
    for t, c in series:
        if t <= ts:
            best = c
    return best


def disc(amount_mon, tid, ts):
    amt = lock_amount(tid)
    if amt <= 0:
        return None
    return (1 - amount_mon * px_at(MON_PX, ts) / (amt * px_at(DUST_PX, ts))) * 100


# ---------------- 直近の成約 ----------------
after = time.time() - HOURS * 3600
sales, cur = [], None
for _ in range(20):
    p = {"event_type": "sale", "after": int(after), "limit": 50}
    if cur:
        p["next"] = cur
    d = os_get(f"/events/collection/{SLUG}", p)
    for e in d.get("asset_events", []):
        n = e.get("nft") or e.get("asset") or {}
        pay = e.get("payment") or {}
        if str(pay.get("symbol", "")).upper() not in ("MON", "WMON"):
            continue
        ts, tid = int(e.get("event_timestamp") or 0), int(n.get("identifier"))
        amt = int(pay.get("quantity", 0)) / 10 ** int(pay.get("decimals", 18))
        sales.append({"ts": ts, "token_id": tid, "mon": amt, "disc": disc(amt, tid, ts),
                      "buyer": str(e.get("buyer", "")).lower(), "seller": str(e.get("seller", "")).lower()})
    cur = d.get("next")
    if not cur:
        break
sales.sort(key=lambda x: x["ts"])

# ---------------- 今の出品（安い順） ----------------
listings, cur = [], None
now = time.time()
for _ in range(20):
    p = {"limit": 100}
    if cur:
        p["next"] = cur
    d = os_get(f"/listings/collection/{SLUG}/all", p)
    for L in d.get("listings", []):
        prm = (L.get("protocol_data") or {}).get("parameters") or {}
        c = ((L.get("price") or {}).get("current")) or {}
        if str(c.get("currency", "")).upper() not in ("MON", "WMON"):
            continue
        tid = next((int(it["identifierOrCriteria"]) for it in prm.get("offer", [])
                    if str(it.get("token", "")).lower() == DUST_LOCK.lower()), None)
        if tid is None:
            continue
        amt = int(c.get("value", 0)) / 10 ** int(c.get("decimals", 18))
        listings.append({"token_id": tid, "mon": amt, "disc": disc(amt, tid, now),
                         "maker": str(prm.get("offerer", "")).lower()})
    cur = d.get("next")
    if not cur:
        break
best = {}
for L in listings:
    if L["disc"] is not None and (L["token_id"] not in best or L["mon"] < best[L["token_id"]]["mon"]):
        best[L["token_id"]] = L
listings = sorted(best.values(), key=lambda x: -x["disc"])

# ---------------- 書き出し ----------------
lines = [f"作成 {datetime.now(JST):%Y-%m-%d %H:%M} JST（割引率は成約時点のDUST・MON価格で計算）", "",
         f"直近{HOURS}時間の成約 {len(sales)}件（時刻は日本時間）"]
for x in sales:
    who = "★自分" if x["buyer"] == ME else x["buyer"][:8]
    mine = "（売り手=自分）" if x["seller"] == ME else ""
    d = "-" if x["disc"] is None else f"{x['disc']:5.1f}%"
    lines.append(f"  {datetime.fromtimestamp(x['ts'], JST):%m/%d %H:%M}  #{x['token_id']:<6} {x['mon']:>9,.1f} MON  割引{d}  買い手 {who}{mine}")
lines += ["", "今の出品（割引の大きい順・上位30件）"]
for L in listings[:30]:
    who = "★自分" if L["maker"] == ME else L["maker"][:8]
    lines.append(f"  #{L['token_id']:<6} {L['mon']:>9,.1f} MON  割引{L['disc']:5.1f}%  出品者 {who}")
open(os.path.join(OUT, "latest.txt"), "w").write("\n".join(lines) + "\n")
json.dump({"sales": sales, "listings": listings}, open(os.path.join(OUT, "latest.json"), "w"), indent=1)
print("\n".join(lines))
