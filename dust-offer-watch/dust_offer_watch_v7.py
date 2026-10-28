# =====================================================================
# veDUST 買いオファー ボット  v7   （Railway などで常時実行）
#   v6からの変更点
#   - 試運転の終了用：OFFER_MODE=off で起動すると、ウォレットのWMONを全部MONに戻す
#     （WMONが無くなるので、OpenSeaに残っているオファーは受けられても支払えず、実質すべて無効になる）
#     その後は観察（成約・出品・競合の記録と朝9時のレポート）だけを続ける
#   v5からの変更点
#   - 再起動のたびに記録（出したオファー・前日の集計）が消え、オファーを出し直したり、朝9時のレポートが
#     出なかったりしたのを直した
#     ・起動通知に「記録の保存先が消えない場所（ボリューム）か」「前回の記録を読めたか」を出す
#     ・記録が無くても、OpenSeaに出ている自分のオファーを読んで「出し済み」として引き継ぐ（二重に出さない）
#     ・朝9時のレポートは、前日の記録が無くても、その時点の様子を送る
#   - 予算の安全策：MON→WMONの自動の両替は、記録で「まだ両替していない」と分かるときだけ行う
#     （記録が消えても、予算以上に両替しない。使えるのはウォレットにあるWMONの範囲まで）
#   - DUST価格：DexScreener がだめなら GeckoTerminal から取る（売買ボット v10 と同じ）。
#     60分以上価格が取れないときは、新しいオファーを出さず、1回だけ通知する
#   v4からの変更点（v5）
#   - OpenSea の回数制限（429）で1件に2分ずつ待たされていたので、出し方を変えた
#     ・オファーは「出す予定の列」に並べ、30秒ごとの確認のたびに少しずつ出す（その間も受けられたかの確認・安全装置は動く）
#     ・制限にかかったら、その1件は列の後ろに戻し、OpenSeaが指定する時間（最低1分・続くたびに倍、最大10分）だけ休む
#     ・出せたオファーも1件ずつ記録する。25件ごとに途中経過を通知する
#   v3からの変更点（v4）
#   - OpenSea に「Bids at this price are not allowed」（この価格の桁ではオファーできない）と断られたので、
#     オファー額を整数のMON（小数点以下切り捨て）にした（OpenSeaはオファー額の小数の桁数を制限している）
#   - オファーの失敗理由を、切らずに全文 Discord に出す
#   - 前回のオファーが1件も出せなかったときは、24時間待たずに起動時（とその後1時間ごと）に出し直す
#   v2からの変更点（v3）
#   - 観察に加えて、実際に OpenSea で個別の買いオファーを出せるようにした（少額の本番試験用）
#     ・環境変数 OFFER_MODE で切り替え：off（出さない）/ dry（既定・出す予定を通知するだけ）/ live（本当に出す）
#     ・予算 OFFER_BUDGET_USD（既定$300）。ボット用ウォレットのMONをその分だけWMONに替え、それを上限に買う
#       （買えた合計が予算に達したら、それ以上は替えない＝予算以上は使わない）
#     ・ロック価値の大きい順に最大100件、ロック価値の45%引きでオファー。期限26時間、24時間ごとに出し直す
#     ・DUSTがオファーを出した時より10%以上下がったら、WMONをMONに戻して全オファーを無効にする（安全装置）
#     ・オファーを受けてもらえたら Discord に通知し、offers_won.csv に記録する
#     ・自分のオファーは競合の集計から外す
#   - 秘密鍵（AUTOBUY_PRIVATE_KEY）は、OFFER_MODE が dry / live のときだけ使う。veDUST売買ボットと同じウォレット
#   v1からの変更点（v2）
#   - v1の起動確認で「特定の1件向けのオファーが1000件（読み取り上限）」と出たので、競合を詳しく見る
#     ・読み取り上限を1000件→6000件に
#     ・買い手ごとに「何件・合計いくら・何%引きくらいで・期限はどれくらい」で出しているかを表示
#     ・自分のオファー対象のうち、すでに45%引きより高いオファーが付いているものを数える
#     ・今出ているオファーの一覧を offers_now.csv に保存する
#   「ロック量の大きい veDUST に、ロック価値の45%引きで個別の買いオファーを出したら
#    どうなるか」を、オファーを出さずに記録する。この数字を見て、本番の条件を詰める
#
#   見ているもの（秘密鍵は使わない）
#     1. 全 veDUST のロック量・無期限か・持ち主（Monad のコントラクトを直接読む。1日1回）
#        → 「オファーを出す対象」と「出すならいくら」の一覧を作る
#     2. OpenSea の veDUST の成約・新規出品（5分ごと）
#        → 45%引き以上で手放した人＝オファーを受けてくれた可能性が高い人、として数える
#     3. OpenSea の veDUST への買いオファー（1時間ごと）
#        → 同じことをしている買い手（競合）がいるか、オファーの通貨（WMONか）を確認する
#
#   割引率 = 1 − 価格(USD) ÷（ロック中のDUST数量 × DUST価格(USD)）   ※売買ボットと同じ計算
#
#   記録・通知すること
#     - 45%引き以上の成約があったら、その場で Discord に通知（観察のみ）
#     - 毎朝9時（日本時間）に、前日の集計と「オファー対象の一覧」の要約を送る
#     - すべての記録を STATE_DIR に CSV で残す
#
#   必要な環境変数（Railway の Variables に入れる）
#     DISCORD_ALERT_WEBHOOK_URL  通知先（veDUSTボットと同じでよい）
#     STATE_DIR                  記録の保存先。Railway ではボリュームの場所（例 /data）
#     AUTOBUY_PRIVATE_KEY        オファーを出すとき必要。veDUST売買ボットと同じ自動売買専用ウォレットの秘密鍵
#     OFFER_MODE                 off / dry / live（未設定なら dry）
#     OFFER_BUDGET_USD           任意。本番試験の予算（ドル）。未設定なら300
#     SKIP_OWNERS                任意。オファーの対象から外すウォレットのアドレス（カンマ区切り）
#                                ボット用ウォレット・自分の本体ウォレットを入れる（秘密鍵ではなくアドレス）
#     MONAD_RPC_URL / OPENSEA_API_KEY   任意。あれば優先して使う
# =====================================================================
import os, sys, json, time, csv, random, traceback
from datetime import datetime, timezone, timedelta
import requests
from web3 import Web3
from eth_abi import encode, decode

VERSION = "dust offer watch v7"

# ---------------- オファーの条件（試算に使う） ----------------
OFFER_DISCOUNT_PCT = 45.0      # ロック価値の何%引きでオファーを出すか
MIN_VALUE_USD      = 10.0      # ロック価値がこの金額（ドル）以上の veDUST だけを対象にする
PERM_ONLY          = True      # 無期限ロックだけを対象にする

# ---------------- 本番のオファー ----------------
MAX_OFFERS         = 100       # 一度に出すオファーの最大件数（ロック価値の大きい順）
OFFER_EVERY_HOURS  = 24        # オファーを出し直す間隔
OFFER_EXPIRE_HOURS = 26        # オファーの有効期限
KILL_DROP_PCT      = 10.0      # DUST（MON建て）がオファー時よりこの%以上下がったら、WMONをMONに戻して全部無効にする
GAS_RESERVE_MON    = 1.0       # ガス代用に残しておくMON
FILL_SECONDS       = 120       # オファーが受けられたかの確認間隔
POST_GAP_SECONDS   = 4         # オファーを1件出すごとの間隔
POST_TICK_SECONDS  = 20        # 30秒ごとの確認のうち、オファーを出すのに使う時間
SUMMARY_HOUR_JST   = 9         # 毎日の集計を送る時刻

# ---------------- 動作の設定 ----------------
SCAN_HOURS     = 24            # 全 veDUST を読み直す間隔
EVENT_SECONDS  = 300           # 成約・出品の確認間隔
OFFERS_SECONDS = 3600          # 他の買い手のオファーの確認間隔
PRICE_SECONDS  = 300           # DUST・MON価格の更新間隔
BATCH_IDS      = 50            # まとめて読む veDUST の数（1回あたり）
OFFER_PAGES    = 60            # 他の買い手のオファーを読むページ数（1ページ100件）

