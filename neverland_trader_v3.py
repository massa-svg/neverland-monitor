# =====================================================================
# Neverland veDUST 自動売買ボット  v3   （Railway などで常時実行）
#   v2からの変更点
#   - 出品に使う「コンジット」（NFTを動かす許可を与える先）を固定値ではなく、Monad上の実際の出品から読み取るように変更
#     （v2の事前確認で、Ethereum等で使われるOpenSeaのコンジットがMonadに無いことが分かったため）
#   - 事前確認に「OpenSeaから読めた出品数」「新着イベントが読めるか」「出品に使うコンジット」を追加
#   v1からの変更点（v2）
#   - 1件あたりの上限額と1日の上限件数をなくした（ウォレットの残高で買える限り買う）
#   - 値下げを「6時間ごとに1ポイント」に変更
#   - 数秒ごとに OpenSea の veDUST 出品を確認し、割引率などの条件を満たしたら即購入する
#   - 買った veDUST は保有したまま OpenSea に出品し、時間とともに段階的に値下げする
#     （値下げは「手取りが購入価格＋1%」になる価格で止まる）
#   - 売れたら Discord に報告する
#
#   動き方は環境変数 TRADER_MODE で切り替える
#     off  : 何もしない（待機だけ）
#     dry  : ドライラン（既定）。購入はシミュレーションまで、出品は「出すはずの価格」を通知するだけ。
#            お金もNFTも一切動かない。シミュレーションで買えた出品は「仮想の保有」として記録し、
#            値下げの予定も通知する
#     live : 本番。実際に購入・出品・値下げする（購入は取り消せない）
#
#   割引率 = 1 − 出品価格(USD) ÷（ロック中のDUST数量 × DUST価格(USD)）   ※v3と同じ計算
#
#   必要な環境変数（Railway の Variables に入れる）
#     TRADER_MODE                off / dry / live（未設定なら dry）
#     AUTOBUY_PRIVATE_KEY        自動売買専用ウォレットの秘密鍵（メインの鍵は絶対に使わない）
#     DISCORD_ALERT_WEBHOOK_URL  通知先（今の neverland-alert と同じでよい）
#     STATE_DIR                  記録の保存先。Railway ではボリュームの場所（例 /data）
#     MONAD_RPC_URL / OPENSEA_API_KEY   任意。あれば優先して使う
#     TEST_MIN_DISCOUNT          任意・ドライランのときだけ有効。購入判定の割引率を一時的に下げて、
#                                実際の出品で「購入データ取得→シミュレーション→出品価格の計算」を試す
# =====================================================================
import os, sys, json, time, random, traceback
from datetime import datetime, timezone, timedelta
import requests
from web3 import Web3

VERSION = "trader v3"

# ---------------- 購入の設定 ----------------
BUY_MIN_DISCOUNT_PCT = 35.0    # この割引率以上なら買う
BUY_MAX_USD          = None    # 1件あたりの上限（ドル）。None なら上限なし（残高で買える限り）
BUY_MAX_PER_DAY      = None    # 1日（日本時間）の上限件数。None なら上限なし
BUY_PERM_ONLY        = True    # True なら無期限ロックのものだけ買う
GAS_RESERVE_MON      = 1.0     # ガス代用にウォレットに残しておくMON

# ---------------- 転売（出品）の設定 ----------------
SELL_ENABLED         = True
SELL_START_DISC_PCT  = 10.0    # 最初の出品の割引率（その時のロック価値に対して）
SELL_STEP_PCT        = 1.0     # 値下げ1回あたりの割引率の上乗せ（ポイント）
SELL_STEP_HOURS      = 6       # 何時間ごとに値下げするか
SELL_FLOOR_PCT       = 1.0     # 下限：購入価格より何%上で値下げを止めるか（マイナスなら購入価格より下）
SELL_FLOOR_BASIS     = "net"   # "net"＝手数料・ロイヤリティを引いた手取りで判定 / "gross"＝出品価格で判定
#   例：ロック価値$50を$30（40%引き）で購入、手数料計11%なら
#       初回 $45（10%引き）→ 6時間ごとに1ptずつ値下げ → 手取り$30.3（＝出品価格 約$34）で停止
SELL_LISTING_DAYS    = 30      # 出品の有効期間
SELL_RAISE_PCT       = 5.0     # DUST高などで目標価格が今の出品より5%以上高くなったら出し直す

# ---------------- 動作の設定 ----------------
FAST_SECONDS   = 5             # 新着出品の確認間隔
FULL_SECONDS   = 180           # 全出品の確認間隔（取りこぼし防止）
SELL_SECONDS   = 60            # 保有・出品の見直し間隔
PRICE_SECONDS  = 60            # DUST・MON価格の更新間隔
CACHE_HOURS    = 24            # ロック情報を使い回す時間

DUST_LOCK   = "0xBB4738D05AD1b3Da57a4881baE62Ce9bb1eEeD6C"   # veDUST（DustLock Proxy）
DUST        = "0xAD96C3dffCD6374294e2573A7fBBA96097CC8d7c"
WMON        = "0x3bd359C1119dA7Da1D913D1C4D2B7c461115433A"
AAVE_ORACLE = "0x94bbA11004B9877d13bb5E1aE29319b6f7bDEdD4"
SEAPORT_DEFAULT  = "0x0000000000000068F116a894984e2DB1123eB395"   # Seaport 1.6（出品データの値を優先）
OS_CONDUIT_KEY   = "0x0000007b02230091a7ed01230072f7006a004d60a8d4e71d599b8104250f0000"   # 他チェーンでのOpenSeaの既定
CONDUIT_CONTROLLER = "0x00000000F9490004C11Cdc6280cF2aaE8B78F7d1"   # Seaport のコンジット管理契約
ZERO_ADDR        = "0x0000000000000000000000000000000000000000"
ZERO32           = "0x" + "00" * 32
RPC_URLS = [u for u in [os.environ.get("MONAD_RPC_URL", "").strip(),
                        "https://rpc.monad.xyz", "https://rpc1.monad.xyz", "https://monad.drpc.org"] if u]
