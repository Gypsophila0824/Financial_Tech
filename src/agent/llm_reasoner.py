"""LLMReasoner：由 LLM 把程式的範本推論改寫成分析說明（移植自同隊組員的 narrate.py）。

分工：買賣哪幾檔、權重、股數、posture 一律由程式決定；LLM 只把事實與程式判斷依據改寫成說明，
不可改變任何決策（六(十四)(6)：決策與交易行為須一致）。

每段文字都經程式檢查，不通過的改用範本文字：
  - 數字：文中每個數字都須出現在提供給 LLM 的事實或程式判斷依據中（允許四捨五入到較少位數）
  - 動作：須寫出該動作的關鍵詞（買進／加碼／減碼／出清／不交易）
  - 用詞：不得自行引入本 ETF 未使用的因子或做法（FORBIDDEN，與策略說明文件產生器共用）；轉述事實中已有的字詞可以
  - 長度：logic 20～1000 字、counter_evidence ≤ 500 字；範本有反面證據時 LLM 也須寫出
LLM 無法使用時全部用範本（Slack 答覆：規則式 Agent 亦符合規定），D-Plan 照常產出——不可因文字品質漏交。

可重現：結果以「PROMPT_VERSION＋模型＋該項輸入」的雜湊為鍵存於 data/narratives/cache.jsonl，
同一份輸入重跑直接沿用、不重新呼叫 LLM（避免「重跑多次挑一份」）。改 prompt 或檢查規則＝改版，須換 PROMPT_VERSION。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import strategy as S
from .llm import LLMError, LLMUnavailable
from .models import InferenceDraft
from .reasoner import StockItem

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "data" / "narratives" / "cache.jsonl"
TPE = timezone(timedelta(hours=8))

PROMPT_VERSION = "narrate-v2"          # v2：事實中已有的字詞（如新聞提到 EPS）可轉述
BATCH = 8
LOGIC_MIN, LOGIC_MAX, COUNTER_MAX = 20, 1000, 500
# 本 ETF 未使用的因子或做法（strategy_doc.py 共用）：寫進文件或推論會與實際做法對不上
FORBIDDEN = ["本益比", "PER", "股價淨值比", "PBR", "EV/EBITDA", "估值", "低估", "價值型", "殖利率", "股息", "股利",
             "現金流", "EPS", "毛利率", "ROA", "負債", "產業分散", "分散產業", "融資", "融券", "Black-Litterman",
             "機器學習", "深度學習", "神經網路"]
ACTION_WORDS = {"BUY": ["買進", "建倉", "納入"], "ADD": ["加碼"], "TRIM": ["減碼"], "SELL_ALL": ["出清", "賣出"],
                "NO_TRADE": ["不交易", "維持", "續抱"]}
ACTION_ZH = {"BUY": "新建倉（買進）", "ADD": "加碼", "TRIM": "減碼", "SELL_ALL": "出清", "NO_TRADE": "不交易（維持持股）"}
NUMBER = re.compile(r"\d+(?:\.\d+)?")

SYSTEM = f"""你是一檔台股主動式 ETF 的 AI Agent 基金經理人。本 ETF 為因子投資型、不限產業：每日以量價動能 {S.SCORE_WEIGHTS['pv_score']:.0%}、
基本面 {S.SCORE_WEIGHTS['fund_score']:.0%}（營收成長 {S.FUND_SCORE_WEIGHTS['B1_rank']:.0%}、營收加速 {S.FUND_SCORE_WEIGHTS['B3_rank']:.0%}）、
新聞事件 {S.SCORE_WEIGHTS['news_score']:.0%}（以重大利空為主）、上市市值前 20 名 {S.SCORE_WEIGHTS['market_cap_score']:.0%}（其餘中性）為 150 檔打分，選出前 {S.N_HOLDINGS} 檔，
權重與（總分 − 0.5）÷ 60 日波動率成正比；已持有者仍在前 {S.BUFFER_RANK} 名就續抱，
權重與目標差距在 {S.NO_TRADE_BAND:.0%} 以內不交易。
今天的買賣決策、目標權重與股數已由程式依公式決定，你的工作是為每一項決策撰寫 D-Plan 的推論說明。

規則：
- 不可改變或質疑決策本身；說明必須支持給定的動作，並明確寫出該動作（買進／加碼／減碼／出清／不交易）。
- 只能使用提供的事實與程式判斷依據中的數字，可以四捨五入到較少位數，不可自行計算新數字或引用外部資訊。
- 說明要解讀事實：哪些因子是主要理由（量價動能、法人籌碼、營收成長與加速、獲利能力），與本 ETF「動能＋營收成長」的主軸如何對應。
- 反面證據（counter_evidence）寫出這項決策的主要風險或不利事實（例如較弱的因子、接近名次邊緣、高波動、新聞利空）；
  若程式判斷依據已有反面證據，必須涵蓋；確實沒有時填空字串。
