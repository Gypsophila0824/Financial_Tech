"""Research-only execution copy: forced cap-top3 trims bypass the no-trade band.
Generated from factor_engine.plan_trades; never used by live entry points.
"""
from .factor_engine import *
from .factor_engine import S, rules, math, pd, Decimal

def plan_trades(picks: pd.DataFrame, holdings: dict[str, int], cash: Decimal, nav: Decimal, close: pd.Series,
                tradable: set[str], benches: dict[str, pd.Series], as_of: str,
                no_trade_band: float | None = None,
                enforce_active_share: bool = True) -> TradePlan:
    band = S.NO_TRADE_BAND if no_trade_band is None else no_trade_band
    if not math.isfinite(band) or not 0 <= band <= 1:
        raise ValueError('無交易帶須介於 0–100%')
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
        within_band = abs(w - cur_w) <= band
        if cur and t in picks.index and within_band and cur_w <= limit and not bool(picks.loc[t].get("force_rebalance", False)):
            band_rule = f"{band:.0%} 以內無交易帶"
            explanation = (f"目前權重 {cur_w:.2%}，與目標 {w:.2%} 偏離 {abs(w-cur_w):.2%}，符合 {band_rule}")
            no_trades.append({"ticker": t, "why": explanation})
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
    if enforce_active_share:
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
    as_report = check_active_share(post_trade_weights(), benches) if enforce_active_share else check_active_share(pd.Series(dtype=float), {})
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