OS_API      = "https://api.opensea.io/api/v2"
STABLES     = {"USDC", "USDT", "USDT0", "AUSD", "USDC.E"}
MON_SYMBOLS = {"MON", "WMON"}
JST         = timezone(timedelta(hours=9))

MODE = (os.environ.get("TRADER_MODE", "").strip().lower() or "dry")
if MODE not in ("off", "dry", "live"):
    MODE = "dry"
PK            = os.environ.get("AUTOBUY_PRIVATE_KEY", "").strip()
TEST_MIN_DISCOUNT = os.environ.get("TEST_MIN_DISCOUNT", "").strip()
if TEST_MIN_DISCOUNT and MODE == "dry":
    BUY_MIN_DISCOUNT_PCT = float(TEST_MIN_DISCOUNT)
WEBHOOK       = os.environ.get("DISCORD_ALERT_WEBHOOK_URL", "").strip()
OS_KEY_SECRET = os.environ.get("OPENSEA_API_KEY", "").strip()
STATE_DIR     = os.environ.get("STATE_DIR", "").strip() or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "state")
STATE_PATH    = os.path.join(STATE_DIR, "neverland_trader.json")
os.makedirs(STATE_DIR, exist_ok=True)

ORACLE_ABI = [{"name": "getAssetPrice", "type": "function", "stateMutability": "view",
               "inputs": [{"type": "address"}], "outputs": [{"type": "uint256"}]}]


def now():
    return datetime.now(JST)


def log(*a):
    print(f"[{now():%m/%d %H:%M:%S}]", *a, flush=True)


def notify(text):
    log(text)
    if not WEBHOOK:
        return
    for i in range(0, len(text), 1950):
        try:
            requests.post(WEBHOOK, json={"content": text[i:i + 1950]}, timeout=15)
        except Exception as e:
            log("通知失敗:", e)


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {}


def save_state(st):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_PATH)


def to_int(v):
    if isinstance(v, int):
        return v
    s = str(v)
    return int(s, 16) if s.lower().startswith("0x") else int(s)


def hexb(v):
    s = str(v)
    return bytes.fromhex(s[2:] if s.startswith("0x") else s)


# ---------------- OpenSea ----------------
def api_key(st):
    if OS_KEY_SECRET:
        return OS_KEY_SECRET
    k = st.get("os_key")
    if k and time.time() < k.get("expires_ts", 0) - 6 * 3600:
        return k["key"]
    r = requests.post(f"{OS_API}/auth/keys", json={}, timeout=30)
    r.raise_for_status()
    d = r.json()
    key = d.get("api_key") or d.get("key") or (d.get("data") or {}).get("api_key")
    if not key:
        raise RuntimeError(f"OpenSeaのAPIキーを取得できませんでした: {str(d)[:200]}")
    try:
        exp_ts = datetime.fromisoformat(str(d.get("expires_at")).replace("Z", "+00:00")).timestamp()
    except Exception:
        exp_ts = time.time() + 6 * 86400
    st["os_key"] = {"key": key, "expires_ts": exp_ts}
    log("OpenSeaのAPIキーを新しく取得しました")
    return key


def os_req(method, path, key, params=None, body=None):
    for i in range(4):
        r = requests.request(method, f"{OS_API}{path}", params=params, json=body, timeout=30,
                             headers={"X-API-KEY": key, "accept": "application/json",
                                      "content-type": "application/json"})
        if r.status_code == 429:
            time.sleep(min(int(r.headers.get("Retry-After", 5) or 5), 30))
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"OpenSea {method} {path.split('?')[0]} 失敗（{r.status_code}）: {r.text[:300]}")
        return r.json()
    raise RuntimeError("OpenSea API の制限にかかりました")


def find_slug(st, key, chain):
    if st.get("os_slug"):
        return st["os_slug"]
    d = os_req("GET", f"/chain/{chain}/contract/{DUST_LOCK}", key)
    slug = d.get("collection")
    if not slug:
        raise RuntimeError(f"veDUSTのOpenSeaコレクションが見つかりません: {str(d)[:200]}")
    st["os_slug"] = slug
    return slug


def all_listings(key, slug):
    out, nxt = [], None
    for _ in range(20):
        params = {"limit": 100}
        if nxt:
            params["next"] = nxt
        d = os_req("GET", f"/listings/collection/{slug}/all", key, params)
        out += [parse_listing(L) for L in d.get("listings", [])]
        nxt = d.get("next")
        if not nxt:
            break
    return [p for p in out if p and p["token_id"] is not None]


def parse_listing(L):
    params = (L.get("protocol_data") or {}).get("parameters") or {}
    token_id = None
    for it in params.get("offer", []):
        if str(it.get("token", "")).lower() == DUST_LOCK.lower():
            token_id = int(it.get("identifierOrCriteria"))
    cur = ((L.get("price") or {}).get("current")) or {}
    value = int(cur.get("value", 0))
    dec = int(cur.get("decimals", 18))
    return {"order_hash": L.get("order_hash"), "token_id": token_id,
            "amount": value / 10 ** dec, "currency": str(cur.get("currency", "")).upper(),
            "value_raw": value, "end": int(params.get("endTime", 0) or 0),
            "protocol_address": L.get("protocol_address") or "",
            "conduit_key": str(params.get("conduitKey", "") or "").lower(),
            "offerer": str(params.get("offerer", "")).lower()}


def recent_listings(key, slug, after_ts):
    """新着の出品イベントだけを取る（速い確認用）。形が読めないものは捨てる"""
    d = os_req("GET", f"/events/collection/{slug}", key,
               {"event_type": "listing", "after": int(after_ts), "limit": 50})
    out = []
    for e in d.get("asset_events", []):
        try:
            asset = e.get("asset") or (e.get("nft") or {})
            if str(asset.get("contract", DUST_LOCK)).lower() != DUST_LOCK.lower():
                continue
            pay = e.get("payment") or {}
            dec = int(pay.get("decimals", 18))
            value = int(pay.get("quantity", 0))
            exp = e.get("expiration_date") or 0
            out.append({"order_hash": e.get("order_hash"), "token_id": int(asset.get("identifier")),
                        "amount": value / 10 ** dec, "currency": str(pay.get("symbol", "")).upper(),
                        "value_raw": value, "end": int(exp) if str(exp).isdigit() else 0,
                        "protocol_address": e.get("protocol_address") or "",
                        "offerer": str(e.get("maker", "")).lower(),
                        "ts": int(e.get("event_timestamp") or 0)})
        except Exception:
            continue
    return [p for p in out if p["order_hash"] and p["value_raw"] > 0]


