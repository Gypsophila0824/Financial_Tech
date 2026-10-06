# 正式150檔候選池建置流程

## 阻擋條件

在下列任一條件未滿足前，不可宣稱產出正式候選池：

1. 主辦方正式150檔清單尚未匯入或以網頁表格人工交叉確認。
2. 每一檔尚未確認為普通股。
3. 上市與上櫃的官方來源、基準日及取得時間未保存。

## 執行順序

1. 優先把主辦方 CSV/Excel 另存為 UTF-8 CSV，放入 `data/raw/organizer/`。
2. 下載或匯入 2026-07 最後交易日的 TWSE 基本資料、收盤價、普通股股數，以及 TPEx 公司市值報表，放入 `data/raw/official/`。
3. 以 `tw_stock_agent.sources` 的檔案讀取介面取得原始列；來源 URL/檔名與 `retrieved_at` 必須保留。
4. 以 `tw_stock_agent.transform` 統一欄名、交易所代碼、代碼格式與日期。
5. 以 `tw_stock_agent.builder` 計算市場內市值排名並輸出 `data/processed/security_master.csv`、`universe_audit.csv`。
6. 對正式輸出使用 `--profile production` 驗證；它要求剛好150檔、TWSE 100檔、TPEX 50檔，以及2330為25%、其餘為10%的完整權重。

## 模組責任

- `sources.py`：下載／載入官方或主辦方來源。下載 URL 必須由呼叫端明確傳入，避免把未驗證端點寫死。
- `transform.py`：欄位與型別正規化，不做商業判斷。
- `builder.py`：普通股篩選、市值計算、排名和CSV輸出。
- `validation.py`：資料契約與正式候選池阻擋規則。

範例檔只用於介面與測試，沒有任何正式名單地位。
