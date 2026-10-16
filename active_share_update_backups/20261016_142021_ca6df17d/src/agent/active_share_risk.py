"""Active Share 資料驗證與本地收盤觀測；不是主辦方違規裁定。"""

from __future__ import annotations

import math
from datetime import date, timedelta

from . import rules
from .datafeed.etf_holdings import ETFS
from .tools.trading_calendar import is_trading_day

EXPECTED_ETFS = frozenset(code for code, _ in ETFS)


def validate_benchmarks(benchmarks, dates: dict[str, str], as_of: str) -> list[str]:
    """缺檔、未來／未知日期或無效權重拒絕；較舊日期只提示，不假定違規。"""
    missing, extra = EXPECTED_ETFS - set(benchmarks), set(benchmarks) - EXPECTED_ETFS
    if missing or extra:
        raise ValueError(f"ETF 清單不完整：缺少 {sorted(missing)}；非官方清單 {sorted(extra)}")
    cutoff = date.fromisoformat(as_of)
    notes = []
    for code, weights in benchmarks.items():
        if not 1 <= len(weights) <= 10 or len(set(weights.keys())) != len(weights):
            raise ValueError(f"{code} 前十大個股空白、重複或超過十檔")
        values = list(weights.values()) if isinstance(weights, dict) else weights.tolist()
        if any(not math.isfinite(float(w)) or not 0 <= float(w) <= 1 for w in values) or not 0 < sum(values) <= 1.0001:
            raise ValueError(f"{code} 權重無效（應為占淨值原始比例，不可正規化）")
        try:
            observed = date.fromisoformat(dates[code])
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError(f"{code} 缺少有效資料日期") from exc
        if observed > cutoff:
            raise ValueError(f"{code} 資料日期 {observed} 晚於檢查時點 {as_of}")
        if observed < cutoff:
            notes.append(f"{code} 資料日 {observed}，較檢查日 {as_of} 舊 {(cutoff-observed).days} 日；待核對最新公告")
    return notes


def previous_session(day: str) -> str:
    d = date.fromisoformat(day) - timedelta(days=1)
    while not is_trading_day(d.isoformat()):
        d -= timedelta(days=1)
    return d.isoformat()


def record_close(history: list[dict], day: str, weights: dict, benchmarks: dict,
                 dates: dict[str, str]) -> list[dict]:
    """同日重算取代，不累加；缺交易日或基準日期落後時不得聲稱已確認連續違規。"""
    if not is_trading_day(day):
        raise ValueError("Active Share 收盤觀測必須為交易日")
    if history and day < max(r['date'] for r in history):
        raise ValueError("不得回寫較早觀測日；請另建歷史回放")
    notes = validate_benchmarks(benchmarks, dates, day)
    top = dict(sorted(weights.items(), key=lambda x: x[1], reverse=True)[:10])
    if any(not math.isfinite(w) or w < 0 for w in top.values()) or sum(top.values()) > 1.0001:
        raise ValueError("投組權重無效，無法計算 Active Share")
    prior = next((r for r in history if r['date'] == previous_session(day)), None)
    rows = {}
    for code, bench in benchmarks.items():
        score = .5 * sum(abs(top.get(t, 0) - bench.get(t, 0)) for t in set(top) | set(bench))
        below = score < rules.ACTIVE_SHARE_MIN
        old = prior['etfs'].get(code, {}) if prior else {}
        current = dates[code] == day
        rows[code] = {'active_share': score, 'benchmark_date': dates[code], 'below_minimum': below,
                      'estimated_breach_streak': (old.get('estimated_breach_streak', 0) + 1) if below else 0,
                      'same_date_breach_streak': (old.get('same_date_breach_streak', 0) + 1) if below and current else 0}
    entry = {'date': day, 'basis': 'local_close_not_official', 'prior_session_observed': prior is not None,
             'notes': notes, 'etfs': rows}
    return [r for r in history if r['date'] != day] + [entry]
