# =====================================================================
# Neverland veDUST マーケット監視  v3   （GitHub Actions で15分ごとに実行）
#   v2からの変更点
#   - ロック情報（DUST数量・期間の状態）を state に保存して使い回す。毎回調べるのは
#     「初めて見る出品」と「保存から24時間たったもの」だけ。通知の直前には必ず最新の値で確認し直す
#     （実行時間の短縮と、ブロックチェーン側の回数制限の回避）
#   v1→v2の変更（引き継ぎ）
#   - ロック期間の表示を修正：「無期限／残り○日／ロック切れ（解除可能）」を正しく判定
#     判定方法：解除日時が未来なら残り日数。そうでなければ veDUST の議決権（balanceOfNFT）を読み、
#     議決権が残っていれば無期限、ゼロならロック切れ。議決権が読めない場合は OpenSea の属性で補う
#   - 割引率の計算・通知の条件は v1 と同じ
#   OpenSea に出品された veDUST（DUSTをロックしたNFT）のうち、
#   「出品価格 ÷ ロックされているDUSTの時価」が MIN_DISCOUNT_PCT（既定35%）以上安いものを Discord に通知
#
#   割引率 = 1 − 出品価格(USD) ÷（ロック中のDUST数量 × DUST価格(USD)）
#   データ
#     - 出品一覧   : OpenSea API v2（APIキーは自動取得。7日ごとに自動更新。Secret OPENSEA_API_KEY があればそれを優先）
#     - ロック量   : DustLock（veDUST）コントラクトの locked(tokenId) をブロックチェーンから直接読む
#                    読めない場合は OpenSea のNFT属性（traits）から推定
#     - 価格       : DUST＝DexScreener、MON＝NeverlandのAaveOracle（WMON）、ステーブルコイン＝1ドル
#   通知済みの出品は二度通知しない（価格を下げて出し直した場合は新しい出品として通知）
#   ※購入時はOpenSeaの手数料・ロイヤリティ（10%は売り手負担）とは別に、ガス代がかかる
#   ※ロックの残り期間が短いほど、早期解除ペナルティが小さく、割安度の意味が変わる点に注意
# =====================================================================
import os, sys, json, time, traceback
from datetime import datetime, timezone, timedelta
import requests
from web3 import Web3

# ---------------- 設定 ----------------
VERSION          = "v3"
CACHE_HOURS      = 24              # ロック情報を使い回す時間
MIN_DISCOUNT_PCT = 35.0
DUST_LOCK        = "0xBB4738D05AD1b3Da57a4881baE62Ce9bb1eEeD6C"   # veDUST（DustLock Proxy）
DUST             = "0xAD96C3dffCD6374294e2573A7fBBA96097CC8d7c"
WMON             = "0x3bd359C1119dA7Da1D913D1C4D2B7c461115433A"
AAVE_ORACLE      = "0x94bbA11004B9877d13bb5E1aE29319b6f7bDEdD4"
RPC_URLS         = [u for u in [os.environ.get("MONAD_RPC_URL", "").strip(),
                                "https://rpc.monad.xyz", "https://rpc1.monad.xyz", "https://monad.drpc.org"] if u]
OS_API           = "https://api.opensea.io/api/v2"
STABLES          = {"USDC", "USDT", "USDT0", "AUSD", "USDC.E"}
MON_SYMBOLS      = {"MON", "WMON"}
STATE_DIR        = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
STATE_PATH       = os.path.join(STATE_DIR, "neverland_market.json")
WEBHOOK          = os.environ.get("DISCORD_ALERT_WEBHOOK_URL", "").strip()
FORCE_STATUS     = os.environ.get("FORCE_STATUS", "no").strip().lower() == "yes"
OS_KEY_SECRET    = os.environ.get("OPENSEA_API_KEY", "").strip()
JST              = timezone(timedelta(hours=9))
ERROR_NOTIFY_AFTER = 3
os.makedirs(STATE_DIR, exist_ok=True)

ORACLE_ABI = [{"name": "getAssetPrice", "type": "function", "stateMutability": "view",
               "inputs": [{"type": "address"}], "outputs": [{"type": "uint256"}]}]


def now():
    return datetime.now(JST)


def notify(text):
    print(text, flush=True)
    if not WEBHOOK:
        print("（DISCORD_ALERT_WEBHOOK_URL が未設定のため通知は送っていません）")
        return
    for i in range(0, len(text), 1950):
        try:
            requests.post(WEBHOOK, json={"content": text[i:i + 1950]}, timeout=15)
        except Exception as e:
            print("通知失敗:", e)


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {"notified": {}, "errors": 0}


