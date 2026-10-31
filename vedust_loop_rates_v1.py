# Neverland 金利・DUST報酬率の読み出し v1（データ集め専用。売買なし・秘密鍵不要）
import json, os, time
from datetime import datetime, timezone
import requests
from web3 import Web3

AAVE_ORACLE = "0x94bbA11004B9877d13bb5E1aE29319b6f7bDEdD4"
REWARDS     = "0x57ea245cCbFAb074baBb9d01d1F0c60525E52cec"     # DustRewardsController
DUST        = "0xAD96C3dffCD6374294e2573A7fBBA96097CC8d7c"
WHALE       = "0xf4db2e9d49817ee4d1b89c214a0dd76b603f9c33"
OUT = os.path.join("analysis", "data", "loop")
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
YEAR = 31536000


def call(to, sig, types=(), vals=()):
    from eth_abi import encode
    data = bytes(Web3.keccak(text=sig))[:4] + (encode(list(types), list(vals)) if types else b"")
    return bytes(w3.eth.call({"to": Web3.to_checksum_address(to), "data": "0x" + data.hex()}))


def words(raw):
    return [int.from_bytes(raw[i:i + 32], "big") for i in range(0, len(raw), 32)]


def addr(w):
    return "0x" + w.to_bytes(32, "big")[-20:].hex()


def cs(a):
    return Web3.to_checksum_address(a)


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


def dust_price():
    for net in ("monad", "monad-mainnet"):
        try:
            r = requests.get(f"https://api.geckoterminal.com/api/v2/simple/networks/{net}/token_price/{DUST}", timeout=20)
            p = float(r.json()["data"]["attributes"]["token_prices"][DUST.lower()])
            if p > 0:
                return p
        except Exception:
            pass
    return None


res = {"run_utc": datetime.now(timezone.utc).isoformat(), "dust_usd": dust_price()}
provider = addr(words(call(AAVE_ORACLE, "ADDRESSES_PROVIDER()"))[0])
pool = addr(words(call(provider, "getPool()"))[0])
w = words(call(pool, "getReservesList()"))
assets = [addr(x) for x in w[2:2 + w[1]]]
rows = []
for a in assets:
    r = {"asset": a, "symbol": sym(a)}
    try:
        rd = words(call(pool, "getReserveData(address)", ["address"], [cs(a)]))
        cfg = rd[0]
        r.update({"supply_apr": rd[2] / 1e27, "borrow_apr": rd[4] / 1e27,
                  "ltv": (cfg & 0xFFFF) / 1e4, "liq_threshold": ((cfg >> 16) & 0xFFFF) / 1e4,
                  "liq_bonus": ((cfg >> 32) & 0xFFFF) / 1e4,
                  "borrowing_enabled": bool((cfg >> 58) & 1), "frozen": bool((cfg >> 57) & 1),
                  "emode_legacy": (cfg >> 168) & 0xFF, "reserve_factor": ((cfg >> 64) & 0xFFFF) / 1e4})
        atoken, vdebt = addr(rd[8]), addr(rd[10])
        d = dec(a)
        px = words(call(AAVE_ORACLE, "getAssetPrice(address)", ["address"], [cs(a)]))[0] / 1e8
        r.update({"price": px, "aToken": atoken, "vDebt": vdebt,
                  "supply_usd": words(call(atoken, "totalSupply()"))[0] / 10 ** d * px,
                  "debt_usd": words(call(vdebt, "totalSupply()"))[0] / 10 ** d * px})
        for side, tok, base in (("supply", atoken, r["supply_usd"]), ("borrow", vdebt, r["debt_usd"])):
            info = []
            try:
                rw = words(call(REWARDS, "getRewardsByAsset(address)", ["address"], [cs(tok)]))
                rlist = [addr(x) for x in rw[2:2 + rw[1]]]
            except Exception as e:
                rlist, info = [], [{"err": str(e)[:120]}]
            for rt in rlist:
                try:
                    dd = words(call(REWARDS, "getRewardsData(address,address)", ["address", "address"], [cs(tok), cs(rt)]))
                    eps = dd[1] / 10 ** dec(rt)
                    end = dd[3]
                    rp = res["dust_usd"] if rt.lower() == DUST.lower() else None
                    active = end > time.time()
                    apr = (eps * YEAR * rp / base) if (rp and base and active) else None
                    info.append({"reward": rt, "symbol": sym(rt), "per_day": eps * 86400, "end": end,
                                 "active": active, "apr": apr})
                except Exception as e:
                    info.append({"reward": rt, "err": str(e)[:120]})
            r[f"{side}_rewards"] = info
    except Exception as e:
        r["err"] = str(e)[:200]
    rows.append(r)
    print(r["symbol"], flush=True)
res["reserves"] = rows
# eモード（v3.2 以降の形式と旧形式の両方を試す）
em = []
for i in range(1, 8):
    for sig in ("getEModeCategoryCollateralConfig(uint8)", "getEModeCategoryData(uint8)"):
        try:
            ww = words(call(pool, sig, ["uint8"], [i]))
            em.append({"id": i, "sig": sig, "words": [str(x) for x in ww[:8]]})
            break
        except Exception:
            continue
res["emode"] = em
try:
    ww = words(call(pool, "getUserEMode(address)", ["address"], [cs(WHALE)]))
    res["whale_emode"] = ww[0]
except Exception as e:
    res["whale_emode"] = str(e)[:100]
with open(os.path.join(OUT, "rates.json"), "w") as f:
    json.dump(res, f, indent=1)
print("完了")
