"""每日資料累積、盤前彩排、歷史回測與正式產出的主控入口。"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace

from . import live
from .compliance import submit_window, summarize
from .datafeed import etf_holdings
from .inputs import StoreInputs
from .reasoner import TemplateReasoner
from .run_daily import run_day
from .state import PortfolioState
from .tools.trading_calendar import is_trading_day
from .validation_outputs import VALIDATION_ROOT, dated_run_name, print_output_paths, update_index

ROOT = Path(__file__).resolve().parents[2]


def previous_trade_date(day):
    day = datetime.fromisoformat(day).date() - timedelta(days=1)
    while not is_trading_day(day.isoformat()):
        day -= timedelta(days=1)
    return day.isoformat()


def target_date(now):
    """早上處理今日；08:55 後處理下一交易日，正式模式仍檢查時窗。"""
    day = now.date()
    if now.time().replace(tzinfo=None) > datetime.strptime('08:55', '%H:%M').time():
        day += timedelta(days=1)
    while not is_trading_day(day.isoformat()):
        day += timedelta(days=1)
    return day.isoformat()


def inspect_etfs(inputs, data_date):
    notes = []
    benches, snapshot_date = inputs._benches(data_date, notes)
    dates = dict(inputs.etf_dates)
    return {'snapshot_date': snapshot_date, 'etf_dates': dates, 'notes': notes,
            'same_day': bool(dates) and all(d == data_date for d in dates.values()),
            'count': len(benches)}


def ensure_etfs(inputs, data_date, refresh=etf_holdings.refresh):
    """合格同日資料直接用；較舊或無效時抓一次，仍舊則明確警示。"""
    before = None
    try:
        before = inspect_etfs(inputs, data_date)
    except (OSError, ValueError, KeyError):
        pass
    if before and before['same_day']:
        return {**before, 'fetched': False}
    fetched = refresh()
    if not fetched['complete']:
        raise ValueError('ETF 抓取不完整，停止每日流程；請查看抓取報告')
    after = inspect_etfs(inputs, data_date)
    return {**after, 'fetched': True, 'fetch_report': fetched,
            'source_pending_update': not after['same_day']}


def source_files():
    files = set()
    for folder in ('data/market', 'data/fundamental', 'data/history', 'data/etf', 'data/news', 'src/agent', 'config'):
        files.update(p for p in (ROOT / folder).rglob('*') if p.is_file()
                     and '__pycache__' not in p.parts and 'archive' not in p.parts)
    files.update(p for p in (ROOT / 'data/security_master.csv', live.LEDGER) if p.exists())
    return sorted(files)


def freeze_sources(out):
    """封存原始輸入與程式，不含 .env；後續檢查原檔未變才准正式執行。"""
    hashes = {}
    for p in source_files():
        relative = p.relative_to(ROOT)
        dest = out / 'sources' / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dest)
        hashes[str(relative)] = hashlib.sha256(dest.read_bytes()).hexdigest()
    (out / 'source_manifest.json').write_text(json.dumps(hashes, ensure_ascii=False, indent=2), encoding='utf-8')
    return hashes


def verify_sources(hashes):
    for name, digest in hashes.items():
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != digest:
            raise ValueError(f'驗證期間輸入或程式變動，請重跑：{name}')
    if set(hashes) != {str(p.relative_to(ROOT)) for p in source_files()}:
        raise ValueError('驗證期間新增或移除輸入，請重跑')


def assess_backtest(result, end, minimum):
    """歷史缺檔只容許起始暖機，不以績效好壞選版本。"""
    summary = result.summary
    if summary.get('daily_warning_count', 0) or summary.get('disqualified_after_three_warnings'):
        raise ValueError('歷史回測有交易違規警告，請檢查報告')
    if not result.compliance.empty:
        for col in ('active_share_confirmed_two_day_etfs', 'active_share_estimated_two_day_etfs'):
            if col in result.compliance and result.compliance[col].fillna('').astype(str).str.len().gt(0).any():
                raise ValueError('歷史回測有連續 Active Share 偏低風險')
    if summary.get('end_date') != end:
        raise ValueError('回測未涵蓋最近完成交易日')
    if len(result.daily_nav) < minimum:
        return 'insufficient_history'
    return 'passed'


def execute(args, out, report):
    T = args.trade_date
    if not is_trading_day(T):
        raise ValueError(f'{T} 非交易日')
    data_date = previous_trade_date(T)
    report.update(trade_date=T, data_date=data_date, min_backtest_days=args.min_backtest_days)
    # checks-only 可在時窗外累積資料；正式產出不可藉此繞過既有時窗。
    if not args.checks_only:
        begin, end = submit_window(datetime.fromisoformat(T).date(), live.TPE)
        if not begin <= datetime.now(live.TPE) <= end:
            raise ValueError('目前不在正式繳交時窗；可用 --checks-only 累積資料與驗證')
    for title, module, required in live.FETCH:
        if module.endswith('etf_holdings'):
            continue
        if not live.run_module(module, print) and required:
            raise ValueError(f'{title}失敗')
    inputs = StoreInputs(history=False)
    report['etfs'] = ensure_etfs(inputs, data_date)
    if inputs.prev_trading_day(T) != data_date:
        raise ValueError('行情未涵蓋前一交易日')
    state = PortfolioState.load(live.LEDGER)
    if state.holdings and state.as_of != data_date:
        raise ValueError('正式帳本尚未完成前一交易日結算與對帳')
    hashes = freeze_sources(out)
    # 僅盤前彩排，不以尚未發生的 T 日價格結算，也不寫正式帳本。
    rehearsal = run_day(T, mode='replay', team_id='REPLAY', inputs=inputs,
                        state=PortfolioState(**asdict(state)), reasoner=TemplateReasoner(), out_dir=out / 'rehearsal')
    ok, text = summarize(rehearsal.checks)
    report['rehearsal'] = {'passed': ok, 'report': text, 'news_policy': 'neutral; formal API may change news scores'}
    if not ok:
        raise ValueError('盤前彩排未通過')
    from .backtest.runner import add_0050_benchmark, run_formal_backtest, write_result
    from .backtest.corporate_actions import load_actions
    history = StoreInputs(history=True, strict_history=True)
    dates = [d for d in history.dates()[1:] if d <= data_date]
    dates = dates[-args.lookback_days:]
    report['backtest'] = {'status': 'insufficient_history'}
    if dates:
        actions = load_actions(args.corporate_actions)[0] if args.corporate_actions else []
        # 只有預先確認整段無歷史輸入才列資料不足；執行中任何錯誤都停止。
        usable = []
        for d in dates:
            try:
                history.day(d)
                usable.append(d)
            except FileNotFoundError:
                if usable:
                    raise ValueError(f'{d} 歷史輸入中途缺檔')
        if usable:
            result = run_formal_backtest(history, start=usable[0], end=data_date, corporate_actions=actions)
            add_0050_benchmark(result)
            write_result(result, out / 'backtest', config={'workflow': True, 'end': data_date})
            status = assess_backtest(result, data_date, args.min_backtest_days)
            report['backtest'] = {'status': status, 'summary': result.summary}
    if report['backtest']['status'] == 'insufficient_history' and not args.allow_insufficient_history:
        raise ValueError('歷史資料不足；已保留報告。依已同意的最低驗收條件可明確使用 --allow-insufficient-history')
    verify_sources(hashes)
    if args.checks_only:
        report['status'] = 'checks_completed'
        return 0
    before_runs = set((live.OUT / 'runs').glob('*'))
    code = live.cmd_run(SimpleNamespace(trade_date=T, team_id=args.team_id, skip_fetch=True,
                                       reconciled=args.reconciled, confirm_ledger=False))
    report['formal_runs'] = [str(p) for p in sorted(set((live.OUT / 'runs').glob('*')) - before_runs)]
    report['status'] = 'ready_for_review' if code == 0 else 'formal_blocked'
    return code


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--trade-date', default=None)
    ap.add_argument('--team-id', required=True)
    ap.add_argument('--checks-only', action='store_true')
    ap.add_argument('--reconciled', action='store_true', help='已核對官方帳本；不代替 live settle')
    ap.add_argument('--lookback-days', type=int, default=20)
    ap.add_argument('--min-backtest-days', type=int, default=5)
    ap.add_argument('--allow-insufficient-history', action='store_true', help='明確允許歷史不足；其他錯誤仍停止')
    ap.add_argument('--corporate-actions', type=Path)
    args = ap.parse_args()
    if args.lookback_days < args.min_backtest_days or args.min_backtest_days < 1:
        ap.error('須符合 lookback-days >= min-backtest-days >= 1')
    args.trade_date = args.trade_date or target_date(datetime.now(live.TPE))
    out = VALIDATION_ROOT / 'daily_workflow' / dated_run_name('trade', args.trade_date)
    out.mkdir(parents=True, exist_ok=False)
    report = {'status': 'running', 'output': str(out)}
    lock = VALIDATION_ROOT / 'daily_workflow/.running.lock'
    locked = False
    try:
        # 避免兩個每日流程互相覆寫輸入；其他寫入者由 source manifest 檢查。
        with lock.open('x', encoding='utf-8') as f:
            f.write(json.dumps({'pid': os.getpid(), 'run': str(out)}))
        locked = True
        code = execute(args, out, report)
    except Exception as exc:
        report.update(status='blocked', error=f'{type(exc).__name__}: {exc}')
        print(report['error'])
        code = 1
    finally:
        if locked:
            lock.unlink()
        (out / 'workflow.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
        update_index()
        day = report.get('trade_date', args.trade_date)
        print_output_paths('每日彩排與回測', out, files=(
            ('流程報告', 'workflow.json'), ('彩排資料夾', 'rehearsal'),
            ('彩排 D-Plan', f'rehearsal/D-Plan_REPLAY_{day}.json'),
            ('彩排審核摘要', f'rehearsal/review_REPLAY_{day}.md'),
            ('彩排合規明細', f'rehearsal/compliance_REPLAY_{day}.json'),
            ('回測報告', 'backtest/report.md'), ('每日 NAV', 'backtest/daily_nav.csv'),
        ))
        for formal_run in report.get('formal_runs', []):
            print(f'正式 D-Plan/API 輸出：{Path(formal_run).resolve()}')
    raise SystemExit(code)


if __name__ == '__main__':
    main()
