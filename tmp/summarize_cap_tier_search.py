from pathlib import Path
import sys,json,hashlib,shutil
import pandas as pd
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.agent.backtest import archive
OUT=Path('output/validation/backtest/cap_tier_search_20261023')
rows=[]
for p in sorted((OUT/'runs').glob('*/search_row.json')):
    row=json.loads(p.read_text(encoding='utf-8'))
    if row['status']=='ok':
        folder=p.parent
        integrity=json.loads((folder/'integrity.json').read_text(encoding='utf-8'))
        # Verify original sealed outputs before adding the two study diagnostics.
        for name,digest in integrity['files'].items():
            assert hashlib.sha256((folder/name).read_bytes()).hexdigest()==digest
        actual={f.name for f in folder.iterdir() if f.is_file()}
        assert actual-set(integrity['files'])-{'integrity.json'} <= {'cap_top3_audit.csv','search_row.json'}
        integrity['files']={f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in folder.iterdir() if f.is_file() and f.name!='integrity.json'}
        (folder/'integrity.json').write_bytes(archive.json_bytes(integrity))
        archive.verify_archive(folder)
        nav=pd.read_csv(folder/'daily_nav.csv')
        row['first7_return']=nav.nav.iloc[6]/nav.prior_nav.iloc[0]-1
        row['last6_return']=nav.nav.iloc[-1]/nav.nav.iloc[6]-1
    rows.append(row)
df=pd.DataFrame(rows)
df.to_csv(OUT/'all_results.csv',index=False,encoding='utf-8-sig')
primary=df[~df.id.str.startswith('user_monthly_')]
valid=primary[(primary.status=='ok') & (~primary.disqualified_after_three_warnings.fillna(True)) & (primary.records==13)].sort_values('cumulative_return',ascending=False)
capvalid=valid[valid.cap>0]
baseline=json.loads((OUT/'candidate_baseline/summary.json').read_text(encoding='utf-8'))
bnav=pd.read_csv(OUT/'candidate_baseline/daily_nav.csv')
base={'id':'candidate_baseline','pv':.4,'fund':.5,'news':.1,'cap':0,'retain_fraction':None,'protect_rank':50,
      **{k:baseline[k] for k in ['cumulative_return','max_drawdown','total_turnover','total_cost']},'excess_vs_candidate':0,
      'first7_return':bnav.nav.iloc[6]/bnav.prior_nav.iloc[0]-1,'last6_return':bnav.nav.iloc[-1]/bnav.nav.iloc[6]-1}
requested=valid[(valid.pv==.3)&(valid.cap==.1)&(valid.retain_fraction==.5)&(valid.protect_rank==70)].iloc[0].to_dict()
bestcap=capvalid.iloc[0].to_dict()
bestprotected=capvalid[capvalid.retain_fraction.notna()].iloc[0].to_dict()
best=valid.iloc[0].to_dict()
chosen=[('候選線基準',base),('指定30/50/10/10、跌出70減半',requested),('含市值組最佳',bestcap),('含市值且有保留規則最佳',bestprotected),('全部組合最佳（含無市值對照）',best)]
lines=['# 市值分級與前三名續抱：組合回測','',
    '共同交易區間：2026-10-05–2026-10-22，共13日；10億元本金。所有組合含估算交易成本，不含股利；新聞中性消融。0050為10/02–10/22收盤價格報酬，未扣成本。','',
    f'完成 {len(primary)} 組主研究、{len(df)-len(primary)} 組使用者月末資料敏感度研究。主研究失敗 {int((primary.status!="ok").sum())} 組。',
    f'主研究中含市值且勝過候選線：{int((capvalid.excess_vs_candidate>0).sum())}/{len(capvalid)} 組。','',
    '|版本|量價/基本面/新聞/市值|報酬|相對候選線（百分點）|最大回撤|雙邊換手|成本（萬元）|',
    '|---|---|---:|---:|---:|---:|---:|']
for label,r in chosen:
    weights='/'.join(f'{100*r[k]:g}' for k in ['pv','fund','news','cap'])
    lines.append(f'|{label}|{weights}|{r["cumulative_return"]:.2%}|{r["excess_vs_candidate"]*100:+.3f}|{r["max_drawdown"]:.2%}|{r["total_turnover"]:.2%}|{r["total_cost"]/10000:.2f}|')