def collection_fees(st, key, slug):
    """出品時に必要な手数料（OpenSea手数料・ロイヤリティ）。1日1回取り直す"""
    c = st.get("fees")
    if c and time.time() - c["ts"] < 86400:
        return c["items"]
    d = os_req("GET", f"/collections/{slug}", key)
    items = [{"bps": int(round(float(f["fee"]) * 100)), "recipient": Web3.to_checksum_address(f["recipient"])}
             for f in d.get("fees", []) if f.get("required")]
    st["fees"] = {"ts": time.time(), "items": items}
    return items


# ---------------- ブロックチェーン ----------------
def connect():
    last = None
    for url in RPC_URLS:
        try:
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 20}))
            w3.eth.chain_id
            return w3
        except Exception as e:
            last = e
    raise RuntimeError(f"RPCに接続できませんでした: {last}")


def call_raw(w3, to, signature, types=(), values=()):
    from eth_abi import encode
    sel = bytes(Web3.keccak(text=signature))[:4]
    return bytes(w3.eth.call({"to": Web3.to_checksum_address(to), "data": "0x" + (sel + encode(list(types), list(values))).hex()}))


def call_uint(w3, signature, token_id, to=DUST_LOCK):
    raw = call_raw(w3, to, signature, ["uint256"], [token_id])
    return [int.from_bytes(raw[i:i + 32], "big") for i in range(0, len(raw), 32)]


def owner_of(w3, token_id):
    w = call_uint(w3, "ownerOf(uint256)", token_id)
    return Web3.to_checksum_address("0x" + w[0].to_bytes(32, "big")[-20:].hex())


def voting_power(w3, token_id):
    for sig in ("balanceOfNFT(uint256)", "votingPower(uint256)", "getVotingPower(uint256)"):
        try:
            w = call_uint(w3, sig, token_id)
            if w:
                return w[0] / 1e18
        except Exception:
            continue
    return None


def locked(w3, token_id):
    """DustLock.locked(tokenId) → (DUST数量, 状態, 残り日数)。状態は 'days' / 'perm' / 'expired' / 'unknown'"""
    w = call_uint(w3, "locked(uint256)", token_id)
    if len(w) < 2:
        raise RuntimeError("locked() の戻り値が短い")
    amt = (w[0] - (1 << 256) if w[0] >= (1 << 255) else w[0]) / 1e18
    end = w[1]
    nowts = time.time()
    if end > nowts + 3600:
        return amt, "days", (end - nowts) / 86400
    power = voting_power(w3, token_id)
    if power is None:
        status = "unknown"
    elif power > 0 and amt > 0 and power >= amt * 0.5:
        status = "perm"
    elif power == 0:
        status = "expired"
    else:
        status = "unknown"
    return amt, status, None


def lock_info(st, w3, token_id, force=False):
    cache = st.setdefault("locks", {})
    c = cache.get(str(token_id))
    nowts = time.time()
    if c and not force and nowts - c["ts"] < CACHE_HOURS * 3600:
        days = (c["end"] - nowts) / 86400 if c["status"] == "days" else None
        if not (c["status"] == "days" and days is not None and days <= 0):
            return c["amt"], c["status"], days
    try:
        amt, status, days = locked(w3, token_id)
    except Exception as e:
        log(f"#{token_id}: ロック量を取得できず（{e}）")
        return None
    cache[str(token_id)] = {"amt": amt, "status": status,
                            "end": nowts + days * 86400 if days is not None else 0, "ts": nowts}
    return amt, status, days


def dexscreener_price(token):
    r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{token}", timeout=20)
    r.raise_for_status()
    pairs = [p for p in (r.json().get("pairs") or []) if "monad" in str(p.get("chainId", "")).lower()
             and str(p.get("baseToken", {}).get("address", "")).lower() == token.lower()]
    if not pairs:
        raise RuntimeError(f"DexScreenerに価格なし: {token}")
    return float(max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))["priceUsd"])


def mon_price(w3):
    try:
        o = w3.eth.contract(address=Web3.to_checksum_address(AAVE_ORACLE), abi=ORACLE_ABI)
        px = o.functions.getAssetPrice(Web3.to_checksum_address(WMON)).call() / 1e8
        if px > 0:
            return px
    except Exception:
        pass
    return dexscreener_price(WMON)


def send_tx(w3, acct, tx):
    """署名して送り、結果を待つ。戻り値は (tx hash, 成功したか)"""
    tx = dict(tx)
    tx.setdefault("from", acct.address)
    tx.setdefault("value", 0)
    tx["chainId"] = w3.eth.chain_id
    if "gas" not in tx:
        tx["gas"] = int(w3.eth.estimate_gas(tx) * 1.2)       # Monadはガス上限分を請求されるので余裕は小さめ
    fill_fees(w3, tx)
    tx["nonce"] = w3.eth.get_transaction_count(acct.address, "pending")
    signed = acct.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
    h = w3.eth.send_raw_transaction(raw)
    hx = "0x" + bytes(h).hex().removeprefix("0x")
    rec = w3.eth.wait_for_transaction_receipt(h, timeout=120)
    return hx, rec.get("status") == 1


def fill_fees(w3, tx):
    base = w3.eth.get_block("latest").get("baseFeePerGas") or w3.eth.gas_price
    try:
        prio = w3.eth.max_priority_fee
    except Exception:
        prio = 0
    tx["maxPriorityFeePerGas"] = prio
    tx["maxFeePerGas"] = base * 2 + prio


