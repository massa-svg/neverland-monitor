# =====================================================================
# veAERO 観察ボット  v2   （Railway などで常時実行・お金は一切動かさない）
#   v1からの変更点
#   - 「読めない」としていた出品の理由を細かく分けて表示する（NFTが消滅・ロック量0・価格が読めない・対象外の通貨）
#   - もう買えない出品（NFTが消滅・持ち主が変わった・出品が古い）は記録から外し、毎回読み直さない
#   - 起動直後に中身の無い「前日の観察レポート」を送っていたのを止めた
#   - 起動通知に、買い注文の一番良い水準（何%引き相当）を出す
#   Aerodrome（Base）の veAERO を対象に、買う前に「本当に儲かる歪みがどれだけ出ているか」を記録する
#
#   見ているもの（すべてBaseのコントラクトを直接読む。APIキー不要）
#     1. Vexy の veAERO 出品（固定価格・ダッチオークション。AERO / USDC / WETH 建て）
#     2. Vexy の veAERO 買い注文（「何%引きなら買う」というオファー）
#     3. 任意：OpenSea（Base）の veAERO 出品（OPENSEA_API_KEY があるときだけ）
#
#   記録・通知すること
#     - 割引率 = 1 − 出品価格(USD) ÷（ロック中のAERO数量 × AERO価格(USD)）   ※veDUSTボットと同じ計算
#     - 即時裁定 = 出品を買って、すぐ一番高い買い注文に売ったときの儲け（手数料は売買それぞれ1%で計算）
#     - 実際の成約（Vexyで売れた出品・埋まった買い注文）と、その時の割引率
#     - 即時裁定が出たら、その場で Discord に通知（同じ出品は1回だけ）
#     - 毎朝9時（日本時間）に、前日の集計を Discord に送る
#     - すべての記録を STATE_DIR に CSV で残す（あとで買う条件を決めるため）
#
#   必要な環境変数（Railway の Variables に入れる）
#     DISCORD_ALERT_WEBHOOK_URL  通知先（veDUSTボットと同じでよい）
#     STATE_DIR                  記録の保存先。Railway ではボリュームの場所（例 /data）
#     BASE_RPC_URL / OPENSEA_API_KEY   任意。あれば優先して使う
# =====================================================================
import os, sys, json, time, csv, traceback
from datetime import datetime, timezone, timedelta
import requests
from web3 import Web3
from eth_abi import encode, decode

VERSION = "aero watch v2"

# ---------------- 通知の設定 ----------------
ALERT_MIN_PROFIT_USD = 1.0     # 即時裁定の儲けがこの金額（ドル）以上なら通知
ALERT_MIN_DISCOUNT   = 30.0    # 割引率がこの%以上の出品も通知（裁定にならなくても）
SUMMARY_HOUR_JST     = 9       # 毎日の集計を送る時刻
FEE_PCT              = 1.0     # Vexy の手数料（買い・売りそれぞれ）。実際の成約から確認して集計に出す
GAS_USD              = 0.05    # 1回の取引のガス代の見込み（Base）

# ---------------- 動作の設定 ----------------
LOOP_SECONDS    = 60           # 出品・買い注文の確認間隔
OFFER_SECONDS   = 300          # 「買い注文に売ったらいくらか」の計算し直し間隔
PRICE_SECONDS   = 60           # AERO・ETH価格の更新間隔
BACKFILL        = 3000         # 初回に読む過去の出品・買い注文の数（新しい順）
LOG_BLOCKS      = 2000         # 成約イベントを一度に読むブロック数

