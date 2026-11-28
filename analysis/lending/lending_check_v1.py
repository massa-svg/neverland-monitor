# Neverland レンディング(WMON)の調査: 金利・利用率・引き出せる量・DUST報酬・ゲートウェイの確認
import os, json, time, requests
from web3 import Web3
RPCS = [u for u in [os.environ.get("MONAD_RPC_URL", "").strip(), "https://rpc.monad.xyz", "https://rpc1.monad.xyz", "https://monad.drpc.org"] if u]
w3 = None
for u in RPCS:
    try:
        x = Web3(Web3.HTTPProvider(u, request_kwargs={"timeout": 30}))
        x.eth.block_number; w3 = x; break
    except Exception as e:
        print("rpc fail", u[:30], e)
C = Web3.to_checksum_address
out = {"time": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()), "block": w3.eth.block_number}
WMON = C("0x3bd359C1119dA7Da1D913D1C4D2B7c461115433A")
MARKETS = {
    "main": dict(provider="0x49D75170F55C964dfdd6726c74fdEDEe75553A0f", pool="0x80F00661b13CC5F6ccd3885bE7b4C9c67545D585",
                 dp="0xfd0b6b6F736376F7B99ee989c749007c7757fDba", ntoken="0xD0fd2Cf7F6CEff4F96B1161F5E995D5843326154",
                 gateway="0x800409dBd7157813BB76501c30e04596Cc478f25"),
    "shmon": dict(provider="0x300fA05F6fc9503EAbaAcB1dec7C35D65229f86f", pool="0x99Cbb76041fB8206E2459085Afb98BBc683Db527",
                  dp=None, ntoken="0xe0fDCBB0855f950dDf5f21595383f7f2a173E01e", gateway="0x05e61c923093e569401Edc2ac6AeAE01E7e60a53"),
}
def call(to, sig, types=(), vals=(), ret=("uint256",)):
    sel = Web3.keccak(text=sig)[:4]
    data = sel + (w3.codec.encode(list(types), list(vals)) if types else b"")
    r = w3.eth.call({"to": C(to), "data": "0x" + data.hex()})
    return w3.codec.decode(list(ret), r)
def safe(f, *a, **k):
    try: return f(*a, **k)
    except Exception as e: return f"ERR {str(e)[:120]}"
RAY = 10**27
def apy(rate):  # Aave: 年率(ray) → 複利APY
    r = rate / RAY
    return (1 + r / 31536000) ** 31536000 - 1
def price(addr):
    try:
        d = requests.get(f"https://api.geckoterminal.com/api/v2/simple/networks/monad/token_price/{addr}", timeout=20).json()
        return float(list(d["data"]["attributes"]["token_prices"].values())[0])
    except Exception as e:
        return None
mon_usd = price(WMON); out["mon_usd"] = mon_usd
for name, m in MARKETS.items():
    o = {}
    for k in ("provider", "pool", "ntoken", "gateway") + (("dp",) if m["dp"] else ()):
        o[f"code_{k}"] = len(w3.eth.get_code(C(m[k])))
    o["provider_getPool"] = safe(lambda: call(m["provider"], "getPool()", ret=("address",))[0])
    dp = m["dp"] or safe(lambda: call(m["provider"], "getPoolDataProvider()", ret=("address",))[0])
    o["dataProvider"] = dp
    o["gateway_getWETHAddress"] = safe(lambda: call(m["gateway"], "getWETHAddress()", ret=("address",))[0])
    o["gateway_owner"] = safe(lambda: call(m["gateway"], "owner()", ret=("address",))[0])
    o["ntoken_UNDERLYING"] = safe(lambda: call(m["ntoken"], "UNDERLYING_ASSET_ADDRESS()", ret=("address",))[0])
    o["ntoken_POOL"] = safe(lambda: call(m["ntoken"], "POOL()", ret=("address",))[0])
    o["ntoken_symbol"] = safe(lambda: call(m["ntoken"], "symbol()", ret=("string",))[0])
    rd = safe(lambda: call(dp, "getReserveData(address)", ("address",), (WMON,), ret=("uint256",) * 6 + ("uint128",) * 2 + ("uint40",) + ("uint256",) * 2 + ("uint40",)))
    if isinstance(rd, tuple):
        unbacked, accr, totA, totS, totV, liqRate, varRate = rd[:7]
        cash = call(WMON, "balanceOf(address)", ("address",), (C(m["ntoken"]),))[0]
        o.update(total_supplied_MON=totA / 1e18, total_borrowed_MON=(totS + totV) / 1e18, cash_withdrawable_MON=cash / 1e18,
                 utilization_pct=100 * (totS + totV) / totA if totA else 0, supply_APY_pct=100 * apy(liqRate), borrow_APY_pct=100 * apy(varRate))
    else:
        o["reserveData"] = rd
    cfg = safe(lambda: call(dp, "getReserveConfigurationData(address)", ("address",), (WMON,), ret=("uint256",) * 5 + ("bool",) * 5))
    if isinstance(cfg, tuple):
        o.update(ltv=cfg[1], reserveFactor_bps=cfg[4], borrowing=cfg[6], active=cfg[8], frozen=cfg[9])
    o["caps"] = safe(lambda: list(call(dp, "getReserveCaps(address)", ("address",), (WMON,), ret=("uint256", "uint256"))))
    o["paused"] = safe(lambda: call(dp, "getPaused(address)", ("address",), (WMON,), ret=("bool",))[0])
    ic = safe(lambda: call(m["ntoken"], "getIncentivesController()", ret=("address",))[0])
    o["incentivesController"] = ic
    if isinstance(ic, str) and not ic.startswith("ERR") and int(ic, 16):
        rws = safe(lambda: call(ic, "getRewardsByAsset(address)", ("address",), (C(m["ntoken"]),), ret=("address[]",))[0])
        o["rewards"] = []
        if isinstance(rws, (list, tuple)):
            for rw in rws:
                d = safe(lambda: call(ic, "getRewardsData(address,address)", ("address", "address"), (C(m["ntoken"]), rw), ret=("uint256",) * 4))
                sym = safe(lambda: call(rw, "symbol()", ret=("string",))[0])
                p = price(rw)
                e = {"reward": rw, "symbol": sym, "data": list(d) if isinstance(d, tuple) else d, "price_usd": p}
                if isinstance(d, tuple) and p and mon_usd and o.get("total_supplied_MON"):
                    yr = d[1] / 1e18 * 31536000 * p
                    e["reward_APR_pct_full_claim"] = 100 * yr / (o["total_supplied_MON"] * mon_usd)
                    e["distributionEnd"] = time.strftime("%Y-%m-%d", time.gmtime(d[3])) if d[3] else None
                o["rewards"].append(e)
        else:
            o["rewards"] = rws
    out[name] = o
s = json.dumps(out, indent=1, default=str)
print(s)
os.makedirs("analysis/data", exist_ok=True)
open(f"analysis/data/lending_check_{time.strftime('%Y%m%d_%H%M', time.gmtime())}.json", "w").write(s)