# ---------------- 購入（Seaport） ----------------
# OpenSea は購入用のデータ（Seaportという取引契約に渡す中身）を返してくれるので、
# それを確認・組み立てて、自分のウォレットで署名して送る
ADD_RCPT   = [("amount", "uint256"), ("recipient", "address")]
BASIC      = [("considerationToken", "address"), ("considerationIdentifier", "uint256"),
              ("considerationAmount", "uint256"), ("offerer", "address"), ("zone", "address"),
              ("offerToken", "address"), ("offerIdentifier", "uint256"), ("offerAmount", "uint256"),
              ("basicOrderType", "uint8"), ("startTime", "uint256"), ("endTime", "uint256"),
              ("zoneHash", "bytes32"), ("salt", "uint256"), ("offererConduitKey", "bytes32"),
              ("fulfillerConduitKey", "bytes32"), ("totalOriginalAdditionalRecipients", "uint256"),
              ("additionalRecipients", ("tuple[]", ADD_RCPT)), ("signature", "bytes")]
OFFER_ITEM = [("itemType", "uint8"), ("token", "address"), ("identifierOrCriteria", "uint256"),
              ("startAmount", "uint256"), ("endAmount", "uint256")]
CONS_ITEM  = OFFER_ITEM + [("recipient", "address")]
ORDER_PARAMS = [("offerer", "address"), ("zone", "address"), ("offer", ("tuple[]", OFFER_ITEM)),
                ("consideration", ("tuple[]", CONS_ITEM)), ("orderType", "uint8"),
                ("startTime", "uint256"), ("endTime", "uint256"), ("zoneHash", "bytes32"),
                ("salt", "uint256"), ("conduitKey", "bytes32"), ("totalOriginalConsiderationItems", "uint256")]
ORDER_COMPONENTS = ORDER_PARAMS[:-1] + [("counter", "uint256")]
ORDER      = [("parameters", ("tuple", ORDER_PARAMS)), ("signature", "bytes")]
ADV_ORDER  = [("parameters", ("tuple", ORDER_PARAMS)), ("numerator", "uint120"), ("denominator", "uint120"),
              ("signature", "bytes"), ("extraData", "bytes")]
CRITERIA   = [("orderIndex", "uint256"), ("side", "uint8"), ("index", "uint256"),
              ("identifier", "uint256"), ("criteriaProof", "bytes32[]")]
SEAPORT_FUNCS = {
    "fulfillBasicOrder": [("parameters", ("tuple", BASIC))],
    "fulfillBasicOrder_efficient_6GL6yc": [("parameters", ("tuple", BASIC))],
    "fulfillOrder": [("order", ("tuple", ORDER)), ("fulfillerConduitKey", "bytes32")],
    "fulfillAdvancedOrder": [("advancedOrder", ("tuple", ADV_ORDER)), ("criteriaResolvers", ("tuple[]", CRITERIA)),
                             ("fulfillerConduitKey", "bytes32"), ("recipient", "address")],
    "cancel": [("orders", ("tuple[]", ORDER_COMPONENTS))],
}


def abi_type(t):
    if isinstance(t, str):
        return t
    kind, comps = t
    inner = "(" + ",".join(abi_type(c[1]) for c in comps) + ")"
    return inner + ("[]" if kind == "tuple[]" else "")


def abi_value(t, v):
    if not isinstance(t, str):
        kind, comps = t
        if kind == "tuple[]":
            return [abi_value(("tuple", comps), x) for x in v]
        if isinstance(v, dict):
            missing = [n for n, _ in comps if n not in v]
            if missing:
                raise RuntimeError(f"取引データに項目がありません: {missing}")
            return tuple(abi_value(ct, v[n]) for n, ct in comps)
        return tuple(abi_value(ct, x) for (n, ct), x in zip(comps, v))
    if t.endswith("[]"):
        return [abi_value(t[:-2], x) for x in v]
    if t == "address":
        return Web3.to_checksum_address(v)
    if t.startswith("uint") or t.startswith("int"):
        return to_int(v)
    if t.startswith("bytes"):
        b = hexb(v)
        return b.rjust(32, b"\0") if t == "bytes32" else b
    return v


def encode_call(name, data):
    from eth_abi import encode
    inputs = SEAPORT_FUNCS[name]
    types = [abi_type(t) for _, t in inputs]
    sig = f"{name}({','.join(types)})"
    values = [abi_value(t, data[n]) for n, t in inputs]
    return sig, bytes(Web3.keccak(text=sig))[:4] + encode(types, values)


def build_calldata(tx):
    """OpenSea の transaction（function名と中身）から送信用データを作る。知らない形なら止める"""
    fn = str(tx.get("function", "")).replace(" ", "")
    name = fn.split("(")[0]
    if name not in SEAPORT_FUNCS or name == "cancel":
        raise RuntimeError(f"未対応の購入方法です: {fn[:80]}")
    data = tx.get("input_data") or {}
    sig, calldata = encode_call(name, data)
    if "(" in fn and fn != sig:
        raise RuntimeError(f"購入データの形が想定と違います: {fn[:200]}")
    suffix = tx.get("calldata_suffix")
    if suffix:
        calldata += hexb(suffix)
    return name, data, calldata


def check_order_matches(name, data, token_id, me):
    """購入データの中身が「veDUSTのこのNFTを、MONで払って、自分が受け取る」ものになっているか確認"""
    if name.startswith("fulfillBasicOrder"):
        p = data["parameters"]
        ok = (str(p["offerToken"]).lower() == DUST_LOCK.lower()
              and to_int(p["offerIdentifier"]) == token_id
              and str(p["considerationToken"]).lower() == ZERO_ADDR)
        if not ok:
            raise RuntimeError("購入データの中身（NFT・支払通貨）が出品と一致しません")
        return
    order = data.get("order") or data.get("advancedOrder")
    offers = order["parameters"]["offer"]
    if not any(str(o["token"]).lower() == DUST_LOCK.lower()
               and to_int(o["identifierOrCriteria"]) == token_id for o in offers):
        raise RuntimeError("購入データの中身（NFT）が出品と一致しません")
    rcpt = str(data.get("recipient", ZERO_ADDR)).lower()
    if rcpt not in (ZERO_ADDR, me.lower()):
        raise RuntimeError("購入したNFTの受取先が自分のウォレットではありません")


def fulfillment(key, chain, r, me):
    body = {"listing": {"hash": r["order_hash"], "chain": chain,
                        "protocol_address": r["protocol_address"] or SEAPORT_DEFAULT},
            "fulfiller": {"address": me}}
    return os_req("POST", "/listings/fulfillment_data", key, body=body)["fulfillment_data"]["transaction"]


