"""官方規則常數。

只放比賽辦法、更新公告與 D-Plan schema v4.2 明文規定的數字，註解附依據。
修改前必須先對照簡章原文；策略自己選的參數請放 strategy.py。
"""

from datetime import time

INITIAL_CAPITAL = 1_000_000_000      # 一、競賽說明：10 億台幣虛擬本金
UNIVERSE_SIZE = 150                  # 六(七)：上市前 100＋上櫃前 50 檔

MIN_HOLDINGS = 20                    # 六(八) 持股 20–30 檔
MAX_HOLDINGS = 30
TSMC = "2330"
MAX_WEIGHT_TSMC = 0.25               # 六(八) 台積電上限 25%
MAX_WEIGHT_OTHER = 0.10              # 六(八) 其餘個股上限 10%
CASH_MAX = 0.25                      # 六(九)、六(十四)(2)：現金不得為負、不得超過淨值 25%；
                                     # Slack 答覆：以每日結算後判定，恰好 25% 不違規
LOT_SIZE = 1000                      # 六(十) 僅整股交易
FEE_RATE = 0.001425                  # 六(十一) 手續費，買賣皆收
TAX_RATE = 0.003                     # 六(十一) 證交稅，賣出收取
ACTIVE_SHARE_TOP_N = 15              # 主辦方更新公告（使用者於 2026-10-16 提供）：比較前 15 大個股
ACTIVE_SHARE_MIN = 0.15              # 同公告：低於 15% 才觸發門檻；恰好 15% 不低於門檻
PASSIVE_OVERWEIGHT_GRACE_DAYS = 5    # 六(十四) 註3：價格波動超限，5 個交易日內須減碼

# 六(三) 繳交時窗：交易日的「前一日」19:30 ～ 當日 08:55。
# 前一日是日曆日：簡章範例「10/26 的繳交時間為 10/25 19:30～10/26 08:55」，10/25 為星期日。
SUBMIT_WINDOW_OPEN = time(19, 30)
SUBMIT_WINDOW_CLOSE = time(8, 55)

# D-Plan schema v4.2
SCHEMA_VERSION = "4.2"
HOLD_NET_FLOW_TOLERANCE = 0.02       # C12：hold 時 |買−賣| ≤ 2% 淨值（server 預設容差）
LAYER_LIMITS = {
    "sources": 50,
    "observations": 100,
    "inferences": 60,
    "decisions": 30,
    "no_trade_decisions": 30,
    "orders": 30,
}


def max_weight(ticker: str) -> float:
    return MAX_WEIGHT_TSMC if ticker == TSMC else MAX_WEIGHT_OTHER
