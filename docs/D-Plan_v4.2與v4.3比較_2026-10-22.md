# D-Plan v4.2 與 v4.3 比較及更新狀態

核對日期：2026-10-22。比較依據為本專案保存的 v4.2 指南／schema，與當日自官方網站下載的 v4.3 原檔；舊文件及既有執行封存不覆寫。

## 主要結論

v4.3 最重要的是以官方 DataPack 的結算持股、現金及可交易淨值作為次日決策基準，取代舊指南「系統不提供、自行記帳」的描述。股票股數推導公式未變；不是將整個策略改寫。原始 schema 有 34 處鍵值差異，其中 7 個約束路徑改動可歸納為 5 項規則變更，其餘為說明文字修訂。

本次已更新新產出的版本號、當前 schema 驗證入口、筆數限制、程式封存所含 schema，以及專案說明與操作手冊。DataPack 原生匯入、官方排除成交及整批作廢的完整接帳仍未實作，已列入正式核准 pending。不能把 schema 升級當成正式平台驗收完成。

## 新舊逐項比較

| 項目 | 舊版 v4.2 | 新版 v4.3 | 本次處理／影響 |
|---|---|---|---|
| D-Plan schema_version | schema 固定 const="4.2" | 字串須符合 ^4\\.\\d+$；指南範例填 4.3 | 新產出填 4.3；新版 schema 仍可結構上接受 4.2，不代表平台已驗收某份舊檔 |
| decisions 上限 | 30 | 60 | rules.LAYER_LIMITS 已改為 60 |
| orders 上限 | 30 | 60 | 已改為 60；可容納先出清舊組合再建立新組合。持股仍只能 20–30 檔，不能誤讀為可持有 60 檔 |
| 每筆 observation 的 source_ref 上限 | 5 | 10 | 由新版官方 schema 驗證 |
| 每筆 decision 的 inference_refs 上限 | 5 | 8 | 由新版官方 schema 驗證 |
| 前日淨值與持股 | 指南稱競賽系統不提供、隊伍自行記帳；同時提醒以後台庫存為準 | 到「文件繳交」下載 DataPack，依系統結算結果決策 | 文件已更新；現有本地推估結算不能冒充官方帳戶匯入 |
| target_weight 分母 | 舊 schema 描述較籠統，指南公式用前日淨值 | 明指 DataPack 的 account.nav_tradable | 不能改用含應收權益的 aum |
| 委託成交與超賣 | 舊指南已提醒「委託未必全部成交、持股以系統為準」，也寫超賣整份拒收 | 保留未必全成交與禁止超賣，改強調先下載 DataPack | 「未必全成交」不是新版首次新增，不應把文字整理誤當新交易規則 |
| hold 容差 | 指南 2% NAV；schema 稱 server 預設 2% | 相同 | 不屬新版數值變更；使用者另提供的官方最新問答確認目前為 2%，調整會由平台公告 |
| 手續費及證交稅 | 指南另列 0.1425%、0.3% 及自行記帳段落 | 自行記帳／費率段落刪除；未另給取整演算法 | 費率仍依競賽辦法；日總手續費依使用者轉述的最新官方聲明，不是本次 schema diff 推導所得 |
| 收件拒收描述 | 多處說明引用 C1/C2/C6/C9/C11/C12/C14 或「拒收」 | 多處改為一般「系統檢核／比對」描述，部分仍明寫整份退回 | 不能僅因刪了「拒收」就認定平台取消檢查或一律改 WARN；目前本地保護維持，分類仍待平台核對 |
| code_version | 指南文字稱格式不限 | 指南明列限英數及 _ . : / -、長度 1–64 | 舊 schema 已有這些約束，是指南與 schema 對齊，不是新增字元限制；pkg:雜湊仍符合 |
| model_provider | 指南強調自報、非白名單 | 刪除該強調文字，保留供應商清單 | schema enum 未改，無新增供應商或解除原結構限制 |
| 正反例 | 2026-10-27 | 2026-10-28 | 新版範例已保存；兩份皆通過 JSON schema，反例的問題須由語意檢查識別，不能只跑 schema |
| ETF 策略文件時程 | 指南寫 10/21～10/26 | 移除具體日期，保留每隊一份 | 未提供新期限，不能解讀為取消期限；仍依競賽辦法與平台公告 |

