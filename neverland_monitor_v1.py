# =====================================================================
# Neverland（Monad）借入レート監視  v1   （GitHub Actions で3時間ごとに実行）
#   cbBTC を借りたときの「実質の受け取り利回り」＝ 借入インセンティブ（DUST等）の年率 − 借入金利（APY）
#   これが MIN_NET_PCT（既定 +1%）を下回ったら Discord の別チャンネルに通知する
#
#   データはすべてブロックチェーンから直接読む（Neverland は Aave V3 ベース）
#     - 借入金利        : Pool.getReserveData(cbBTC) の currentVariableBorrowRate
#     - 借入残高         : 変動借入トークン（variableDebtCBBTC）の totalSupply
#     - インセンティブ    : 借入トークンの IncentivesController → getRewardsByAsset / getRewardsData
#     - 価格            : AaveOracle（cbBTC、WMON）／報酬トークンのオラクル／予備として DexScreener
#   ※アプリ画面の「LOCK +x%」（veDUSTロックによる上乗せ）は含めない
#   ※アドレスは Neverland 公式ドキュメント（docs.neverland.money/smart-contracts）より
# =====================================================================
import os, sys, json, time, traceback
from datetime import datetime, timezone, timedelta
import requests
from web3 import Web3

# ---------------- 設定 ----------------
VERSION      = "v1"
RPC_URLS     = [u for u in [os.environ.get("MONAD_RPC_URL", "").strip(),
                            "https://rpc.monad.xyz", "https://rpc1.monad.xyz", "https://monad.drpc.org"] if u]
POOL_ADDRESSES_PROVIDER = "0x49D75170F55C964dfdd6726c74fdEDEe75553A0f"   # メインマーケット
POOL         = "0x80F00661b13CC5F6ccd3885bE7b4C9c67545D585"
AAVE_ORACLE  = "0x94bbA11004B9877d13bb5E1aE29319b6f7bDEdD4"
DUST         = "0xAD96C3dffCD6374294e2573A7fBBA96097CC8d7c"
WMON         = "0x3bd359C1119dA7Da1D913D1C4D2B7c461115433A"
NWMON        = "0xD0fd2Cf7F6CEff4F96B1161F5E995D5843326154"
# 監視する銘柄：(表示名, 原資産アドレス, 変動借入トークン（照合用）, 通知のしきい値%)
MONITORS = [
    ("cbBTC", "0xd18B7EC58Cdf4876f6AFebd3Ed1730e4Ce10414b", "0x74F2ef7c9cf6D3a3587c3F91569827CC17715F08", 1.0),
]
REALERT_HOURS = 24          # しきい値を下回っている間は、24時間ごとに再通知
ERROR_NOTIFY_AFTER = 3      # 連続3回取得に失敗したらエラー通知
SECONDS_PER_YEAR = 31536000
STATE_DIR    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
STATE_PATH   = os.path.join(STATE_DIR, "neverland_monitor.json")
HIST_PATH    = os.path.join(STATE_DIR, "neverland_rates.csv")
WEBHOOK      = os.environ.get("DISCORD_ALERT_WEBHOOK_URL", "").strip()
FORCE_STATUS = os.environ.get("FORCE_STATUS", "no").strip().lower() == "yes"
JST          = timezone(timedelta(hours=9))
os.makedirs(STATE_DIR, exist_ok=True)

ERC20_ABI = [
    {"name": "decimals", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint8"}]},
    {"name": "symbol", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "string"}]},
    {"name": "totalSupply", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint256"}]},
    {"name": "getIncentivesController", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "address"}]},
    {"name": "UNDERLYING_ASSET_ADDRESS", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "address"}]},
]
REWARDS_ABI = [
    {"name": "getRewardsByAsset", "type": "function", "stateMutability": "view",
     "inputs": [{"type": "address"}], "outputs": [{"type": "address[]"}]},
    {"name": "getRewardsData", "type": "function", "stateMutability": "view",
     "inputs": [{"type": "address"}, {"type": "address"}],
     "outputs": [{"type": "uint256"}, {"type": "uint256"}, {"type": "uint256"}, {"type": "uint256"}]},
    {"name": "getRewardOracle", "type": "function", "stateMutability": "view",
     "inputs": [{"type": "address"}], "outputs": [{"type": "address"}]},
]
ORACLE_ABI = [
    {"name": "getAssetPrice", "type": "function", "stateMutability": "view",
     "inputs": [{"type": "address"}], "outputs": [{"type": "uint256"}]},
    {"name": "BASE_CURRENCY_UNIT", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint256"}]},
]
FEED_ABI = [
    {"name": "latestAnswer", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "int256"}]},
    {"name": "decimals", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint8"}]},
]


