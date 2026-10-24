"""Fractional, lagged-signal research proxy; not contest execution accounting."""
from pathlib import Path
import sys,json,pickle,copy,hashlib
from itertools import product
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pandas as pd
import numpy as np
from src.agent.inputs import StoreInputs
from src.agent.datafeed import pv_factors
from src.agent.datafeed.benchmark_prices import _parse
from src.agent.cap_tier_research import CapTierResearch
from src.agent.state import PortfolioState
from src.agent import rules

ROOT=Path('output/validation/backtest/cap_tier_search_20261023')
OUT=ROOT/'quarter_research'
OUT.mkdir(exist_ok=True)
monthly=pd.read_csv(ROOT/'sources/user_month_end_market_caps.csv',dtype={'ticker':str})
store=StoreInputs(history=True,strict_history=True,ignore_etf_holdings=True)
dates=[d for d in store.dates() if '2026-06-01'<=d<='2026-09-30']
sourcehash=hashlib.sha256()
for p in ['data/market/daily.csv','data/market/twse_exright.csv','data/history/fundamental/fund_history.csv']:
    sourcehash.update(Path(p).read_bytes())
cache=OUT/f'day_cache_{sourcehash.hexdigest()[:12]}.pkl'
if cache.exists():
    with cache.open('rb') as f: days=pickle.load(f)
else:
    days={}
    for i,d in enumerate(dates):
        day=store.day(d)
        eligible=monthly[monthly.date<=day.data_date]
        ranks=pd.Series(21,index=day.master.index,dtype=int)
        if len(eligible):
            one=eligible[eligible.date==eligible.date.max()].set_index('ticker')
            ranks=one.rank_within_fixed20.reindex(day.master.index).fillna(21).astype(int)
        day.cap_research_ranks=ranks
        days[d]=day
        if i%10==0: print('PREPARED',i+1,len(dates),d,flush=True)
    with cache.open('wb') as f: pickle.dump(days,f)
returns,warnings=pv_factors.adjusted_returns(store.daily)
bad=[w for w in warnings if '2026-06-01'<=w.split()[0]<='2026-09-30']
if bad: raise ValueError('Unresolved price jumps, not replaced by zero: '+str(bad))

def sim(pv,cap,retain,start='2026-07-01',end='2026-09-30',special=70,save=None):
    adapter=CapTierResearch(pv_weight=pv,cap_weight=cap,retain_fraction=retain,protect_rank=special,enforce_active_share=False)
    tickers=days[dates[0]].master.index
    value=pd.Series(0.,index=tickers)
    cash=1e9
    nav=1e9
    peak=nav
    state=PortfolioState.initial()
    rows=[]
    holdings=[]
    for d in [d for d in dates if start<=d<=end]:
        prior=nav
        ret=returns.loc[d].reindex(tickers)
        exposed=value>0
        missing=ret.isna()&exposed
        # A no-quote suspension is carried at last mark, as in StoreInputs.close.
        # Nonmissing invalid returns are never filled or clipped in this study.
        if ret[exposed].dropna().abs().gt(.105).any(): raise ValueError('Unresolved return '+d)
        value*=1+ret.fillna(0)
        nav=float(value.sum()+cash)
        state.nav=nav
        state.holdings={t:1 for t in value[value>0].index}
        day=copy.deepcopy(days[d])
        target=adapter(day,state)
        desired=target.picks.weight.reindex(tickers).fillna(0)*nav
        current=value/nav
        selected=pd.Series(tickers.isin(target.picks.index),index=tickers)
        force=target.picks.force_rebalance.reindex(tickers).fillna(False).astype(bool)
        limits=pd.Series(.095,index=tickers)
        if '2330' in limits.index: limits.loc['2330']=.245
        change=(desired-value).abs()/nav
        rebalance=(~selected)|(value==0)|(change>.03)|(current>limits)|force
        # A stock lacking a current close cannot be traded by this proxy.
        tradable=pd.Series(~ret.isna(),index=tickers)
        after=value.where(~(rebalance&tradable),desired)
        cash_after=cash-float((after-value).sum())
        # Same cash-trigger levels as the production policy, but fractional shares.
        if cash_after/nav>.08:
            for t in (desired-after).sort_values(ascending=False).index:
                if cash_after/nav<=.08: break
                amount=max(0,float(desired[t]-after[t])) if tradable[t] else 0
                after[t]+=amount
                cash_after-=amount
        elif cash_after/nav<.03:
            for t in (after-desired).sort_values(ascending=False).index:
                if cash_after/nav>=.03: break
                amount=max(0,float(after[t]-desired[t])) if tradable[t] else 0
                after[t]-=amount
                cash_after+=amount
        delta=after-value
        buys=float(delta.clip(lower=0).sum())
        sells=float((-delta.clip(upper=0)).sum())
        cost=(buys+sells)*rules.FEE_RATE+sells*rules.TAX_RATE
        cash=cash_after-cost
        if cash<0: raise ValueError('Negative cash in proxy '+d)
        value=after
        nav=float(value.sum()+cash)
        peak=max(peak,nav)
        rows.append(dict(date=d,nav=nav,prior_nav=prior,daily_return=nav/prior-1,drawdown=nav/peak-1,
            cost=cost,turnover=(buys+sells)/prior,cash_pct=cash/nav,holdings=int((value>0).sum()),
            carried_missing_quote_count=int(missing.sum())))
        if save:
            holdings.extend({'date':d,'ticker':t,'weight':float(value[t]/nav)} for t in value[value>0].index)
    frame=pd.DataFrame(rows)
    row={'pv':pv,'fund':adapter.fund_weight,'news':.1,'cap':cap,'retain':retain,'special_rank':special,
         'start':start,'end':end,'days':len(frame),'return':nav/1e9-1,'max_drawdown':float(frame.drawdown.min()),
         'cost':float(frame.cost.sum()),'turnover':float(frame.turnover.sum()),
         'training_jul_aug':float(frame.loc[frame.date<='2026-08-31','nav'].iloc[-1]/1e9-1) if start>='2026-07-01' else None,
         'trim_count':sum(a['trim_triggered'] for a in adapter.audit)}
    for month,g in frame.groupby(frame.date.str[:7]):
        row[month]=float(g.nav.iloc[-1]/g.prior_nav.iloc[0]-1)
    if save:
        dest=OUT/save
        dest.mkdir(exist_ok=True)
        frame.to_csv(dest/'daily_nav.csv',index=False,encoding='utf-8-sig')
        pd.DataFrame(holdings).to_csv(dest/'holdings.csv',index=False,encoding='utf-8-sig')
        pd.DataFrame(adapter.audit).to_csv(dest/'cap_top3_audit.csv',index=False,encoding='utf-8-sig')
        (dest/'summary.json').write_text(json.dumps(row,ensure_ascii=False,indent=2),encoding='utf-8')
    return row