其餘持股 20–30、現金 0–25%、台積電 25%／其餘 10%、整張股數推導、引用鏈、持股覆蓋與資金調度 funding_for 的核心要求未因本次文件修改而放寬。

## DataPack：新取得的獨立規格

專案原先沒有保存舊 DataPack 規格，因此以下是新納入的契約整理，不冒稱為 DataPack 新舊版差異。

1. trade_date 是接下來要提交 D-Plan 的交易日；as_of_date 才是帳戶已結算到的日期。通常後者為前一交易日，不可直接互換。
2. 首日 as_of_date=null、account 為真實初始資產、holdings=[]；若 missing 含 prev_settlement，account、holdings 都為 null。null 不能當零持股或重置 10 億元。
3. account.cash 是可交易現金；account.nav_tradable 是現金與可交易持股收盤市值之和，作為權重與現金比率分母。aum 另含應收配息、應收配股價值，不能代入上述分母。
4. receivable_cash、receivable_shares 賽期內不可投入交易；holdings_count 只計 shares>0，不能直接用 holdings 陣列長度。
5. prev_day_orders.outcome 分 none、unknown、voided、executed。executed 仍可能有 excluded_codes，代表個別排除，不是全部委託成交；voided 需保留 void_reasons。這是接帳必須處理的結果，不是人工選擇交易。
6. 缺 prev_close 時部分檢查會 skipped，cash_ratio_tradable 為 null；missing 非空不能假裝資料齊備。個別持股 close_price=null 時官方估值以 0 計入，不能用本地舊價冒充官方值。
7. violations 是違規「天數」，notices 是提示「列數」，不能把同一天多項 fail 直接當成多次警告。
8. 規格書檔名 v4.3 與 DataPack 內 schema_version="3.0" 是不同版號領域，不能把官方 DataPack 改成 4.3。

## 現金差額的解釋範圍

本次 10/22 的 25 筆 BUY 股數與官方持股吻合，DataPack outcome=executed、excluded_codes=[]、missing=[]，應收現金及配股皆為 0。新版文件說明 DataPack 為決策基準，但不會自行消除本地推算與官方現金的差額。

按已更新的日總手續費暫捨去至元，專案推算現金 77,183,601.80 元，官方為 77,309,266.08 元，差 125,664.28 元。指南與 schema 沒有公布足以重建這筆差額的成交價精度／費稅取整細節；不能由「已升級 v4.3」宣稱已對帳。詳見同日 output/validation/acceptance/cash_audit_2026-10-22_233850/README.md。

## 已更新與仍待完成

- 已更新：SCHEMA_VERSION=4.3、官方 schema 驗證路徑、60 筆限制、codeversion 封存新版 schema；來源與舊版均保留。
- 已更新：CLAUDE.md、官方文件索引、值班手冊、每日流程與重現說明；新增可重查的逐行 diff、JSON 約束差異與來源雜湊。
- 未完成：DataPack 原生匯入、完整日期／missing／帳戶恆等式驗證、官方部分排除與作廢接帳、應收權益接帳，以及官方結果與本地推算差異的受控記錄流程。正式 gate 維持關閉。
- 未改動：正式策略參數、正式帳本、過往 D-Plan、既有回測與程式封存。使用舊版重現時須還原當時封存程式與 v4.2 schema。

## 原始差異與來源

本次驗證：schema 邊界、新版產出、程式封存、每日流程、合規、提交結算、費用及回測相關測試共 82 passed、1 skipped；官方 5 份下載原檔 SHA256 相符，新增索引與比較報告的本地連結無缺檔。這些是本地工程驗證，並非官方平台驗收。

- [指南逐行差異](規格比較_v4.2_v4.3/guide.diff)
- [schema 逐行差異](規格比較_v4.2_v4.3/schema.diff)
- [完整 schema 鍵值差異](規格比較_v4.2_v4.3/schema_changes.json)
- [純結構約束差異](規格比較_v4.2_v4.3/schema_constraint_changes.json)
- [來源 SHA256](規格比較_v4.2_v4.3/sources_sha256.json)
- [官方檔案與下載清單](../比賽文件/官方更新_v4.3_2026-10-22/)

官方下載：[指南](https://esun-ai-challenge.tw/api/events/fundaiagent/attachments/28/download/)、[schema](https://esun-ai-challenge.tw/api/events/fundaiagent/attachments/27/download/)、[DataPack 規格](https://esun-ai-challenge.tw/api/events/fundaiagent/attachments/29/download/)。
