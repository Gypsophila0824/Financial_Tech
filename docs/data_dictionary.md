# 資料字典與資料契約 v0.2（候選池草案，待全組確認）

所有檔案均為 UTF-8；股票代碼必須作為文字讀取；日期為 `YYYY-MM-DD`；時間為 ISO 8601 且含 `+08:00`。缺失值只能留空（CSV）或使用 `null`/`NaN`（Parquet），不可填 0。

## 固定候選池

### `security_master.csv`

|欄位|型別|必填|規則|
|---|---|---|---|
|universe_id|string|是|固定候選池版本，例如 `contest-2026-07-31`|
|ticker|string|是|4–6 位數字字串，保留前導零|
|name|string|是|公司名稱|
|market|enum|是|`TWSE` 或 `TPEX`|
|industry|string|是|官方產業分類；未知時不可杜撰|
|security_type|enum|是|目前只允許 `COMMON_STOCK`|
|listing_date|date|否|上市／上櫃日期|
|is_eligible|boolean|是|正式候選池必為 `true`|
|max_weight|decimal|是（正式檔）|2330 為 `0.25`，其餘個股為 `0.10`；示範檔可留空但不得用於正式產出|
|source|string|是|主辦方或官方資料來源識別|
|source_reference|string|否|來源檔名、URL或文件識別|
|source_as_of_date|date|是|來源資料基準日|
|updated_at|datetime|是|資料更新時間，Asia/Taipei|

主鍵為 `(universe_id, ticker)`。`security_master` 原規格缺少資料來源、基準日、上市日與證券類型；本專案將其加入，以免資格判斷不可稽核。本文件是候選池草案，須由共同資料欄位維護者確認後才可升為全組契約。

### `universe_audit.csv`

保留題述欄位，並新增 `universe_id`、`listing_date`、`market_cap_method`、`max_weight`、`weight_rule_reference`、`source_reference`、`retrieved_at`。主鍵為 `(universe_id, cutoff_date, ticker)`。

`market_cap_method` 只能是：

- `CLOSE_X_COMMON_SHARES`：`closing_price × shares_outstanding`
- `OFFICIAL_MARKET_CAP_REPORT`：官方市值報表

對 TPEX，若官方報表只提供市值，`closing_price` 與 `shares_outstanding` 可留空，絕不可以 0 補值。市值排名是**各市場內**的排名，以符合「上市前100、上櫃前50」的選取方式。

## 後續資料表

|檔案|主鍵|必要補充／驗證|
|---|---|---|
|`market_daily.parquet`|`(date,ticker)`|`market`、OHLC、`volume`、`turnover`、`source`、`retrieved_at`；OHLC/量價缺失應失敗，不得補 0。`turnover` 定義為新臺幣成交金額。另需保存主辦方成交用的當日成交均價／執行價，且不可用收盤價替代。|
|`quant_ranking.csv`|`(as_of_date,ticker,model_version)`|`quant_score` 在 [0,1]；每一日期選取恰好 45 檔（正式執行）。|
|`company_aliases.csv`|`(ticker,alias,alias_type)`|一個別名一列；不可重複；別名不可為空。|
|`news_articles.parquet`|`(article_id,ticker)`|`published_at`、URL、來源、比對信心與去重雜湊必須可追溯。|
|`news_scores.csv`|`(as_of_date,ticker,model_version)`|`news_score` 在 [-1,1]；`event_score`、`confidence` 在 [0,1]；`has_news=false` 時 `article_count=0` 且分數為 0。|

## 已釐清的定義問題

- `security_master` 的簡版欄位不足以證明「固定」、「普通股」及來源，已擴充。
- `universe_audit` 原本未記錄權重規則與市值來源／方法，無法重現選股，已擴充。
- TPEx 官方市值與「收盤價 × 普通股數」可能採不同揭露口徑，必須在 `market_cap_method` 揭露，禁止混算。
- 依競賽簡章：2330 權重上限為 25%，其餘個股為 10%；每日持股 20–30 檔；現金必須嚴格小於 NAV 的25%；Active Share 不得低於20%達連續2個交易日。詳細落地規則見 `competition_design_principles.md`。
