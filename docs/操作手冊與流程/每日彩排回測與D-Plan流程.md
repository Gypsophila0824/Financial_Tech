# 每日資料累積、彩排、回測與 D-Plan

新入口 `src.agent.daily_workflow` 串接既有正式策略，不調整參數、不自動上傳平台。

## 操作

從專案根目錄每天執行（建議下一交易日前一日曆日 19:30 後）：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m src.agent.daily_workflow --team-id TEAM_11161 --checks-only
```

此指令累積資料、執行盤前彩排和最近 20 個已完成交易日的嚴格歷史回測。自動選日期：08:55 前選今日或下一交易日，之後選下一交易日；週末與休市依本地交易日曆。亦可明確指定 `--trade-date YYYY-MM-DD`。資料抓取取得的是目前來源，不會替指定的過去日期偽造資料。

完成前一交易日 `live settle` 與官方帳本核對後，執行正式產出：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m src.agent.daily_workflow --team-id TEAM_11161 --reconciled
```

`--reconciled` 是操作者已核對後台的聲明，不能由排程盲目代填。正式產出仍使用 `live.cmd_run` 的上線核准、時窗、帳本、API、驗證與封存機制。現有 `config/official_policy.json` 待辦未完成時會阻擋；本功能不修改核准旗標。

## 資料與驗收

1. 更新行情、法人、基本面與新聞；新聞抓取失敗沿用既有降級政策。
2. ETF 有完整且逐檔同日的合格快照就直接使用；缺少、不完整或較舊則抓取一次。抓取不完整停止。來源仍為較舊公告時明示 `source_pending_update`，沿用既有日期驗證與警示，不把抓取時間當成公告時間，不自訂官方過期天數。
3. 保存程式、原始資料及帳本副本與 SHA256 到本次 `sources/`；不含 `.env`。正式產出前確認這些來源沒有變動。流程彼此以 lock 防止並行；若程序被強制終止，先確認 lock 內 PID 已停止才手動移除 `.running.lock`。
4. 使用正式帳本副本及目前輸入執行 REPLAY 盤前彩排，使用中性新聞與範本文字；不結算未發生的交易日、不寫正式帳本。正式 API 新聞可能改變分數，因此正式結果仍須重新驗證，彩排不保證與正式委託相同。
5. 正式回測截止於 T-1，預設檢查最近 20 日、至少 5 個有效交易日，可用 `--lookback-days` 與 `--min-backtest-days` 設定。不使用未來快照、不略過回測中途缺檔、不略過公司事件錯誤；`--corporate-actions <JSON>` 可提供已核實事件。新聞歷史缺少時維持中性消融，不宣稱完整重現正式策略。
6. 歷史違規警告、連續 Active Share 偏低、彩排 REJECT、輸入變動等會停止。回測報酬率不作版本挑選或自動調參依據。
7. 歷史樣本不足獨立標示；預設停止。若已決定允許起始累積期的不足，可明確加 `--allow-insufficient-history`，不會略過其他風險或錯誤。樣本不足仍不代表策略已驗證。
8. 正式模式呼叫既有 API／快取，必要時依既有中性新聞與範本降級。成功只標「可送審」，仍由人審核、上傳及登錄回執。

## 輸出與限制

每次存至 `output/validation/daily_workflow/trade_<交易日>_<時間與識別碼>/`，例如 `trade_2026-10-21_190142_8ac8a1f3/`。內含 `workflow.json`、`source_manifest.json`、`sources/`、`rehearsal/` 及可執行時的 `backtest/`。正式產出另存既有 `output/live/runs/`，路徑記在報告 `formal_runs`。流程結束時命令列會列出本次資料夾、D-Plan、彩排審核摘要與合規明細、回測 `report.md` 與 NAV（若已產生）、`workflow.json`、正式輸出（若有）及總索引的絕對路徑；可直接複製路徑傳給組員。

本次也修正歷史 `simulate --data real`：使用嚴格歷史快照，結算傳入當日可得 ETF 基準及逐檔日期，保存收盤連續風險紀錄；合成 mock 不冒充真實觀測。舊彩排結果未重寫。

此功能提供每日可呼叫的命令，尚未安裝背景排程。歷史 ETF 公告日與抓取時間不能完全證明發布截止時間；不得據此宣稱官方當日合規。策略績效、公司事件完整性與平台核對仍遵守既有驗收限制。

## 統一檢視位置

所有新彩排與回測輸出集中在 `output/validation/`。單獨執行 `simulate` 彩排會列出輸出資料夾及最近一日 D-Plan／審核摘要；`backtest.runner` 回測會列出輸出資料夾、`report.md`、`daily_nav.csv` 與 `compliance.csv`。先開啟命令列顯示的「彩排與回測總索引」絕對路徑，或從專案根目錄開啟 `output/validation/README.md` 查看所有執行紀錄。

- `daily_workflow/trade_<交易日>_<run>/`：每日流程，包含當次彩排、回測及來源封存。
- `replay/period_<起日>_to_<迄日>_<run>/`：獨立歷史彩排。
- `replay_api/period_<起日>_to_<迄日>_<run>/`：API 彩排。
- `mock/period_<起日>_to_<迄日>_<run>/`：合成資料模擬。
- `backtest/formal_<起日>_to_<迄日>_<run>/`：獨立正式策略回測。

每次新彩排分開保存；資料夾名稱含起訖日期及唯一識別碼。同一天多次執行會建立不同資料夾。要接續舊彩排，使用 `simulate --resume output/validation/replay/<日期區間資料夾>`（mock 使用對應目錄）。回測自訂 `--out-dir` 也必須位於 `output/validation/` 下的新子目錄，不能覆寫既有結果。
舊結果不搬移、不改寫，索引附舊目錄連結，避免破壞封存與重現路徑。`docs` 保留說明文件；正式 D-Plan 維持 `output/live/runs/`。