MARKET   = "0x6b478209974bd27e6cf661fef86c68072b0d6738"   # Vexy veAERO 出品
OFFERS   = "0x2903ee1a9dc4C8230651004D11f733787A0f69c4"   # Vexy veAERO 買い注文
VE_AERO  = "0xebf418fe2512e7e6bd9b87a8f0f294acdc67e6b4"   # veAERO（VotingEscrow）
AERO     = "0x940181a94a35a4569e4529a3cdfb74e38fd98631"
USDC     = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
WETH     = "0x4200000000000000000000000000000000000006"
MULTICALL = "0xcA11bde05977b3631167028862bE2a173976CA11"  # Multicall3（まとめて読む）
CURRENCIES = {AERO.lower(): ("AERO", 18), USDC.lower(): ("USDC", 6), WETH.lower(): ("WETH", 18)}
RPC_URLS = [u for u in [os.environ.get("BASE_RPC_URL", "").strip(),
                        "https://mainnet.base.org", "https://base-rpc.publicnode.com", "https://base.drpc.org"] if u]
OS_API = "https://api.opensea.io/api/v2"
JST    = timezone(timedelta(hours=9))

WEBHOOK       = os.environ.get("DISCORD_ALERT_WEBHOOK_URL", "").strip()
OS_KEY        = os.environ.get("OPENSEA_API_KEY", "").strip()
STATE_DIR     = os.environ.get("STATE_DIR", "").strip() or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "state")
STATE_PATH    = os.path.join(STATE_DIR, "aero_watch.json")
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
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_PATH)


def append_csv(name, row):
    path = os.path.join(STATE_DIR, name)
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        if new:
            w.writeheader()
        w.writerow(row)


def cs(a):
    return Web3.to_checksum_address(a)


def addr_of(word):
    return "0x" + word.to_bytes(32, "big")[-20:].hex()


def words(raw):
    return [int.from_bytes(raw[i:i + 32], "big") for i in range(0, len(raw) - len(raw) % 32, 32)]


def selector(sig):
    return bytes(Web3.keccak(text=sig))[:4]


def calldata(sig, types=(), values=()):
    return selector(sig) + encode(list(types), list(values))


# ---------------- チェーンの読み取り ----------------
def connect():
    last = None
    for url in RPC_URLS:
        try:
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 20}))
            if w3.eth.chain_id != 8453:
                raise RuntimeError(f"Baseではありません（chain {w3.eth.chain_id}）")
            log("RPC:", url)
            return w3
        except Exception as e:
            last = e
    raise RuntimeError(f"RPCに接続できませんでした: {last}")


def call(w3, to, sig, types=(), values=()):
    return bytes(w3.eth.call({"to": cs(to), "data": "0x" + calldata(sig, types, values).hex()}))


def multicall(w3, calls, batch=150):
    """[(宛先, 呼び出しデータ)] をまとめて読む。失敗した呼び出しは None"""
    out = []
    for i in range(0, len(calls), batch):
        part = calls[i:i + batch]
        data = calldata("aggregate3((address,bool,bytes)[])", ["(address,bool,bytes)[]"],
                        [[(cs(t), True, d) for t, d in part]])
        for attempt in range(3):
            try:
                raw = bytes(w3.eth.call({"to": cs(MULTICALL), "data": "0x" + data.hex()}))
                break
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(2 + attempt * 3)
        res = decode(["(bool,bytes)[]"], raw)[0]
        out += [bytes(r) if ok else None for ok, r in res]
    return out


def read_listings(w3, ids):
    """Vexy の出品 listings(id) → 辞書"""
    raws = multicall(w3, [(MARKET, calldata("listings(uint256)", ["uint256"], [i])) for i in ids])
    out = {}
    for i, raw in zip(ids, raws):
        if not raw:
            continue
        w = words(raw)
        if len(w) < 11:
            continue
        out[i] = {"id": i, "seller": addr_of(w[0]).lower(), "nonce": w[1], "collection": addr_of(w[2]).lower(),
                  "nft": w[3], "currency": addr_of(w[4]).lower(), "slope_max": w[5], "price_raw": w[6],
                  "slope_dur": w[7], "fixed_dur": w[8], "end": w[9], "sold": w[10]}
    return out


