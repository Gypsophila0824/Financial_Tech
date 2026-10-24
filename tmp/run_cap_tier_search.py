from pathlib import Path
import sys, json, copy, time, hashlib, shutil
from itertools import product
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pandas as pd
import openpyxl
from src.agent.backtest import archive, runner
from src.agent.cap_tier_research import CapTierResearch
from src.agent.cap_research_execution import plan_trades

OUT=Path('output/validation/backtest/cap_tier_search_20261023')
OUT.mkdir(parents=True,exist_ok=True)
SRC=Path(r'C:\Users\jessi\Downloads\2026_06_09_20securities_marketcap_estimates.xlsx')
sources=OUT/'sources'
sources.mkdir(exist_ok=True)
shutil.copy2(SRC,sources/SRC.name)
wb=openpyxl.load_workbook(SRC,read_only=True,data_only=True)
detail=[]
for row in list(wb['價格與股數'].values)[5:]:
    if not hasattr(row[0],'date'): continue
    d,t,name,kind,price,shares,cap,cap100,status,shown,reported,_,ss,ps,original=row
    assert abs(price*shares-cap)<max(1,cap*1e-10)
    assert abs(cap/1e8-cap100)<1e-6
    detail.append(dict(date=d.date().isoformat(),ticker=str(t),name=name,kind=kind,
        close=price,shares=shares,market_cap_100m=cap100,status=status,
        shares_source=ss,price_source=ps))
monthly=pd.DataFrame(detail)
assert len(monthly)==80 and not monthly.duplicated(['date','ticker']).any()
monthly['rank_within_fixed20']=monthly.groupby('date').market_cap_100m.rank(ascending=False,method='first').astype(int)
monthly.to_csv(sources/'user_month_end_market_caps.csv',index=False,encoding='utf-8-sig')
for name in ['twse_market_cap_top20.csv','twse_market_cap_top20_manifest.json']:
    shutil.copy2(Path('data/market')/name,sources/name)
(sources/'manifest.json').write_text(json.dumps({'workbook_sha256':hashlib.sha256(SRC.read_bytes()).hexdigest(),
    'rows':80,'dates':sorted(monthly.date.unique()),'fixed_securities':20,
    'includes_etf_0050':True,'unit':'TWD 100 million','timing':'month-end observations usable only on following trading dates; no backward filling',
    'limitations':['Fixed 20 names, not verified full-market top20','Monthly observations, not daily ranks','Share counts include estimates','No historical ETF holdings prior to October']},ensure_ascii=False,indent=2),encoding='utf-8')

class CachedArchive(archive.ArchivedInputs):
    def __init__(self,path):
        super().__init__(path)
        self.cached={d:super(CachedArchive,self).day(d) for d in self.rows}
        cap=pd.read_csv(sources/'twse_market_cap_top20.csv',dtype={'ticker':str})
        for day in self.cached.values():
            one=cap[cap.date==day.data_date]
            if len(one)!=20 or one.ticker.duplicated().any():
                raise ValueError('Missing/duplicate market-cap snapshot '+day.data_date)
            ranks=pd.Series(21,index=day.master.index,dtype=int)
            for r in one.itertuples():
                if r.ticker in ranks.index: ranks.loc[r.ticker]=int(r.rank)
            day.cap_research_ranks=ranks
    def day(self,d): return copy.deepcopy(self.cached[d])

inputs=CachedArchive(OUT/'candidate_baseline')
baseline=json.loads((OUT/'candidate_baseline/summary.json').read_text(encoding='utf-8'))
allrows=[]
monthly_mode='--monthly' in sys.argv
refine_mode='--refine' in sys.argv
if monthly_mode:
    for day in inputs.cached.values():
        available=monthly[monthly.date<=day.data_date]
        snapshot=available[available.date==available.date.max()].set_index('ticker')
        day.cap_research_ranks=snapshot.rank_within_fixed20.reindex(day.master.index).fillna(21).astype(int)
