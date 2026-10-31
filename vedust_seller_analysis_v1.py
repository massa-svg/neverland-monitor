# veDUST 大口売り手の資金の行き先 v1（データ集め専用。売買はしない・秘密鍵不要）
# 出力: analysis/data/seller/ に JSON を保存（GitHub Actions がリポジトリに保存する）
import json, os, time
from datetime import datetime, timezone
import requests
from web3 import Web3

SELLERS   = ["0xf4db2e9d49817ee4d1b89c214a0dd76b603f9c33"]
DAYS      = 30                      # ブロックチェーンをさかのぼる日数
DUST_LOCK = "0xBB4738D05AD1b3Da57a4881baE62Ce9bb1eEeD6C"
OS_API    = "https://api.opensea.io/api/v2"
OUT       = os.path.join("analysis", "data", "seller")
RPC_URLS  = [u for u in [os.environ.get("MONAD_RPC_URL", "").strip(),
                         "https://rpc.monad.xyz", "https://rpc1.monad.xyz", "https://monad.drpc.org"] if u]
TRANSFER  = "0x" + Web3.keccak(text="Transfer(address,address,uint256)").hex().removeprefix("0x")
os.makedirs(OUT, exist_ok=True)


def log(*a):
    print(datetime.now(timezone.utc).strftime("%H:%M:%S"), *a, flush=True)


def save(name, obj):
    with open(os.path.join(OUT, name), "w") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, default=str)
    log(f"保存 {name}")


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


def connect():
    good = []
    for u in RPC_URLS:
        try:
            w3 = Web3(Web3.HTTPProvider(u, request_kwargs={"timeout": 40}))
            w3.eth.chain_id
            good.append(w3)
            log("RPC OK", u)
        except Exception as e:
            log("RPC失敗", u, e)
    if not good:
        raise RuntimeError("RPCに接続できません")
    return good


W3S = connect()
w3 = W3S[0]


def topic(addr):
    return "0x" + "0" * 24 + addr.lower()[2:]