lines += ['|0050|—|−5.23%|+1.364|待另算|—|未扣|','',
    '## 規則與搜尋範圍','',
    '- 第一階段80組：量價10/20/25/30/35/40/45/50%；市值0/5/10/15/20%；新聞固定10%；基本面補足100%。每組比較普通前50名續抱、與市值前三名特別續抱（跌出綜合70名減至50%目標）。市值0%為機制對照，不能當成市值因子勝出。',
    '- 第二階段於指定配重及第一階段前三名組合附近，另測保留25/75/100%、排名60/80門檻。這是同樣本探索，並非樣本外驗證。',
    '- 市值分數：1名2、2名1.5、3名1、4–5名0.8、6–10名0.65、11–20名0.55、其餘0.5。',
    '- 特別保留只適用已持有的市值前三名；排名51–70仍續抱，排名71起降低目標。原目標指同日、相同入選股的未減碼分數/波動率目標，不是每天對現有股數反覆減半。釋出權重分配給其餘持股，仍保留25檔及5%目標現金。',
    '- 市值跌出前三名後恢復普通前50名續抱規則。重大利空否決、單檔上限、Active Share、現金及整張限制優先。觸發減碼的股票解除3%無交易帶，至少保留一張；其餘持股仍用3%無交易帶。',
    f'- 指定版本減碼觸發 {int(requested["trim_trigger_count"])} 次；特別續抱延長 {int(requested["protection_extension_count"])} 個股票日。','',
    '## 時段分解','',
    '前7日／後6日為既有搜尋樣本的歸因分解，後6日已參與選優，不能稱為未見驗證集。','',
    '|版本|前7日|後6日|','|---|---:|---:|']
for label,r in chosen:
    lines.append(f'|{label}|{r["first7_return"]:.2%}|{r["last6_return"]:.2%}|')
monthly=df[df.id.str.startswith('user_monthly_')]
lines += ['','## 使用者6–9月檔案與來源限制','',
    '- 已抽取80筆月末市值，四個日期各20檔，含0050；76筆個股收盤價與專案行情一致。市值乘法與單位換算全部通過核對。',
    '- 固定20檔名單不是全市場前20的證明。四個月末名單內前三名均為2330、2454、2308。',
    '- 月末資料只能自下一個交易日使用，不以6月底數值回填6月初，也不從四個月末插值製造每日市值。敏感度研究在10月沿用9/30名單內排名，與逐日估算排行分開報告。',
    '- 原工作簿含股數估算、緯穎配股及其他增資待核註記；未改寫原檔。',
    '- 歷史ETF持股快照最早2026-10-02，因此目前仍無法給出完整合規的6–9月投組回測。',
    '- 主研究10月市值來源是既有估算：使用10/12股數快照，0050部分單位數由10/22倒推。即使行情與基本面使用歷史可得資料，市值仍不是嚴格歷史時點認證資料，可能含事後資訊。',
    '- 僅13個交易日且多組選優；任何較佳結果均為樣本內結果，不能視為未來績效證明。','',
    '|月末資料敏感度版本|報酬|相對候選線（百分點）|','|---|---:|---:|']
for r in monthly.itertuples():
    if r.status=='ok': lines.append(f'|{r.id}|{r.cumulative_return:.2%}|{r.excess_vs_candidate*100:+.3f}|')
lines += ['','## 重現與檢查','',
    '- `all_results.csv`列出全部組合、失敗及分段報酬；每個成功組合有逐日NAV、持股、交易、風控、來源封存及市值前三名稽核。',
    '- 市值0%、量價40%、基本面50%、無特別續抱的組合與候選線基準報酬、成本一致。',
    '- 5項研究測試通過：市值分段邊界、70/71名減碼、市值跌出前三解除、利空優先、強制減碼解除無交易帶且普通行為不變。',
    '- 正式策略參數、候選線及正式帳本未修改；新增研究模組沒有接入正式入口。','']
(OUT/'比較報告.md').write_text('\n'.join(lines),encoding='utf-8')
for name in ['run_cap_tier_search.py','summarize_cap_tier_search.py','prepare_cap_tier_search.py']:
    shutil.copy2(Path('tmp')/name,OUT/'sources'/name)
(OUT/'headline.json').write_text(json.dumps({'primary_runs':len(primary),'monthly_runs':len(monthly),'cap_winners':int((capvalid.excess_vs_candidate>0).sum()),'requested':requested,'best_cap':bestcap,'best_overall':best},ensure_ascii=False,indent=2,default=str),encoding='utf-8')
print(json.dumps({'runs':len(primary),'monthly':len(monthly),'cap_winners':int((capvalid.excess_vs_candidate>0).sum()),'requested_return':requested['cumulative_return'],'best_cap':bestcap['id'],'best_cap_return':bestcap['cumulative_return'],'best':best['id'],'best_return':best['cumulative_return']},ensure_ascii=False))
