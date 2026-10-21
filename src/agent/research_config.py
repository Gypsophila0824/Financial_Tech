"""研究線專屬參數。

這些設定只供 Shadow／研究回測使用，絕不參與正式選股、D-Plan 或委託；正式
策略參數仍只存在 ``strategy.py``。任何調整都應以新版本與回測報告封存。
"""

MODEL_VERSION = "shadow-quant-mixed-fundamental-v7-85-5-10"

# 量價子因子組合；基準技術配方留存供舊實驗重現。
# 控制實驗固定目前研究線的實驗量價配方，只調整總分中量價／基本面比例。
BASELINE_TECHNICAL_WEIGHTS = {
    "momentum": 0.35,
    "trend": 0.25,
    "volume": 0.20,
    "strength": 0.10,
    "risk": 0.10,
}

# 2026-10-17：使用者授權替換研究實驗組，正式線維持。
# strength 是 20 日報酬減橫斷面中位數，百分位與 20 日動能重複；
# 降低其比重及 momentum，將各 5% 轉至 volume、risk。
# 這是待驗證假設，不代表績效已改善；兩組比較時共用此子因子與基本面／新聞資料口徑。
EXPERIMENTAL_TECHNICAL_WEIGHTS = {
    "momentum": 0.30,
    "trend": 0.25,
    "volume": 0.25,
    "strength": 0.05,
    "risk": 0.15,
}

# 研究總分配重：對照組 90/0/10；候選組 85/5/10。
BASELINE_RESEARCH_WEIGHTS = {"technical": 0.90, "fundamental": 0.00, "news": 0.10}
EXPERIMENTAL_RESEARCH_WEIGHTS = {"technical": 0.85, "fundamental": 0.05, "news": 0.10}
if any(abs(sum(weights.values()) - 1.0) > 1e-12 for weights in
       (BASELINE_RESEARCH_WEIGHTS, EXPERIMENTAL_RESEARCH_WEIGHTS)):
    raise ValueError("Research weights must sum to 100%")

# 相容既有研究使用者：代表控制實驗的實驗組量價權重。
TECHNICAL_WEIGHTS = EXPERIMENTAL_TECHNICAL_WEIGHTS
MOMENTUM_WEIGHTS = {"5d": 0.20, "20d": 0.40, "60d": 0.40}
VOLUME_WEIGHTS = {"ratio_20d": 0.60, "turnover_20d": 0.40}

# 營收衰退且獲利指標非正的品質閘門，對照與候選組共用。
REVENUE_BLOCK_THRESHOLD = -10.0
PROFITABILITY_BLOCK_THRESHOLD = 0.0

# 研究投組專屬續抱門檻；45 檔訊號控制實驗仍維持直接排名。
# 2026-10-17 使用者澄清：只改研究線，正式線 BUFFER_RANK 維持 45。
PORTFOLIO_BUFFER_RANK = 55