def get_logs(frm, to, topics, budget_sec=900):
    """新しいブロックから古い方へ、範囲の上限に合わせて分割しながら集める。時間切れなら途中まで"""
    out, step, e, t0 = [], 50_000, to, time.time()
    last = ""
    while e >= frm and time.time() - t0 < budget_sec:
        b = max(e - step + 1, frm)
        ok = False
        for c in W3S:
            try:
                out += c.eth.get_logs({"fromBlock": b, "toBlock": e, "topics": topics})
                ok = True
                break
            except Exception as ex:
                last = str(ex)[:160]
        if ok:
            e = b - 1
            step = min(step * 2, 50_000)
        elif step <= 100:
            log("ログ取得をあきらめた範囲", b, e, last)
            e = b - 1
        else:
            step = max(step // 4, 100)
    log(f"ログ範囲 {e + 1}〜{to}（目標 {frm}）step={step} {last[:80]}")
    return out, e + 1


meta_cache = {}


def token_meta(addr):
    if addr in meta_cache:
        return meta_cache[addr]
    m = {}
    for name, sig in (("symbol", "symbol()"), ("decimals", "decimals()")):
        try:
            raw = w3.eth.call({"to": Web3.to_checksum_address(addr), "data": "0x" + Web3.keccak(text=sig)[:4].hex().removeprefix("0x")})
            if name == "decimals":
                m[name] = int.from_bytes(raw[-32:], "big")
            else:
                try:
                    from eth_abi import decode
                    m[name] = decode(["string"], raw)[0]
                except Exception:
                    m[name] = raw.rstrip(b"\0").decode(errors="ignore")[-12:]
        except Exception:
            pass
    meta_cache[addr] = m
    return m


latest = w3.eth.block_number
start_block = max(latest - int(DAYS * 86400 / 0.4), 0)
log("ブロック", start_block, "→", latest)
result = {}
for s in SELLERS:
    info = {"address": s}
    cs = Web3.to_checksum_address(s)
    info["nonce"] = w3.eth.get_transaction_count(cs)
    info["mon_balance"] = w3.eth.get_balance(cs) / 1e18
    info["code_len"] = len(w3.eth.get_code(cs))
    # OpenSea 上の動き（出品・成約・移転）
    try:
        ev, cur = [], None
        for _ in range(40):
            p = {"chain": "monad", "after": int(time.time() - 90 * 86400), "limit": 50}
            if cur:
                p["next"] = cur
            d = os_get(f"/events/accounts/{s}", p)
            ev += d.get("asset_events", [])
            cur = d.get("next")
            if not cur:
                break
        info["os_events"] = [{"type": e.get("event_type"), "ts": e.get("event_timestamp"),
                              "contract": (e.get("nft") or e.get("asset") or {}).get("contract"),
                              "token_id": (e.get("nft") or e.get("asset") or {}).get("identifier"),
                              "from": e.get("from_address") or e.get("seller") or e.get("maker"),
                              "to": e.get("to_address") or e.get("buyer"),
                              "pay": e.get("payment"), "tx": e.get("transaction")} for e in ev]
    except Exception as e:
        info["os_err"] = str(e)[:200]
    # トークンの出入り（ERC20 と NFT の Transfer）
    logs = []
    for label, topics in (("out", [TRANSFER, topic(s)]), ("in", [TRANSFER, None, topic(s)])):
        got, reached = get_logs(start_block, latest, topics)
        info[f"covered_from_block_{label}"] = reached
        log(f"{s[:8]} {label}: {len(got)}件")
        for L in got:
            t = [x.hex() if hasattr(x, "hex") else x for x in L["topics"]]
            nft = len(t) == 4
            val = int(t[3], 16) if nft else int(L["data"].hex() if hasattr(L["data"], "hex") else L["data"], 16) if (L["data"] not in (b"", "0x")) else 0
            logs.append({"dir": label, "token": L["address"].lower(), "block": L["blockNumber"],
                         "tx": L["transactionHash"].hex() if hasattr(L["transactionHash"], "hex") else L["transactionHash"],
                         "from": "0x" + t[1][-40:], "to": "0x" + t[2][-40:], "nft": nft, "value": str(val)})
    toks = {}
    for x in logs:
        toks.setdefault(x["token"], token_meta(x["token"]))
    info["tokens"] = toks
    info["transfers"] = logs
    # 時刻（ブロック → 時刻）
    bt = {}
    for b in sorted({x["block"] for x in logs})[:4000]:
        try:
            bt[b] = w3.eth.get_block(b)["timestamp"]
        except Exception:
            pass
    info["block_ts"] = bt
    # 取引の中身（送り先・関数）を、ロック新規作成とそれ以外の代表から最大200件
    txs = []
    for h in list(dict.fromkeys(x["tx"] for x in logs))[:200]:
        try:
            tx = w3.eth.get_transaction(h)
            rc = w3.eth.get_transaction_receipt(h)
            txs.append({"tx": h, "from": tx["from"].lower(), "to": (tx["to"] or "").lower(),
                        "value_mon": tx["value"] / 1e18, "selector": ("0x" + bytes(tx["input"]).hex())[:10],
                        "status": rc["status"], "n_logs": len(rc["logs"]),
                        "log_addrs": sorted({l["address"].lower() for l in rc["logs"]})})
        except Exception as e:
            txs.append({"tx": h, "err": str(e)[:120]})
    info["txs"] = txs
    # 今の残高（出入りのあったトークン）
    bals = {}
    for tkn, m in toks.items():
        try:
            raw = w3.eth.call({"to": Web3.to_checksum_address(tkn),
                               "data": "0x70a08231" + "0" * 24 + s[2:]})
            bals[tkn] = int.from_bytes(raw[-32:], "big") / 10 ** m.get("decimals", 18)
        except Exception:
            pass
    info["balances_now"] = bals
    result[s] = info
save("sellers.json", result)
save("meta.json", {"run_utc": datetime.now(timezone.utc).isoformat(), "start_block": start_block, "latest": latest, "days": DAYS})
log("完了")