def read_offers(w3, ids):
    """Vexy の買い注文 offers(id) → 辞書"""
    raws = multicall(w3, [(OFFERS, calldata("offers(uint256)", ["uint256"], [i])) for i in ids])
    out = {}
    for i, raw in zip(ids, raws):
        if not raw:
            continue
        w = words(raw)
        if len(w) < 7:
            continue
        out[i] = {"id": i, "budget": w[0] / 1e18, "buyer": addr_of(w[1]).lower(), "mult": w[2] / 1e18,
                  "locked_min": w[3] / 1e18, "locked_max": w[4] / 1e18, "lock_time_max": w[5], "end": w[6]}
    return out


def read_locks(w3, nfts):
    """veAERO.locked(id) → (AERO数量, 終了時刻, 無期限か)。所有者・承認もまとめて読む"""
    calls = []
    for n in nfts:
        calls += [(VE_AERO, calldata("locked(uint256)", ["uint256"], [n])),
                  (VE_AERO, calldata("ownerOf(uint256)", ["uint256"], [n])),
                  (VE_AERO, calldata("getApproved(uint256)", ["uint256"], [n]))]
    raws = multicall(w3, calls)
    out = {}
    for k, n in enumerate(nfts):
        lk, ow, ap = raws[3 * k:3 * k + 3]
        if not lk or not ow:
            continue
        w = words(lk)
        if len(w) < 2:
            continue
        amt = (w[0] - (1 << 256) if w[0] >= (1 << 255) else w[0]) / 1e18
        perm = bool(w[2]) if len(w) >= 3 else False
        out[n] = {"amt": amt, "end": w[1], "perm": perm, "owner": addr_of(words(ow)[0]).lower(),
                  "approved": addr_of(words(ap)[0]).lower() if ap else ""}
    return out


def approved_all(w3, pairs):
    """isApprovedForAll(持ち主, Vexy) をまとめて読む"""
    raws = multicall(w3, [(VE_AERO, calldata("isApprovedForAll(address,address)", ["address", "address"],
                                             [cs(o), cs(MARKET)])) for o in pairs])
    return {o: bool(raw and words(raw)[0]) for o, raw in zip(pairs, raws)}


# ---------------- 価格 ----------------
def dexscreener_price(token):
    r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{token}", timeout=20)
    r.raise_for_status()
    pairs = [p for p in (r.json().get("pairs") or []) if str(p.get("chainId", "")).lower() == "base"
             and str(p.get("baseToken", {}).get("address", "")).lower() == token.lower()]
    if not pairs:
        raise RuntimeError(f"DexScreenerに価格なし: {token}")
    return float(max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))["priceUsd"])


# ---------------- OpenSea（任意） ----------------
def os_listings(st):
    """OpenSea（Base）の veAERO 出品。キーが無ければ空"""
    if not OS_KEY:
        return []
    h = {"X-API-KEY": OS_KEY, "accept": "application/json"}
    if not st.get("os_slug"):
        r = requests.get(f"{OS_API}/chain/base/contract/{VE_AERO}", headers=h, timeout=30)
        r.raise_for_status()
        st["os_slug"] = r.json().get("collection")
    out, nxt = [], None
    for _ in range(10):
        params = {"limit": 100}
        if nxt:
            params["next"] = nxt
        r = requests.get(f"{OS_API}/listings/collection/{st['os_slug']}/all", headers=h, params=params, timeout=30)
        if r.status_code == 429:
            time.sleep(5)
            continue
        r.raise_for_status()
        d = r.json()
        for L in d.get("listings", []):
            params_ = (L.get("protocol_data") or {}).get("parameters") or {}
            nft = None
            for it in params_.get("offer", []):
                if str(it.get("token", "")).lower() == VE_AERO.lower():
                    nft = int(it.get("identifierOrCriteria"))
            cur = ((L.get("price") or {}).get("current")) or {}
            if nft is None or not cur:
                continue
            out.append({"nft": nft, "amount": int(cur.get("value", 0)) / 10 ** int(cur.get("decimals", 18)),
                        "symbol": str(cur.get("currency", "")).upper(), "order_hash": L.get("order_hash")})
        nxt = d.get("next")
        if not nxt:
            break
    return out


