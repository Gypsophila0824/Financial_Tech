"""Run production vs current research and render daily + monthly reports.

Strict historical inputs and event rules remain enabled. Missing coverage is never
converted to a successful whole-period NAV. ETF benchmarks use daily closes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import traceback
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from ..inputs import StoreInputs
from ..strategy_interface import ResearchPortfolio
from .. import strategy
from ..datafeed.benchmark_prices import BENCHMARKS, fetch_prices
from . import archive
from .corporate_actions import load_actions
from .runner import run_formal_backtest, write_result

END='2026-10-13'


def dump(path: Path, value):
    with path.open('x',encoding='utf-8') as f:
        json.dump(value,f,ensure_ascii=False,indent=2,allow_nan=False)


def close_returns(prices: pd.DataFrame, dates: list[str], tickers=BENCHMARKS):
    """Calculate daily close-to-close and calendar-month close returns.

    Monthly return is last close in the month divided by the preceding month's
    last close. Missing price dates remain missing; they are never forward-filled.
    """
    rows=[]
    if prices.empty:
        return pd.DataFrame(columns=['date','ticker','close','daily_return']), pd.DataFrame(
            columns=['month','ticker','monthly_return'])
    full=prices.loc[prices.ticker.isin(tickers)].copy().sort_values(['ticker','date'])
    full['daily_return']=full.groupby('ticker').close.pct_change()
    frame=(pd.MultiIndex.from_product([dates,tickers],names=['date','ticker']).to_frame(index=False)
           .merge(full[['date','ticker','close','daily_return']],on=['date','ticker'],how='left'))
    # The first requested date uses the prior available close from the full history.
    month_rows=[]
    full=full.assign(month=full.date.str[:7])
    for ticker,g in full.groupby('ticker'):
        g=g.drop_duplicates('date').set_index('date').sort_index()
        for month, mg in g.groupby(g.index.str[:7], sort=True):
            period_dates=[d for d in dates if d.startswith(month)]
            if not period_dates:
                continue
            current=[d for d in mg.index if d in period_dates]
            prior=[d for d in g.index if d<current[0]] if current else []
            value=None
            if current and prior and current[-1] == period_dates[-1]:
                value=float(g.loc[current[-1],'close']/g.loc[prior[-1],'close']-1)
            month_rows.append({'month':month,'ticker':ticker,'monthly_return':value})
    return frame[['date','ticker','close','daily_return']], pd.DataFrame(month_rows)


def portfolio_months(frame):
    """Monthly returns use actual prior equity; cumulative returns use YTD initial equity."""
    initial=float(frame.prior_nav.iloc[0])
    rows={}
    for month,g in frame.groupby(frame.date.str[:7],sort=True):
        rows[month]={'monthly_return':float(g.nav.iloc[-1]/g.prior_nav.iloc[0]-1),
                     'ytd_return':float(g.nav.iloc[-1]/initial-1),
                     'profit':float(g.nav.iloc[-1]-g.prior_nav.iloc[0]),
                     'final_nav':float(g.nav.iloc[-1])}
    return rows


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out-dir',type=Path,required=True)
    parser.add_argument('--fetch-benchmark',action='store_true',help='從證交所抓 0050、00981A、00991A 每日收盤價')
    parser.add_argument('--corporate-actions',type=Path,help='已核實事件 JSON；缺事件不得推算')
    args=parser.parse_args()
    out=archive.safe_output(args.out_dir)
    out.mkdir(parents=True,exist_ok=False)
    actions=load_actions(args.corporate_actions)[0] if args.corporate_actions else []
    spec={'recent':['2026-09-01',END],'ytd':['2026-01-01',END],
          'production_buffer':strategy.BUFFER_RANK,'research_manifest':ResearchPortfolio(25).manifest(),
          'news':'neutral_ablation','claims_unseen_oos':False,'monthly_mode':'continuous_YTD_account',
          'benchmarks':list(BENCHMARKS),
          'benchmark':'daily and monthly price returns from official closes; excludes dividends and trading costs',
          'corporate_actions':str(args.corporate_actions) if args.corporate_actions else None,
          'at':datetime.now(timezone.utc).isoformat(),'formal_promotion':False}
    dump(out/'attempt.json',spec)
    snapshot,hashes=archive.capture_sources(True)
    if args.corporate_actions:
        raw=args.corporate_actions.read_bytes()
        (out/'corporate_actions_source.json').write_bytes(raw)
        hashes[args.corporate_actions.resolve()]=hashlib.sha256(raw).hexdigest()
    for name,raw in snapshot.items():
        (out/name).write_bytes(raw)
    inputs=StoreInputs(history=True,strict_history=True)
    all_dates=inputs.dates()
    prices,benchmark_sources=fetch_prices(all_dates, out/'benchmark_cache') if args.fetch_benchmark else (
        pd.DataFrame(columns=['date','ticker','close']), [])
    prices=prices.loc[prices.date<=END].copy()
    dump(out/'benchmark_sources.json',benchmark_sources)
    prices.to_csv(out/'benchmark_prices.csv',index=False,encoding='utf-8-sig')
    results={}
    for period,(start,end) in [('recent',spec['recent']),('ytd',spec['ytd'])]:
        folder=out/period
        folder.mkdir()
        expected=[d for d in all_dates if start<=d<=end]
        missing=[{'trade_date':d,'data_date':inputs.prev_trading_day(d)} for d in expected
                 if not any(p.stem[-10:]<=inputs.prev_trading_day(d)
                            for p in (archive.ROOT/'data/etf').glob('active_etf_holdings_*.csv'))]
        dump(folder/'coverage.json',{'expected_dates':expected,'missing_historical_etf_dates':missing})
        results[period]={}
        for name,adapter in [('production',None),('research',ResearchPortfolio(25))]:
            print(period,name,start,end,flush=True)
            archive.check_unchanged(hashes)
            try:
                r=run_formal_backtest(StoreInputs(history=True,strict_history=True),start=start,end=end,
                                      strategy_adapter=adapter,corporate_actions=actions,terminal_date=end)
                write_result(r,folder/name,config=spec)
                complete=r.daily_nav.date.tolist()==expected
                row={'status':'complete' if complete else 'partial_coverage',
                     'summary':r.summary,'actual_dates':r.daily_nav.date.tolist()}
                if complete:
                    row['monthly']=portfolio_months(r.daily_nav)
            except Exception as exc:
                row={'status':'failed','error':str(exc),'traceback':traceback.format_exc()}
            results[period][name]=row
            dump(folder/(name+'_status.json'),row)
        complete=all(r['status']=='complete' for r in results[period].values())
        dump(folder/'comparison.json',{'requested_start':start,'requested_end':end,
                                      'fair_comparison_complete':complete,'results':results[period]})
    archive.check_unchanged(hashes)
    dump(out/'all_results.json',results)
    pct=lambda x:'—' if x is None else f'{x:.4%}'
    # Report one: fresh account over the requested recent interval.
    dates=[d for d in all_dates if spec['recent'][0]<=d<=END]
    daily_bench,_=close_returns(prices,dates)
    daily_bench.to_csv(out/'01_每日收盤報酬.csv',index=False,encoding='utf-8-sig')
    lines=['# 比較一：2026/9/1～10/13',f'指定 {len(dates)} 個資料交易日。','',
           '以下逐日以當日收盤價除以前一個有資料交易日收盤價計算；缺價留白，不沿用舊價。','',
           '| 日期 | 0050 日報酬 | 00981A 日報酬 | 00991A 日報酬 |','|---|---:|---:|---:|']
    daily_wide=daily_bench.pivot(index='date',columns='ticker',values='daily_return')
    for d in dates:
        vals=[pct(daily_wide.loc[d,t]) if d in daily_wide.index and t in daily_wide.columns else '—'
              for t in BENCHMARKS]
        lines.append(f'| {d} | {vals[0]} | {vals[1]} | {vals[2]} |')
    lines+=['','ETF 日報酬以每日收盤價計算，只反映價格變化，不含股利、交易成本及整張交易限制。','']
    lines+=reasons(out/'recent',results['recent'])
    (out/'01_九月一日至十月十三日.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    # Report two: close-based monthly returns for ETF references and portfolio.
    annual_dates=[d for d in all_dates if '2026-01-01'<=d<=END]
    _,monthly_bench=close_returns(prices,annual_dates)
    lines=['# 比較二：2026 年各月報酬（至 10/13）','',
           'ETF 月報酬＝當月最後收盤價 ÷ 前一個月最後收盤價 − 1。2026/10 為截至 10/13 的月內報酬；只反映價格變化，不含股利與交易成本。','',
           '| 月份 | 交易日數 | 研究月報酬 | 正式月報酬 | 0050 月報酬 | 00981A 月報酬 | 00991A 月報酬 |',
           '|---|---:|---:|---:|---:|---:|---:|']
    csv=[]
    for month in [f'2026-{m:02d}' for m in range(1,11)]:
        md=[d for d in annual_dates if d.startswith(month)]
        bm=monthly_bench.set_index(['month','ticker']).monthly_return if not monthly_bench.empty else pd.Series(dtype=float)
        row={'month':month,'days':len(md)}
        for ticker in BENCHMARKS:
            row[ticker+'_monthly_price_return']=bm.get((month,ticker))
        for name in ['research','production']:
            result=results['ytd'][name]
            m=result.get('monthly',{}).get(month,{}) if result['status']=='complete' else {}
            row[name+'_monthly_return']=m.get('monthly_return')
        csv.append(row)
        lines.append(f'| {month}{"（至13日）" if month=="2026-10" else ""} | {len(md)} | '
                     f'{pct(row["research_monthly_return"])} | {pct(row["production_monthly_return"])} | '
                     f'{pct(row["0050_monthly_price_return"])} | {pct(row["00981A_monthly_price_return"])} | '
                     f'{pct(row["00991A_monthly_price_return"])} |')
    pd.DataFrame(csv).to_csv(out/'02_各月收盤報酬.csv',index=False,encoding='utf-8-sig')
    lines+=['','ETF 月報酬依每日收盤價推算，不計股利、交易成本或整張交易限制。',
            '策略欄位為 — 表示完整月度資金曲線尚未完成，並非零報酬。']
    lines+=reasons(out/'ytd',results['ytd'])
    (out/'02_各月收盤報酬.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    integrity={p.relative_to(out).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
               for p in out.rglob('*') if p.is_file()}
    dump(out/'delivery_integrity.json',integrity)
    print('Reports saved:',out,flush=True)


def reasons(folder,results):
    coverage=json.loads((folder/'coverage.json').read_text(encoding='utf-8'))
    lines=['','## 完成狀態','']
    for name,r in results.items():
        lines.append(f'- {name}: {r["status"]}；{r.get("error","詳見封存 summary")}')
    lines.append(f'- 請求 {len(coverage["expected_dates"])} 日；缺當時可用 ETF 快照 {len(coverage["missing_historical_etf_dates"])} 日。未用未來資料回填。')
    return lines


if __name__=='__main__':
    main()