def save_state(st):
    with open(STATE_PATH, "w") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


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
    exp = d.get("expires_at")
    try:
        exp_ts = datetime.fromisoformat(str(exp).replace("Z", "+00:00")).timestamp()
    except Exception:
        exp_ts = time.time() + 6 * 86400
    st["os_key"] = {"key": key, "expires_ts": exp_ts}
    print("OpenSeaのAPIキーを新しく取得しました")
    return key


def os_get(path, key, params=None):
    for i in range(4):
        r = requests.get(f"{OS_API}{path}", headers={"X-API-KEY": key, "accept": "application/json"},
                         params=params, timeout=30)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", 10)))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("OpenSea API の制限にかかりました")


def find_chain(st, key):
    if st.get("os_chain"):
        return st["os_chain"]
    chain = "monad"
    try:
        d = os_get("/chains", key)
        items = d.get("chains", d if isinstance(d, list) else [])
        for c in items:
            ident = str(c.get("identifier") or c.get("chain") or c.get("name") or "")
            if "monad" in ident.lower() and "test" not in ident.lower():
                chain = c.get("identifier") or c.get("chain") or ident
                break
    except Exception as e:
        print("チェーン一覧の取得に失敗（'monad' と仮定）:", e)
    st["os_chain"] = chain
    return chain


def find_slug(st, key, chain):
    if st.get("os_slug"):
        return st["os_slug"]
    d = os_get(f"/chain/{chain}/contract/{DUST_LOCK}", key)
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
        d = os_get(f"/listings/collection/{slug}/all", key, params)
        out += d.get("listings", [])
        nxt = d.get("next")
        if not nxt:
            break
    return out


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
            "end": int(params.get("endTime", 0) or 0)}


def nft_traits(key, chain, token_id):
    try:
        d = os_get(f"/chain/{chain}/contract/{DUST_LOCK}/nfts/{token_id}", key)
        return (d.get("nft") or {}).get("traits") or []
    except Exception:
        return []


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


def call_uint(w3, signature, token_id):
    sel = bytes(Web3.keccak(text=signature))[:4]
    raw = bytes(w3.eth.call({"to": Web3.to_checksum_address(DUST_LOCK),
                             "data": "0x" + (sel + token_id.to_bytes(32, "big")).hex()}))
    return [int.from_bytes(raw[i:i + 32], "big") for i in range(0, len(raw), 32)]


DEBUG_LEFT = [5]   # Actionsのログに生データを出す件数（調査用）


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
    power = None
    if end > nowts + 3600:
        status, days = "days", (end - nowts) / 86400
    else:
        power = voting_power(w3, token_id)
        days = None
        if power is None:
            status = "unknown"
        elif power > 0 and amt > 0 and power >= amt * 0.5:
            status = "perm"
        elif power == 0:
            status = "expired"
        else:
            status = "unknown"
    if DEBUG_LEFT[0] > 0:
        DEBUG_LEFT[0] -= 1
        print(f"[調査] #{token_id} locked()={w[:4]} 議決権={power} → {status}")
    return amt, status, days


def status_from_traits(traits):
    for t in traits:
        name, val = str(t.get("trait_type", "")).lower(), str(t.get("value", "")).lower()
        if any(k in name or k in val for k in ("infinite", "permanent", "無期限")):
            if val in ("false", "no", "0"):
                continue
            return "perm"
    return "unknown"


def dust_from_traits(traits):
    for t in traits:
        name = str(t.get("trait_type", "")).lower()
        if "dust" in name and ("lock" in name or "amount" in name) and "ve" not in name.replace("dust", ""):
            try:
                return float(str(t.get("value")).replace(",", ""))
            except Exception:
                pass
    return None


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


def lock_info(st, w3, key, chain, token_id, force=False):
    """ロック情報を保存データから返す（古い・未保存・force のときはチェーンから取得して保存）"""
    cache = st.setdefault("locks", {})
    c = cache.get(str(token_id))
    nowts = time.time()
    if c and not force and nowts - c["ts"] < CACHE_HOURS * 3600:
        days = (c["end"] - nowts) / 86400 if c["status"] == "days" else None
        if c["status"] == "days" and days is not None and days <= 0:
            pass                                          # 期限を過ぎたので取り直す
        else:
            return c["amt"], c["status"], days, c["src"] + "（保存）"
    try:
        amt, status, days = locked(w3, token_id)
        src = "チェーン"
    except Exception as e:
        traits = nft_traits(key, chain, token_id)
        amt, status, days, src = dust_from_traits(traits), status_from_traits(traits), None, "属性"
        if amt is None:
            print(f"#{token_id}: ロック量を取得できず（{e}）")
            return None
    end = nowts + days * 86400 if days is not None else 0
    cache[str(token_id)] = {"amt": amt, "status": status, "end": end, "src": src, "ts": nowts}
    return amt, status, days, src