# ---------------- 本体 ----------------
class Watcher:
    def __init__(self):
        self.w3 = connect()
        self.st = load_state()
        self.st.setdefault("listings", {})   # 有効な出品（id → 出品）
        self.st.setdefault("offers", {})     # 有効な買い注文
        self.st.setdefault("alerted", {})
        self.st.setdefault("day", {})
        self.locks = {}
        self.offer_px = {}                   # nft → (一番高い買い注文のAERO額, 買い注文id)
        self.offer_ts = 0
        self.price_ts = 0
        self.px = {}
        self.why = {}

    # --- 価格 ---
    def refresh_prices(self, force=False):
        if not force and time.time() - self.price_ts < PRICE_SECONDS:
            return
        try:
            self.px = {"AERO": dexscreener_price(AERO), "WETH": dexscreener_price(WETH), "USDC": 1.0}
            self.price_ts = time.time()
        except Exception as e:
            log("価格の取得に失敗:", e)
            if not self.px:
                raise

    def usd(self, amount, symbol):
        return amount * self.px.get({"ETH": "WETH", "USDBC": "USDC"}.get(symbol, symbol), 0)

    # --- 出品・買い注文の読み込み ---
    def sync_ids(self, key, contract, length_sig, reader):
        n = words(call(self.w3, contract, length_sig))[0]
        start = self.st.get(f"next_{key}")
        if start is None:
            start = max(0, n - BACKFILL)
        ids = list(range(start, n))
        if ids:
            got = reader(self.w3, ids)
            store = self.st[key]
            for i, x in got.items():
                if key == "listings" and (x["sold"] or x["end"] < time.time() or x["collection"] != VE_AERO.lower()):
                    continue
                if key == "offers" and (x["end"] < time.time() or x["budget"] <= 0):
                    continue
                store[str(i)] = x
            self.st[f"next_{key}"] = n
        return n

    def refresh_active(self):
        """有効な出品・買い注文を読み直し、売れた・取り消されたものを外す"""
        ls = self.st["listings"]
        if ls:
            fresh = read_listings(self.w3, [int(i) for i in ls])
            for i in list(ls):
                x = fresh.get(int(i))
                if x is None:
                    continue
                if x["sold"]:
                    self.record_sale(ls[i], x["sold"])
                    del ls[i]
                elif x["end"] < time.time():
                    del ls[i]
                else:
                    x["last_px"] = ls[i].get("last_px")
                    ls[i] = x
        of = self.st["offers"]
        if of:
            fresh = read_offers(self.w3, [int(i) for i in of])
            for i in list(of):
                x = fresh.get(int(i))
                if x is None:
                    continue
                if x["end"] < time.time() or x["budget"] <= 0:
                    del of[i]
                else:
                    if x["budget"] < of[i]["budget"] - 1e-9:
                        self.record_offer_fill(of[i], x)
                    of[i] = x

    def listing_prices(self, ids):
        raws = multicall(self.w3, [(MARKET, calldata("listingPrice(uint256)", ["uint256"], [int(i)])) for i in ids])
        return {i: words(r)[0] for i, r in zip(ids, raws) if r}

    def nonces(self, items):
        raws = multicall(self.w3, [(MARKET, calldata("sellerNftNonce(address,address,uint256)",
                                                     ["address", "address", "uint256"],
                                                     [cs(x["seller"]), cs(VE_AERO), x["nft"]])) for x in items])
        return [words(r)[0] if r else None for r in raws]

    def best_offers(self, nfts, locks):
        """各veAEROを買い注文に売ったら一番いくらになるか（AERO額）。ロック量が範囲外の買い注文は読まない"""
        offers = list(self.st["offers"].values())
        if not offers or not nfts:
            return {}
        pairs = [(n, o) for n in nfts for o in offers if n in locks
                 and o["locked_min"] - 1e-9 <= locks[n]["amt"] <= (o["locked_max"] or 1e30) + 1e-9]
        raws = multicall(self.w3, [(OFFERS, calldata("offerPrice(uint256,uint256)", ["uint256", "uint256"],
                                                     [o["id"], n])) for n, o in pairs])
        best = {}
        for (n, o), r in zip(pairs, raws):
            if not r:
                continue
            p = words(r)[0] / 1e18
            if p <= 0 or p > o["budget"] + 1e-9:
                continue
            if p > best.get(n, (0, None))[0]:
                best[n] = (p, o["id"])
        return best

    # --- 評価 ---
    def evaluate(self):
        ls = self.st["listings"]
        items = list(ls.values())
        if not items:
            return []
        nfts = sorted({x["nft"] for x in items})
        self.locks = read_locks(self.w3, nfts)
        owners = sorted({x["seller"] for x in items})
        appr = approved_all(self.w3, owners)
        cur_nonce = self.nonces(items)
        prices = self.listing_prices([str(x["id"]) for x in items])
        why = {"有効": 0, "NFTが消滅": 0, "ロック量0": 0, "持ち主が変わった": 0, "出品が古い（nonce）": 0,
               "Vexyへの許可なし": 0, "価格が読めない": 0, "対象外の通貨": 0}
        dead = ("NFTが消滅", "ロック量0", "持ち主が変わった", "出品が古い（nonce）")
        good = []
        for x, nonce in zip(items, cur_nonce):
            lk = self.locks.get(x["nft"])
            raw_px = prices.get(str(x["id"]))
            sym, dec = CURRENCIES.get(x["currency"], ("?", 18))
            reason = None
            if not lk:
                reason = "NFTが消滅"
            elif lk["amt"] <= 0:
                reason = "ロック量0"
            elif lk["owner"] != x["seller"]:
                reason = "持ち主が変わった"
            elif nonce != x["nonce"]:
                reason = "出品が古い（nonce）"
            elif raw_px is None:
                reason = "価格が読めない"
            elif sym == "?":
                reason = "対象外の通貨"
                self.st.setdefault("odd_currency", {})[x["currency"]] = x["id"]
            if reason:
                why[reason] += 1
                if reason in dead:
                    ls.pop(str(x["id"]), None)
            elif not (appr.get(x["seller"]) or lk["approved"] == MARKET.lower()):
                why["Vexyへの許可なし"] += 1
            else:
                why["有効"] += 1
                good.append((x, lk, raw_px, sym, dec))
        self.why = why
        if time.time() - self.offer_ts > OFFER_SECONDS:
            self.offer_px = self.best_offers(sorted({g[0]["nft"] for g in good}), self.locks)
            self.offer_ts = time.time()
        rows = []
        for x, lk, raw_px, sym, dec in good:
            amount = raw_px / 10 ** dec
            price_usd = self.usd(amount, sym)
            value_usd = lk["amt"] * self.px["AERO"]
            disc = (1 - price_usd / value_usd) * 100 if value_usd > 0 else 0
            ls[str(x["id"])]["last_px"] = {"amount": amount, "symbol": sym, "usd": price_usd,
                                           "disc": disc, "ts": time.time()}
            op = self.offer_px.get(x["nft"])
            profit = None
            if op:
                cost = price_usd * (1 + FEE_PCT / 100) + GAS_USD * 2
                profit = op[0] * self.px["AERO"] * (1 - FEE_PCT / 100) - cost
            days = (lk["end"] - time.time()) / 86400 if not lk["perm"] else None
            rows.append({"src": "Vexy", "id": x["id"], "nft": x["nft"], "amt": lk["amt"], "perm": lk["perm"],
                         "days": days, "amount": amount, "symbol": sym, "usd": price_usd, "value": value_usd,
                         "disc": disc, "offer": op, "profit": profit, "dutch": bool(x["slope_dur"])})
        return rows

    def evaluate_opensea(self):
        try:
            got = os_listings(self.st)
        except Exception as e:
            log("OpenSeaの出品を読めず:", e)
            return []
        if not got:
            return []
        nfts = sorted({g["nft"] for g in got})
        locks = read_locks(self.w3, nfts)
        missing = [n for n in nfts if n not in self.offer_px]
        if missing:
            self.offer_px.update(self.best_offers(missing, locks))
        rows = []
        for g in got:
            lk = locks.get(g["nft"])
            if not lk or lk["amt"] <= 0:
                continue
            price_usd = self.usd(g["amount"], g["symbol"])
            value_usd = lk["amt"] * self.px["AERO"]
            op = self.offer_px.get(g["nft"])
            profit = None
            if op:
                profit = op[0] * self.px["AERO"] * (1 - FEE_PCT / 100) - price_usd - GAS_USD * 2
            rows.append({"src": "OpenSea", "id": g["order_hash"], "nft": g["nft"], "amt": lk["amt"],
                         "perm": lk["perm"], "days": None if lk["perm"] else (lk["end"] - time.time()) / 86400,
                         "amount": g["amount"], "symbol": g["symbol"], "usd": price_usd, "value": value_usd,
                         "disc": (1 - price_usd / value_usd) * 100 if value_usd > 0 else 0,
                         "offer": op, "profit": profit, "dutch": False})
        return rows

    # --- 記録 ---
    def day(self):
        d = now().strftime("%Y-%m-%d")
        if self.st["day"].get("date") != d:
            self.st["day_prev"] = self.st["day"]
            self.st["day"] = {"date": d, "sales": [], "fills": [], "arb": [], "max_disc": None, "snap": None}
        return self.st["day"]

    def record_sale(self, x, sold_ts):
        p = x.get("last_px")
        lk = self.locks.get(x["nft"]) or {}
        row = {"time": datetime.fromtimestamp(sold_ts, JST).strftime("%Y-%m-%d %H:%M"), "listing": x["id"],
               "nft": x["nft"], "aero": round(lk.get("amt", 0), 2), "perm": lk.get("perm"),
               "price": round(p["amount"], 4) if p else "", "symbol": p["symbol"] if p else "",
               "usd": round(p["usd"], 2) if p else "", "disc_pct": round(p["disc"], 1) if p else ""}
        append_csv("vexy_sales.csv", row)
        self.day()["sales"].append(row)
        log("Vexyで成約:", row)

    def record_offer_fill(self, old, new):
        used = old["budget"] - new["budget"]
        row = {"time": now().strftime("%Y-%m-%d %H:%M"), "offer": old["id"], "aero_paid": round(used, 2),
               "usd": round(used * self.px.get("AERO", 0), 2), "mult": round(old["mult"], 4)}
        append_csv("vexy_offer_fills.csv", row)
        self.day()["fills"].append(row)
        log("買い注文が埋まった:", row)

    def check_alerts(self, rows):
        d = self.day()
        for r in rows:
            if d["max_disc"] is None or r["disc"] > d["max_disc"]["disc"]:
                d["max_disc"] = {"disc": round(r["disc"], 1), "nft": r["nft"], "usd": round(r["usd"], 2),
                                 "src": r["src"]}
            key = f"{r['src']}:{r['id']}:{round(r['usd'])}"
            arb = r["profit"] is not None and r["profit"] >= ALERT_MIN_PROFIT_USD
            if not arb and r["disc"] < ALERT_MIN_DISCOUNT:
                continue
            if key in self.st["alerted"]:
                continue
            self.st["alerted"][key] = time.time()
            row = {"time": now().strftime("%Y-%m-%d %H:%M"), "src": r["src"], "id": r["id"], "nft": r["nft"],
                   "aero": round(r["amt"], 2), "perm": r["perm"], "price": round(r["amount"], 4),
                   "symbol": r["symbol"], "usd": round(r["usd"], 2), "disc_pct": round(r["disc"], 1),
                   "offer_aero": round(r["offer"][0], 2) if r["offer"] else "",
                   "profit_usd": round(r["profit"], 2) if r["profit"] is not None else ""}
            append_csv("opportunities.csv", row)
            if arb:
                d["arb"].append(row)
            lock = "無期限" if r["perm"] else f"残り{r['days']:.0f}日" if r["days"] else "期限切れ"
            head = "💰 即時裁定のチャンス" if arb else "🔎 大きな割引の出品"
            text = (f"{head}（観察のみ・購入はしません）\n"
                    f"{r['src']} veAERO #{r['nft']}：{r['amt']:,.0f} AERO（{lock}）\n"
                    f"出品 {r['amount']:,.4g} {r['symbol']}（${r['usd']:,.2f}）＝ {r['disc']:.1f}%引き")
            if r["offer"]:
                text += f"\n買い注文に売ると {r['offer'][0]:,.0f} AERO（${r['offer'][0] * self.px['AERO']:,.2f}）"
                text += f"\n手数料・ガス差し引きの儲け ${r['profit']:,.2f}"
            notify(text)
        cutoff = time.time() - 14 * 86400
        self.st["alerted"] = {k: v for k, v in self.st["alerted"].items() if v > cutoff}

    def snapshot(self, rows):
        """その日の市場の様子（割引率の分布・買い注文の水準）を1つ残す"""
        d = self.day()
        if not rows:
            return
        discs = sorted(r["disc"] for r in rows)
        offers = [r for r in rows if r["offer"]]
        offer_disc = sorted((1 - r["offer"][0] / r["amt"]) * 100 for r in offers if r["amt"] > 0)
        d["snap"] = {"n": len(rows), "median": round(discs[len(discs) // 2], 1), "max": round(discs[-1], 1),
                     "over20": sum(1 for x in discs if x >= 20), "over30": sum(1 for x in discs if x >= 30),
                     "offer_best": round(offer_disc[0], 1) if offer_disc else None,
                     "offer_n": len(self.st["offers"]), "aero": self.px.get("AERO")}

    def summary(self, d, title):
        s = d.get("snap") or {}
        sales = d.get("sales", [])
        fills = d.get("fills", [])
        arb = d.get("arb", [])
        lines = [f"📊 veAERO 観察レポート（{title} {d.get('date', '')}）",
                 f"AERO ${s.get('aero') or self.px.get('AERO', 0):.3f}",
                 f"Vexyの有効な出品 {s.get('n', 0)}件：割引率の中央値 {s.get('median', '-')}%・最大 {s.get('max', '-')}%"
                 f"・20%以上 {s.get('over20', 0)}件・30%以上 {s.get('over30', 0)}件",
                 f"有効な買い注文 {s.get('offer_n', 0)}件：出品中のveAEROに対する一番良い買値は"
                 f" {s.get('offer_best', '-')}%引き相当"]
        if sales:
            ds = sorted(x["disc_pct"] for x in sales if x["disc_pct"] != "")
            lines.append(f"Vexyで売れた出品 {len(sales)}件（合計 ${sum(x['usd'] or 0 for x in sales):,.0f}）"
                         + (f"：割引率 {ds[0]}〜{ds[-1]}%" if ds else ""))
        else:
            lines.append("Vexyで売れた出品 0件")
        lines.append(f"埋まった買い注文 {len(fills)}件（合計 {sum(x['aero_paid'] for x in fills):,.0f} AERO）")
        if arb:
            lines.append(f"💰 即時裁定のチャンス {len(arb)}回・儲けの合計 ${sum(x['profit_usd'] for x in arb):,.2f}"
                         f"（最大 ${max(x['profit_usd'] for x in arb):,.2f}）")
        else:
            lines.append("即時裁定のチャンス 0回")
        md = d.get("max_disc")
        if md:
            lines.append(f"この日一番の割引：{md['src']} #{md['nft']} {md['disc']}%引き（${md['usd']:,.2f}）")
        fee = self.st.get("fee_check")
        if fee:
            lines.append(f"手数料の確認（実際の成約から）：{fee}")
        notify("\n".join(lines))

    def check_fee(self):
        """実際の成約イベントから、Vexyの手数料が価格の何%かを確かめる（1回だけ）"""
        if self.st.get("fee_check"):
            return
        try:
            head = self.w3.eth.block_number
            topic = "0x" + Web3.keccak(text="BuyListing(uint256,address,uint256,address,address,uint256,uint256)").hex().removeprefix("0x")
            for k in range(30):
                to_b = head - k * LOG_BLOCKS
                logs = self.w3.eth.get_logs({"address": cs(MARKET), "topics": [topic],
                                             "fromBlock": to_b - LOG_BLOCKS + 1, "toBlock": to_b})
                for lg in logs:
                    w = words(bytes(lg["data"]))
                    if len(w) >= 2 and w[-2] > 0:
                        pct = w[-1] / w[-2] * 100
                        self.st["fee_check"] = f"手数料 {pct:.2f}%（価格に対して）"
                        log("手数料の確認:", self.st["fee_check"])
                        return
        except Exception as e:
            log("手数料の確認に失敗:", e)

    # --- ループ ---
    def tick(self):
        self.refresh_prices()
        self.day()
        self.sync_ids("listings", MARKET, "listingsLength()", read_listings)
        self.sync_ids("offers", OFFERS, "offersLength()", read_offers)
        self.refresh_active()
        rows = self.evaluate() + self.evaluate_opensea()
        self.check_alerts(rows)
        self.snapshot([r for r in rows if r["src"] == "Vexy"])
        if now().hour == SUMMARY_HOUR_JST and self.st.get("summary_sent") != now().strftime("%Y-%m-%d"):
            if (self.st.get("day_prev") or {}).get("snap"):
                self.summary(self.st["day_prev"], "前日")
            self.st["summary_sent"] = now().strftime("%Y-%m-%d")
        save_state(self.st)
        return rows

    def run(self):
        self.refresh_prices(force=True)
        rows = self.tick()
        self.check_fee()
        best = max(rows, key=lambda r: r["disc"]) if rows else None
        arbs = [r for r in rows if r["profit"] is not None and r["profit"] > 0]
        text = (f"🟢 {VERSION} 起動（観察のみ・お金は動かしません）\n"
                f"AERO ${self.px['AERO']:.3f} / ETH ${self.px['WETH']:,.0f}\n"
                f"Vexyの有効な出品 {sum(1 for r in rows if r['src'] == 'Vexy')}件・買い注文 {len(self.st['offers'])}件"
                f"・OpenSeaの出品 {sum(1 for r in rows if r['src'] == 'OpenSea')}件"
                + ("" if OS_KEY else "（OPENSEA_API_KEY 未設定のため見ていません）"))
        if best:
            text += f"\n今一番の割引：{best['src']} #{best['nft']} {best['disc']:.1f}%引き（${best['usd']:,.2f}）"
        text += f"\n今すぐ裁定できる出品：{len(arbs)}件"
        snap = (self.st.get("day") or {}).get("snap") or {}
        if snap.get("offer_best") is not None:
            text += f"\n買い注文の一番良い水準：出品中のveAEROに対して {snap['offer_best']}%引き相当"
        text += "\nVexyの出品の内訳：" + "・".join(f"{k} {v}" for k, v in self.why.items() if v)
        if self.st.get("odd_currency"):
            text += "\n対象外の通貨：" + ", ".join(list(self.st["odd_currency"])[:3])
        if self.st.get("fee_check"):
            text += f"\n{self.st['fee_check']}"
        notify(text)
        fails = 0
        while True:
            time.sleep(LOOP_SECONDS)
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
