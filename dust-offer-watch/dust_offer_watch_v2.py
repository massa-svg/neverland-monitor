# =====================================================================
# veDUST 買いオファー 試運転ボット  v2   （Railway などで常時実行・お金は一切動かさない）
#   v1からの変更点
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
#     SKIP_OWNERS                任意。オファーの対象から外すウォレットのアドレス（カンマ区切り）
#                                ボット用ウォレット・自分の本体ウォレットを入れる（秘密鍵ではなくアドレス）
#     MONAD_RPC_URL / OPENSEA_API_KEY   任意。あれば優先して使う
# =====================================================================
import os, sys, json, time, csv, traceback
from datetime import datetime, timezone, timedelta
import requests
from web3 import Web3
from eth_abi import encode, decode

VERSION = "dust offer watch v2"

# ---------------- オファーの条件（試算に使う） ----------------
OFFER_DISCOUNT_PCT = 45.0      # ロック価値の何%引きでオファーを出すか
MIN_VALUE_USD      = 10.0      # ロック価値がこの金額（ドル）以上の veDUST だけを対象にする
PERM_ONLY          = True      # 無期限ロックだけを対象にする
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
RPC_URLS = [u for u in [os.environ.get("MONAD_RPC_URL", "").strip(),
                        "https://rpc.monad.xyz", "https://rpc1.monad.xyz", "https://monad.drpc.org"] if u]
OS_API      = "https://api.opensea.io/api/v2"
STABLES     = {"USDC", "USDT", "USDT0", "AUSD", "USDC.E"}
MON_SYMBOLS = {"MON", "WMON"}
POWER_SIGS  = ("balanceOfNFT(uint256)", "votingPower(uint256)", "getVotingPower(uint256)")
JST         = timezone(timedelta(hours=9))

WEBHOOK       = os.environ.get("DISCORD_ALERT_WEBHOOK_URL", "").strip()
OS_KEY_SECRET = os.environ.get("OPENSEA_API_KEY", "").strip()
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
    r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{token}", timeout=20)
    r.raise_for_status()
    pairs = [p for p in (r.json().get("pairs") or []) if "monad" in str(p.get("chainId", "")).lower()
             and str(p.get("baseToken", {}).get("address", "")).lower() == token.lower()]
    if not pairs:
        raise RuntimeError(f"DexScreenerに価格なし: {token}")
    return float(max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))["priceUsd"])


def mon_price(w3):
    try:
        w = words(call(w3, AAVE_ORACLE, "getAssetPrice(address)", ["address"], [cs(WMON)]))
        if w and w[0] > 0:
            return w[0] / 1e8
    except Exception:
        pass
    return dexscreener_price(WMON)


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
                "offerer": str(params.get("offerer", "")).lower(), "end": int(params.get("endTime", 0) or 0)}
    except Exception:
        return None


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
            self.dust_px = dexscreener_price(DUST)
            self.mon_px = mon_price(self.w3)
            self.px_ts = time.time()
        except Exception as e:
            log("価格の取得に失敗:", e)
            if self.dust_px is None:
                raise

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
        if now().hour == SUMMARY_HOUR_JST and self.st.get("summary_sent") != now().strftime("%Y-%m-%d"):
            if self.st.get("day_prev"):
                self.summary(self.st["day_prev"], "前日")
            self.st["summary_sent"] = now().strftime("%Y-%m-%d")
        save_state(self.st)

    def run(self):
        self.refresh_prices(force=True)
        notify(f"🟢 {VERSION} 起動（観察のみ・お金は動かしません）\n"
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
