from pathlib import Path
import json,pickle,hashlib,shutil
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pandas as pd
OUT=Path('output/validation/backtest/cap_tier_search_20261023')
Q=OUT/'quarter_research'
s=json.loads((Q/'quarter_summary.json').read_text(encoding='utf-8'))
grid=pd.read_csv(Q/'grid_results.csv')
lines=['# 第三季市值策略比較（研究估算）','',
    '2026-07-01–2026-09-30，共63個交易日，起始10億元現金；6月另列。策略使用同一套參考價還原報酬、前日選股訊號、次日收盤再平衡與費稅估算。此模型採可分割持股，停用Active Share，不是正式整張委託與股利權益帳本。','',
    '|版本|量價/基本面/新聞/市值|7月|8月|9月|第三季|最大回撤|',
    '|---|---|---:|---:|---:|---:|---:|']
labels=[('候選線',s['baseline_Q3']),('指定版本：排名70外減半',s['requested_Q3']),
        ('保留前三名規則最佳',s['best_with_protection_Q3']),
        ('一般續抱最佳，亦為7–8月選出的組合',s['selected_on_Jul_Aug'])]
for label,r in labels:
    weights='/'.join(f'{100*r[k]:g}' for k in ['pv','fund','news','cap'])
    lines.append(f'|{label}|{weights}|{r["2026-07"]:.2%}|{r["2026-08"]:.2%}|{r["2026-09"]:.2%}|{r["return"]:.2%}|{r["max_drawdown"]:.2%}|')
lines += ['',f'搜尋{len(grid)}組，第三季報酬勝過候選線的組合共{s["Q3_winners"]}組。',
    '完整組合列於 `grid_results.csv`。訓練選擇只依7–8月報酬排序；9月沒有參與該版本的選擇。固定20檔名單本身仍帶有事後選樣限制，因此此時間切分不等於獨立外部驗證。','',
    '## 0050價格基準與6月','',
    '|期間|0050價格報酬|','|---|---:|']
for label,key in [('6月','June'),('7月','July'),('8月','August'),('9月','September'),('第三季','Q3'),('6–9月','June_September')]:
    lines.append(f'|{label}|{s["benchmark_0050_price_only"][key]:.2%}|')
lines += ['',
    f'候選線研究估算：6月獨立起始報酬{s["baseline_June"]["return"]:.2%}；6/1起連續持有至9/30為{s["baseline_June_September"]["return"]:.2%}。',
    '6–9月連續持有與7/1重新從現金起始的第三季回測，持倉路徑不同，不能把兩段獨立起始報酬相加。',
    '0050取TWSE官方月行情封存的未還原收盤價，不含配息與交易成本。策略採參考價還原報酬代理，兩者收益口徑不同，因此這裡不以兩者的細小差額宣稱打敗0050。','',
    '## 市值資料時點','',
    '- 使用使用者工作簿6/30、7/31、8/31、9/30的固定20檔證券市值估算。含0050，並非經全市場驗證的各月前20名。',
    '- 7月使用6月底排名，8月使用7月底排名，9月使用8月底排名；月底已收盤資料可供下一個交易日使用。未向前回填、未以月底資料插值每日排名。',
    '- 第一份市值快照為6/30，故市值策略的6月及6–9月完整報酬尚無可用數字；未以未來排名補算。',
    '- 月末排名之間實際是否跌出市值前三未知；本研究是月末更新的保留規則，不代表逐日市值監控。','',
    '## 會計與執行限制','',
    '- 參考價還原報酬會消除除權息與分割的機械跳動，但不是核實現金配息再投資總報酬，也不是競賽期末遞延股利NAV。',
    '- 原始公司事件股利／配股明細不足，不能產出完整正式會計口徑的季度績效；本研究不假造事件。',
    '- 沒有6–9月ETF歷史持股，Active Share未驗證；現金、權重及費稅採研究近似，不宣稱符合正式提交規範。',
    '- 訊號用t-1資料，t日先計既有曝險報酬，再於收盤調整，新增股票從下一交易日開始貢獻；第一日現金不獲取當日股票報酬。',
    '- 手續費按雙邊成交金額0.1425%，股票賣出稅0.3%；採可分割持股、未模擬整張捨入、成交均價與滑價。',
    '- 額外前70名保留／減碼不覆蓋重大利空否決與單檔上限；目前新聞為中性消融。','',
    '## 10月整張帳本搜尋','',
    '10/05–10/22的128組主研究與6組月末資料敏感度研究，見上層 `比較報告.md`。其成交與股利口徑不同，不能將研究季度NAV直接接到10月整張帳本NAV。','']