def prepare_buy(w3, key, chain, r, acct):
    """購入トランザクションを作り、シミュレーションまで行う（送らない）"""
    me = acct.address
    tx = fulfillment(key, chain, r, me)
    to = Web3.to_checksum_address(tx["to"])
    if r["protocol_address"] and to.lower() != r["protocol_address"].lower():
        raise RuntimeError(f"送り先の契約が出品と違います: {to}")
    value = to_int(tx.get("value") or 0)
    if value > r["value_raw"] + 1:
        raise RuntimeError(f"支払額が出品価格より大きい: {value} > {r['value_raw']}")
    name, data, calldata = build_calldata(tx)
    check_order_matches(name, data, r["token_id"], me)
    t = {"from": me, "to": to, "value": value, "data": "0x" + calldata.hex(), "chainId": w3.eth.chain_id}
    gas = w3.eth.estimate_gas(t)            # ここで失敗＝このまま送っても失敗する取引
    t["gas"] = int(gas * 1.2)
    fill_fees(w3, t)
    t["nonce"] = w3.eth.get_transaction_count(me, "pending")
    return t, name


# ---------------- ボット本体 ----------------
class Trader:
    def __init__(self):
        self.st = load_state()
        self.st.setdefault("seen", {})          # 判定済みの出品（order_hash → 時刻）
        self.st.setdefault("bought", [])        # 購入履歴
        self.st.setdefault("positions", {})     # 保有中（token_id → 情報）
        self.acct = None
        if PK:
            from eth_account import Account
            self.acct = Account.from_key(PK)
        self.w3 = connect()
        self.chain = "monad"
        self.key = api_key(self.st)
        self.slug = find_slug(self.st, self.key, self.chain)
        self.dust_px = self.mon_px = None
        self.px_ts = self.full_ts = self.sell_ts = 0
        self.last_event_ts = int(time.time()) - 120
        self.errors = 0

    # ---- 価格 ----
    def refresh_prices(self, force=False):
        if force or time.time() - self.px_ts > PRICE_SECONDS:
            self.dust_px = dexscreener_price(DUST)
            self.mon_px = mon_price(self.w3)
            self.px_ts = time.time()

    def usd(self, r):
        if r["currency"] in MON_SYMBOLS:
            return r["amount"] * self.mon_px
        if r["currency"] in STABLES:
            return r["amount"]
        return None

    # ---- 購入 ----
    def evaluate(self, r, force=False):
        price_usd = self.usd(r)
        if price_usd is None:
            return None
        info = lock_info(self.st, self.w3, r["token_id"], force=force)
        if info is None or info[0] <= 0:
            return None
        amt, status, days = info
        disc = (1 - price_usd / (amt * self.dust_px)) * 100
        return {**r, "price_usd": price_usd, "dust": amt, "status": status, "days": days, "discount": disc}

    def consider(self, listings):
        """出品の一覧から、買う条件を満たすものを探して処理する"""
        me = self.acct.address.lower() if self.acct else ""
        cands = []
        for r in listings:
            if r["order_hash"] in self.st["seen"] or r["offerer"] == me:
                continue
            e = self.evaluate(r)
            if e and e["discount"] >= BUY_MIN_DISCOUNT_PCT:
                cands.append(e)
        for e in sorted(cands, key=lambda x: -x["discount"]):
            self.try_buy(e)

    def try_buy(self, r):
        t = now()
        self.st["seen"][r["order_hash"]] = t.isoformat()
        reasons = []
        if r["currency"] not in MON_SYMBOLS:
            reasons.append(f"MON以外の通貨（{r['currency']}）")
        if BUY_MAX_USD is not None and r["price_usd"] > BUY_MAX_USD:
            reasons.append(f"上限${BUY_MAX_USD:.0f}超え（${r['price_usd']:,.0f}）")
        if BUY_PERM_ONLY and r["status"] != "perm":
            reasons.append("無期限ロックではない")
        if r["end"] and r["end"] < time.time() + 30:
            reasons.append("出品の期限切れ間近")
        if reasons:
            log(f"#{r['token_id']}（割引{r['discount']:+.1f}%）は購入対象外: {'、'.join(reasons)}")
            return
        today = t.strftime("%Y-%m-%d")
        n_today = sum(1 for b in self.st["bought"] if b.get("day") == today and b.get("mode") == MODE)
        label = "🧪 ドライラン" if MODE == "dry" else "🛒 自動購入"
        url = f"https://opensea.io/assets/{self.chain}/{DUST_LOCK}/{r['token_id']}"
        if BUY_MAX_PER_DAY is not None and n_today >= BUY_MAX_PER_DAY:
            notify(f"⏸ {label}：本日の上限（{BUY_MAX_PER_DAY}件）に達したため見送り  #{r['token_id']} 割引{r['discount']:+.1f}%\n{url}")
            return

        # 購入直前にロック量を最新の値で確認し直す
        e = self.evaluate(r, force=True)
        if e is None or e["discount"] < BUY_MIN_DISCOUNT_PCT or (BUY_PERM_ONLY and e["status"] != "perm"):
            notify(f"{label}：#{r['token_id']} は最新の確認で条件外になったため見送り\n{url}")
            return
        r = e
        head = (f"#{r['token_id']}  割引 {r['discount']:+.1f}%  価格 {r['amount']:,.4g} MON（${r['price_usd']:,.2f}）"
                f"  ロック {r['dust']:,.2f} DUST（${r['dust'] * self.dust_px:,.2f}）")
        if not self.acct:
            notify(f"{label}：買う条件を満たした出品です（秘密鍵が未設定のためシミュレーションなし）\n```\n{head}\n```\n{url}")
            return
        try:
            bal = self.w3.eth.get_balance(self.acct.address) / 1e18
            if bal < r["value_raw"] / 1e18 + GAS_RESERVE_MON:
                raise RuntimeError(f"ウォレットの残高不足（{bal:,.4g} MON。価格＋ガス用{GAS_RESERVE_MON:g} MONが必要）")
            tx, fname = prepare_buy(self.w3, self.key, self.chain, r, self.acct)
        except Exception as ex:
            notify(f"{label}：#{r['token_id']} の購入準備に失敗したため見送り\n```\n{head}\n{str(ex)[:600]}\n```\n{url}")
            return
        rec = {"day": today, "mode": MODE, "token_id": r["token_id"], "order_hash": r["order_hash"],
               "price_mon": r["amount"], "price_usd": round(r["price_usd"], 4), "ts": t.isoformat()}
        if MODE == "dry":
            gas_mon = tx["gas"] * tx["maxFeePerGas"] / 1e18
            self.st["bought"].append({**rec, "result": "simulated"})
            self.open_position(r, virtual=True)
            notify(f"{label}：シミュレーション成功。本番ならこの出品を購入していました\n```\n{head}\n"
                   f"方法 {fname}  ガス上限 約{gas_mon:.4g} MON\n```\n{url}")
            return

        # ---- 本番：ここから先は取り消せない ----
        try:
            signed = self.acct.sign_transaction(tx)
            raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
            h = self.w3.eth.send_raw_transaction(raw)
            hx = "0x" + bytes(h).hex().removeprefix("0x")
            self.st["bought"].append({**rec, "tx": hx, "result": "sent"})
            save_state(self.st)                         # 途中で止まっても件数を数え漏らさない
            receipt = self.w3.eth.wait_for_transaction_receipt(h, timeout=120)
            ok = receipt.get("status") == 1
            got = False
            try:
                got = owner_of(self.w3, r["token_id"]).lower() == self.acct.address.lower()
            except Exception:
                pass
            self.st["bought"][-1]["result"] = "ok" if ok and got else ("tx_ok" if ok else "failed")
            if ok:
                self.open_position(r, virtual=False, tx=hx)
            mark = "✅ 購入完了" if ok and got else ("⚠ 取引は成功、NFTの受取は未確認" if ok else "❌ 購入失敗（取引が取り消されました。ガス代のみ消費）")
            notify(f"{mark}\n```\n{head}\n```\n{url}\nhttps://monadscan.com/tx/{hx}")
        except Exception as ex:
            notify(f"❌ 自動購入：#{r['token_id']} の送信中にエラー\n```\n{str(ex)[:600]}\n```\n{url}")

    # ---- 転売 ----
    def open_position(self, r, virtual, tx=None):
        self.st["positions"][str(r["token_id"])] = {
            "token_id": r["token_id"], "virtual": virtual, "buy_mon": r["amount"],
            "buy_usd": r["price_usd"], "buy_ts": time.time(), "buy_tx": tx,
            "protocol_address": r["protocol_address"] or SEAPORT_DEFAULT, "listings": []}

    def fee_rate(self):
        return sum(f["bps"] for f in collection_fees(self.st, self.key, self.slug)) / 10000

    def target_usd(self, pos):
        """今出すべき価格（USD）と、その内訳"""
        info = lock_info(self.st, self.w3, pos["token_id"])
        if info is None:
            return None
        value = info[0] * self.dust_px
        steps = int((time.time() - pos["buy_ts"]) // (SELL_STEP_HOURS * 3600))
        disc = SELL_START_DISC_PCT + SELL_STEP_PCT * steps
        ladder = value * (1 - disc / 100)
        floor = pos["buy_usd"] * (1 + SELL_FLOOR_PCT / 100)
        if SELL_FLOOR_BASIS == "net":
            floor = floor / (1 - self.fee_rate())
        price = max(ladder, floor)
        return {"usd": price, "value": value, "disc": (1 - price / value) * 100, "at_floor": ladder <= floor,
                "floor": floor, "step": steps}

    def review_positions(self):
        for tid, pos in list(self.st["positions"].items()):
            try:
                self.review_one(pos)
            except Exception as ex:
                log(f"#{tid} の出品見直しでエラー: {ex}")

    def review_one(self, pos):
        tid = pos["token_id"]
        url = f"https://opensea.io/assets/{self.chain}/{DUST_LOCK}/{tid}"
        if pos["virtual"]:
            if time.time() - pos["buy_ts"] > 7 * 86400:
                del self.st["positions"][str(tid)]
                return
        else:
            owner = owner_of(self.w3, tid)
            if owner.lower() != self.acct.address.lower() and not pos["listings"]:
                notify(f"⚠ #{tid} はウォレットにありません（出品前）。転売の管理から外します\n{url}")
                del self.st["positions"][str(tid)]
                return
            if owner.lower() != self.acct.address.lower():
                last = pos["listings"][-1] if pos["listings"] else None
                sold = f"直近の出品 {last['price_mon']:,.4g} MON（約${last['usd']:,.2f}）" if last else "出品記録なし"
                notify(f"💰 売却を確認：#{tid}（購入 {pos['buy_mon']:,.4g} MON・${pos['buy_usd']:,.2f}）\n{sold}\n"
                       f"手数料・ロイヤリティ約{self.fee_rate() * 100:.1f}%差し引き後が手取りです\n{url}")
                del self.st["positions"][str(tid)]
                return
        tg = self.target_usd(pos)
        if tg is None:
            return
        want_mon = tg["usd"] / self.mon_px
        cur = pos["listings"][-1] if pos["listings"] else None
        need_cancel = False
        if cur:
            below_floor = cur["price_mon"] * self.mon_px < tg["floor"] * 0.99      # MON安で下限割れ
            if below_floor or want_mon > cur["price_mon"] * (1 + SELL_RAISE_PCT / 100):
                need_cancel = True                              # 値上げ：古い安い出品を取り消してから出し直す
            elif tg["step"] > cur.get("step", -1) and want_mon < cur["price_mon"] * 0.995:
                pass                                            # 値下げの時刻：新しい出品を出すだけ（古い高い出品は残っても害はない）
            else:
                return
        tag = "（下限）" if tg["at_floor"] else f"（値下げ{tg['step']}回目）" if tg["step"] else "（初回）"
        msg = (f"#{tid}  {want_mon:,.4g} MON（${tg['usd']:,.2f}・割引{tg['disc']:.1f}%）{tag}\n"
               f"ロック価値 ${tg['value']:,.2f} ／ 購入 ${pos['buy_usd']:,.2f} ／ 下限 ${tg['floor']:,.2f}")
        if pos["virtual"] or MODE != "live" or not SELL_ENABLED:
            if need_cancel:
                pos["listings"] = []
            pos["listings"].append({"price_mon": want_mon, "usd": tg["usd"], "ts": time.time(),
                                    "step": tg["step"], "virtual": True})
            notify(f"🧪 ドライラン：本番ならこの価格で出品します{'（古い出品を取り消して出し直し）' if need_cancel else ''}\n```\n{msg}\n```")
            return
        if need_cancel:
            self.cancel_listings(pos)
        self.create_listing(pos, want_mon, tg["usd"], tg["step"])
        notify(f"🏷 出品しました\n```\n{msg}\n```\n{url}")

    def learn_market(self, listings):
        """Monad上の実際の出品から、使われている conduitKey と Seaport の契約アドレスを覚える"""
        keys, seas = {}, {}
        for r in listings:
            if r.get("conduit_key"):
                keys[r["conduit_key"]] = keys.get(r["conduit_key"], 0) + 1
            if r.get("protocol_address"):
                seas[r["protocol_address"].lower()] = seas.get(r["protocol_address"].lower(), 0) + 1
        if keys:
            self.st["conduit_key"] = max(keys, key=keys.get)
        if seas:
            self.st["seaport"] = Web3.to_checksum_address(max(seas, key=seas.get))

    def conduit(self):
        """(conduitKey, 承認を与える先のアドレス)。conduitKey が 0 なら Seaport 自身に承認する"""
        key = self.st.get("conduit_key") or ZERO32
        seaport = self.st.get("seaport") or SEAPORT_DEFAULT
        if int(key, 16) == 0:
            return ZERO32, Web3.to_checksum_address(seaport)
        raw = call_raw(self.w3, CONDUIT_CONTROLLER, "getConduit(bytes32)", ["bytes32"], [hexb(key)])
        addr = Web3.to_checksum_address("0x" + raw[12:32].hex())
        exists = int.from_bytes(raw[32:64], "big") == 1
        if not exists:
            raise RuntimeError(f"出品に使うコンジットが見つかりません（key {key}）")
        return key, addr

    def ensure_approval(self):
        _, operator = self.conduit()
        ok = call_raw(self.w3, DUST_LOCK, "isApprovedForAll(address,address)", ["address", "address"],
                      [self.acct.address, operator])
        if int.from_bytes(ok[-32:], "big") == 1:
            return
        from eth_abi import encode
        data = bytes(Web3.keccak(text="setApprovalForAll(address,bool)"))[:4] + encode(["address", "bool"], [operator, True])
        hx, success = send_tx(self.w3, self.acct, {"to": Web3.to_checksum_address(DUST_LOCK), "data": "0x" + data.hex()})
        if not success:
            raise RuntimeError(f"OpenSeaへの出品許可（承認）に失敗: {hx}")
        notify(f"🔑 OpenSeaで出品できるよう veDUST の承認を行いました\nhttps://monadscan.com/tx/{hx}")

    def create_listing(self, pos, price_mon, usd, step):
        self.ensure_approval()
        seaport = Web3.to_checksum_address(pos["protocol_address"])
        total = int(price_mon * 1e18)
        fees = collection_fees(self.st, self.key, self.slug)
        cons, fee_sum = [], 0
        for f in fees:
            amt = total * f["bps"] // 10000
            fee_sum += amt
            cons.append({"itemType": 0, "token": ZERO_ADDR, "identifierOrCriteria": "0",
                         "startAmount": str(amt), "endAmount": str(amt), "recipient": f["recipient"]})
        cons.insert(0, {"itemType": 0, "token": ZERO_ADDR, "identifierOrCriteria": "0",
                        "startAmount": str(total - fee_sum), "endAmount": str(total - fee_sum),
                        "recipient": self.acct.address})
        counter = int.from_bytes(call_raw(self.w3, seaport, "getCounter(address)", ["address"], [self.acct.address])[:32], "big")
        start = int(time.time()) - 60
        comp = {"offerer": self.acct.address, "zone": ZERO_ADDR,
                "offer": [{"itemType": 2, "token": Web3.to_checksum_address(DUST_LOCK),
                           "identifierOrCriteria": str(pos["token_id"]), "startAmount": "1", "endAmount": "1"}],
                "consideration": cons, "orderType": 0, "startTime": str(start),
                "endTime": str(start + SELL_LISTING_DAYS * 86400), "zoneHash": ZERO32,
                "salt": str(random.getrandbits(128)), "conduitKey": self.conduit()[0], "counter": str(counter)}
        sig = self.sign_order(seaport, comp)
        params = {k: v for k, v in comp.items() if k != "counter"}
        params["totalOriginalConsiderationItems"] = len(cons)
        params["counter"] = comp["counter"]
        d = os_req("POST", f"/orders/{self.chain}/seaport/listings", self.key,
                   body={"parameters": params, "signature": sig, "protocol_address": seaport})
        h = ((d.get("order") or {}).get("order_hash")) or d.get("order_hash")
        pos["listings"].append({"price_mon": price_mon, "usd": usd, "ts": time.time(), "order_hash": h, "step": step,
                                "components": comp})
        save_state(self.st)

    def sign_order(self, seaport, comp):
        types = {
            "EIP712Domain": [{"name": "name", "type": "string"}, {"name": "version", "type": "string"},
                             {"name": "chainId", "type": "uint256"}, {"name": "verifyingContract", "type": "address"}],
            "OrderComponents": [{"name": n, "type": "OfferItem[]" if n == "offer" else "ConsiderationItem[]"
                                 if n == "consideration" else t} for n, t in ORDER_COMPONENTS],
            "OfferItem": [{"name": n, "type": t} for n, t in OFFER_ITEM],
            "ConsiderationItem": [{"name": n, "type": t} for n, t in CONS_ITEM],
        }
        msg = abi_dict(("tuple", ORDER_COMPONENTS), comp)
        full = {"types": types, "primaryType": "OrderComponents",
                "domain": {"name": "Seaport", "version": "1.6", "chainId": self.w3.eth.chain_id,
                           "verifyingContract": seaport},
                "message": msg}
        from eth_account import Account
        from eth_account.messages import encode_typed_data
        signed = Account.sign_message(encode_typed_data(full_message=full), PK)
        return "0x" + bytes(signed.signature).hex().removeprefix("0x")

    def cancel_listings(self, pos):
        comps = [L["components"] for L in pos["listings"] if L.get("components")]
        if not comps:
            return
        seaport = Web3.to_checksum_address(pos["protocol_address"])
        _, data = encode_call("cancel", {"orders": comps})
        hx, ok = send_tx(self.w3, self.acct, {"to": seaport, "data": "0x" + data.hex()})
        if not ok:
            raise RuntimeError(f"出品の取り消しに失敗: {hx}")
        pos["listings"] = [L for L in pos["listings"] if not L.get("components")]
        log(f"#{pos['token_id']} の古い出品を取り消しました {hx}")

    def preflight(self):
        """起動時の事前確認（取引に使う契約がMonad上にあるか、手数料、承認の状態）"""
        lines = []
        listings = all_listings(self.key, self.slug)
        self.learn_market(listings)
        lines.append(f"OpenSeaの出品 {len(listings)}件を読めました")
        try:
            ev = recent_listings(self.key, self.slug, time.time() - 7 * 86400)
            lines.append(f"新着イベント 読める（直近7日 {len(ev)}件）")
        except Exception as ex:
            lines.append(f"新着イベント ★読めない（3分ごとの全件確認で代用）: {str(ex)[:120]}")
        seaport = self.st.get("seaport") or SEAPORT_DEFAULT
        ok = len(self.w3.eth.get_code(Web3.to_checksum_address(seaport))) > 0
        lines.append(f"Seaport {seaport[:10]}… {'あり' if ok else '★見つからない'}")
        try:
            key, operator = self.conduit()
            lines.append(f"出品のコンジット key {key[:12]}… → {operator[:10]}…")
        except Exception as ex:
            operator = None
            lines.append(f"出品のコンジット ★確認できない: {str(ex)[:150]}")
        fees = collection_fees(self.st, self.key, self.slug)
        lines.append("出品手数料 " + " + ".join(f"{f['bps'] / 100:g}%" for f in fees) + f"（計{self.fee_rate() * 100:g}%）")
        if self.acct and operator:
            ok = call_raw(self.w3, DUST_LOCK, "isApprovedForAll(address,address)", ["address", "address"],
                          [self.acct.address, operator])
            lines.append("出品の承認 " + ("済み" if int.from_bytes(ok[-32:], "big") == 1 else "未（初回の出品時に自動で行います）"))
        if TEST_MIN_DISCOUNT and MODE == "dry":
            lines.append(f"★テスト中：割引{BUY_MIN_DISCOUNT_PCT:g}%以上で購入判定（ドライランのみ）")
        notify("🔍 事前確認\n```\n" + "\n".join(lines) + "\n```")

    # ---- ループ ----
    def tick(self):
        self.refresh_prices()
        nowts = time.time()
        if nowts - self.full_ts > FULL_SECONDS:
            listings = all_listings(self.key, self.slug)
            self.learn_market(listings)
            self.full_ts = nowts
        else:
            try:
                listings = recent_listings(self.key, self.slug, self.last_event_ts - 30)
            except Exception as ex:
                if not getattr(self, "events_warned", False):
                    log(f"新着イベントが取得できないため、全出品の確認だけで動きます: {ex}")
                    self.events_warned = True
                listings = []
            if listings:
                self.last_event_ts = max(self.last_event_ts, max(p.get("ts", 0) for p in listings))
        self.consider(listings)
        if nowts - self.sell_ts > SELL_SECONDS:
            self.review_positions()
            self.sell_ts = nowts
            cutoff = now() - timedelta(days=30)
            self.st["seen"] = {h: ts for h, ts in self.st["seen"].items() if datetime.fromisoformat(ts) > cutoff}
            self.st["bought"] = self.st["bought"][-300:]
            self.key = api_key(self.st)
        save_state(self.st)

    def run(self):
        who = self.acct.address if self.acct else "（秘密鍵なし）"
        bal = f"{self.w3.eth.get_balance(self.acct.address) / 1e18:,.4g} MON" if self.acct else "-"
        notify(f"▶ veDUST自動売買ボット {VERSION} 起動  モード：{MODE}\nウォレット {who}（残高 {bal}）\n"
               f"購入：割引{BUY_MIN_DISCOUNT_PCT:g}%以上"
               f"・{'1件$' + format(BUY_MAX_USD, 'g') + 'まで' if BUY_MAX_USD is not None else '金額上限なし'}"
               f"・{'1日' + str(BUY_MAX_PER_DAY) + '件まで' if BUY_MAX_PER_DAY is not None else '件数上限なし（残高の範囲）'}"
               f"{'・無期限のみ' if BUY_PERM_ONLY else ''}\n"
               f"出品：初回割引{SELL_START_DISC_PCT:g}% → {SELL_STEP_HOURS}時間ごとに{SELL_STEP_PCT:g}pt値下げ → "
               f"{'手取り' if SELL_FLOOR_BASIS == 'net' else '出品価格'}が購入価格{SELL_FLOOR_PCT:+g}%になる価格で停止")
        try:
            self.preflight()
        except Exception as ex:
            notify(f"⚠ 事前確認でエラー: {str(ex)[:500]}")
        if MODE == "off":
            while True:
                time.sleep(3600)
        while True:
            try:
                self.tick()
                self.errors = 0
            except Exception:
                self.errors += 1
                tb = traceback.format_exc()
                log(tb)
                if self.errors in (10, 100) or self.errors % 1000 == 0:
                    notify(f"⚠ 自動売買ボット：{self.errors}回連続でエラー\n```\n{tb[-1200:]}\n```")
                time.sleep(min(60, 5 * self.errors))
                try:
                    self.w3 = connect()
                except Exception:
                    pass
            time.sleep(FAST_SECONDS)


def abi_dict(t, v):
    """署名用に、数値は int、アドレスはチェックサム、bytes は bytes に揃える（名前付きのまま）"""
    if not isinstance(t, str):
        kind, comps = t
        if kind == "tuple[]":
            return [abi_dict(("tuple", comps), x) for x in v]
        return {n: abi_dict(ct, v[n]) for n, ct in comps}
    return abi_value(t, v)


if __name__ == "__main__":
    Trader().run()
