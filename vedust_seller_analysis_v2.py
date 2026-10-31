# veDUST 大口売り手 v2: ロックを作った取引の中身（DUSTの出どころ）を読む。データ集め専用
import json, os
from datetime import datetime, timezone
from web3 import Web3

TXS = ["0x8da245c24cb7bf6e47b56a93e6f383be5705e5079e66bf46675a87fce47d5006", "0xbbcdffc20fae8c74a02db5ad800e0b98f6783e2578e4c8d2ea9d0bebc59448cd", "0xd5d881c6f074d814ec6fad0cc550f64a43144aa70b18e698cbaa73ee8596bca6", "0x39179059c7aeec0cbdd06a8eafb7c924ab6d13b950ca1e3500b399a79bf3ad6c", "0x1ad0116673a331424d97dabb06741c739018f385b47255271b1dc14f8b36e33d", "0xf59bacb6c7659d61e0e22213a3cc15113109be4638cfb989af035a716cff70b1", "0xa0160b4631d95ae9cc4d5e93c47460c982cdcc387bbbd2f82895bdc529459388", "0xafd3c2055ad734cfb27d40325c2b4e98b353a1e380c1a102003c02c6d6a0360d", "0xd0272efc2e26827569ac1cddd869e3e8ca70fe7300e8205ce3073f2a117495b1", "0x4266150a1164c4d58e5b62d244763dcec5a0262518f6375d89c2ab2077639f53", "0xddbb02b13e4a748ebcf475578e6b174bef52f3dd88ba4b9a0a882a038e8e33e7", "0x0b2edbf9f4749686360ce74d9d00c6a25ba202e52aa9fe8bf39f361c155a920b", "0x642e7b8915433ef2e6eb263b0a7a5f0727fb97134b573e4ecb49acae5089b8ad", "0x08fa6fd280ea67c65559bfdd3eabc157e37d8bf2f761d01f264061bc6fe1979a", "0x10ec2b9cd5bb0eabdfa2e1d765dbfa5d6fd21e9b739d26a10d7f12ce5cfdc837", "0x10ec2b9cd5bb0eabdfa2e1d765dbfa5d6fd21e9b739d26a10d7f12ce5cfdc837"]
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


def hx(v):
    return "0x" + bytes(v).hex()


def sym(addr):
    try:
        from eth_abi import decode
        raw = w3.eth.call({"to": Web3.to_checksum_address(addr), "data": "0x95d89b41"})
        return decode(["string"], raw)[0]
    except Exception:
        return ""


out, syms = [], {}
for h in dict.fromkeys(TXS):
    try:
        tx = w3.eth.get_transaction(h)
        rc = w3.eth.get_transaction_receipt(h)
        blk = w3.eth.get_block(rc["blockNumber"])
        logs = []
        for L in rc["logs"]:
            a = L["address"].lower()
            if a not in syms:
                syms[a] = sym(a)
            logs.append({"addr": a, "topics": [hx(t) for t in L["topics"]], "data": hx(L["data"])})
        out.append({"tx": h, "ts": blk["timestamp"], "from": tx["from"].lower(), "to": (tx["to"] or "").lower(),
                    "value_mon": tx["value"] / 1e18, "input": hx(tx["input"])[:2000], "logs": logs})
        print("ok", h[:12], len(logs), flush=True)
    except Exception as e:
        out.append({"tx": h, "err": str(e)[:200]})
with open(os.path.join(OUT, "mint_txs.json"), "w") as f:
    json.dump({"run_utc": datetime.now(timezone.utc).isoformat(), "symbols": syms, "txs": out}, f, indent=1)
print("完了")
