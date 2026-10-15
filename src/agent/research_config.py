"""研究線專屬參數。

這些設定只供 Shadow／研究回測使用，絕不參與正式選股、D-Plan 或委託；正式
策略參數仍只存在 ``strategy.py``。任何調整都應以新版本與回測報告封存。
"""

MODEL_VERSION = "shadow-quant-fundamental-v4-control-audit"

# 量價趨勢是研究線主排名的唯一可調整訊號組合。
# 基準組是暫停前既有量價配方；控制實驗只能變更 EXPERIMENTAL_*，其餘
# 候選池、基本面、新聞、選股、續抱與成本口徑均由 research.py 固定。
BASELINE_TECHNICAL_WEIGHTS = {
    "momentum": 0.35,
    "trend": 0.25,
    "volume": 0.20,
    "strength": 0.10,
    "risk": 0.10,
}

# 尚未經團隊批准的新量價配方時，預設與基準組相同。這會產生「零差異」的
# 控制結果，目的是驗證實驗管線，不得把它描述為新訊號優於舊訊號。
EXPERIMENTAL_TECHNICAL_WEIGHTS = BASELINE_TECHNICAL_WEIGHTS.copy()

# 相容既有研究使用者：代表控制實驗的實驗組量價權重。
TECHNICAL_WEIGHTS = EXPERIMENTAL_TECHNICAL_WEIGHTS
MOMENTUM_WEIGHTS = {"5d": 0.20, "20d": 0.40, "60d": 0.40}
VOLUME_WEIGHTS = {"ratio_20d": 0.60, "turnover_20d": 0.40}

# 基本面僅是品質閘門，不會與量價分數加權混合。
REVENUE_BLOCK_THRESHOLD = -10.0
PROFITABILITY_BLOCK_THRESHOLD = 0.0
