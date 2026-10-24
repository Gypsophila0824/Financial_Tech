"""Daily Agent：單一交易日的完整決策管線，產出 D-Plan、合規報告、審核摘要與輸入包。

流程：輸入（行情、量價與基本面因子、主動型 ETF 前十五大、新聞）→ factor_engine 評分選股、配權重、
      Active Share 控制 → 決策與現金規則 → 官方公式算委託 → 證據鏈（sources／observations）→
      推論文字（範本或 LLM 改寫）→ D-Plan → 合規檢查。

合規紅線（六(一)、六(三)）：正式模式不得有任何模擬元件；人只能依合規報告決定過／退回，不得手改 D-Plan。
決策、權重、股數、posture 一律由程式決定；LLM 只改寫推論文字與判讀新聞。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pandas as pd

from . import codeversion, dplan_writer, rules, strategy as S
from .bundle import save_bundle
from .compliance import Check, ComplianceContext, run_checks
from .factor_engine import build_targets, plan_trades
from .governance import write_manifest
from .inputs import DayInputs
from .models import DecisionDraft, Evidence, InferenceDraft, MarketViewDraft, NoTradeDraft, Observation, OrderDraft, Source
from .reasoner import StockItem
from .review import write_review
from .tools.order_generation import derive_orders
from .tools.trading_calendar import is_submit_day
from .tools.universe import load_universe

TAIPEI = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parents[2]
LEVEL = {"high": "重大", "medium": "中度"}

# mock：全部模擬；replay：真實歷史資料彩排（推論可為範本）；live：正式繳交，全部必須是真實元件
MODES = {
    "mock": {"team_id": "MOCK", "note": "MOCK：模擬資料產出，僅供測試管線，不可提交"},
    "replay": {"team_id": "REPLAY", "note": "MOCK REPLAY：真實歷史資料彩排，不可提交"},
    "live": {"team_id": None, "note": None},
}


@dataclass
class DayResult:
    plan: dict
    checks: list[Check]
    orders: list[OrderDraft]
    path: Path
    trade_plan: object = None
    targets: object = None


def _now() -> str:
    return datetime.now(TAIPEI).replace(microsecond=0).isoformat()


def pct(x, digits=1) -> str:
    return f"{x * 100:+.{digits}f}%"


def news_event(raw: str) -> tuple[str, str, str]:
    """'high:財報獲利:理由' → ('重大', '財報獲利', '理由')"""
    level, kind, reason = raw.split(":", 2)
    return LEVEL.get(level, level), kind, reason


def news_text(nr) -> str:
    """寫進推論的新聞面文字：優先用 LLM 摘要，沒有摘要時改用最嚴重利空／最好利多的判斷理由"""
    if nr.get("summary"):
        return nr["summary"]
    parts = []
    for label, raw in (("利空", nr.get("worst_event")), ("利多", nr.get("best_event"))):
        if raw:
            level, kind, reason = news_event(raw)
            parts.append(f"{level}{label}（{kind}）：{reason}")
    return "；".join(parts)


def build_evidence(day: DayInputs, tickers: list[str]) -> tuple[Evidence, dict[str, str], dict[str, str], dict]:
    """sources／observations：市場廣度＋每檔因子事實＋新聞事實（依組員 generate_dplan.py 的寫法）。"""
    ev = Evidence()
    for spec in list(day.market_sources.values()) + list(day.fund_sources.values()):
        ev.add_source(Source(spec.key, spec.authority, spec.url, spec.content_as_of, name=spec.name))
    ms, fs = day.market_sources, day.fund_sources

    pv = day.pv
    above = int((pv["A3"] > 0).sum())
    breadth = above / len(pv)
    med20 = float(pv["A1"].median())
    ev.add_observation(Observation(
        key="obs:breadth", source_keys=[ms["quotes_twse"].key, ms["quotes_tpex"].key], topic="universe_breadth",
        statement=f"150 檔標的中 {above} 檔收盤價高於 60 日均價（{breadth:.1%}）；"
                  f"近 20 日還原漲跌幅中位數 {pct(med20)}（依交易所收盤價計算）",
        values={"above_ma60_count": above, "above_ma60_pct": round(breadth * 100, 2),
                "median_ret20_pct": round(med20 * 100, 2)}))

    def market_refs(market: str) -> list[str]:
        side = "twse" if market == "TWSE" else "tpex"
        refs = [ms[f"quotes_{side}"].key, ms[f"insti_{side}"].key]
        if "market_cap_twse" in ms:
            refs.append(ms["market_cap_twse"].key)
        if "history" in fs:
            return refs + [fs["history"].key]
        return refs + [fs[f"{side}_revenue"].key, fs[f"{side}_margin"].key]

    obs_of, news_obs_of = {}, {}
    for t in tickers:
        p, f, m = pv.loc[t], day.fund.loc[t], day.master.loc[t]
        values, parts = {}, []
        for key, label, val in (("ret20_pct", "近 20 日還原漲跌", p["A1"]), ("vs_ma60_pct", "較 60 日均價", p["A3"])):
            if pd.notna(val):
                values[key] = round(float(val) * 100, 2)
                parts.append(f"{label} {pct(val)}")
        if pd.notna(p["A4"]):
            values["insti10_to_value20"] = round(float(p["A4"]), 4)
            parts.append(f"近 10 日三大法人買賣超金額為 20 日均成交金額之 {p['A4']:.2f} 倍")
        if pd.notna(f["B1"]):
            values["revenue_yoy_pct"] = round(float(f["B1"]), 2)
            parts.append(f"{f['rev_month']} 營收年增 {f['B1']:+.1f}%")
            if pd.notna(f["rev_ytd_yoy"]):
                values["revenue_ytd_yoy_pct"] = round(float(f["rev_ytd_yoy"]), 2)
                parts.append(f"今年累計營收年增 {f['rev_ytd_yoy']:+.1f}%")
        if pd.notna(f["B2"]):
            values["op_margin_pct"] = round(float(f["B2"]), 2)
            parts.append(f"{f['op_quarter']} 營業利益率 {f['B2']:.1f}%")
        refs = market_refs(m["market"])
        if pd.notna(f["B4"]):
            values["roe_pct"] = round(float(f["B4"]), 2)
            parts.append(f"{f['roe_quarter']} 年化 ROE {f['B4']:.1f}%")
            if f.get("roe_src") and isinstance(f["roe_src"], str):
                # 改引用損益表＋資產負債表取代營益分析，維持 source_ref ≤ 5
                market, kind = f["roe_src"].split(":")
                stmt = []
                for tbl in ("is", "bs"):
                    spec = day.statement_sources.get((market, tbl, kind))
                    if spec:
                        if spec.key not in ev.sources:
                            ev.add_source(Source(spec.key, spec.authority, spec.url, spec.content_as_of, name=spec.name))
                        stmt.append(spec.key)
                refs = refs[:3] + stmt
        cap_score = day.market_cap_score.get(t, S.NEUTRAL_SCORE) if day.market_cap_score is not None else S.NEUTRAL_SCORE
        values["market_cap_top20_score"] = round(float(cap_score), 4)
        if "market_cap_twse" in ms and cap_score > S.NEUTRAL_SCORE:
            cap_label = "上市市值前20名估算加權分數" if "估算" in ms["market_cap_twse"].name else "上市市值前20名加權分數"
            parts.append(f"{cap_label} {cap_score:.2f}")
        elif "market_cap_twse" not in ms:
            parts.append("無同日上市市值前20快照，市值分數採中性")
        values["close"] = float(day.close[t])
        obs_of[t] = ev.add_observation(Observation(
            key=f"obs:stk:{t}", source_keys=refs, topic="stock_factors",
            statement=f"{t} {m['name']}：收盤 {day.close[t]:,.2f}；" + "；".join(parts), values=values))

    # 新聞（source＋observation 只放標題等事實；判斷與摘要寫進 inference）
    news = day.news
    if news is not None:
        budget = rules.LAYER_LIMITS["observations"] - len(ev.observations)
        for t in tickers:
            if t not in news.index or len(news_obs_of) >= budget:
                continue
            nr = news.loc[t]
            ids = [i for i in str(nr.get("evidence_ids") or "").split() if i in day.news_items][:S.NEWS_PER_TICKER]
            keys = []
            for i in ids:
                x = day.news_items[i]
                key = f"src:news:{i}"
                if key not in ev.sources:
                    if len(ev.sources) >= rules.LAYER_LIMITS["sources"]:
                        continue
                    ev.add_source(Source(
                        key, x["authority"],
                        x["url"], x["published_at"], published_at=x["published_at"], archive_url=x.get("archive_url"),
                        name=f"{'公開資訊觀測站重大訊息' if x['source'] == 'mops' else '鉅亨網'}：{x['title']}"[:300]))
                keys.append(key)
            if not keys:
                continue
            titles = "；".join(f"{day.news_items[i]['published_at'][:10]}「{day.news_items[i]['title']}」"
                              for i in ids if f"src:news:{i}" in keys)
            news_obs_of[t] = ev.add_observation(Observation(
                key=f"obs:news:{t}", source_keys=keys, topic="stock_news",
                statement=f"{t} {day.master.loc[t, 'name']} 近 {S.NEWS_DAYS} 日以該公司為主角的新聞"
                          f" {int(nr['news_items'])} 則，包括：{titles}"[:500],
                values={"news_items": int(nr["news_items"]), "cited_items": len(keys)}))
    stats = {"above": above, "breadth": breadth, "med20": med20}
    return ev, obs_of, news_obs_of, stats


def run_day(
    trade_date: str,
    *,
    mode: str,                       # "mock" / "replay" / "live"
    team_id: str,
    inputs,                          # inputs.StoreInputs / MockInputs / bundle.BundleInputs
    state,
    reasoner,
    out_dir: Path,
    news=None,                       # tools.news_scoring（選用）：產生新聞分數與新聞清單
) -> DayResult:
    components = {"inputs": inputs, "reasoner": reasoner}
    if news is not None:
        components["news"] = news
    mocks = [name for name, c in components.items() if getattr(c, "is_mock", False)]
    if mode not in MODES:
        raise ValueError(f"未知模式 {mode}")
    if mode == "live" and mocks:
        raise RuntimeError(f"正式模式不得使用模擬元件：{mocks}（六(一)、六(三)）")
    if mode == "replay" and "inputs" in mocks:
        raise RuntimeError("彩排模式必須使用真實資料")
    if MODES[mode]["team_id"] and team_id != MODES[mode]["team_id"]:
        raise RuntimeError(f"{mode} 模式的 team_id 必須是 {MODES[mode]['team_id']}，避免誤交")
    if mode == "live" and not is_submit_day(trade_date):
        raise RuntimeError(f"{trade_date} 證交所休市，不是交易日（休市日沒有 D-Plan）")
    if mode == 'live':
        from .official_policy import require_formal_release
        require_formal_release()

    started = f"{trade_date}T06:00:00+08:00" if mode == "mock" else _now()
    day: DayInputs = inputs.day(trade_date)
    if news is not None:
        day.news, day.news_items = news.signals(day)
    holdings = {t: int(s) for t, s in state.holdings.items() if s > 0}
    if mode == "live" and holdings and state.as_of != day.data_date:
        raise RuntimeError(f"帳本結算日 {state.as_of} ≠ 資料基準日 {day.data_date}，請先依後台庫存明細更新帳本")

    # ---- 評分、選股、配權重、決策（factor_engine）----
    targets = build_targets(day.master, day.pv, day.fund, day.news, list(holdings), day.benches,
                            market_cap_score=day.market_cap_score)
    nav, cash = Decimal(str(state.nav)), Decimal(str(state.cash))
    tp = plan_trades(targets.picks, holdings, cash, nav, day.close, day.tradable, day.benches, day.data_date)

    tickers = [x["ticker"] for x in tp.decisions] + [x["ticker"] for x in tp.no_trades]
    if news is not None and mode == "live":
        news.archive(day, tickers)       # 可能引用的鉅亨網新聞送 Wayback 存檔（填 archive_url）
    evidence, obs_of, news_obs_of, stats = build_evidence(day, tickers)

    # ---- 範本推論（決策、權重、股數由程式決定；文字可再交給 LLM 改寫）----
    picks, veto, pv = targets.picks, targets.vetoed, day.pv
    items = []
    for x in tp.decisions + tp.no_trades:
        t = x["ticker"]
        name = day.master.loc[t, "name"]
        if t in picks.index:
            r = picks.loc[t]
            weak = [lbl for lbl, col in (("短期動能", "A1_rank"), ("中期動能", "A2_rank"), ("趨勢", "A3_rank"),
                                         ("法人買超", "A4_rank")) if pv.loc[t, col] < 0.3]
            logic = (f"{t} {name} 量價分數 {r['pv_score']:.2f}、基本面分數 {r['fund_score']:.2f}、"
                     f"新聞分數 {r['news_score']:.2f}，依 55/35/10 加權總分 {r['total_score']:.3f}，"
                     f"在 150 檔中排名第 {int(r['rank'])}，符合本 ETF 動能＋成長的多因子選股條件；"
                     f"依（總分−0.5）÷ 60 日波動率 {r['vol60']:.2%} 配置，目標權重 {x.get('target_weight', r['weight']):.2%}。")
            counter = f"{'、'.join(weak)}因子排名落在後 30%" if weak else None
        elif t in veto.index:
            v = veto.loc[t]
            level, kind, reason = news_event(v["worst_event"])
            logic = (f"{t} {name} 觸發重大利空否決規則：近 {S.NEWS_DAYS} 日有{level}利空新聞（{kind}：{reason}），"
                     f"且收盤價較 60 日均價 {pct(float(v['A3']))}、已跌破均線；依本 ETF 風控規則出清，"
                     f"不受換股緩衝保護（今日總分排名第 {int(v['rank'])}）。")
            counter = None
        else:
            logic = (f"{t} {name} 不在今日目標名單：總分已跌出前 {S.BUFFER_RANK} 名換股緩衝區，"
                     f"依本 ETF 選股規則應出清，資金留待配置於排名較高的標的。")
            counter = None
        facts = {obs_of[t]: evidence.observations[obs_of[t]].statement}
        if t in news_obs_of:
            nr = day.news.loc[t]
            facts[news_obs_of[t]] = evidence.observations[news_obs_of[t]].statement
            if t not in veto.index or nr.get("summary"):
                logic += f"新聞面（近 {S.NEWS_DAYS} 日）：{news_text(nr).rstrip('。')}。"
            if nr.get("worst_event"):
                level, kind, reason = news_event(nr["worst_event"])
                counter = "；".join(filter(None, [counter, f"新聞{level}利空（{kind}）：{reason}"]))
        if "why" in x:
            logic += f"今日不交易：{x['why']}。"
        items.append(StockItem(key=f"inf:{t}", ticker=t, name=name,
                               action="NO_TRADE" if "why" in x else x["action"], facts=facts,
                               logic=logic[:1000], counter=counter[:500] if counter else None))
    inferences: dict[str, InferenceDraft] = reasoner.stocks(items)

    decisions = [DecisionDraft(
        key=f"dec:{x['ticker']}", ticker=x["ticker"], action=x["action"], target_weight=x["target_weight"],
        inference_keys=[f"inf:{x['ticker']}"],
        risk_check=f"目標權重 {x['target_weight']:.2%} ≤ 個股上限{'25%' if x['ticker'] == rules.TSMC else '10%'}；"
                   f"委託後持股與現金由驗證器重算") for x in tp.decisions]
    no_trades = [NoTradeDraft(ticker=x["ticker"], reason_keys=[f"inf:{x['ticker']}"], reason=x["why"][:300])
                 for x in tp.no_trades]
    close = {t: float(p) for t, p in day.close.items()}
    orders = derive_orders(decisions, holdings, close, float(nav))
    expected = sorted((d["ticker"], d["side"], d["shares"]) for d in tp.decisions)
    if sorted((o.ticker, o.side, o.shares) for o in orders) != expected:
        raise RuntimeError("委託股數與策略引擎不一致（官方公式重算不符），停止產出")

    # ---- market_view：市場廣度研判（描述用，不擇時）＋ 由委託總效果決定的 posture ----
    breadth, med20 = stats["breadth"], stats["med20"]
    regime = "risk_on" if breadth >= S.BREADTH_ON else "risk_off" if breadth <= S.BREADTH_OFF else "neutral"
    lo, hi = tp.cash_range
    mv_logic = (f"150 檔標的中 {breadth:.1%} 站上 60 日均價、近 20 日漲跌幅中位數 {pct(med20)}，判定市場為 {regime}。"
                f"本 ETF 為因子投資型，不依市場研判擇時，以約 {1 - S.TARGET_CASH:.0%} 股票、{S.TARGET_CASH:.0%} 現金為目標配置"
                f"（估算現金低於 {S.CASH_RESTORE:.0%} 時減碼偏重持股、高於 {S.CASH_CAP:.0%} 時補買偏低持股）；"
                f"今日委託淨買超 {tp.net:.2%} 淨值，估算委託後現金比 {tp.cash_pct:.2%}"
                f"（現金下限 {float(tp.floor):.2%} ＝ max(1%, 淨買進金額 × 8%)，因應成交均價可能高於前日收盤），"
                f"故淨曝險意圖為 {tp.intent}、操作姿態為 {tp.stance}。")
    mv_counter = None if regime == "risk_on" else \
        f"站上 60 日均價家數比例 {breadth:.1%}，市場動能並非全面偏多，個股選擇以總分排名為準"
    logic, counter = reasoner.market_view({"obs:breadth": evidence.observations["obs:breadth"].statement},
                                          mv_logic, mv_counter)
    mv = MarketViewDraft(basis_keys=["obs:breadth"], logic=logic, regime=regime, stance=tp.stance,
                         net_exposure_intent=tp.intent, cash_range=(lo, hi), counter_evidence=counter)

    completed = f"{trade_date}T06:10:00+08:00" if mode == "mock" else _now()
    # 如實填寫當日實際用到的 LLM（推論改寫、新聞判讀）；都沒用到時為規則式
    llm_models = ([reasoner.model_version.removeprefix("rule-based+")] if reasoner.model_provider == "anthropic"
                  else []) + sorted(getattr(news, "models_used", set()) if news is not None else set())
    metadata = {"model_provider": "anthropic" if llm_models else reasoner.model_provider,
                "model_version": ("rule-based+" + "+".join(dict.fromkeys(llm_models)))[:60] if llm_models
                else reasoner.model_version,
                "run_started_at": started, "run_completed_at": completed, "code_version": codeversion.current()}
    if reasoner.calls:
        metadata.update(input_tokens=reasoner.input_tokens, output_tokens=reasoner.output_tokens,
                        tool_use_count=reasoner.calls)

    plan = dplan_writer.assemble(
        team_id=team_id, trade_date=trade_date, evidence=evidence, market_view=mv, inferences=inferences,
        decisions=decisions, no_trades=no_trades, orders=orders, agent_metadata=metadata,
        note=MODES[mode]["note"])
    path = dplan_writer.write(plan, out_dir)
    # D-Plan schema 不允許加入策略欄位；以相鄰封存檔保存當日策略清單與雜湊。
    write_manifest(out_dir / f"strategy_manifest_{team_id}_{trade_date}.json")

    universe = load_universe()
    bench = {k: v.to_dict() for k, v in day.benches.items()}
    save_bundle(out_dir / f"inputs_{team_id}_{trade_date}.json", day=day, state=state, plan=plan)
    notes = list(day.notes) + [f"略過：{s}" for s in tp.skipped]
    ctx = ComplianceContext(state=state, prev_close=close, universe=universe, benchmarks=bench,
                            filename=path.name, live=(mode == "live"), data_notes=day.notes,
                            etf_dates=day.etf_dates, data_date=day.data_date)
    checks = run_checks(plan, ctx)
    report = out_dir / f"compliance_{team_id}_{trade_date}.json"
    report.write_text(json.dumps([c.__dict__ for c in checks], ensure_ascii=False, indent=1), encoding="utf-8")
    if all(c.passed for c in checks if c.rule == "六(七) 僅限 150 檔"):
        write_review(plan, checks, state, close, universe, bench, out_dir, extra=notes + [
            f"目標權重 Active Share 調整：{a}" for a in targets.adjustments] + (
            [f"現金回補（補買帶內偏低持股）：{'、'.join(tp.topped_up)}"] if tp.topped_up else []) + (
            [f"現金下限回補（減碼帶內偏重持股）：{'、'.join(tp.restored)}"] if tp.restored else []) + (
            [f"委託後 Active Share 調整：{'、'.join(tp.as_fixed)}"] if tp.as_fixed else []) + (
            [f"為守住現金下限少買 {tp.trimmed} 張"] if tp.trimmed else []) + (
            [f"推論文字改用範本：{f}" for f in getattr(reasoner, 'fallback', [])]))
    return DayResult(plan=plan, checks=checks, orders=orders, path=path, trade_plan=tp, targets=targets)