DUST_LOCK   = "0xBB4738D05AD1b3Da57a4881baE62Ce9bb1eEeD6C"   # veDUST（DustLock Proxy）
DUST        = "0xAD96C3dffCD6374294e2573A7fBBA96097CC8d7c"
WMON        = "0x3bd359C1119dA7Da1D913D1C4D2B7c461115433A"
AAVE_ORACLE = "0x94bbA11004B9877d13bb5E1aE29319b6f7bDEdD4"
MULTICALL   = "0xcA11bde05977b3631167028862bE2a173976CA11"   # Multicall3（まとめて読む）
SEAPORT_DEFAULT = "0x0000000000000068F116a894984e2DB1123eB395"   # Seaport 1.6
ZERO_ADDR   = "0x0000000000000000000000000000000000000000"
ZERO32      = "0x" + "00" * 32
RPC_URLS = [u for u in [os.environ.get("MONAD_RPC_URL", "").strip(),
                        "https://rpc.monad.xyz", "https://rpc1.monad.xyz", "https://monad.drpc.org"] if u]
OS_API      = "https://api.opensea.io/api/v2"
STABLES     = {"USDC", "USDT", "USDT0", "AUSD", "USDC.E"}
MON_SYMBOLS = {"MON", "WMON"}
POWER_SIGS  = ("balanceOfNFT(uint256)", "votingPower(uint256)", "getVotingPower(uint256)")
JST         = timezone(timedelta(hours=9))

WEBHOOK       = os.environ.get("DISCORD_ALERT_WEBHOOK_URL", "").strip()
OS_KEY_SECRET = os.environ.get("OPENSEA_API_KEY", "").strip()
PK            = os.environ.get("AUTOBUY_PRIVATE_KEY", "").strip()
OFFER_MODE    = (os.environ.get("OFFER_MODE", "").strip().lower() or "dry")
if OFFER_MODE not in ("off", "dry", "live"):
    OFFER_MODE = "dry"
try:
    OFFER_BUDGET_USD = float(os.environ.get("OFFER_BUDGET_USD", "").strip() or 300)
except ValueError:
    OFFER_BUDGET_USD = 300.0
SKIP_OWNERS   = {a.strip().lower() for a in os.environ.get("SKIP_OWNERS", "").split(",") if a.strip()}
STATE_DIR     = os.environ.get("STATE_DIR", "").strip() or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "state")
STATE_PATH    = os.path.join(STATE_DIR, "dust_offer_watch.json")
os.makedirs(STATE_DIR, exist_ok=True)


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
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, STATE_PATH)


def append_csv(name, row):
    path = os.path.join(STATE_DIR, name)
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        if new:
            w.writeheader()
        w.writerow(row)


def write_csv(name, rows):
    if not rows:
        return
    path = os.path.join(STATE_DIR, name)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def cs(a):
    return Web3.to_checksum_address(a)


def addr_of(word):
    return "0x" + word.to_bytes(32, "big")[-20:].hex()


def words(raw):
    return [int.from_bytes(raw[i:i + 32], "big") for i in range(0, len(raw) - len(raw) % 32, 32)]


def calldata(sig, types=(), values=()):
    return bytes(Web3.keccak(text=sig))[:4] + encode(list(types), list(values))


# ---------------- チェーンの読み取り ----------------
def connect():
    last = None
    for url in RPC_URLS:
        try:
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 30}))
            w3.eth.chain_id
            log("RPC:", url)
            return w3
        except Exception as e:
            last = e
    raise RuntimeError(f"RPCに接続できませんでした: {last}")


def call(w3, to, sig, types=(), values=()):
    return bytes(w3.eth.call({"to": cs(to), "data": "0x" + calldata(sig, types, values).hex()}))


def multicall(w3, calls):
    """[(宛先, 呼び出しデータ)] を1回でまとめて読む。失敗した呼び出しは None"""
    data = calldata("aggregate3((address,bool,bytes)[])", ["(address,bool,bytes)[]"],
                    [[(cs(t), True, d) for t, d in calls]])
    for attempt in range(4):
        try:
            raw = bytes(w3.eth.call({"to": cs(MULTICALL), "data": "0x" + data.hex()}))
            break
        except Exception:
            if attempt == 3:
                raise
            time.sleep(2 + attempt * 3)
    return [bytes(r) if ok else None for ok, r in decode(["(bool,bytes)[]"], raw)[0]]


def parse_lock(raw_locked, raw_power, nowts):
    """locked(id) と議決権から (DUST数量, 状態, 残り日数)。状態は 'perm' / 'days' / 'expired' / 'unknown'
       判定は売買ボット v9 と同じ（未来の解除日時があれば期限付き、議決権≒数量なら無期限）"""
    w = words(raw_locked)
    if len(w) < 2:
        return None
    amt = (w[0] - (1 << 256) if w[0] >= (1 << 255) else w[0]) / 1e18
    future = [x for x in w[1:] if nowts + 3600 < x < 4_000_000_000]
    if future:
        return amt, "days", (max(future) - nowts) / 86400
    flag = w[-1] if len(w) >= 3 and w[-1] in (0, 1) else None
    if not raw_power:
        return amt, "unknown", None
    power = words(raw_power)[0] / 1e18
    if power == 0:
        return amt, "expired", None
    if amt > 0 and power >= amt * 0.9999 and flag != 0:
        return amt, "perm", None
    return amt, "unknown", None


def max_token_id(w3):
    """一番新しい veDUST の番号。tokenId() が無ければ、持ち主のいる番号を探って調べる"""
    for sig in ("tokenId()", "totalNftsMinted()"):
        try:
            w = words(call(w3, DUST_LOCK, sig))
            if w and 0 < w[0] < 10_000_000:
                return w[0]
        except Exception:
            continue
    last, step, empty = 0, 2000, 0
    for start in range(1, 10_000_000, step):
        probe = list(range(start, start + step, 20))
        raws = multicall(w3, [(DUST_LOCK, calldata("ownerOf(uint256)", ["uint256"], [i])) for i in probe])
        hits = [i for i, r in zip(probe, raws) if r]
        if hits:
            last, empty = max(hits) + 19, 0
        else:
            empty += 1
            if empty >= 3 and last:
                return last
    return last


def find_power_sig(w3, token_id):
    for sig in POWER_SIGS:
        try:
            if words(call(w3, DUST_LOCK, sig, ["uint256"], [token_id])):
                return sig
        except Exception:
            continue
    return None


def is_contracts(w3, addrs):
    out = {}
    for a in addrs:
        for attempt in range(3):
            try:
                out[a] = len(bytes(w3.eth.get_code(cs(a)))) > 0
                break
            except Exception:
                time.sleep(1 + attempt * 2)
    return out


# ---------------- 価格 ----------------
def dexscreener_price(token):
    r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{token}", headers=HTTP_HEADERS, timeout=20)
    r.raise_for_status()
    pairs = [p for p in (r.json().get("pairs") or []) if "monad" in str(p.get("chainId", "")).lower()
             and str(p.get("baseToken", {}).get("address", "")).lower() == token.lower()]
    if not pairs:
        raise RuntimeError(f"DexScreenerに価格なし: {token}")
    return float(max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))["priceUsd"])


HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (dust-offer-watch)", "accept": "application/json"}
GT_NETWORKS = ["monad", "monad-mainnet"]


def geckoterminal_price(token):
    last = None
    for net in list(GT_NETWORKS):
        try:
            r = requests.get(f"https://api.geckoterminal.com/api/v2/simple/networks/{net}/token_price/{token}",
                             headers=HTTP_HEADERS, timeout=20)
            r.raise_for_status()
            prices = ((r.json().get("data") or {}).get("attributes") or {}).get("token_prices") or {}
            px = float(prices.get(token.lower()) or 0)
            if px > 0:
                if GT_NETWORKS[0] != net:
                    GT_NETWORKS.remove(net)
                    GT_NETWORKS.insert(0, net)
                return px
            last = f"{net}: 価格なし"
        except Exception as e:
            last = f"{net}: {str(e)[:120]}"
    raise RuntimeError(f"GeckoTerminalに価格なし（{last}）")


def token_price(token):
    """DexScreener → GeckoTerminal の順に試す"""
    try:
        return dexscreener_price(token)
    except Exception as e1:
        try:
            return geckoterminal_price(token)
        except Exception as e2:
            raise RuntimeError(f"DexScreener: {str(e1)[:150]} ／ {str(e2)[:150]}")