# ---------------- 共通 ----------------
def now_jst():
    return datetime.now(JST)


def notify(text):
    print(text, flush=True)
    if not WEBHOOK:
        print("（DISCORD_ALERT_WEBHOOK_URL が未設定のため通知は送っていません）")
        return
    try:
        requests.post(WEBHOOK, json={"content": text[:1990]}, timeout=15)
    except Exception as e:
        print("通知失敗:", e)


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {"assets": {}, "errors": 0}


def save_state(st):
    with open(STATE_PATH, "w") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


def connect():
    last = None
    for url in RPC_URLS:
        try:
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 20}))
            cid = w3.eth.chain_id
            print(f"RPC接続: {url}（chain id {cid}）")
            return w3
        except Exception as e:
            last = e
            print(f"RPC接続失敗: {url}: {e}")
    raise RuntimeError(f"どのRPCにも接続できませんでした: {last}")


def C(a):
    return Web3.to_checksum_address(a)


def dexscreener_price(token):
    r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{token}", timeout=20)
    r.raise_for_status()
    pairs = [p for p in (r.json().get("pairs") or []) if "monad" in str(p.get("chainId", "")).lower()
             and str(p.get("baseToken", {}).get("address", "")).lower() == token.lower()]
    if not pairs:
        raise RuntimeError("DexScreenerに該当ペアなし")
    best = max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))
    return float(best["priceUsd"])


# ---------------- 読み取り ----------------
def reserve_data(w3, asset):
    """Pool.getReserveData（Aave V3 の ReserveDataLegacy）を32バイトずつ読む（構造体の名前の違いに左右されない）"""
    sel = bytes(Web3.keccak(text="getReserveData(address)"))[:4]
    data = sel + bytes(12) + bytes.fromhex(C(asset)[2:])
    raw = bytes(w3.eth.call({"to": C(POOL), "data": "0x" + data.hex()}))
    if len(raw) < 11 * 32:
        raise RuntimeError(f"getReserveData の戻り値が想定より短い（{len(raw)}バイト）")
    words = [raw[i:i + 32] for i in range(0, len(raw), 32)]
    as_int = lambda b: int.from_bytes(b, "big")
    as_addr = lambda b: C("0x" + bytes(b[-20:]).hex())
    return {"variable_borrow_rate_ray": as_int(words[4]), "variable_debt_token": as_addr(words[10])}


def oracle_price_usd(w3, oracle, asset):
    o = w3.eth.contract(address=C(oracle), abi=ORACLE_ABI)
    try:
        unit = o.functions.BASE_CURRENCY_UNIT().call()
    except Exception:
        unit = 10 ** 8
    return o.functions.getAssetPrice(C(asset)).call() / unit


def reward_price_usd(w3, controller, reward):
    """報酬トークンの価格：①報酬オラクル ②AaveOracle（nTokenは原資産で） ③DexScreener"""
    try:
        feed = controller.functions.getRewardOracle(C(reward)).call()
        if int(feed, 16) != 0:
            f = w3.eth.contract(address=C(feed), abi=FEED_ABI)
            try:
                dec = f.functions.decimals().call()
            except Exception:
                dec = 8
            px = f.functions.latestAnswer().call() / 10 ** dec
            if px > 0:
                return px, "報酬オラクル"
    except Exception:
        pass
    target = reward
    try:
        target = w3.eth.contract(address=C(reward), abi=ERC20_ABI).functions.UNDERLYING_ASSET_ADDRESS().call()
    except Exception:
        pass
    try:
        px = oracle_price_usd(w3, AAVE_ORACLE, target)
        if px > 0:
            return px, "AaveOracle"
    except Exception:
        pass
    return dexscreener_price(target), "DexScreener"


