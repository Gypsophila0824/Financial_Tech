# 在 VS Code 顯示回測比較報告

新增的 `src.agent.backtest.compare_report` 將既有雙策略回測輸出整理成 Markdown 表格，並不重新執行策略。回測完成與報表呈現是兩個步驟，可在同一工作流程依序呼叫；不能把報表生成成功視為回測成功。

在 VS Code 開啟專案根目錄，選「終端機 → 新增終端機」，執行：

```powershell
$reportName = Get-Date -Format 'yyyyMMdd_HHmmss_ffff'
.\.venv\Scripts\python.exe -X utf8 -m src.agent.backtest.compare_report --comparison output/backtest/v6_research_pair_20260901_20261013_01/comparison.json --out-dir "output/backtest/report_$reportName"
```

開啟畫面印出的 report.md，按 Ctrl+Shift+V 預覽。

## 輸入與呈現

- 輸入為內部雙研究版本 comparison.json；需含 spec.start／end、results 及 fair_comparison_complete。完成版本的 baseline/、experiment/ 同層各須有 runner 產生的完整封存。
- 完整資料時：驗證封存完整性、共同來源快照、完整請求日期、相同初始本金與逐日帳本連續性，再產生期間損益、期末淨值、報酬與逐月比較。所有金額來自每日 NAV，不由訊號前推平均報酬換算。
- 不完整資料時：顯示失敗原因及資料覆蓋情況，不產生虛構的月損益。
- 產出：report.md、summary.json、source_comparison.json、report_integrity.json；只有完成時才有 monthly_comparison.csv。
- 月表採同一資金帳戶連續累積。每月報酬分母為該月首日 prior_nav，並非每月重新投入 10 億；因此不可與每月重置的截圖數字混稱。
- `compare_report` 這個既有投組損益展示工具沒有獨立 ETF 行情；研究線訊號報告已在下方加入三檔 ETF 價格基準。
- 此入口只展示既有雙策略比較；不支援把單獨正式 runner 的 report.md 當成雙策略 comparison.json。既有失敗比較缺少事件及 ETF 資料，報表仍會呈現未完成。

## 研究回測自動比較 ETF

執行研究線訊號回測時，`src.agent.research` 會自動從證交所月行情 API 取得 0050、00981A、00991A 收盤價，並在每次新研究輸出中加入 5／20／60 日同期價格報酬比較。執行方式：

```powershell
$runName = Get-Date -Format 'yyyyMMdd_HHmmss_ffff'
.\.venv\Scripts\python.exe -X utf8 -m src.agent.research --selected-count 45 --periods 5,20,60 --out-dir "output/research/$runName"
```

每份新研究輸出都會包含 `report.md`、`benchmark_prices.csv`、`benchmark_comparison.csv`、`benchmark_summary.csv`、`benchmark_sources.json` 及原始回應 `benchmark_raw/`。報告用相同排名日與前瞻終點計算三檔 ETF 的價格報酬，不含配息或交易成本；缺少 ETF 起訖收盤價時顯示覆蓋數，不補零。首次下載需逐月取得三檔 ETF 資料，可能多等一至兩分鐘；舊月份會存於 `output/benchmark_cache` 重用，每次執行更新當月資料。

報表與回測輸出都要求新目錄，不覆寫舊結果。研究回測不會再修改正式設定或正式上線核准。

## 指令一：每日正式策略檢查

只想先看今天正式策略會怎麼選股，並檢查近期正式策略回測時，從專案根目錄執行：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m src.agent.daily_workflow --team-id TEAM_11161 --checks-only
```

這條指令更新資料、產生今天的盤前彩排，並以**目前正式策略參數**回測最近 20 個已完成交易日（預設至少 5 日）。輸出集中在 `output/validation/daily_workflow/`，當次 `workflow.json` 會記錄彩排和回測狀態，回測目錄內可看 `report.md`、`daily_nav.csv`、`active_share_detail.csv`。正式回測只到最近完成交易日；當日彩排是下個交易決策的預演，並不是當日已實現報酬。`--checks-only` 不產生或提交正式 D-Plan，也不會跑研究候選。

## 指令二：正式線與研究線並排比較

要比較正式策略與不同研究參數，在另一個獨立輸出目錄執行：

```powershell
$runName = Get-Date -Format 'yyyyMMdd_HHmmss_ffff'
.\.venv\Scripts\python.exe -X utf8 -m src.agent.backtest.team_strategy_search `
  --recent-start 2026-09-01 --end 2026-10-20 --ytd-start 2026-01-01 `
  --out-dir "output/validation/backtest/team_strategy_search_$runName"
```

`--end` 可省略，省略時採資料庫最新交易日；`--recent-start` 指定近期比較起日，`--ytd-start` 指定逐月與年初累積區間起日。此入口先更新 ETF 持股，再以相同日期、初始本金、成交與成本規則跑正式線及研究候選；輸出最近區間摘要、逐月／年初累積、ETF 參考、各版本設定和來源封存。正式線已採 `pv_40_fund_50` 同一組設定，因此比較清單不會再把相同設定當作另一個研究候選重複列出。輸出必須放在 `output/validation/` 下的新目錄。

研究線比較屬離線實驗，不會改正式設定或產生 D-Plan。歷史 ETF 持股仍按實際公告日期做 point-in-time 檢查，今天抓到的資料不會回填過去；缺資料、公司事件或策略覆蓋不完整會明確標示失敗／未完成，不以缺漏日補報酬。新聞採中性消融，沒有歷史新聞快照時不代表完整重現正式策略。

若只需重建已完成的正式線／研究線比較報告，而不重新回測，使用 `team_strategy_search --render-existing --out-dir <既有比較目錄>`。

輸出中的 `etf_holdings_update.json` 記錄 ETF 快照更新、沿用資料與缺漏；今日抓到的 ETF 持股不會回填到歷史日期。缺資料或事件明細造成的回測失敗會保留在各策略狀態檔，不以缺漏日補報酬。

## 比較報告呈現工具

`src.agent.backtest.compare_report` 只整理既有雙策略 `comparison.json`，不會重新執行策略，也不接受單獨正式 runner 的 `report.md`。使用前須先由相應回測入口完成並封存策略結果。