def mon_price(w3):
    try:
        w = words(call(w3, AAVE_ORACLE, "getAssetPrice(address)", ["address"], [cs(WMON)]))
        if w and w[0] > 0:
            return w[0] / 1e8
    except Exception:
        pass
    return token_price(WMON)


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


def os_req(path, key, params=None):
    for i in range(4):
        r = requests.get(f"{OS_API}{path}", params=params, timeout=30,
                         headers={"X-API-KEY": key, "accept": "application/json"})
        if r.status_code == 429:
            time.sleep(min(int(r.headers.get("Retry-After", 5) or 5), 30))
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"OpenSea GET {path.split('?')[0]} 失敗（{r.status_code}）: {r.text[:300]}")
        return r.json()
    raise RuntimeError("OpenSea API の制限にかかりました")


def parse_event(e):
    """成約・出品イベント → 辞書。読めなければ None"""
    try:
        nft = e.get("nft") or e.get("asset") or {}
        if str(nft.get("contract", DUST_LOCK)).lower() != DUST_LOCK.lower():
            return None
        pay = e.get("payment") or {}
        qty = int(e.get("quantity") or 1) or 1
        value = int(pay.get("quantity", 0))
        if value <= 0:
            return None
        return {"type": e.get("event_type"), "token_id": int(nft.get("identifier")),
                "amount": value / 10 ** int(pay.get("decimals", 18)) / qty,
                "currency": str(pay.get("symbol", "")).upper(),
                "ts": int(e.get("event_timestamp") or e.get("closing_date") or 0),
                "buyer": str(e.get("buyer", "") or "").lower(),
                "seller": str(e.get("seller", "") or e.get("maker", "") or "").lower(),
                "order_hash": e.get("order_hash") or e.get("transaction") or ""}
    except Exception:
        return None


def parse_offer(o):
    """買いオファー → 辞書（item=特定の1件向け / collection=どれでも）。読めなければ None"""
    try:
        params = (o.get("protocol_data") or {}).get("parameters") or {}
        price = o.get("price") or {}
        value = int(price.get("value", 0))
        if value <= 0:
            return None
        kind, token_id, units = "collection", None, 1
        for it in params.get("consideration", []):
            if str(it.get("token", "")).lower() != DUST_LOCK.lower():
                continue
            units = max(int(it.get("startAmount", 1) or 1), 1)
            if int(it.get("itemType", 0)) in (2, 3):
                kind, token_id = "item", int(it.get("identifierOrCriteria"))
        return {"kind": kind, "token_id": token_id,
                "amount": value / 10 ** int(price.get("decimals", 18)) / units, "units": units,
                "currency": str(price.get("currency", "")).upper(),
                "offerer": str(params.get("offerer", "")).lower(), "end": int(params.get("endTime", 0) or 0),
                "zone": str(params.get("zone", "") or "").lower(), "order_type": params.get("orderType"),
                "zone_hash": str(params.get("zoneHash", "") or "").lower(),
                "conduit_key": str(params.get("conduitKey", "") or "").lower(),
                "protocol_address": str(o.get("protocol_address") or "").lower(),
                "pay_token": str(((params.get("offer") or [{}])[0]).get("token", "")).lower()}
    except Exception:
        return None


# ---------------- Seaport（オファーの署名・送信） ----------------
OFFER_ITEM = [("itemType", "uint8"), ("token", "address"), ("identifierOrCriteria", "uint256"),
              ("startAmount", "uint256"), ("endAmount", "uint256")]
CONS_ITEM  = OFFER_ITEM + [("recipient", "address")]
ORDER_COMPONENTS = [("offerer", "address"), ("zone", "address"), ("offer", ("tuple[]", OFFER_ITEM)),
                    ("consideration", ("tuple[]", CONS_ITEM)), ("orderType", "uint8"),
                    ("startTime", "uint256"), ("endTime", "uint256"), ("zoneHash", "bytes32"),
                    ("salt", "uint256"), ("conduitKey", "bytes32"), ("counter", "uint256")]


def to_int(v):
    if isinstance(v, int):
        return v
    s = str(v)
    return int(s, 16) if s.lower().startswith("0x") else int(s)


def hexb(v):
    s = str(v)
    return bytes.fromhex(s[2:] if s.startswith("0x") else s)


def abi_dict(t, v):
    """署名用に、数値は int、アドレスはチェックサム、bytes は bytes に揃える（名前付きのまま）"""
    if not isinstance(t, str):
        kind, comps = t
        if kind == "tuple[]":
            return [abi_dict(("tuple", comps), x) for x in v]
        return {n: abi_dict(ct, v[n]) for n, ct in comps}
    if t == "address":
        return cs(v)
    if t.startswith("uint") or t.startswith("int"):
        return to_int(v)
    if t.startswith("bytes"):
        b = hexb(v)
        return b.rjust(32, b"\0") if t == "bytes32" else b
    return v


def sign_order(w3, seaport, version, comp):
    types = {
        "EIP712Domain": [{"name": "name", "type": "string"}, {"name": "version", "type": "string"},
                         {"name": "chainId", "type": "uint256"}, {"name": "verifyingContract", "type": "address"}],
        "OrderComponents": [{"name": n, "type": "OfferItem[]" if n == "offer" else "ConsiderationItem[]"
                             if n == "consideration" else t} for n, t in ORDER_COMPONENTS],
        "OfferItem": [{"name": n, "type": t} for n, t in OFFER_ITEM],
        "ConsiderationItem": [{"name": n, "type": t} for n, t in CONS_ITEM],
    }
    full = {"types": types, "primaryType": "OrderComponents",
            "domain": {"name": "Seaport", "version": version, "chainId": w3.eth.chain_id,
                       "verifyingContract": cs(seaport)},
            "message": abi_dict(("tuple", ORDER_COMPONENTS), comp)}
    from eth_account import Account
    from eth_account.messages import encode_typed_data
    signed = Account.sign_message(encode_typed_data(full_message=full), PK)
    return "0x" + bytes(signed.signature).hex().removeprefix("0x")


def send_tx(w3, acct, tx):
    """署名して送り、結果を待つ。戻り値は (tx hash, 成功したか)"""
    tx = dict(tx)
    tx.setdefault("from", acct.address)
    tx.setdefault("value", 0)
    tx["chainId"] = w3.eth.chain_id
    if "gas" not in tx:
        tx["gas"] = int(w3.eth.estimate_gas(tx) * 1.2)
    base = w3.eth.get_block("latest").get("baseFeePerGas") or w3.eth.gas_price
    try:
        prio = w3.eth.max_priority_fee
    except Exception:
        prio = 0
    tx["maxPriorityFeePerGas"] = prio
    tx["maxFeePerGas"] = base * 2 + prio
    tx["nonce"] = w3.eth.get_transaction_count(acct.address, "pending")
    signed = acct.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
    h = w3.eth.send_raw_transaction(raw)
    hx = "0x" + bytes(h).hex().removeprefix("0x")
    rec = w3.eth.wait_for_transaction_receipt(h, timeout=120)
    return hx, rec.get("status") == 1


class RateLimited(Exception):
    def __init__(self, wait):
        super().__init__(f"OpenSea API の制限（{wait:.0f}秒待つよう指示）")
        self.wait = wait


def os_post(path, key, body):
    r = requests.post(f"{OS_API}{path}", json=body, timeout=30,
                      headers={"X-API-KEY": key, "accept": "application/json", "content-type": "application/json"})
    if r.status_code == 429:
        try:
            wait = float(r.headers.get("Retry-After") or 0)
        except ValueError:
            wait = 0
        raise RateLimited(wait)
    if r.status_code >= 400:
        raise RuntimeError(f"OpenSea POST {path} 失敗（{r.status_code}）: {r.text[:300]}")
    return r.json()


