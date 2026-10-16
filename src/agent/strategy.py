"""策略參數（因子投資型、不限產業）。

來源：同隊組員的因子策略（factor_spec.md），2026-10-13 依團隊決定整合為主線策略；
數值沿用其 188 日回測後定案的設定（2026-10-04）。
這裡的每個值都會影響決策行為，改任何一個都等於改版（code_version 會自動跟著變），
且必須與「ETF 投資策略說明文件」宣告的主題、因子一致。說明文件提交後不可修改（Slack 答覆）。
"""

THEME_NAME = "因子投資型（動能＋營收成長，不限產業）"

# ---- 評分（factor_spec.md 第 1～3 節）----
SCORE_WEIGHTS = {"pv_score": 0.55, "fund_score": 0.35, "news_score": 0.10}   # 量價／基本面／新聞
NEUTRAL_SCORE = 0.5            # 缺值與沒新聞的中性分；配權重時從總分扣除

# ---- 選股與權重（factor_spec.md 第 6 節、build_portfolio.py）----
N_HOLDINGS = 25
BUFFER_RANK = 45               # 換股緩衝：已持有者仍在前 45 名就續抱（回測由 35 改 45，降低周轉）
TARGET_CASH = 0.05             # 目標現金；規則 0–25%
CAP_2330 = 0.24                # 規則 25%，留 1% 緩衝（加碼超限當日即記警告）
CAP_OTHER = 0.09               # 規則 10%，留 1% 緩衝
AS_THRESHOLD = 0.30            # 目標權重的 Active Share 自訂門檻（官方 15% ＋ 15% 緩衝；ETF 持股資料比檢查日晚一天）
MAX_AS_ITERATIONS = 100
VETO_MATERIALITY = "high"      # 重大利空否決：近 3 日重大利空新聞…
VETO_TREND_MAX = 0.0           # …且收盤價低於 60 日均價（A3 < 0）→ 不買進、已持有出清（不受換股緩衝保護）

# ---- 下單與現金（generate_dplan.py）----
NO_TRADE_BAND = 0.02           # 已持有者權重偏離目標小於 2 個百分點不交易（回測由 1% 改 2%）
CASH_CAP = 0.08                # 估算現金 > 8%：解除帶內偏低持股的無交易帶、補買到目標（現金回補）
CASH_RESTORE = 0.03            # 估算現金 < 3%：解除帶內偏重持股的無交易帶、減碼到目標（現金下限回補）
POST_AS_THRESHOLD = 0.25       # 委託後預估持股的 Active Share 下限（官方 15% ＋ 10% 緩衝）
LIMIT_2330, LIMIT_OTHER = 0.245, 0.095   # 對齊整張後的權重上限；超過一定減碼（因應被動超限 5 日規則）
PRICE_BUFFER = 0.08            # 現金下限 = max(1%, 淨買進金額 × 8%)：成交均價可能高於前日收盤
CASH_FLOOR_MIN = 0.01
CASH_RANGE_HALF = 0.02         # 宣告的目標現金區間 = 估算現金 ± 2 個百分點（限制在 0–25%）

# ---- 市場研判（僅描述，不擇時）----
BREADTH_ON, BREADTH_OFF = 0.6, 0.4   # 150 檔站上 60 日均價比例 ≥ 60% risk_on、≤ 40% risk_off

# ---- 新聞（factor_spec.md 第 2 節 C）----
NEWS_DAYS = 3
NEWS_PER_TICKER = 2            # D-Plan 每檔最多引用幾則新聞（schema 上限：sources 50、observations 100）

# ---- 自我檢查的預警門檻 ----
ACTIVE_SHARE_WARN = 0.25
CASH_BUFFER_WARN = 0.01