- 不可提及本 ETF 未使用的因子或做法：估值、本益比、股價淨值比、股息、現金流量、負債、產業分散等。
- 繁體中文，語氣正式、具體；logic 80～400 字，counter_evidence 150 字以內。"""

ITEMS_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {"ticker": {"type": "string"}, "logic": {"type": "string"}, "counter_evidence": {"type": "string"}},
        "required": ["ticker", "logic", "counter_evidence"], "additionalProperties": False}}},
    "required": ["items"], "additionalProperties": False,
}
MARKET_SCHEMA = {"type": "object", "properties": {"logic": {"type": "string"}}, "required": ["logic"],
                 "additionalProperties": False}


def numbers_ok(text: str, source: str) -> list[str]:
    """text 中每個數字都須等於 source 中某個數字，或其四捨五入到較少位數的結果；回傳不符的數字"""
    pool = [(float(s), len(s.split(".")[1]) if "." in s else 0) for s in NUMBER.findall(source)]
    bad = []
    for s in NUMBER.findall(text):
        d = len(s.split(".")[1]) if "." in s else 0
        w = float(s)
        if not any(d <= dv and abs(round(v, d) - w) < 1e-9 for v, dv in pool):
            bad.append(s)
    return bad


def item_prompt(it: StockItem) -> dict:
    return {"ticker": it.ticker, "name": it.name, "action": ACTION_ZH[it.action], "facts": list(it.facts.values()),
            "program_logic": it.logic, "program_counter_evidence": it.counter or ""}


def item_source(it: StockItem) -> str:
    return "\n".join([SYSTEM, it.ticker, it.name, *it.facts.values(), it.logic, it.counter or ""])


def introduced(text: str, given: str) -> list[str]:
    """text 自行引入、但提供的事實與程式依據（given，不含 SYSTEM）中沒有的未使用因子或做法。
    轉述事實可以（例如新聞寫到財報 EPS），自行拿來當判斷依據不行。"""
    return [w for w in FORBIDDEN if w.lower() in text.lower() and w.lower() not in given.lower()]


def check_item(it: StockItem, out: dict) -> list[str]:
    errors = []
    logic, counter = out["logic"].strip(), out["counter_evidence"].strip()
    if not LOGIC_MIN <= len(logic) <= LOGIC_MAX:
        errors.append(f"logic {len(logic)} 字，須介於 {LOGIC_MIN}～{LOGIC_MAX} 字")
    if len(counter) > COUNTER_MAX:
        errors.append(f"counter_evidence {len(counter)} 字，超過 {COUNTER_MAX} 字")
    if it.counter and not counter:
        errors.append("程式判斷依據有反面證據，counter_evidence 不可為空")
    if not any(w in logic for w in ACTION_WORDS[it.action]):
        errors.append(f"logic 須寫明動作（{'／'.join(ACTION_WORDS[it.action])}）")
    bad = numbers_ok(logic + "\n" + counter, item_source(it))
    if bad:
        errors.append(f"出現事實中沒有的數字：{'、'.join(bad[:5])}")
    used = introduced(logic + counter, "\n".join([*it.facts.values(), it.logic, it.counter or ""]))
    if used:
        errors.append(f"出現本 ETF 未使用的因子或做法：{'、'.join(used)}")
    return errors


class LLMReasoner:
    """llm：llm.AnthropicLLM（或測試用 FakeLLM），需提供 json(system, user, schema) 與 model_version。"""

    def __init__(self, llm, trade_date: str | None = None, cache_path: Path = CACHE):
        self.llm = llm
        self.is_mock = getattr(llm, "is_mock", False)
        self.trade_date = trade_date
        self.cache_path = cache_path
        self.fallback: list[str] = []
        self.warnings: list[str] = []
        self.used = 0
        self.unavailable = False

    # ---- 寫進 agent_metadata：如實填寫實際用到的模型 ----
    @property
    def model_provider(self) -> str:
        return "anthropic" if self.used else "other"

    @property
    def model_version(self) -> str:
        return f"rule-based+{self.llm.model_version}"[:60] if self.used else "rule-based-template-v1"

    @property
    def input_tokens(self):
        return getattr(self.llm, "input_tokens", 0)

    @property
    def output_tokens(self):
        return getattr(self.llm, "output_tokens", 0)

    @property
    def calls(self):
        return getattr(self.llm, "calls", 0)

    # ---- 快取 ----
    def _key(self, payload: dict) -> str:
        model = getattr(self.llm, "model", getattr(self.llm, "model_version", ""))
        raw = json.dumps({"v": PROMPT_VERSION, "model": model, **payload}, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def _load_cache(self) -> dict:
        if not self.cache_path.exists():
            return {}
        with open(self.cache_path, encoding="utf-8") as f:
            return {r["key"]: r for r in (json.loads(line) for line in f if line.strip())}

    def _save(self, rows: list[dict]) -> None:
        if rows:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.cache_path, "a", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def _ask(self, schema: dict, user: str) -> dict | None:
        if self.unavailable:
            return None
        try:
            return self.llm.json(SYSTEM, user, schema)
        except LLMUnavailable as exc:
            self.unavailable = True
            self.warnings.append(f"LLM 無法使用（{exc}），推論文字使用範本")
        except LLMError as exc:
            self.warnings.append(f"LLM 撰寫失敗：{exc}")
        return None

    # ---- 個股推論 ----
    def stocks(self, items: list[StockItem]) -> dict[str, InferenceDraft]:
        cache = self._load_cache()
        keys = {it.key: self._key({"trade_date": self.trade_date, **item_prompt(it)}) for it in items}
        written = {it.key: cache[keys[it.key]] for it in items if keys[it.key] in cache}
        todo = [it for it in items if it.key not in written]
        now = datetime.now(TPE).replace(microsecond=0).isoformat()
        rows = []
        for i in range(0, len(todo), BATCH):
            batch = todo[i:i + BATCH]
            user = ("請為以下每一項決策撰寫推論說明，items 依相同順序、使用相同 ticker 輸出：\n"
                    + json.dumps([item_prompt(x) for x in batch], ensure_ascii=False, indent=1))
            data = self._ask(ITEMS_SCHEMA, user)
            outs = {o["ticker"]: o for o in data["items"]} if data else {}
            for it in batch:
                out = outs.get(it.ticker)
                if not data:
                    continue
                errors = check_item(it, out) if out else ["未產出"]
                if errors:
                    self.fallback.append(f"{it.ticker}：{'；'.join(errors)}")
                    continue
                row = {"key": keys[it.key], "trade_date": self.trade_date, "ticker": it.ticker,
                       "logic": out["logic"].strip(), "counter_evidence": out["counter_evidence"].strip(),
                       "prompt_version": PROMPT_VERSION, "created_at": now}
                written[it.key] = row
                rows.append(row)
        self._save(rows)
        self.used += len(written)
        result = {}
        for it in items:
            w = written.get(it.key)
            logic = w["logic"] if w else it.logic
            counter = (w["counter_evidence"] or None) if w else it.counter
            result[it.key] = InferenceDraft(key=it.key, premise_keys=list(it.facts), logic=logic[:1000],
                                            conclusion=None, counter_evidence=counter[:500] if counter else None)
        return result

    # ---- 市場觀點 ----
    def market_view(self, facts: dict[str, str], logic: str, counter: str | None) -> tuple[str, str | None]:
        payload = {"trade_date": self.trade_date, "market": {"facts": list(facts.values()), "program_logic": logic}}
        key = self._key(payload)
        cache = self._load_cache()
        if key in cache:
            self.used += 1
            return cache[key]["logic"], counter
        data = self._ask(MARKET_SCHEMA,
                         "請撰寫今日 market_view 的說明（logic，80～400 字）：解讀市場寬度與漲跌，"
                         "並說明今日淨曝險意圖、操作姿態與估算現金比的由來。不可改變程式已決定的 regime、stance、"
                         "posture。\n" + json.dumps(payload["market"], ensure_ascii=False, indent=1))
        if not data:
            return logic, counter
        text = data["logic"].strip()
        bad = numbers_ok(text, "\n".join([SYSTEM, *facts.values(), logic]))
        used = introduced(text, "\n".join([*facts.values(), logic]))
        if not LOGIC_MIN <= len(text) <= LOGIC_MAX or bad or used:
            self.fallback.append(f"market_view：數字 {bad}、用詞 {used}、長度 {len(text)}")
            return logic, counter
        self._save([{"key": key, "trade_date": self.trade_date, "ticker": "market_view", "logic": text,
                     "prompt_version": PROMPT_VERSION,
                     "created_at": datetime.now(TPE).replace(microsecond=0).isoformat()}])
        self.used += 1
        return text, counter
