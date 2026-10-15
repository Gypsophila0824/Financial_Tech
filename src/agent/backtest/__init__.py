"""正式策略的歷史回測。

此套件只重播既有正式選股、配權與交易邏輯；不建立 D-Plan、不寫入正式帳本，
也不會改變 ``strategy.py`` 的策略參數。請由 ``backtest.runner`` 匯入或以
``python -m src.agent.backtest.runner`` 執行，避免套件初始化時先載入入口模組。
"""
