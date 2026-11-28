# ゲートウェイの関数があるか・預け入れのシミュレーション
import os, json
from web3 import Web3
for u in [os.environ.get("MONAD_RPC_URL", "").strip(), "https://rpc.monad.xyz", "https://rpc1.monad.xyz"]:
    if not u: continue
    try:
        w3 = Web3(Web3.HTTPProvider(u, request_kwargs={"timeout": 30})); w3.eth.block_number; break
    except Exception: pass
C = Web3.to_checksum_address
GW, POOL, NT = C("0x800409dBd7157813BB76501c30e04596Cc478f25"), C("0x80F00661b13CC5F6ccd3885bE7b4C9c67545D585"), C("0xD0fd2Cf7F6CEff4F96B1161F5E995D5843326154")
code = w3.eth.get_code(GW).hex()
for s in ["depositETH(address,address,uint16)", "withdrawETH(address,uint256,address)", "withdrawETHWithPermit(address,uint256,address,uint256,uint8,bytes32,bytes32)", "getWETHAddress()"]:
    print(s, Web3.keccak(text=s)[:4].hex() in code)
me = C("0x1111111111111111111111111111111111111111")
data = Web3.keccak(text="depositETH(address,address,uint16)")[:4] + w3.codec.encode(["address", "address", "uint16"], [POOL, me, 0])
tx = {"from": me, "to": GW, "value": 10**21, "data": "0x" + data.hex()}
ov = {me: {"balance": hex(10**22)}}
try:
    print("simulate deposit 1000 MON:", w3.eth.call(tx, "latest", ov).hex())
    g = w3.eth.estimate_gas(tx, "latest", ov); print("gas", g)
except Exception as e:
    print("simulate ERR", e)