if '--save-protected' in sys.argv:
    row=sim(.5,.05,.5,save='best_with_protection_Q3')
    summary=json.loads((OUT/'quarter_summary.json').read_text(encoding='utf-8'))
    summary['best_with_protection_Q3']=row
    (OUT/'quarter_summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print('PROTECTED_QUARTER_SAVED',json.dumps(row),flush=True)
    sys.exit(0)
baseline=sim(.4,0,None,save='candidate_Q3')
june=sim(.4,0,None,start='2026-06-01',end='2026-06-30',save='candidate_June')
continuous=sim(.4,0,None,start='2026-06-01',end='2026-09-30',save='candidate_June_September')
print('BASELINE_Q3',json.dumps(baseline),flush=True)
requested=sim(.3,.1,.5,save='requested_Q3')
grid=list(product([.2,.3,.375,.4,.475,.5],[.01,.025,.05,.10,.15],[None,.5]))
rows=[]
for i,(pv,cap,retain) in enumerate(grid):
    row=sim(pv,cap,retain)
    row['excess_vs_candidate']=row['return']-baseline['return']
    rows.append(row)
    pd.DataFrame(rows).to_csv(OUT/'grid_results.csv',index=False,encoding='utf-8-sig')
    if i%5==0: print('QUARTER_GRID',i+1,len(grid),json.dumps(row),flush=True)
df=pd.DataFrame(rows)
# Selection uses Jul-Aug only. September is evaluated after that selection.
selected=df.sort_values('training_jul_aug',ascending=False).iloc[0]
train_selected=sim(float(selected.pv),float(selected.cap),None if pd.isna(selected.retain) else float(selected.retain),save='selected_on_Jul_Aug')
best=df.sort_values('return',ascending=False).iloc[0]
full_selected=sim(float(best.pv),float(best.cap),None if pd.isna(best.retain) else float(best.retain),save='best_Q3_in_sample')
requested['excess_vs_candidate']=requested['return']-baseline['return']
benchmark=[]
for month in ['202605','202606','202607','202608','202609']:
    paths=list(Path('output').glob(f'**/0050_{month}.json'))
    if not paths: raise ValueError('Missing 0050 cache '+month)
    raw=paths[0].read_bytes()
    parsed=_parse(raw,'0050',month)
    benchmark.append(parsed)
    (OUT/f'0050_{month}_source.json').write_bytes(raw)
b=pd.concat(benchmark).drop_duplicates('date').set_index('date').close.sort_index()
b.to_csv(OUT/'0050_prices.csv',encoding='utf-8-sig')
bench={}
for label,start,end in [('June','2026-05-29','2026-06-30'),('July','2026-06-30','2026-07-31'),('August','2026-07-31','2026-08-31'),('September','2026-08-31','2026-09-30'),('Q3','2026-06-30','2026-09-30'),('June_September','2026-05-29','2026-09-30')]:
    bench[label]=float(b.loc[end]/b.loc[start]-1)
result={'model':'fractional adjusted-reference-return proxy; next-session closing rebalance using prior-day scores; not formal order accounting',
    'baseline_Q3':baseline,'baseline_June':june,'baseline_June_September':continuous,'requested_Q3':requested,
    'selected_on_Jul_Aug':train_selected,'best_Q3_in_sample':full_selected,'benchmark_0050_price_only':bench,
    'grid_count':len(rows),'Q3_winners':int((df['return']>baseline['return']).sum()),
    'limitations':['Active Share disabled: ETF holdings absent before October','Fractional shares and closing execution proxy, not official whole-lot average-fill ledger',
        'Adjusted-reference price returns remove ex-right/ex-dividend mechanical jumps; not verified cash-dividend total returns or contest-deferred dividend NAV',
        'Fixed20 monthly universe may omit historical larger companies','June cap strategy unavailable before first June30 snapshot; no backward filling',
        'September used only after July-August training selection; full-Q3 winner remains in-sample','0050 uses unadjusted price returns, excludes distributions; strategy adjusted-reference proxy is not exactly the same return convention']}
(OUT/'quarter_summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print('QUARTER_COMPLETE',json.dumps(result,ensure_ascii=False),flush=True)