# ---------------- 本体 ----------------
class Watcher:
    def __init__(self):
        self.w3 = connect()
        self.st = load_state()
        self.st.setdefault("locks", {})        # token_id → {amt, status, owner}
        self.st.setdefault("contract", {})     # 持ち主のアドレス → コントラクトか
        self.st.setdefault("day", {})
        self.st.setdefault("alerted", {})
        self.key = api_key(self.st)
        self.slug = self.find_slug()
        self.dust_px = self.mon_px = None
        self.px_ts = self.event_ts = self.offers_ts = 0
        self.competitors = {}
        self.my_offers = []
        self.fresh_state = not os.path.exists(STATE_PATH)
        self.acct = None
        if PK:                                          # off でもWMONをMONに戻すために使う
            from eth_account import Account
            self.acct = Account.from_key(PK)
        self.st.setdefault("offer", {"spent_usd": 0.0, "round_ts": 0, "ratio": None, "active": {}, "won": []})
        self.fill_ts = 0
        self.last_event_ts = int(self.st.get("last_event_ts") or time.time() - 3600)

    def find_slug(self):
        if self.st.get("os_slug"):
            return self.st["os_slug"]
        d = os_req(f"/chain/monad/contract/{DUST_LOCK}", self.key)
        slug = d.get("collection") or "voting-escrow-dust"
        self.st["os_slug"] = slug
        return slug

    # --- 価格 ---
    def refresh_prices(self, force=False):
        if not force and time.time() - self.px_ts < PRICE_SECONDS:
            return
        try:
            self.dust_px = token_price(DUST)
            self.mon_px = mon_price(self.w3)
            if self.st.get("price_alerted"):
                notify("✅ DUST価格がまた取れるようになりました")
                self.st["price_alerted"] = False
            self.px_ts = time.time()
        except Exception as e:
            log("価格の取得に失敗:", e)
            if self.dust_px is None:
                raise
            if time.time() - self.px_ts > 3600 and not self.st.get("price_alerted"):
                self.st["price_alerted"] = True
                notify(f"⚠️ DUST価格が60分以上取れていません。新しいオファーは出さずに待ちます（{str(e)[:200]}）")

    def usd(self, amount, currency):
        if currency in MON_SYMBOLS:
            return amount * self.mon_px
        if currency in STABLES:
            return amount
        return None

    def day(self):
        d = now().strftime("%Y-%m-%d")
        if self.st["day"].get("date") != d:
            self.st["day_prev"] = self.st["day"]
            self.st["day"] = {"date": d, "sales": [], "listings": [], "offers": None}
        return self.st["day"]

    # --- 1. 全 veDUST の読み取り ---
    def scan(self):
        t0 = time.time()
        top = max_token_id(self.w3)
        sig = self.st.get("power_sig")
        for n in range(top, max(top - 50, 0), -1):           # 議決権を読む関数の名前を先に調べる
            if sig:
                break
            sig = find_power_sig(self.w3, n)
        self.st["power_sig"] = sig
        k = 3 if sig else 2
        ids = list(range(1, top + 1))
        locks, nowts = {}, time.time()
        for i in range(0, len(ids), BATCH_IDS):
            part = ids[i:i + BATCH_IDS]
            calls = []
            for n in part:
                calls += [(DUST_LOCK, calldata("ownerOf(uint256)", ["uint256"], [n])),
                          (DUST_LOCK, calldata("locked(uint256)", ["uint256"], [n]))]
                if sig:
                    calls.append((DUST_LOCK, calldata(sig, ["uint256"], [n])))
            raws = multicall(self.w3, calls)
            for j, n in enumerate(part):
                ow, lk = raws[k * j], raws[k * j + 1]
                pw = raws[k * j + 2] if sig else None
                if not ow or not lk:
                    continue
                info = parse_lock(lk, pw, nowts)
                if info is None or info[0] <= 0:
                    continue
                locks[str(n)] = {"amt": round(info[0], 6), "status": info[1], "owner": addr_of(words(ow)[0])}
            time.sleep(0.1)
        self.st["locks"] = locks
        self.st["scan_ts"] = time.time()
        self.st["scan_top"] = top
        log(f"veDUSTを{len(locks)}件読みました（番号 1〜{top}・{time.time() - t0:.0f}秒）")
        return self.candidates(check_contracts=True)

    def candidates(self, check_contracts=False):
        """オファーの対象になる veDUST と、出すならいくらか"""
        rows = []
        for tid, x in self.st["locks"].items():
            if PERM_ONLY and x["status"] != "perm":
                continue
            value = x["amt"] * self.dust_px
            if value < MIN_VALUE_USD or x["owner"].lower() in SKIP_OWNERS:
                continue
            rows.append({"token_id": int(tid), "owner": x["owner"], "dust": x["amt"], "value_usd": value})
        if check_contracts:
            unknown = sorted({r["owner"].lower() for r in rows} - set(self.st["contract"]))
            if unknown:
                log(f"持ち主 {len(unknown)}件がウォレットかコントラクトかを確認します")
                self.st["contract"].update(is_contracts(self.w3, unknown))
        out = []
        for r in rows:
            if self.st["contract"].get(r["owner"].lower()):
                continue                                  # 金庫などのコントラクトはオファーを受けられない
            offer = r["value_usd"] * (1 - OFFER_DISCOUNT_PCT / 100)
            out.append({**r, "offer_usd": offer, "offer_mon": offer / self.mon_px})
        return sorted(out, key=lambda r: -r["value_usd"])

    def save_candidates(self, rows):
        write_csv(f"candidates_{now():%Y-%m-%d}.csv",
                  [{"token_id": r["token_id"], "owner": r["owner"], "dust": round(r["dust"], 2),
                    "value_usd": round(r["value_usd"], 2), "offer_usd": round(r["offer_usd"], 2),
                    "offer_mon": round(r["offer_mon"], 2)} for r in rows])

    def candidate_text(self, rows):
        if not rows:
            return "オファーの対象：0件"
        bins = [(10, 50), (50, 200), (200, 1000), (1000, None)]
        parts = []
        for lo, hi in bins:
            sel = [r for r in rows if r["value_usd"] >= lo and (hi is None or r["value_usd"] < hi)]
            label = f"${lo:,}〜{hi:,}" if hi else f"${lo:,}以上"
            parts.append(f"{label} {len(sel)}件")
        total = sum(r["offer_usd"] for r in rows)
        owners = len({r["owner"].lower() for r in rows})
        skipped = sum(1 for x in self.st["locks"].values() if self.st["contract"].get(x["owner"].lower()))
        return (f"オファーの対象（無期限・ロック価値${MIN_VALUE_USD:g}以上・{OFFER_DISCOUNT_PCT:g}%引き）："
                f"{len(rows)}件・持ち主 {owners}人\n"
                f"  内訳：{'・'.join(parts)}\n"
                f"  全部に出すと合計 ${total:,.0f}（{total / self.mon_px:,.0f} MON）分のオファー"
                f"（最大は1件 ${rows[0]['offer_usd']:,.0f}）\n"
                f"  ※コントラクトが持っている veDUST（{skipped}件）は外しています")

    # --- 2. 成約・出品 ---
    def lock_of(self, token_id):
        x = self.st["locks"].get(str(token_id))
        if x:
            return x
        try:
            sig = self.st.get("power_sig")
            pw = call(self.w3, DUST_LOCK, sig, ["uint256"], [token_id]) if sig else None
            info = parse_lock(call(self.w3, DUST_LOCK, "locked(uint256)", ["uint256"], [token_id]), pw, time.time())
            if info and info[0] > 0:
                x = {"amt": info[0], "status": info[1], "owner": ""}
                self.st["locks"][str(token_id)] = x
                return x
        except Exception as e:
            log(f"#{token_id}: ロック量を取得できず（{e}）")
        return None

    def check_events(self):
        after = self.last_event_ts
        newest = after
        for etype in ("sale", "listing"):
            d = os_req(f"/events/collection/{self.slug}", self.key,
                       {"event_type": etype, "after": int(after), "limit": 50})
            for e in d.get("asset_events", []):
                r = parse_event(e)
                if not r or r["ts"] <= after:
                    continue
                newest = max(newest, r["ts"])
                self.record_event(etype, r)
        self.last_event_ts = newest
        self.st["last_event_ts"] = newest

    def record_event(self, etype, r):
        price_usd = self.usd(r["amount"], r["currency"])
        lk = self.lock_of(r["token_id"])
        if price_usd is None or not lk or lk["amt"] <= 0:
            return
        value = lk["amt"] * self.dust_px
        disc = (1 - price_usd / value) * 100
        row = {"time": datetime.fromtimestamp(r["ts"], JST).strftime("%Y-%m-%d %H:%M"), "type": etype,
               "token_id": r["token_id"], "dust": round(lk["amt"], 2), "lock": lk["status"],
               "price": round(r["amount"], 4), "currency": r["currency"], "usd": round(price_usd, 2),
               "value_usd": round(value, 2), "disc_pct": round(disc, 1),
               "buyer": r["buyer"], "seller": r["seller"]}
        append_csv("sales.csv" if etype == "sale" else "listings.csv", row)
        self.day()["sales" if etype == "sale" else "listings"].append(row)
        key = f"{etype}:{r['order_hash']}:{r['token_id']}"
        if (etype == "sale" and disc >= OFFER_DISCOUNT_PCT and value >= MIN_VALUE_USD
                and key not in self.st["alerted"]):
            self.st["alerted"][key] = time.time()
            lock = "無期限" if lk["status"] == "perm" else lk["status"]
            notify(f"🔎 {OFFER_DISCOUNT_PCT:g}%引き以上の成約（観察のみ）\n"
                   f"#{r['token_id']}：{lk['amt']:,.0f} DUST（{lock}・価値${value:,.2f}）が"
                   f" {r['amount']:,.4g} {r['currency']}（${price_usd:,.2f}）＝ {disc:.1f}%引きで売れました\n"
                   f"→ 先にオファーを出していれば、受けてもらえた可能性がある取引です\n"
                   f"https://opensea.io/item/monad/{DUST_LOCK.lower()}/{r['token_id']}")
        cutoff = time.time() - 14 * 86400
        self.st["alerted"] = {k: v for k, v in self.st["alerted"].items() if v > cutoff}

    # --- 3. 他の買い手のオファー ---
    def check_offers(self):
        items, colls = [], []
        nxt = None
        for _ in range(OFFER_PAGES):
            params = {"limit": 100}
            if nxt:
                params["next"] = nxt
            d = os_req(f"/offers/collection/{self.slug}/all", self.key, params)
            items += [o for o in map(parse_offer, d.get("offers", [])) if o]
            nxt = d.get("next")
            if not nxt:
                break
        try:
            d = os_req(f"/offers/collection/{self.slug}", self.key)
            colls = [o for o in map(parse_offer, d.get("offers", [])) if o]
        except Exception as e:
            log("コレクション全体へのオファーを読めず:", e)
        self.learn_offer_form(items)
        me = self.acct.address.lower() if self.acct else ""
        self.my_offers = [o for o in items if me and o["offerer"] == me and o["kind"] == "item"
                          and (not o["end"] or o["end"] > time.time())]
        items = [o for o in items if o["offerer"] != me]
        serious, currencies, best = [], {}, {}
        for o in items + colls:
            currencies[o["currency"]] = currencies.get(o["currency"], 0) + 1
            if o["kind"] != "item" or o["token_id"] is None:
                continue
            lk = self.st["locks"].get(str(o["token_id"]))
            usd = self.usd(o["amount"], o["currency"])
            if not lk or usd is None or lk["amt"] <= 0:
                continue
            value = lk["amt"] * self.dust_px
            disc = (1 - usd / value) * 100
            if usd > best.get(o["token_id"], (0,))[0]:
                best[o["token_id"]] = (usd, disc, o["offerer"])
            if usd >= MIN_VALUE_USD * 0.3:
                serious.append({"token_id": o["token_id"], "usd": usd, "value_usd": value, "disc_pct": disc,
                                "offerer": o["offerer"], "end": o["end"]})
        self.competitors = best
        # 買い手ごとのまとめ（誰が・何件・何%引きくらいで出しているか）
        by = {}
        for x in serious:
            by.setdefault(x["offerer"], []).append(x)
        top = []
        for who, xs in sorted(by.items(), key=lambda kv: -len(kv[1]))[:5]:
            ds = sorted(x["disc_pct"] for x in xs)
            lives = sorted((x["end"] - time.time()) / 3600 for x in xs if x["end"])
            top.append({"offerer": who, "n": len(xs), "best_disc": round(ds[0], 1),
                        "median_disc": round(ds[len(ds) // 2], 1), "total_usd": round(sum(x["usd"] for x in xs), 0),
                        "median_hours_left": round(lives[len(lives) // 2], 0) if lives else ""})
        # 自分のオファー対象のうち、すでに自分の値段以上のオファーが付いているもの
        cands = self.candidates()
        beaten = [c for c in cands if best.get(c["token_id"], (0,))[0] >= c["offer_usd"]]
        best_coll = max((self.usd(o["amount"], o["currency"]) or 0 for o in colls), default=0)
        snap = {"time": now().strftime("%Y-%m-%d %H:%M"), "item_offers": len(items),
                "pages_full": len(items) >= OFFER_PAGES * 100,
                "collection_offers": len(colls), "serious_item_offers": len(serious),
                "serious_offerers": len(by),
                "best_item_disc": round(min((x["disc_pct"] for x in serious), default=0), 1) if serious else "",
                "candidates": len(cands), "candidates_with_offer": sum(1 for c in cands if c["token_id"] in best),
                "beaten": len(beaten), "beaten_value_usd": round(sum(c["value_usd"] for c in beaten), 0),
                "best_collection_usd": round(best_coll, 2),
                "currencies": " ".join(f"{k}:{v}" for k, v in sorted(currencies.items()))}
        append_csv("offers_competition.csv", snap)
        for t in top:
            append_csv("offers_by_buyer.csv", {"time": snap["time"], **t})
        write_csv("offers_now.csv", [{"token_id": x["token_id"], "offerer": x["offerer"], "usd": round(x["usd"], 2),
                                      "value_usd": round(x["value_usd"], 2), "disc_pct": round(x["disc_pct"], 1),
                                      "hours_left": round((x["end"] - time.time()) / 3600, 1) if x["end"] else ""}
                                     for x in sorted(serious, key=lambda x: -x["value_usd"])])
        snap["top"] = top
        self.day()["offers"] = snap
        return snap

    def competition_text(self, off):
        if not off:
            return "他の買い手のオファー：未確認"
        lines = [f"他の買い手のオファー：特定の1件向け {off['item_offers']}件"
                 + ("以上（読み取り上限）" if off.get("pages_full") else "")
                 + f"・うち意味のある金額（$3以上）{off['serious_item_offers']}件・{off['serious_offerers']}人"
                 + f"・全体向けの最高 ${off['best_collection_usd']:,.2f}"]
        for t in off.get("top", []):
            left = f"・残り期限の中央値 {t['median_hours_left']:g}時間" if t["median_hours_left"] != "" else ""
            lines.append(f"  {t['offerer'][:10]}…：{t['n']}件（合計 ${t['total_usd']:,.0f}）"
                         f"・{t['median_disc']}%引きが中心（一番強いもので{t['best_disc']}%引き）{left}")
        if off.get("candidates"):
            lines.append(f"自分の対象 {off['candidates']}件のうち、すでにオファーが付いているもの {off['candidates_with_offer']}件・"
                         f"{OFFER_DISCOUNT_PCT:g}%引きより高い値段のオファーが付いているもの {off['beaten']}件"
                         f"（ロック価値の合計 ${off['beaten_value_usd']:,.0f}）")
        lines.append(f"オファーの通貨：{off['currencies'] or '不明'}")
        return "\n".join(lines)

    # --- 4. 本番のオファー ---
    def learn_offer_form(self, items):
        """Monad上で OpenSea に受け付けられている実際のオファーから、形式（zone・orderType・conduitKey）を覚える"""
        forms, pays = {}, {}
        for o in items:
            if o["kind"] != "item" or not o["zone"] or o["order_type"] is None:
                continue
            f = (o["zone"], int(o["order_type"]), o["zone_hash"] if len(o["zone_hash"]) == 66 else ZERO32,
                 o["conduit_key"] if len(o["conduit_key"]) == 66 else ZERO32, o["protocol_address"] or SEAPORT_DEFAULT.lower())
            forms[f] = forms.get(f, 0) + 1
            pays[o["pay_token"]] = pays.get(o["pay_token"], 0) + 1
        if forms:
            z, ot, zh, ck, sp = max(forms, key=forms.get)
            self.st["offer_form"] = {"zone": z, "order_type": ot, "zone_hash": zh, "conduit_key": ck,
                                     "seaport": sp, "seen": forms[(z, ot, zh, ck, sp)],
                                     "pay_token": max(pays, key=pays.get) if pays else WMON.lower()}

    def seaport_info(self, seaport):
        """Seaport.information() → (バージョン, コンジット管理契約)"""
        if not getattr(self, "_sp_info", None):
            raw = call(self.w3, seaport, "information()")
            version, _, controller = decode(["string", "bytes32", "address"], raw)
            self._sp_info = (version, cs(controller))
        return self._sp_info

    def operator(self, form):
        """WMONを動かす許可を与える先（コンジット。conduitKeyが0ならSeaport自身）"""
        if int(form["conduit_key"], 16) == 0:
            return cs(form["seaport"])
        controller = self.seaport_info(form["seaport"])[1]
        raw = call(self.w3, controller, "getConduit(bytes32)", ["bytes32"], [hexb(form["conduit_key"])])
        if len(raw) < 64 or int.from_bytes(raw[32:64], "big") != 1:
            raise RuntimeError(f"オファーに使うコンジットが見つかりません（key {form['conduit_key'][:12]}…）")
        return cs("0x" + raw[12:32].hex())

    def balances(self):
        me = self.acct.address
        native = self.w3.eth.get_balance(me) / 1e18
        wmon = words(call(self.w3, WMON, "balanceOf(address)", ["address"], [me]))[0] / 1e18
        return native, wmon

    def wrap(self, mon):
        hx, ok = send_tx(self.w3, self.acct, {"to": cs(WMON), "value": int(mon * 1e18),
                                              "data": "0x" + calldata("deposit()").hex()})
        if not ok:
            raise RuntimeError(f"MON→WMONの交換に失敗: {hx}")
        return hx

    def unwrap_all(self):
        _, wmon = self.balances()
        if wmon <= 0:
            return None
        raw = words(call(self.w3, WMON, "balanceOf(address)", ["address"], [self.acct.address]))[0]
        hx, ok = send_tx(self.w3, self.acct, {"to": cs(WMON),
                                              "data": "0x" + calldata("withdraw(uint256)", ["uint256"], [raw]).hex()})
        if not ok:
            raise RuntimeError(f"WMON→MONの交換に失敗: {hx}")
        return hx

    def ensure_wmon_approval(self, operator):
        w = words(call(self.w3, WMON, "allowance(address,address)", ["address", "address"],
                       [self.acct.address, operator]))
        if w and w[0] >= 10 ** 30:
            return False
        hx, ok = send_tx(self.w3, self.acct, {"to": cs(WMON), "data": "0x" + calldata(
            "approve(address,uint256)", ["address", "uint256"], [operator, (1 << 256) - 1]).hex()})
        if not ok:
            raise RuntimeError(f"WMONの使用許可（承認）に失敗: {hx}")
        notify(f"🔑 オファーの支払いに使えるよう WMON の承認を行いました\nhttps://monadscan.com/tx/{hx}")
        return True

    def fees(self):
        c = self.st.get("fees")
        if c and time.time() - c["ts"] < 86400:
            return c["items"]
        d = os_req(f"/collections/{self.slug}", self.key)
        items = [{"bps": int(round(float(f["fee"]) * 100)), "recipient": cs(f["recipient"])}
                 for f in d.get("fees", []) if f.get("required")]
        self.st["fees"] = {"ts": time.time(), "items": items}
        return items

    def ratio(self):
        return self.dust_px / self.mon_px            # DUST 1枚が何MONか

    def offer_plan(self):
        """予算の残りで出せるオファーの一覧（ロック価値の大きい順）"""
        o = self.st["offer"]
        left = OFFER_BUDGET_USD - o["spent_usd"]
        if self.acct and o.get("wrapped"):
            left = min(left, self.balances()[1] * self.mon_px)       # 両替済みなら、手元のWMONが上限
        me = self.acct.address.lower() if self.acct else ""
        rows = [r for r in self.candidates() if r["owner"].lower() != me and r["offer_usd"] <= left]
        return rows[:MAX_OFFERS], left

    def post_offer(self, form, operator, token_id, price_mon):
        seaport = cs(form["seaport"])
        pay = cs(form.get("pay_token") or WMON)
        total = int(round(price_mon * 100)) * 10 ** 16          # 小数点以下2桁までのぴったりの値にする
        cons = [{"itemType": 2, "token": cs(DUST_LOCK), "identifierOrCriteria": str(token_id),
                 "startAmount": "1", "endAmount": "1", "recipient": self.acct.address}]
        for f in self.fees():
            amt = total * f["bps"] // 10000
            cons.append({"itemType": 1, "token": pay, "identifierOrCriteria": "0",
                         "startAmount": str(amt), "endAmount": str(amt), "recipient": f["recipient"]})
        counter = words(call(self.w3, seaport, "getCounter(address)", ["address"], [self.acct.address]))[0]
        start = int(time.time()) - 60
        comp = {"offerer": self.acct.address, "zone": cs(form["zone"]),
                "offer": [{"itemType": 1, "token": pay, "identifierOrCriteria": "0",
                           "startAmount": str(total), "endAmount": str(total)}],
                "consideration": cons, "orderType": form["order_type"], "startTime": str(start),
                "endTime": str(start + OFFER_EXPIRE_HOURS * 3600), "zoneHash": form["zone_hash"],
                "salt": str(random.getrandbits(128)), "conduitKey": form["conduit_key"], "counter": str(counter)}
        sig = sign_order(self.w3, seaport, self.seaport_info(seaport)[0], comp)
        params = {k: v for k, v in comp.items() if k != "counter"}
        params["totalOriginalConsiderationItems"] = len(cons)
        params["counter"] = comp["counter"]
        d = os_post(f"/orders/monad/seaport/offers", self.key,
                    {"parameters": params, "signature": sig, "protocol_address": seaport})
        return ((d.get("order") or {}).get("order_hash")) or d.get("order_hash") or ""

    def offer_round(self):
        """予算の分だけWMONを用意し、ロック価値の大きい順にオファーを出す（24時間ごと）"""
        o = self.st["offer"]
        plan, left = self.offer_plan()
        o["round_ts"] = time.time()
        if not plan:
            notify(f"⏸ オファー：予算の残り ${left:,.0f} で出せる対象がありません（使った額 ${o['spent_usd']:,.0f}）")
            return
        total_usd = sum(r["offer_usd"] for r in plan)
        head = (f"{len(plan)}件・ロック価値 ${plan[-1]['value_usd']:,.0f}〜${plan[0]['value_usd']:,.0f}"
                f"・オファー額 ${plan[-1]['offer_usd']:,.2f}〜${plan[0]['offer_usd']:,.2f}"
                f"（{OFFER_DISCOUNT_PCT:g}%引き・期限{OFFER_EXPIRE_HOURS}時間）\n"
                f"予算の残り ${left:,.0f}（{left / self.mon_px:,.0f} MON）。全部の額面は ${total_usd:,.0f} ですが、"
                f"受けてもらえた分だけ支払うので、使うのは予算まで")
        if OFFER_MODE != "live" or not self.acct:
            why = "秘密鍵が未設定" if not self.acct else "OFFER_MODE=dry"
            notify(f"🧪 オファーの予定（{why}のため出していません）\n{head}")
            return
        form = self.st.get("offer_form")
        if not form:
            notify("⚠️ オファーの形式を実際のオファーから読めないため、出すのを見送りました")
            return
        operator = self.operator(form)
        self.ensure_wmon_approval(operator)
        native, wmon = self.balances()
        want = left / self.mon_px
        if wmon > 0 and not o.get("wrapped"):
            o["wrapped"] = True                                  # すでにWMONがある＝両替済みとみなす（記録が消えた場合の安全策）
        if wmon < want * 0.98 and not o.get("wrapped"):
            need = want - wmon
            if native - GAS_RESERVE_MON < need:
                notify(f"⚠️ オファー：MONが足りません（WMONに替えるのに {need:,.0f} MON 必要・手持ち {native:,.0f} MON）。"
                       f"ボット用ウォレットにMONを入れてください。今ある分（WMON {wmon:,.0f}）の範囲で出します")
            else:
                hx = self.wrap(need)
                o["wrapped"] = True
                notify(f"🔁 オファー用に {need:,.0f} MON を WMON に替えました\nhttps://monadscan.com/tx/{hx}")
                wmon += need
        o["active"] = {}
        o["queue"] = [{"token_id": r["token_id"], "price_mon": float(int(r["offer_usd"] / self.mon_px)),   # 整数のMON
                       "usd": r["offer_usd"], "value_usd": r["value_usd"], "dust": r["dust"], "owner": r["owner"],
                       "tries": 0} for r in plan]
        o["queue"] = [q for q in o["queue"] if q["price_mon"] <= wmon]
        o["run"] = {"posted": 0, "fails": [], "limited": 0, "errors": 0, "head": head, "start": time.time()}
        o["cooldown_until"] = 0
        o["ratio"] = self.ratio()
        o["wmon_last"] = wmon
        save_state(self.st)
        notify(f"📤 オファーを出し始めます：{len(o['queue'])}件（OpenSeaの回数制限に合わせて少しずつ出します）")

    def post_some(self):
        """出す予定の列から、少しずつオファーを出す（30秒ごとの確認のたびに呼ばれる）"""
        o = self.st["offer"]
        q = o.get("queue") or []
        if not q or time.time() < o.get("cooldown_until", 0):
            return
        form = self.st["offer_form"]
        operator = self.operator(form)
        run = o["run"]
        t0 = time.time()
        while q and time.time() - t0 < POST_TICK_SECONDS:
            item = q.pop(0)
            try:
                h = self.post_offer(form, operator, item["token_id"], item["price_mon"])
                o["active"][str(item["token_id"])] = {"order_hash": h, "price_mon": item["price_mon"], "usd": item["usd"],
                                                      "value_usd": item["value_usd"], "dust": item["dust"],
                                                      "owner": item["owner"], "ts": time.time(),
                                                      "end": time.time() + OFFER_EXPIRE_HOURS * 3600}
                run["posted"] += 1
                o["limit_wait"] = max(o.get("limit_wait", 60) * 0.8, 60)
                log(f"#{item['token_id']} にオファー {item['price_mon']:,.0f} WMON（${item['usd']:,.2f}）")
                if run["posted"] % 25 == 0:
                    notify(f"📨 途中経過：{run['posted']}件出しました（残り {len(q)}件）")
            except RateLimited as e:
                run["limited"] += 1
                q.append(item)
                wait = min(max(e.wait, o.get("limit_wait", 60)), 600)
                o["limit_wait"] = min(wait * 2, 600)
                o["cooldown_until"] = time.time() + wait
                log(f"OpenSeaの回数制限：{wait:.0f}秒休みます（残り {len(q)}件）")
                break
            except Exception as e:
                run["errors"] += 1
                log(f"#{item['token_id']} のオファー失敗:", e)
                if len(run["fails"]) < 3:
                    run["fails"].append(f"#{item['token_id']}（{item['price_mon']:,.0f} WMON）: {str(e)[:600]}")
                item["tries"] += 1
                if item["tries"] < 2:
                    q.append(item)
                if run["errors"] >= 3 and run["posted"] == 0:
                    q.clear()                                  # 同じ理由で全部断られている。列を捨てて報告する
            time.sleep(POST_GAP_SECONDS)
        o["queue"] = q
        if not q:
            o["last_posted"] = run["posted"]
            o["wmon_last"] = self.balances()[1]
            mins = (time.time() - run["start"]) / 60
            text = (f"📨 オファーを出しました：{run['posted']}件（{mins:.0f}分・回数制限で休んだ回数 {run['limited']}）\n"
                    f"{run['head']}")
            if run["fails"]:
                text += f"\n失敗 {run['errors']}件（最初のもの）：{run['fails'][0]}"
            notify(text)
        save_state(self.st)

    def check_fills(self):
        """自分のオファーが受けられたか（NFTが自分のウォレットに来たか）を確認する"""
        o = self.st["offer"]
        act = o.get("active") or {}
        if not act or not self.acct:
            return
        ids = [int(t) for t in act]
        raws = multicall(self.w3, [(DUST_LOCK, calldata("ownerOf(uint256)", ["uint256"], [i])) for i in ids])
        me = self.acct.address.lower()
        _, wmon = self.balances()
        paid = max(o.get("wmon_last", wmon) - wmon, 0)          # 前回の確認から減ったWMON（オファーで払った分）
        for tid, raw in zip(ids, raws):
            x = act.get(str(tid))
            if raw and addr_of(words(raw)[0]).lower() == me:
                if paid < x["price_mon"] * 0.9:
                    log(f"#{tid} が届いたがWMONは減っていない（売買ボットの購入とみなす）")
                    del act[str(tid)]
                    continue
                paid -= x["price_mon"]
                o["spent_usd"] += x["usd"]
                row = {"time": now().strftime("%Y-%m-%d %H:%M"), "token_id": tid, "dust": round(x["dust"], 2),
                       "price_mon": round(x["price_mon"], 2), "usd": round(x["usd"], 2),
                       "value_usd_at_offer": round(x["value_usd"], 2), "value_usd_now": round(x["dust"] * self.dust_px, 2),
                       "seller": x["owner"]}
                append_csv("offers_won.csv", row)
                o["won"].append(row)
                del act[str(tid)]
                notify(f"🎉 オファーを受けてもらえました：#{tid}\n"
                       f"{x['dust']:,.0f} DUST（今の価値 ${row['value_usd_now']:,.2f}）を {x['price_mon']:,.0f} WMON（${x['usd']:,.2f}）で購入\n"
                       f"本番試験の累計：{len(o['won'])}件・${o['spent_usd']:,.2f}／予算 ${OFFER_BUDGET_USD:,.0f}\n"
                       f"https://opensea.io/item/monad/{DUST_LOCK.lower()}/{tid}")
            elif time.time() > x["end"]:
                del act[str(tid)]
        o["wmon_last"] = wmon

    def check_kill(self):
        """DUSTが（MON建てで）大きく下がったら、WMONをMONに戻して全オファーを無効にする"""
        o = self.st["offer"]
        if not o.get("active") or not o.get("ratio") or OFFER_MODE != "live" or not self.acct:
            return
        drop = (1 - self.ratio() / o["ratio"]) * 100
        if drop < KILL_DROP_PCT:
            return
        hx = self.unwrap_all()
        n = len(o["active"])
        o["active"] = {}
        o["queue"] = []
        o["round_ts"] = time.time() - (OFFER_EVERY_HOURS - 1) * 3600     # 1時間後に今の価格で出し直す
        save_state(self.st)
        notify(f"🛑 安全装置：DUSTがオファー時より{drop:.1f}%下がったので、WMONをMONに戻して"
               f"オファー{n}件を無効にしました。1時間後に今の価格で出し直します"
               + (f"\nhttps://monadscan.com/tx/{hx}" if hx else ""))

    def offer_preflight(self):
        lines = [f"オファー：{OFFER_MODE}（予算 ${OFFER_BUDGET_USD:,.0f}・使った額 ${self.st['offer']['spent_usd']:,.2f}）"]
        if not self.acct:
            lines.append("秘密鍵が未設定のため、オファーは出しません（観察のみ）")
            return "\n".join(lines)
        native, wmon = self.balances()
        lines.append(f"ウォレット {self.acct.address[:8]}…：MON {native:,.0f}・WMON {wmon:,.0f}")
        form = self.st.get("offer_form")
        if form:
            lines.append(f"オファーの形式（実際のオファー{form['seen']}件から）：zone {form['zone'][:10]}…・orderType {form['order_type']}"
                         f"・支払い {('WMON' if form['pay_token'] == WMON.lower() else form['pay_token'][:10] + '…')}")
            try:
                op = self.operator(form)
                ok = words(call(self.w3, WMON, "allowance(address,address)", ["address", "address"],
                                [self.acct.address, op]))[0] >= 10 ** 30
                lines.append(f"WMONの承認 {'済み' if ok else '未（最初にオファーを出すときに自動で行います）'}")
            except Exception as e:
                lines.append(f"★コンジットを確認できません: {str(e)[:120]}")
        else:
            lines.append("★オファーの形式を実際のオファーから読めていません")
        fees = self.fees()
        lines.append("手数料（売り手の受取から差し引き）" + " + ".join(f"{f['bps'] / 100:g}%" for f in fees))
        plan, left = self.offer_plan()
        if plan:
            lines.append(f"出す予定：{len(plan)}件・オファー額 ${plan[-1]['offer_usd']:,.2f}〜${plan[0]['offer_usd']:,.2f}"
                         f"・WMONに替える額 約{max(left / self.mon_px - wmon, 0):,.0f} MON")
        return "\n".join(lines)

    # --- 集計 ---
    def summary(self, d, title):
        sales = d.get("sales", [])
        lists = d.get("listings", [])
        off = d.get("offers") or {}
        lines = [f"📊 veDUST オファー試運転レポート（{title} {d.get('date', '')}）",
                 f"DUST ${self.dust_px:.4f} / MON ${self.mon_px:.4f}"]
        if sales:
            hit = [x for x in sales if x["disc_pct"] >= OFFER_DISCOUNT_PCT and x["value_usd"] >= MIN_VALUE_USD]
            mid = [x for x in sales if 35 <= x["disc_pct"] < OFFER_DISCOUNT_PCT]
            ds = sorted(x["disc_pct"] for x in sales)
            lines.append(f"成約 {len(sales)}件（合計 ${sum(x['usd'] for x in sales):,.0f}）：割引率 {ds[0]}〜{ds[-1]}%")
            lines.append(f"  うち{OFFER_DISCOUNT_PCT:g}%引き以上 {len(hit)}件（合計 ${sum(x['usd'] for x in hit):,.0f}）"
                         f"・35〜{OFFER_DISCOUNT_PCT:g}%引き {len(mid)}件")
            buyers = {}
            for x in hit + mid:
                buyers[x["buyer"][:10]] = buyers.get(x["buyer"][:10], 0) + 1
            if buyers:
                lines.append("  安く買った人：" + "・".join(f"{b}… {n}件" for b, n in
                                                         sorted(buyers.items(), key=lambda kv: -kv[1])[:5]))
        else:
            lines.append("成約 0件")
        big = [x for x in lists if x["disc_pct"] >= OFFER_DISCOUNT_PCT and x["value_usd"] >= MIN_VALUE_USD]
        lines.append(f"新しい出品 {len(lists)}件・うち{OFFER_DISCOUNT_PCT:g}%引き以上（価値${MIN_VALUE_USD:g}以上） {len(big)}件")
        lines.append(self.competition_text(off))
        lines.append(self.candidate_text(self.candidates()))
        o = self.st.get("offer") or {}
        if OFFER_MODE != "off":
            lines.append(f"自分のオファー：出ている {len(o.get('active') or {})}件・受けてもらえた累計 {len(o.get('won') or [])}件"
                         f"（${o.get('spent_usd', 0):,.2f}／予算 ${OFFER_BUDGET_USD:,.0f}）")
        notify("\n".join(lines))

    # --- ループ ---
    def tick(self):
        self.refresh_prices()
        self.day()
        if time.time() - self.st.get("scan_ts", 0) > SCAN_HOURS * 3600:
            self.save_candidates(self.scan())
        if time.time() - self.event_ts > EVENT_SECONDS:
            self.check_events()
            self.event_ts = time.time()
        if time.time() - self.offers_ts > OFFERS_SECONDS:
            try:
                self.check_offers()
            except Exception as e:
                log("オファーの確認に失敗:", e)
            self.offers_ts = time.time()
        if OFFER_MODE != "off":
            if time.time() - self.fill_ts > FILL_SECONDS:
                self.check_fills()
                self.check_kill()
                self.fill_ts = time.time()
            o = self.st["offer"]
            wait = OFFER_EVERY_HOURS * 3600 if o.get("last_posted", 1) else 3600      # 1件も出せなかったら1時間後に再挑戦
            fresh_px = time.time() - self.px_ts < 3600
            if self.st.get("offer_form") and fresh_px and not o.get("queue") and time.time() - o["round_ts"] > wait:
                self.offer_round()
            if o.get("queue"):
                self.post_some()
        if now().hour == SUMMARY_HOUR_JST and self.st.get("summary_sent") != now().strftime("%Y-%m-%d"):
            if self.st.get("day_prev"):
                self.summary(self.st["day_prev"], "前日")
            else:
                self.summary(self.day(), "再起動後の記録のみ")
            self.st["summary_sent"] = now().strftime("%Y-%m-%d")
        save_state(self.st)

    def run(self):
        self.refresh_prices(force=True)
        notify(f"🟢 {VERSION} 起動（オファー：{OFFER_MODE}）\n"
               f"全 veDUST のロック量を読んでいます（数分かかります）…")
        if time.time() - self.st.get("scan_ts", 0) > SCAN_HOURS * 3600:
            rows = self.scan()
            self.save_candidates(rows)
        else:
            rows = self.candidates()
        off = self.check_offers()
        self.offers_ts = time.time()
        save_state(self.st)
        n_perm = sum(1 for x in self.st["locks"].values() if x["status"] == "perm")
        text = (f"✅ 読み取り完了\nDUST ${self.dust_px:.4f} / MON ${self.mon_px:.4f}\n"
                f"veDUST {len(self.st['locks'])}件（うち無期限 {n_perm}件・番号 1〜{self.st.get('scan_top')}）"
                f"・議決権の読み方 {self.st.get('power_sig') or '不明'}\n"
                f"{self.candidate_text(rows)}\n"
                f"{self.competition_text(off)}")
        if SKIP_OWNERS:
            text += f"\n対象から外したウォレット：{len(SKIP_OWNERS)}件"
        notify(text)
        o = self.st["offer"]
        if OFFER_MODE != "off" and not o.get("active") and not o.get("queue") and self.my_offers:
            for m in self.my_offers:                          # 記録は無いが、OpenSeaに自分のオファーが出ている → 引き継ぐ
                lk = self.st["locks"].get(str(m["token_id"])) or {}
                o["active"][str(m["token_id"])] = {"order_hash": "", "price_mon": m["amount"], "usd": m["amount"] * self.mon_px,
                                                   "value_usd": lk.get("amt", 0) * self.dust_px, "dust": lk.get("amt", 0),
                                                   "owner": lk.get("owner", ""), "ts": time.time(),
                                                   "end": m["end"] or time.time() + OFFER_EXPIRE_HOURS * 3600}
            newest_end = max(m["end"] for m in self.my_offers if m["end"]) if any(m["end"] for m in self.my_offers) else time.time()
            o["round_ts"] = newest_end - OFFER_EXPIRE_HOURS * 3600   # 最後に出した時刻から24時間後に出し直す
            o["wrapped"] = True
            o["wmon_last"] = self.balances()[1]
        elif OFFER_MODE != "off" and not o.get("active") and not o.get("queue"):
            o["round_ts"] = 0                                 # 出ているオファーが無ければ、起動後すぐに出す
        save_state(self.st)
        mount = os.path.ismount(STATE_DIR) or os.path.ismount(os.path.dirname(STATE_PATH))
        notify(f"💾 記録：{'前回の記録を読みました' if not self.fresh_state else '前回の記録はありません（新しく始めます）'}"
               f"・保存先 {STATE_DIR}（{'ボリューム＝再起動しても消えない' if mount else '★ボリュームではない＝再起動のたびに消えます'}）"
               + (f"\nOpenSeaに出ている自分のオファー {len(self.my_offers)}件・引き継いだもの {len(o.get('active') or {})}件"
                  if OFFER_MODE != "off" else ""))
        if OFFER_MODE == "off" and self.acct:
            try:
                native, wmon = self.balances()
                if wmon > 0:
                    hx = self.unwrap_all()
                    native2, wmon2 = self.balances()
                    notify(f"🔁 オファー停止：WMON {wmon:,.0f} をMONに戻しました（残っているオファーは支払えないので実質無効）\n"
                           f"ウォレット MON {native:,.0f} → {native2:,.0f}・WMON {wmon2:,.0f}\nhttps://monadscan.com/tx/{hx}")
                else:
                    notify(f"🔁 オファー停止中：WMONはありません（MON {native:,.0f}）")
            except Exception as e:
                notify(f"⚠️ WMON→MONの戻しに失敗しました（{str(e)[:300]}）")
        if OFFER_MODE != "off":
            notify("🔍 オファーの事前確認\n```\n" + self.offer_preflight() + "\n```")
        fails = 0
        while True:
            time.sleep(30)
            try:
                self.tick()
                fails = 0
            except Exception as e:
                fails += 1
                log("エラー:", e)
                traceback.print_exc()
                if fails in (5, 60):
                    notify(f"⚠️ {VERSION}：確認が{fails}回続けて失敗しています（{str(e)[:200]}）")
                if fails % 10 == 0:
                    try:
                        self.w3 = connect()
                        self.key = api_key(self.st)
                    except Exception:
                        pass


if __name__ == "__main__":
    try:
        Watcher().run()
    except Exception as e:
        notify(f"🛑 {VERSION} が止まりました：{str(e)[:300]}")
        traceback.print_exc()
        time.sleep(60)
        sys.exit(1)
