"""因子策略引擎：評分 → 選股 → 配權重 → Active Share 控制 → 決策與委託。

移植自同隊組員的 build_portfolio.py（選股、配權重、Active Share）與 generate_dplan.py（決策、現金規則、
委託後 Active Share、posture），邏輯逐行對照原始碼，只改成「傳入資料、回傳結果」的函式（不讀寫檔案），
由 run_daily.py 組裝成 D-Plan。參數一律取自 strategy.py。

Active Share 官方口徑（Slack 答覆）：與每檔主動型 ETF 分別計算；雙方皆用占淨值的原始權重，不重新加總為 100%；
採當日公告的 ETF 前十五大（含海外股，排除期貨、選擇權）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
import math

import pandas as pd

from . import rules, strategy as S
from .news_policy import news_veto

FEE, TAX = Decimal(str(rules.FEE_RATE)), Decimal(str(rules.TAX_RATE))


# ---------------------------------------------------------------- Active Share
def etf_top_holdings(holdings: pd.DataFrame) -> dict[str, pd.Series]:
    """每檔 ETF 實際公布的前 15 大個股（含海外股，排除期貨、選擇權），權重轉為小數。
    台股以代號為鍵，海外股加上市場別避免與台股代號相撞，無代號者以名稱為鍵。"""
    h = holdings.copy()
    h["code"] = h["code"].fillna("").astype(str)
    s = h[h["is_stock"].astype(str) == "True"].copy()
    s["key"] = s.apply(lambda r: r["code"] if r["market"] in ("TW", "TWO") else f"{r['market']}:{r['code'] or r['name']}",
                       axis=1)
    if s.duplicated(["etf_id", "key"]).any():
        raise ValueError("ETF 持股有重複股票識別，請核對原始資料")
    s["weight_pct"] = pd.to_numeric(s["weight_pct"])
    if not s["weight_pct"].between(0, 100).all():
        raise ValueError("ETF 個股權重缺值或超出 0–100%，不能略過後計算前十五大")
    s = s.sort_values("weight_pct", ascending=False).groupby("etf_id").head(rules.ACTIVE_SHARE_TOP_N)
    return {etf: g.set_index("key")["weight_pct"] / 100 for etf, g in s.groupby("etf_id")}


def active_share(ours: pd.Series, bench: pd.Series) -> float:
    """½ × Σ|我方權重 − ETF 權重|，只出現在一方的股票另一方視為 0"""
    idx = ours.index.union(bench.index)
    return 0.5 * float((ours.reindex(idx, fill_value=0) - bench.reindex(idx, fill_value=0)).abs().sum())


def check_active_share(weights: pd.Series, benches: dict[str, pd.Series]) -> pd.DataFrame:
    """每檔 ETF 的 Active Share 與重疊股票，由低到高排序"""
    top = weights.sort_values(ascending=False).head(rules.ACTIVE_SHARE_TOP_N)
    rows = [{"etf_id": etf, "active_share": active_share(top, b), "overlap": " ".join(top.index.intersection(b.index))}
            for etf, b in benches.items()]
    return pd.DataFrame(rows).set_index("etf_id").sort_values("active_share")


# ---------------------------------------------------------------- 選股與配權重（build_portfolio.py）
def cap_weights(raw: pd.Series, caps: pd.Series, total: float) -> pd.Series:
    """依 raw 的比例分配 total，超過 caps 的部分依比例轉給未達上限者，反覆直到全部符合"""
    if (raw.empty or not raw.index.is_unique or not raw.index.equals(caps.index)
            or not math.isfinite(total) or total <= 0
            or not raw.map(math.isfinite).all() or not (raw > 0).all()
            or not caps.map(math.isfinite).all() or not (caps >= 0).all()
            or caps.sum() < total - 1e-12):
        raise ValueError('配權輸入無效或權重上限總和不足；禁止產生 NaN 投組')
    w = raw / raw.sum() * total
    fixed = pd.Series(False, index=w.index)
    for _ in range(len(w)):
        over = (w > caps) & ~fixed
        if not over.any():
            break
        fixed |= over
        w[fixed] = caps[fixed]
        free = ~fixed
        w[free] = raw[free] / raw[free].sum() * (total - w[fixed].sum())
    return w


@dataclass
class Targets:
    scored: pd.DataFrame          # 150 檔總分與排名
    picks: pd.DataFrame           # 目標持股（weight、cap）
    as_report: pd.DataFrame       # 目標權重的 Active Share
    vetoed: pd.DataFrame          # 重大利空否決名單（held 欄標示是否持有）
    adjustments: list[str] = field(default_factory=list)


def build_targets(master: pd.DataFrame, pv: pd.DataFrame, fund: pd.DataFrame, news: pd.DataFrame | None,
                  held: list[str], benches: dict[str, pd.Series]) -> Targets:
    if any(not f.index.is_unique for f in (master, pv, fund)) or (news is not None and not news.index.is_unique):
        raise ValueError('策略輸入有重複股票索引')
    if not benches:
        raise ValueError('缺少 ETF 基準')
    if news is not None and not news.empty:
        scores = pd.to_numeric(news['news_score'], errors='raise')
        if not scores.dropna().between(0, 1).all():
            raise ValueError('新聞分數超出 0–1')
    df = master[["name", "market", "industry"]].copy()
    df = df.join(pv[["pv_score", "vol60", "A3"]]).join(fund[["fund_score"]])
    if news is not None and len(news):
        df = df.join(news[["news_score", "worst_event"]])
    else:
        df["news_score"], df["worst_event"] = S.NEUTRAL_SCORE, ""
    cols = list(S.SCORE_WEIGHTS)
    df[cols] = df[cols].fillna(S.NEUTRAL_SCORE)
    df["worst_event"] = df["worst_event"].fillna("")
    if (df['worst_event'].str.startswith(f'{S.VETO_MATERIALITY}:') & df['A3'].isna()).any():
        raise ValueError('重大利空缺少正式 A3；不得默默解除否決')
    df["veto"] = news_veto(df["worst_event"], df["A3"])
    df["total_score"] = sum(df[c] * w for c, w in S.SCORE_WEIGHTS.items())
    df = df.sort_values("total_score", ascending=False)
    df["rank"] = range(1, len(df) + 1)

    # 換股緩衝：已持有者仍在前 BUFFER_RANK 名就續抱；重大利空否決者不買進、已持有者不受緩衝保護
    kept = [t for t in df.index if t in held and df.loc[t, "rank"] <= S.BUFFER_RANK and not df.loc[t, "veto"]]
    new = [t for t in df.index if t not in kept and not df.loc[t, "veto"]][: max(S.N_HOLDINGS - len(kept), 0)]
    vetoed = df[df["veto"]].copy()
    vetoed["held"] = [t in held for t in vetoed.index]
    picks = df.loc[kept + new].copy()
    if picks.empty:
        raise ValueError('沒有可用候選股票')
    # 權重 ∝（總分 − 0.5）÷ 60 日波動率；下限 0.01 避免總分約 0.5 的股票權重歸零
    edge = (picks["total_score"] - S.NEUTRAL_SCORE).clip(lower=0.01)
    raw_weight = edge / picks["vol60"].fillna(picks["vol60"].median())
    if not raw_weight.map(math.isfinite).all() or not (raw_weight > 0).all():
        raise ValueError('波動率為零／負值／不可得，無法安全配權')
    caps = pd.Series(S.CAP_OTHER, index=picks.index)
    if rules.TSMC in caps.index:
        caps[rules.TSMC] = S.CAP_2330

    adjustments = []
    for _ in range(S.MAX_AS_ITERATIONS):
        weights = cap_weights(raw_weight, caps, 1 - S.TARGET_CASH)
        report = check_active_share(weights, benches)
        worst = report.iloc[0]
        if worst["active_share"] >= S.AS_THRESHOLD:
            break
        top = weights.sort_values(ascending=False).head(rules.ACTIVE_SHARE_TOP_N)
        b = benches[worst.name]
        shared = top.index.intersection(b.index)
        if shared.empty:
            raise RuntimeError(f"{worst.name} 與我方前 15 大沒有重疊，Active Share 仍只有 {worst['active_share']:.1%}")
        target = max(shared, key=lambda t: min(top[t], b[t]))
        # 從「目前權重」往下調，而非從上限往下調
        caps[target] = max(min(caps[target], weights[target]) - 0.01, 0.005)
        adjustments.append(f"{target} 上限調降至 {caps[target]:.1%}（因 {worst.name} 僅 {worst['active_share']:.1%}）")
    else:
        raise RuntimeError(f"調整 {S.MAX_AS_ITERATIONS} 次仍無法讓 Active Share 全部 ≥ {S.AS_THRESHOLD:.0%}")

    # 目標權重取 4 位小數：組員管線經 CSV 存檔（round(4)）後才下單，回測亦同；保持一致，
    # 否則落在「四捨五入到最接近整張」邊界的股票會差一張（10/02 驗收時 6770 差 1 張）
    picks["weight"] = weights.round(4)
    picks["cap"] = caps
    picks = picks.sort_values("weight", ascending=False)
    return Targets(scored=df, picks=picks, as_report=report, vetoed=vetoed, adjustments=adjustments)


# ---------------------------------------------------------------- 決策與委託（generate_dplan.py）
def target_shares(weight, nav: Decimal, price) -> int:
    """官方公式：floor(target_weight × 前日淨值 ÷ 前日收盤價 ÷ 1000) × 1000"""
    lots = (Decimal(str(weight)) * Decimal(str(nav)) / Decimal(str(price)) / 1000).to_integral_value(ROUND_FLOOR)
    return int(lots) * 1000


def lots_weight(lots: int, nav: Decimal, price) -> float:
    """剛好換算回 lots 張的最小 target_weight（小數 6 位，無條件進位）"""
    tw = (lots * Decimal(str(price)) * 1000 / nav * 10 ** 6).to_integral_value(ROUND_CEILING) / 10 ** 6
    return float(tw)


def lot_aligned_weight(weight, nav: Decimal, price, cap) -> float:
    """把目標權重對齊到最接近的整張（官方公式無條件捨去，高價股會少買近一張），不超過 cap。"""
    lot_value = Decimal(str(price)) * 1000
    lots = int((Decimal(str(weight)) * nav / lot_value).to_integral_value(ROUND_HALF_UP))
    while lots > 0 and lots * lot_value / nav > Decimal(str(cap)):
        lots -= 1
    return lots_weight(lots, nav, price)


def estimate_cash(cash: Decimal, decisions: list[dict]) -> tuple[Decimal, Decimal, Decimal]:
    """以 T-1 收盤價估算委託後的（買進金額, 賣出金額, 現金）；含手續費與證交稅"""
    buy = sum((Decimal(str(d["price"])) * d["shares"] for d in decisions if d["side"] == "BUY"), Decimal(0))
    sell = sum((Decimal(str(d["price"])) * d["shares"] for d in decisions if d["side"] == "SELL"), Decimal(0))
    return buy, sell, cash - buy * (1 + FEE) + sell * (1 - FEE - TAX)


def cash_floor(buy: Decimal, sell: Decimal, nav: Decimal) -> Decimal:
    """現金下限 ＝ max(1%, 淨買進金額 × 8%)"""
    return max(Decimal(str(S.CASH_FLOOR_MIN)), Decimal(str(S.PRICE_BUFFER)) * max(buy - sell, Decimal(0)) / nav)


@dataclass
class TradePlan:
    decisions: list[dict]          # ticker, action, target_weight, shares, side, target_shares, cur, price, base_weight
    no_trades: list[dict]          # ticker, why
    skipped: list[str]
    buy: Decimal
    sell: Decimal
    cash_after: Decimal
    floor: Decimal
    trimmed: int
    topped_up: list[str]
    restored: list[str]
    as_fixed: list[str]
    as_report: pd.DataFrame
    intent: str
    stance: str
    cash_range: tuple[float, float]

    @property
    def cash_pct(self) -> float:
        return float(self._cash_pct)


def plan_trades(picks: pd.DataFrame, holdings: dict[str, int], cash: Decimal, nav: Decimal, close: pd.Series,
                tradable: set[str], benches: dict[str, pd.Series], as_of: str) -> TradePlan:
    navf = float(nav)
    decisions, no_trades, skipped, band_under, band_over = [], [], [], [], []
    for t in list(picks.index) + [t for t in holdings if t not in picks.index]:
        cur = holdings.get(t, 0)
        price = float(close[t])
        w = float(picks.loc[t, "weight"]) if t in picks.index else 0.0
        cur_w = cur * price / navf
        limit = S.LIMIT_2330 if t == rules.TSMC else S.LIMIT_OTHER
        if cur and t not in tradable:      # 前一交易日無成交（多為暫停交易）：照舊持有、以最近收盤價估值
            no_trades.append({"ticker": t, "why": f"{as_of} 無成交（可能暫停交易），以最近收盤價 {price:,.2f} 估值、暫不下單"})
            continue
        if not cur and t not in tradable:
            skipped.append(f"{t} {as_of} 無成交（可能暫停交易），不建倉")
            continue
        # 超過權重上限緩衝（多為股價上漲被動超限）時不適用無交易帶，一定減碼回目標（辦法註 3）
        if cur and t in picks.index and abs(w - cur_w) < S.NO_TRADE_BAND and cur_w <= limit:
            no_trades.append({"ticker": t, "why": f"目前權重 {cur_w:.2%}，與目標 {w:.2%} 偏離小於 "
                                                   f"{S.NO_TRADE_BAND:.0%} 無交易帶"})
            if w > cur_w:
                band_under.append((w - cur_w, t, cur, price, w))
            elif cur_w > w:
                band_over.append((cur_w - w, t, cur, price, w))
            continue
        tw = lot_aligned_weight(w, nav, price, limit) if w else 0.0
        ts = target_shares(tw, nav, price)
        delta = ts - cur if ts > 0 else -(cur // 1000 * 1000)
        if delta == 0:
            if cur:
                no_trades.append({"ticker": t, "why": f"依官方公式換算目標股數與目前持股相同（{cur:,} 股）"})
            else:
                skipped.append(f"{t} 目標權重 {tw:.2%} 不足一張（股價 {price:,.0f}），不建倉")
            continue
        action = "BUY" if cur == 0 else "SELL_ALL" if ts == 0 else "ADD" if delta > 0 else "TRIM"
        decisions.append({"ticker": t, "action": action, "target_weight": tw, "shares": abs(delta),
                          "side": "BUY" if delta > 0 else "SELL", "target_shares": ts, "cur": cur, "price": price,
                          "base_weight": w})

    def force_to_target(t, cur, price, w):
        """解除無交易帶，把帶內持股調整到目標；已有決策的股票不再處理（重複決策會被拒收）"""
        nonlocal no_trades
        if any(x["ticker"] == t for x in decisions):
            return False
        tw = lot_aligned_weight(w, nav, price, S.LIMIT_2330 if t == rules.TSMC else S.LIMIT_OTHER)
        ts = target_shares(tw, nav, price)
        if ts == cur or ts == 0:
            return False
        no_trades = [x for x in no_trades if x["ticker"] != t]
        decisions.append({"ticker": t, "action": "ADD" if ts > cur else "TRIM", "target_weight": tw,
                          "shares": abs(ts - cur), "side": "BUY" if ts > cur else "SELL", "target_shares": ts,
                          "cur": cur, "price": price, "base_weight": w})
        return True

    # 現金回補：估算現金高於上限時，依缺口大小補買帶內偏低的持股
    topped_up = []
    for _, t, cur, price, w in sorted(band_under, reverse=True):
        if estimate_cash(cash, decisions)[2] / nav <= Decimal(str(S.CASH_CAP)):
            break
        if force_to_target(t, cur, price, w):
            topped_up.append(t)

    # 現金下限回補：估算現金低於下限時，依超出幅度減碼帶內偏重的持股
    restored = []
    for _, t, cur, price, w in sorted(band_over, reverse=True):
        if estimate_cash(cash, decisions)[2] / nav >= Decimal(str(S.CASH_RESTORE)):
            break
        if force_to_target(t, cur, price, w):
            restored.append(t)

    # 委託後預估持股的 Active Share：不足時減碼重疊且偏重的持股
    in_band = {t: (cur, price, w) for _, t, cur, price, w in band_under + band_over}

    def post_trade_weights():
        after = dict(holdings)
        for x in decisions:
            after[x["ticker"]] = x["target_shares"]
        return pd.Series({t: s * float(close[t]) / navf for t, s in after.items() if s > 0}, dtype=float)

    as_fixed = []
    for _ in range(len(in_band) + 1):
        weights = post_trade_weights()
        as_report = check_active_share(weights, benches)
        worst = as_report.iloc[0]
        if worst["active_share"] >= S.POST_AS_THRESHOLD:
            break
        overlap = weights.nlargest(rules.ACTIVE_SHARE_TOP_N).index.intersection(benches[worst.name].index)
        decided = {x["ticker"] for x in decisions}
        cands = [t for t in overlap if t in in_band and t not in decided and weights[t] > in_band[t][2]]
        if not cands:
            break
        t = max(cands, key=lambda t: weights[t] - in_band[t][2])
        if force_to_target(t, *in_band[t]):
            as_fixed.append(t)
        else:
            in_band.pop(t)

    # 守住現金下限：估算現金低於下限時逐張少買（新建倉至少 1 張、加碼至少比現有多 1 張）
    trimmed = 0
    while True:
        buy, sell, cash_after = estimate_cash(cash, decisions)
        floor = cash_floor(buy, sell, nav)
        if cash_after / nav >= floor:
            break
        movable = [d for d in decisions if d["side"] == "BUY" and d["target_shares"] - 1000 >= d["cur"] + 1000]
        if not movable:
            break
        d = max(movable, key=lambda d: (d["target_shares"] - 1000) * d["price"] / navf / d["base_weight"])
        d["target_weight"] = lots_weight(d["target_shares"] // 1000 - 1, nav, d["price"])
        d["target_shares"] = target_shares(d["target_weight"], nav, d["price"])
        d["shares"] = d["target_shares"] - d["cur"]
        trimmed += 1

    # 仍低於下限：先取消加碼、再取消目標權重最低的新建倉（持股不少於 20 檔）
    while cash_after / nav < floor:
        n_after = len([t for t in holdings if t in picks.index]) + sum(d["action"] == "BUY" for d in decisions)
        cands = [d for d in decisions if d["side"] == "BUY" and (d["action"] == "ADD" or n_after > rules.MIN_HOLDINGS)]
        if not cands:
            break
        d = min(cands, key=lambda d: (d["action"] != "ADD", d["base_weight"]))
        d["target_shares"] -= 1000
        d["shares"] -= 1000
        trimmed += 1
        if d["shares"] == 0:
            decisions.remove(d)
            if d["action"] == "ADD":
                no_trades.append({"ticker": d["ticker"], "why": "估算現金低於下限，暫不加碼"})
            else:
                skipped.append(f"{d['ticker']} 估算現金低於下限，暫不建倉")
        else:
            d["target_weight"] = lots_weight(d["target_shares"] // 1000, nav, d["price"])
        buy, sell, cash_after = estimate_cash(cash, decisions)
        floor = cash_floor(buy, sell, nav)

    # posture 由委託總效果決定（C12 一致）：|淨買賣| ≤ 2% 淨值 → hold，否則 increase／reduce
    as_report = check_active_share(post_trade_weights(), benches)
    cash_pct = float(cash_after / nav)
    net = float((buy - sell) / nav)
    intent = "hold" if abs(net) <= rules.HOLD_NET_FLOW_TOLERANCE else "increase" if net > 0 else "reduce"
    stance = {"increase": "aggressive", "hold": "neutral", "reduce": "defensive"}[intent]
    lo = max(0.0, round(cash_pct - S.CASH_RANGE_HALF, 4))
    hi = min(rules.CASH_MAX, round(cash_pct + S.CASH_RANGE_HALF, 4))
    if not 0 <= cash_pct <= rules.CASH_MAX:
        raise RuntimeError(f"委託後估算現金比 {cash_pct:.2%} 超出 0–25%（已自動少買 {trimmed} 張）")
    plan = TradePlan(decisions, no_trades, skipped, buy, sell, cash_after, floor, trimmed, topped_up, restored,
                     as_fixed, as_report, intent, stance, (lo, hi))
    plan._cash_pct = cash_pct
    plan.net = net
    return plan
