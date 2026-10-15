"""帳本核對（移植自同隊組員的 check_ledger.py）：避免抄錯後台數字造成超賣（C13 整份拒收）或委託股數不符。

帳本 data/state/ledger.json 由 `live settle` 依官方規則推算，或由值班人依競技場後台「庫存明細」修正。
核對方式：結算日 as_of 當天輸入包中的「執行前帳本」＋當日 D-Plan 委託，依官方規則
（當日成交均價全數成交、手續費 0.1425%、證交稅 0.3%）推算結算後應有的持股與現金，與帳本比對：
  - 股數不符 → 錯誤（停止執行）。初賽期間配股不加進持股（Slack：由主辦方累積至賽後計入），
    除權不會造成股數差異；當日無成交均價的委託會註明可能未成交。
  - 現金差異超過容許值、淨值與「現金＋持股市值」不一致 → 只警告（現金股利賽後才撥入、手續費進位方式未公開）。
  - 格式：股票須在 150 檔內、股數為非負整數且為整股、現金非負。
股數有差異、但已對照後台確認帳本無誤（例如委託未成交）時，以 --confirm-ledger 記錄確認；
確認紀錄綁定帳本內容的雜湊，之後若再修改需重新確認。
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from . import rules
from .state import trade_fee, trade_tax
from .tools.universe import load_universe

CASH_TOLERANCE = 0.0005   # 現金差異容許值（淨值比例）
NAV_TOLERANCE = 0.005     # 淨值與「現金＋持股市值」差異容許值


def digest(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def check(ledger_path: Path, out_dir: Path, team_id: str, daily: pd.DataFrame) -> tuple[list, list, list]:
    """回傳 (errors, warnings, notes)。daily：data/market/daily.csv 的內容。"""
    pos = json.loads(ledger_path.read_text(encoding="utf-8"))
    as_of, holdings, cash, nav = pos.get("as_of"), pos.get("holdings", {}), pos.get("cash"), pos.get("nav")
    errors, warnings, notes = [], [], []

    bad_t = sorted(set(holdings) - set(load_universe()))
    if bad_t:
        errors.append(f"持股含 150 檔以外的代號：{bad_t}（是否抄錯代號？）")
    bad_s = [f"{t}:{s}" for t, s in holdings.items()
             if not isinstance(s, int) or s < 0 or s % rules.LOT_SIZE]
    if bad_s:
        errors.append(f"股數須為 1000 的非負整數倍：{bad_s}")
    if not isinstance(cash, (int, float)) or cash < 0:
        errors.append(f"現金須為非負數字：{cash}")
    if not isinstance(nav, (int, float)) or nav <= 0:
        errors.append(f"淨值須為正數：{nav}")
    holdings = {t: s for t, s in holdings.items() if isinstance(s, int) and s > 0}

    if as_of is None:
        if holdings:
            errors.append("有持股但 as_of（結算日）為 null")
        else:
            notes.append(f"初始狀態：現金 {cash:,.0f}、淨值 {nav:,.0f}、無持股，不需核對")
        return errors, warnings, notes
    try:
        date.fromisoformat(as_of)
    except (TypeError, ValueError):
        errors.append(f"as_of 須為 YYYY-MM-DD：{as_of}")
        return errors, warnings, notes
    if errors:
        return errors, warnings, notes

    if as_of not in set(daily["date"]):
        warnings.append(f"行情資料尚未包含結算日 {as_of}，無法核對淨值與成交金額（請先抓取最新行情）")
        return errors, warnings, notes
    day = daily[daily["date"] == as_of].set_index("ticker")
    close = daily[(daily["date"] <= as_of) & daily["close"].notna()].groupby("ticker")["close"].last()

    mv = sum(s * close[t] for t, s in holdings.items())
    if abs(cash + mv - nav) > NAV_TOLERANCE * nav:
        warnings.append(f"淨值 {nav:,.0f} 與「現金 {cash:,.0f}＋持股市值 {mv:,.0f}（{as_of} 收盤價）」"
                        f"相差 {(nav - cash - mv) / nav:+.2%}，請確認淨值、現金是否抄錯")

    bundle_path = out_dir / f"inputs_{team_id}_{as_of}.json"
    plan_path = out_dir / f"D-Plan_{team_id}_{as_of}.json"
    settlement = ledger_path.parent / 'history' / f'settlement_{as_of}.json'
    if settlement.exists():
        record = json.loads(settlement.read_text(encoding='utf-8'))
        from .submissions import state_digest
        from .state import PortfolioState
        if record.get('ledger_digest') != state_digest(PortfolioState(**pos)):
            errors.append('帳本與已對帳結算稽核雜湊不符；請查明修改原因')
        else:
            notes.append('帳本與成功提交／官方結算稽核一致')
        return errors, warnings, notes
    if not bundle_path.exists() or not plan_path.exists():
        warnings.append(f"找不到交易日 {as_of} 的 D-Plan 或輸入包，無法核對股數（若當日未繳交，持股應與前一日相同）")
        return errors, warnings, notes
    before = json.loads(bundle_path.read_text(encoding="utf-8"))["state"]
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    expected = {t: int(s) for t, s in before["holdings"].items() if int(s) > 0}
    exp_cash = float(before["cash"])
    unpriced = []
    for o in plan["orders"]:
        t, n = o["ticker"], o["shares"]
        px = day["avg_price"].get(t)
        if px is None or pd.isna(px):
            unpriced.append(t)
            continue
        amt = n * float(px)
        expected[t] = expected.get(t, 0) + (n if o["side"] == "BUY" else -n)
        exp_cash += -(amt + trade_fee(amt)) if o["side"] == "BUY" else amt - trade_fee(amt) - trade_tax(amt)
    expected = {t: s for t, s in expected.items() if s > 0}

    diffs = []
    for t in sorted(set(expected) | set(holdings)):
        e, a = expected.get(t, 0), holdings.get(t, 0)
        if e != a:
            why = "；當日無成交均價（委託可能未成交）" if t in unpriced else ""
            diffs.append(f"{t} 應為 {e:,} 股、帳本為 {a:,} 股{why}")
    if diffs:
        errors.append("股數與「前日帳本＋當日委託」推算不符（抄錯會造成超賣或委託股數不符）：\n      "
                      + "\n      ".join(diffs))
    else:
        notes.append(f"股數與推算一致（{len(holdings)} 檔，依 {plan_path.name} 的 {len(plan['orders'])} 筆委託）")
    if abs(exp_cash - cash) > CASH_TOLERANCE * nav:
        warnings.append(f"現金 {cash:,.0f} 與推算 {exp_cash:,.0f} 相差 {cash - exp_cash:+,.0f}"
                        f"（{(cash - exp_cash) / nav:+.3%}），請對照後台確認")
    else:
        notes.append(f"現金與推算一致（相差 {cash - exp_cash:+,.0f}）")
    return errors, warnings, notes


def confirmed(ledger_path: Path, as_of: str | None, confirm: bool, errors: list) -> bool:
    """股數差異已人工對照後台確認（綁定帳本內容雜湊）。confirm=True 時記錄這次確認。"""
    raw = ledger_path.read_text(encoding="utf-8")
    marker = ledger_path.parent / "history" / f"confirmed_{as_of}.json"
    if confirm and errors:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"as_of": as_of, "ledger_digest": digest(raw), "errors": errors,
                                      "confirmed_at": datetime.now().replace(microsecond=0).isoformat()},
                                     ensure_ascii=False, indent=1), encoding="utf-8")
        return True
    return marker.exists() and json.loads(marker.read_text(encoding="utf-8")).get("ledger_digest") == digest(raw)
