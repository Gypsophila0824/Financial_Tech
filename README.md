# 台股投資決策 Agent

目前範圍為固定候選池與資料契約。`data/examples/` 全部都是**非正式假資料**，不得用於競賽交易、回測或產生正式交易書。

## 快速開始

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
python -m tw_stock_agent.cli validate --table security_master --input data/examples/security_master.csv --profile example
```

正式名單建置請參閱 `docs/universe_runbook.md`。沒有主辦方名單、官方來源檔與單檔權重規則時，工具會保留待確認狀態，不會把示範資料當成正式150檔。