checks=[]
for directory in ['candidate_Q3','requested_Q3','best_with_protection_Q3','selected_on_Jul_Aug','best_Q3_in_sample','candidate_June','candidate_June_September']:
    r=json.loads((Q/directory/'summary.json').read_text(encoding='utf-8'))
    nav=pd.read_csv(Q/directory/'daily_nav.csv')
    h=pd.read_csv(Q/directory/'holdings.csv')
    assert len(nav)==r['days'] and nav.date.is_unique
    assert abs(float((1+nav.daily_return).prod()-1)-r['return'])<1e-10
    assert nav.cost.ge(0).all() and nav.cash_pct.ge(0).all()
    total=h.groupby('date').weight.sum().reindex(nav.date).to_numpy()+nav.cash_pct.to_numpy()
    assert max(abs(total-1))<1e-10
    assert nav.carried_missing_quote_count.sum()==0
    assert nav.holdings.between(20,30).all()
    checks.append({'run':directory,'nav_and_month_compounding':'passed','weights_plus_cash':'passed','missing_held_returns':0,'holdings_20_to_30':'passed'})
cache=next(Q.glob('day_cache_*.pkl'))
with cache.open('rb') as f: days=pickle.load(f)
assert all(day.data_date<d for d,day in days.items())
train_best=grid.sort_values('training_jul_aug',ascending=False).iloc[0]
assert s['selected_on_Jul_Aug']['pv']==float(train_best.pv)
assert s['selected_on_Jul_Aug']['cap']==float(train_best.cap)
(Q/'checks.json').write_text(json.dumps({'checks':checks,'all_84_signal_dates_before_execution_date':True,'Jul_Aug_selection_reconciled':True},indent=2),encoding='utf-8')
lines+=['檢查：7組重點輸出的日報酬複利、現金加持股權重、20–30檔持股、缺漏報酬及84個訊號日期均通過核對；7–8月選擇結果亦與搜尋表一致。','']
(Q/'季度比較報告.md').write_text('\n'.join(lines),encoding='utf-8')
shutil.copy2('tmp/quarter_cap_study.py',Q/'quarter_cap_study.py')
shutil.copy2('tmp/report_quarter_cap_study.py',Q/'report_quarter_cap_study.py')
sources={}
for path in ['data/market/daily.csv','data/market/twse_exright.csv','data/history/fundamental/fund_history.csv','src/agent/cap_tier_research.py','src/agent/cap_research_execution.py']:
    sources[path]=hashlib.sha256(Path(path).read_bytes()).hexdigest()
(Q/'source_hashes.json').write_text(json.dumps(sources,indent=2),encoding='utf-8')
old=(OUT/'比較報告.md').read_text(encoding='utf-8')
old+='\n## 後續季度研究\n\n已完成第三季60組研究比較，另列6月及6–9月。見 `quarter_research/季度比較報告.md`；其參考價還原與可分割持股口徑不同於本表整張帳本。\n\n補充：第三階段另測低市值1/2.5/7.5%與量價37.5/40/47.5/50%的24組，另加4組無市值對照，均為同樣本探索。\n'
(OUT/'比較報告.md').write_text(old,encoding='utf-8')
print(json.dumps({'quarter_runs':len(grid),'winners':s['Q3_winners'],'baseline':s['baseline_Q3']['return'],'requested':s['requested_Q3']['return'],'train_selected':s['selected_on_Jul_Aug'],'best':s['best_Q3_in_sample']},ensure_ascii=False))