def run(pv,cap,retain,rank=70,band=.03):
    key=f'pv{round(pv*100):02d}_cap{round(cap*100):02d}_retain{0 if retain is None else round(retain*100):03d}_rank{rank}_band{round(band*100):02d}'
    if monthly_mode: key='user_monthly_'+key
    path=OUT/'runs'/key
    if path.exists():
        allrows.append(json.loads((path/'search_row.json').read_text(encoding='utf-8')))
        return
    adapter=CapTierResearch(pv_weight=pv,cap_weight=cap,retain_fraction=retain,protect_rank=rank,no_trade_band=band)
    row={'id':key,'pv':pv,'fund':adapter.fund_weight,'news':.1,'cap':cap,'retain_fraction':retain,'protect_rank':rank,'band':band,
         'cap_source':'user fixed20 month-end snapshots, carried forward' if monthly_mode else 'project estimated daily full-market top20'}
    try:
        with patch.object(runner,'plan_trades',plan_trades):
            result=runner.run_formal_backtest(inputs,start='2026-10-05',end='2026-10-22',strategy_adapter=adapter,price_only=True)
        assert result.daily_nav.date.tolist()==list(inputs.rows)
        row.update({k:result.summary[k] for k in ['cumulative_return','max_drawdown','total_cost','total_turnover','records','daily_warning_count','disqualified_after_three_warnings']})
        row['excess_vs_candidate']=row['cumulative_return']-baseline['cumulative_return']
        row['trim_trigger_count']=sum(r['trim_triggered'] for r in adapter.audit)
        row['protection_extension_count']=sum(r['protected'] and r['score_rank']>50 for r in adapter.audit)
        row['status']='ok'
        runner.write_result(result,path,config={'study':'cap-tier-search','source_workbook':str(SRC),'cap_source':row['cap_source'],'point_in_time_cap_warning':'historical ranks use estimated share snapshots; not certified strict point-in-time'})
        pd.DataFrame(adapter.audit).to_csv(path/'cap_top3_audit.csv',index=False,encoding='utf-8-sig')
    except Exception as e:
        row.update(status='failed',error=str(e))
        path.mkdir(parents=True,exist_ok=True)
    (path/'search_row.json').write_text(json.dumps(row,ensure_ascii=False,indent=2),encoding='utf-8')
    allrows.append(row)
    filename='monthly_sensitivity_results.csv' if monthly_mode else ('refinement_results.csv' if refine_mode else 'search_results.csv')
    pd.DataFrame(allrows).to_csv(OUT/filename,index=False,encoding='utf-8-sig')
    print(json.dumps(row,ensure_ascii=False),flush=True)

# Prespecified coarse grid; news, cash, sizing and standard holding buffer fixed.
# Cap=0 controls isolate weight changes without claiming a market-cap improvement.
grid=list(product([.10,.20,.25,.30,.35,.40,.45,.50],[0,.05,.10,.15,.20],[None,.50]))
plan={'phase1_count':len(grid),'pv':[.10,.20,.25,.30,.35,.40,.45,.50],'cap':[0,.05,.10,.15,.20],
      'retain':[None,.5],'news':.1,'fund':'1-pv-cap-news','special_rank':70,'no_trade_band':.03,
      'phase2':'retain25/75/100 and rank60/80 at requested weights and top3 phase1 weights; post-selection exploration, not out-of-sample',
      'evaluation':'All 13 sessions, plus chronological first7/last6 attribution; last6 is not untouched holdout.'}
(OUT/'search_plan.json').write_text(json.dumps(plan,indent=2),encoding='utf-8')
if monthly_mode:
    primary=pd.read_csv(OUT/'search_results.csv')
    if (OUT/'refinement_results.csv').exists():
        primary=pd.concat([primary,pd.read_csv(OUT/'refinement_results.csv')],ignore_index=True)
    good=primary[(primary.status=='ok') & (primary.cap>0)].sort_values('cumulative_return',ascending=False)
    combinations={(.3,.1,None),(.3,.1,.5)}|{(float(r.pv),float(r.cap),None if pd.isna(r.retain_fraction) else float(r.retain_fraction)) for r in good.head(4).itertuples()}
    for pv,cap,retain in sorted(combinations,key=str): run(pv,cap,retain)
    print('MONTHLY_SENSITIVITY_COMPLETE',len(allrows),flush=True)
    sys.exit(0)
if refine_mode:
    refinement={'pv':[.375,.4,.475,.5],'cap':[.01,.025,.075],'retain':[None,.5],
                'count':24,'reason':'One final lower-cap-weight sensitivity round; selected after coarse-grid results; in-sample only.'}
    (OUT/'refinement_plan.json').write_text(json.dumps(refinement,indent=2),encoding='utf-8')
    for pv,cap,retain in product(refinement['pv'],refinement['cap'],refinement['retain']):
        run(pv,cap,retain)
    for pv,retain in product([.375,.475],[None,.5]):
        run(pv,0,retain)
    print('REFINEMENT_COMPLETE',len(allrows),flush=True)
    sys.exit(0)
for pv,cap,retain in grid: run(pv,cap,retain)
valid=sorted([r for r in allrows if r['status']=='ok' and r['cap']>0],key=lambda r:r['cumulative_return'],reverse=True)
weights={(.30,.10)}|{(r['pv'],r['cap']) for r in valid[:3]}
for pv,cap in sorted(weights):
    for retain in [.25,.75,1.0]: run(pv,cap,retain)
    for rank in [60,80]: run(pv,cap,.5,rank=rank)
pd.DataFrame(allrows).to_csv(OUT/'search_results.csv',index=False,encoding='utf-8-sig')
print('SEARCH_COMPLETE',len(allrows),flush=True)