# ---------------- メイン ----------------
def main():
    st = load_state()
    t = now()
    try:
        key = api_key(st)
        chain = find_chain(st, key)
        slug = find_slug(st, key, chain)
        raw = all_listings(key, slug)
        w3 = connect()
        dust_px = dexscreener_price(DUST)
        mon_px = mon_price(w3)
    except Exception:
        st["errors"] = st.get("errors", 0) + 1
        tb = traceback.format_exc()
        print(tb)
        if st["errors"] == ERROR_NOTIFY_AFTER or FORCE_STATUS:
            notify(f"⚠ veDUSTマーケット監視：{st['errors']}回連続で取得に失敗\n```\n{tb[-1200:]}\n```")
        save_state(st)
        return
    st["errors"] = 0

    rows = []
    for L in raw:
        p = parse_listing(L)
        if p["token_id"] is None:
            continue
        if p["currency"] in MON_SYMBOLS:
            price_usd = p["amount"] * mon_px
        elif p["currency"] in STABLES:
            price_usd = p["amount"]
        else:
            print(f"未対応の通貨 {p['currency']}（#{p['token_id']}）は対象外")
            continue
        info = lock_info(st, w3, key, chain, p["token_id"])
        if info is None:
            continue
        dust_amt, status, days, src = info
        value_usd = dust_amt * dust_px
        if value_usd <= 0:
            continue
        disc = (1 - price_usd / value_usd) * 100
        if disc >= MIN_DISCOUNT_PCT and p["order_hash"] not in st["notified"] and src.endswith("（保存）"):
            info = lock_info(st, w3, key, chain, p["token_id"], force=True)   # 通知前に最新の値で確認
            if info is None:
                continue
            dust_amt, status, days, src = info
            value_usd = dust_amt * dust_px
            if value_usd <= 0:
                continue
            disc = (1 - price_usd / value_usd) * 100
        if status == "unknown" and disc >= MIN_DISCOUNT_PCT:
            status = status_from_traits(nft_traits(key, chain, p["token_id"]))
        rows.append({**p, "price_usd": price_usd, "dust": dust_amt, "value_usd": value_usd,
                     "discount": disc, "days": days, "status": status, "src": src})

    rows.sort(key=lambda r: -r["discount"])
    link = lambda r: f"https://opensea.io/assets/{chain}/{DUST_LOCK}/{r['token_id']}"
    dur = lambda r: {"perm": "無期限", "expired": "ロック切れ・解除可能", "unknown": "期間不明"}.get(
        r["status"], f"残り{r['days']:.0f}日" if r["days"] is not None else "期間不明")

    def line(r):
        return (f"#{r['token_id']}  割引 {r['discount']:+.1f}%  価格 {r['amount']:,.2f} {r['currency']}（${r['price_usd']:,.0f}）"
                f"  ロック {r['dust']:,.0f} DUST（${r['value_usd']:,.0f}・{dur(r)}）")

    hits = [r for r in rows if r["discount"] >= MIN_DISCOUNT_PCT and r["order_hash"] not in st["notified"]]
    for r in hits:
        notify(f"💎 veDUST 割安出品（{MIN_DISCOUNT_PCT:.0f}%以上）  {t:%m/%d %H:%M}\n```\n{line(r)}\n```\n{link(r)}\n"
               f"DUST ${dust_px:.4g} ／ MON ${mon_px:.4g}（ロック量の取得元：{r['src']}）")
        st["notified"][r["order_hash"]] = t.isoformat()

    if FORCE_STATUS:
        top = "\n".join(line(r) for r in rows[:10]) or "（出品なし）"
        notify(f"ℹ veDUST マーケット（手動確認）  {t:%m/%d %H:%M}  出品{len(rows)}件  DUST ${dust_px:.4g} ／ MON ${mon_px:.4g}\n```\n{top}\n```")

    cutoff = t - timedelta(days=30)
    st["notified"] = {h: ts for h, ts in st["notified"].items() if datetime.fromisoformat(ts) > cutoff}
    listed = {str(r["token_id"]) for r in rows}
    st["locks"] = {k: v for k, v in st.get("locks", {}).items()
                   if k in listed or time.time() - v["ts"] < 7 * 86400}   # 出品が消えて1週間たった分は削除
    print(f"出品 {len(rows)}件、うち通知 {len(hits)}件")
    save_state(st)


if __name__ == "__main__":
    main()