def measure(w3, name, asset, vdebt_expected):
    rd = reserve_data(w3, asset)
    vdebt = rd["variable_debt_token"]
    if vdebt.lower() != vdebt_expected.lower():
        raise RuntimeError(f"{name}: 借入トークンのアドレスがドキュメントと一致しません（チェーン上 {vdebt}）")
    rate = rd["variable_borrow_rate_ray"] / 1e27
    borrow_apy = ((1 + rate / SECONDS_PER_YEAR) ** SECONDS_PER_YEAR - 1) * 100

    erc_asset = w3.eth.contract(address=C(asset), abi=ERC20_ABI)
    erc_debt = w3.eth.contract(address=vdebt, abi=ERC20_ABI)
    a_dec = erc_asset.functions.decimals().call()
    total_debt = erc_debt.functions.totalSupply().call() / 10 ** a_dec
    a_px = oracle_price_usd(w3, AAVE_ORACLE, asset)
    debt_usd = total_debt * a_px

    ctrl_addr = erc_debt.functions.getIncentivesController().call()
    ctrl = w3.eth.contract(address=C(ctrl_addr), abi=REWARDS_ABI)
    rewards = ctrl.functions.getRewardsByAsset(vdebt).call()
    now_ts = int(time.time())
    parts = []
    for rw in rewards:
        _, eps, _, end = ctrl.functions.getRewardsData(vdebt, C(rw)).call()
        erc_rw = w3.eth.contract(address=C(rw), abi=ERC20_ABI)
        try:
            sym = erc_rw.functions.symbol().call()
        except Exception:
            sym = rw[:8]
        r_dec = erc_rw.functions.decimals().call()
        if eps == 0 or (end and end < now_ts) or debt_usd <= 0:
            parts.append({"symbol": sym, "apr": 0.0, "price": None, "src": "配布停止中"})
            continue
        px, src = reward_price_usd(w3, ctrl, rw)
        apr = eps * SECONDS_PER_YEAR / 10 ** r_dec * px / debt_usd * 100
        parts.append({"symbol": sym, "apr": apr, "price": px, "src": src,
                      "ends": datetime.fromtimestamp(end, JST).strftime("%m/%d %H:%M") if end else "-"})
    reward_apr = sum(p["apr"] for p in parts)
    return {"name": name, "borrow_apy": borrow_apy, "reward_apr": reward_apr, "net": reward_apr - borrow_apy,
            "parts": parts, "debt_usd": debt_usd, "asset_px": a_px, "controller": ctrl_addr}


def fmt(m, th):
    lines = [f"{m['name']}  実質 {m['net']:+.2f}%（しきい値 {th:+.1f}%）",
             f"  借入金利 −{m['borrow_apy']:.2f}% ／ インセンティブ +{m['reward_apr']:.2f}%"]
    for p in m["parts"]:
        px = f"${p['price']:.4g}" if p.get("price") else "-"
        lines.append(f"    {p['symbol']}: +{p['apr']:.2f}%（価格 {px}・{p['src']}・終了 {p.get('ends', '-')}）")
    lines.append(f"  借入総額 ${m['debt_usd']:,.0f}  {m['name']}価格 ${m['asset_px']:,.0f}")
    return "\n".join(lines)


# ---------------- メイン ----------------
def main():
    st = load_state()
    t = now_jst()
    try:
        w3 = connect()
        results = [(measure(w3, n, a, v), th) for n, a, v, th in MONITORS]
    except Exception:
        st["errors"] = st.get("errors", 0) + 1
        tb = traceback.format_exc()
        print(tb)
        if st["errors"] == ERROR_NOTIFY_AFTER or FORCE_STATUS:
            notify(f"⚠ Neverland監視：{st['errors']}回連続でデータ取得に失敗\n```\n{tb[-1200:]}\n```")
        save_state(st)
        return
    st["errors"] = 0

    rows = []
    for m, th in results:
        a = st["assets"].setdefault(m["name"], {"below": False, "last_alert": None})
        below = m["net"] < th
        body = fmt(m, th)
        print(body)
        rows.append(f"{t:%Y-%m-%d %H:%M},{m['name']},{m['borrow_apy']:.4f},{m['reward_apr']:.4f},{m['net']:.4f},{m['debt_usd']:.0f}")
        last = datetime.fromisoformat(a["last_alert"]) if a.get("last_alert") else None
        if below and (not a["below"] or last is None or t - last >= timedelta(hours=REALERT_HOURS)):
            head = "🚨 Neverland 借入レート警告" if not a["below"] else "🚨（継続中）Neverland 借入レート警告"
            notify(f"{head}  {t:%m/%d %H:%M}\n```\n{body}\n```\n→ 借入の返済・縮小を検討してください")
            a["last_alert"] = t.isoformat()
        elif not below and a["below"]:
            notify(f"✅ Neverland 借入レート回復  {t:%m/%d %H:%M}\n```\n{body}\n```")
            a["last_alert"] = None
        elif FORCE_STATUS:
            notify(f"ℹ Neverland 借入レート（手動確認）  {t:%m/%d %H:%M}\n```\n{body}\n```")
        a["below"] = below

    new = not os.path.exists(HIST_PATH)
    with open(HIST_PATH, "a") as f:
        if new:
            f.write("time_jst,asset,borrow_apy,reward_apr,net,debt_usd\n")
        f.write("\n".join(rows) + "\n")
    save_state(st)


if __name__ == "__main__":
    main()
