"""新聞分數（以重大利空為主），移植自同隊組員的 score_news.py 與 archive_news.py。

  新聞來源  data/news/items.jsonl（datafeed.news 累積：公開資訊觀測站重大訊息＋鉅亨網，以「(代號-TW)」對應股票，
            不以公司名稱比對，避免簡稱誤配）
  分類      Claude Haiku 5.5，逐則判斷對每檔相關股票的 relevance／sentiment／materiality／event_type／reason；
            存 data/news/labels.jsonl（鍵＝新聞 id＋PROMPT_VERSION），重跑一律沿用、不重新呼叫 LLM。
  計分      只計 relevance=primary（重大訊息一律視為 primary）：0.5 − 最嚴重利空扣分 ＋ 最好利多加分（限 0–1）；
            扣分 high 0.5、medium 0.25；加分 high 0.15、medium 0.075；low 不計；沒有新聞＝0.5 中性。
  摘要      Claude Sonnet 5.5，為有 medium 以上新聞的股票寫一句摘要，存 data/news/summaries.jsonl 沿用。
  存檔      可能引用的鉅亨網新聞送 Wayback Machine，存 data/news/archive.jsonl（填 D-Plan 的 archive_url）。

防偷看未來：只採用「發布時間 ≤ 截止時間」的新聞（正式＝執行當下；彩排＝交易日 05:30），回看 3 天。
LLM 無法使用時：未判斷的新聞視為沒有新聞（中性），不中斷每日流程（Slack 答覆：規則式 Agent 亦合規）。
"""

from __future__ import annotations

import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import truststore

from .. import strategy as S
from ..llm import LLMError, LLMUnavailable

TPE = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parents[3]
NEWS_DIR = ROOT / "data" / "news"
ITEMS, LABELS, SUMMARIES, ARCHIVE = (NEWS_DIR / n for n in
                                     ("items.jsonl", "labels.jsonl", "summaries.jsonl", "archive.jsonl"))

# 2026-10-13 同一批 40 則新聞比較：Haiku 4.5 會把剛立案的 337 調查、例行背書保證判為重大利空（觸發否決出清），
# 也會把公關稿順帶提及判為主角；Haiku 5.5 依 prompt 分級判讀正確。effort low 與預設品質相近、輸出少三分之一。
CLASSIFY_MODEL = "claude-haiku-5-5"
CLASSIFY_EFFORT = "low"
SUMMARY_MODEL = "claude-sonnet-5-5"
PROMPT_VERSION = "classify-v3"          # v1 組員 Haiku 4.5；v2 Haiku 5.5；v3 代號限定為指定股票（修正判讀被丟棄）
SUMMARY_PROMPT_VERSION = "summary-v1"
WORKERS = 4
PENALTY = {"high": 0.5, "medium": 0.25}
BONUS = {"high": 0.15, "medium": 0.075}
EVENT_TYPES = ["財報獲利", "營收", "訂單客戶", "產能營運", "法律監管", "財務資本", "人事治理", "產業題材", "股價市場", "其他"]

CLASSIFY_SYSTEM = """你是台股研究員，負責判斷一則新聞或公司公告對「指定股票」的影響，供量化選股模型使用。

對每一檔指定股票，判斷：
1. relevance：primary＝這則內容主要在講這家公司；mention＝只是順帶提及（盤勢總結、類股列舉、同業比較）。
2. sentiment：對這家公司的基本面或股價是 positive（利多）、negative（利空）或 neutral（中性）。
3. materiality：影響程度。
   high（重大）——可能實質改變公司獲利或風險的事件，例如：
     利空：財報虧損或獲利大幅衰退、下修財測、重要客戶流失或大幅砍單、停工停產或重大災害、
           掏空弊案或檢調搜索、違約或債務危機、股票遭處置或變更交易方法、重大訴訟敗訴、
           會計師出具非無保留意見、大股東或董監大量申報轉讓持股。
     利多：獲利大幅優於預期、上修財測、取得重大訂單或關鍵新客戶、重大併購、大幅擴產且已有客戶承諾。
   medium（中度）——有影響但不致改變公司前景，例如：月營收年減或月減明顯、毛利率下滑、
     多家法人下修目標價、主要主管非例行離職；或相對應的一般利多。
   low（輕微）——例行或資訊量低的內容，例如：面額變更、更名、股東會與董事會例行決議、
     代理發言人等例行人事、取得或處分一般資產、參展與合作意向；
     以及只描述股價漲跌、成交量、法人買賣超、技術線型的報導（這些已由量價因子反映，不重複計入）。
4. event_type：從給定選項中選最接近的一項。
5. reason：一句話說明判斷依據，引用內容中的具體事實或數字，50 字以內，使用繁體中文。

原則：
- 只根據內容本身判斷，不要加入內容沒有提到的推測。
- 預期、傳聞、分析師個人看法，重要程度比已發生的事實低一級。
- 不確定時選較低的重要程度。
- 每一檔指定股票都要輸出一筆，代號必須與指定的完全相同。"""

CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {"labels": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "ticker": {"type": "string"},
            "relevance": {"type": "string", "enum": ["primary", "mention"]},
            "sentiment": {"type": "string", "enum": ["positive", "negative", "neutral"]},
            "materiality": {"type": "string", "enum": ["high", "medium", "low"]},
            "event_type": {"type": "string", "enum": EVENT_TYPES},
            "reason": {"type": "string"},
        },
        "required": ["ticker", "relevance", "sentiment", "materiality", "event_type", "reason"],
        "additionalProperties": False}}},
    "required": ["labels"], "additionalProperties": False,
}

def classify_schema(tickers: list[str]) -> dict:
    """ticker 限定為這則新聞的指定代號（結構化輸出強制，避免寫成「代號 名稱」）"""
    schema = json.loads(json.dumps(CLASSIFY_SCHEMA))
    schema["properties"]["labels"]["items"]["properties"]["ticker"] = {"type": "string", "enum": list(tickers)}
    return schema


SUMMARY_SYSTEM = """你是台股基金的研究員，為每日投資決策報告撰寫個股新聞摘要。
根據提供的新聞判斷結果，用一到兩句繁體中文（100 字以內）總結這檔股票近期的重要消息及其對投資判斷的意義。
只陳述提供內容中的事實與判斷，不加入新的資訊或預測；有利空時先寫利空。"""
SUMMARY_SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"],
                  "additionalProperties": False}

AVAILABLE = "https://archive.org/wayback/available?url={url}"
SAVE = "https://web.archive.org/save/{url}"
SNAPSHOT = re.compile(r"^https://web\.archive\.org/web/\d{14}/")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def append_jsonl(path: Path, rows: list[dict]) -> None:
    if rows:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")


class NewsScorer:
    is_mock = False

    def __init__(self, classify_llm=None, summary_llm=None, live: bool = True):
        """classify_llm／summary_llm：llm.AnthropicLLM（None 表示不呼叫 LLM，未判斷的新聞視為中性）。"""
        self.classify_llm, self.summary_llm, self.live = classify_llm, summary_llm, live
        self.warnings: list[str] = []
        self.models_used: set[str] = set()

    def cutoff(self, trade_date: str) -> datetime:
        if self.live:
            return datetime.now(TPE).replace(microsecond=0)
        return datetime.fromisoformat(f"{trade_date}T05:30:00+08:00")

    def _llm(self, llm, system, user, schema, label):
        try:
            out = llm.json(system, user, schema, max_tokens=4096)
            self.models_used.add(llm.model_version)
            return out
        except LLMUnavailable as exc:
            raise
        except LLMError as exc:
            self.warnings.append(f"{label}：{exc}")
            return None

    def _classify(self, items: list[dict], names: dict[str, str]) -> dict[str, list[dict]]:
        if not self.classify_llm or not items:
            return {}

        def one(x):
            tickers = "、".join(f"{t} {names.get(t, '')}" for t in x["tickers"])
            kind = "公司重大訊息公告（由公司自行發布）" if x["source"] == "mops" else "財經新聞"
            user = (f"類型：{kind}\n發布時間：{x['published_at']}\n指定股票：{tickers}\n"
                    f"可選事件類型：{'、'.join(EVENT_TYPES)}\n\n標題：{x['title']}\n\n內容：\n{x['body']}")
            out = self._llm(self.classify_llm, CLASSIFY_SYSTEM, user, classify_schema(x["tickers"]), f"分類 {x['id']}")
            if out is None:
                return x["id"], None
            # 代號只取開頭數字（模型偶爾照抄「1590 亞德客-KY」），避免判讀結果被丟掉
            labs = [{**lab, "ticker": lab["ticker"].strip().split()[0] if lab["ticker"].strip() else ""}
                    for lab in out["labels"]]
            return x["id"], [lab for lab in labs if lab["ticker"] in x["tickers"]]

        done = {}
        try:
            first = one(items[0])                     # 先確認 LLM 可用，再平行處理其餘
        except LLMUnavailable as exc:
            self.warnings.append(f"LLM 無法使用（{exc}），未判斷的新聞一律視為中性")
            return {}
        results = [first]
        with ThreadPoolExecutor(WORKERS) as pool:
            try:
                results += list(pool.map(one, items[1:]))
            except LLMUnavailable as exc:
                self.warnings.append(f"LLM 無法使用（{exc}），未判斷的新聞一律視為中性")
        for i, labs in results:
            if labs is not None:
                done[i] = labs
        return done

    def signals(self, day) -> tuple[pd.DataFrame, dict[str, dict]]:
        names = day.master["name"].to_dict()
        end = self.cutoff(day.trade_date)
        start = end - timedelta(days=S.NEWS_DAYS)
        items = [x for x in read_jsonl(ITEMS) if start < datetime.fromisoformat(x["published_at"]) <= end]
        labels = {r["key"]: r for r in read_jsonl(LABELS)}
        summaries = {r["key"]: r for r in read_jsonl(SUMMARIES)}

        todo = [x for x in items if f"{x['id']}|{PROMPT_VERSION}" not in labels]
        new = self._classify(todo, names)
        now = datetime.now(TPE).replace(microsecond=0).isoformat()
        rows = [{"key": f"{i}|{PROMPT_VERSION}", "id": i, "prompt_version": PROMPT_VERSION,
                 "model": CLASSIFY_MODEL, "labeled_at": now, "labels": v} for i, v in new.items()]
        append_jsonl(LABELS, rows)
        labels.update({r["key"]: r for r in rows})
        unlabeled = sum(f"{x['id']}|{PROMPT_VERSION}" not in labels for x in items)
        if unlabeled:
            self.warnings.append(f"新聞 {unlabeled} 則未經 LLM 判斷，視為中性")

        per = {t: [] for t in names}
        for x in items:
            rec = labels.get(f"{x['id']}|{PROMPT_VERSION}")
            if rec and rec["labels"]:
                self.models_used.add(rec["model"])       # 沿用快取的判讀也如實記錄實際判讀的模型
            for lab in rec["labels"] if rec else []:
                if lab["ticker"] in per and (lab["relevance"] == "primary" or x["source"] == "mops"):
                    per[lab["ticker"]].append({**lab, "id": x["id"], "title": x["title"],
                                               "published_at": x["published_at"]})
        out = []
        for t, labs in per.items():
            neg = [l for l in labs if l["sentiment"] == "negative" and l["materiality"] in PENALTY]
            pos = [l for l in labs if l["sentiment"] == "positive" and l["materiality"] in BONUS]
            worst = max(neg, key=lambda l: PENALTY[l["materiality"]], default=None)
            best = max(pos, key=lambda l: BONUS[l["materiality"]], default=None)
            score = 0.5 - (PENALTY[worst["materiality"]] if worst else 0) + (BONUS[best["materiality"]] if best else 0)
            evidence = sorted(neg + pos, key=lambda l: (l["sentiment"] != "negative", l["published_at"]))
            out.append({"ticker": t, "news_items": len(labs), "news_score": round(min(max(score, 0), 1), 4),
                        "worst_event": f"{worst['materiality']}:{worst['event_type']}:{worst['reason']}" if worst else "",
                        "best_event": f"{best['materiality']}:{best['event_type']}:{best['reason']}" if best else "",
                        "evidence_ids": " ".join(dict.fromkeys(l["id"] for l in evidence)), "summary": "",
                        "_evidence": evidence})

        # 摘要（只寫有 medium 以上新聞的股票）
        key = lambda r: f"{r['ticker']}|{r['evidence_ids']}|{SUMMARY_PROMPT_VERSION}"  # noqa: E731
        new_rows = []
        for r in out:
            if not r["_evidence"] or key(r) in summaries or not self.summary_llm:
                continue
            lines = "\n".join(f"- [{e['id']}] {e['published_at'][:10]} {e['sentiment']}／{e['materiality']}："
                              f"{e['title']}（{e['reason']}）" for e in r["_evidence"])
            try:
                res = self._llm(self.summary_llm, SUMMARY_SYSTEM, f"股票：{r['ticker']} {names[r['ticker']]}\n"
                                f"近期重要新聞判斷：\n{lines}", SUMMARY_SCHEMA, f"摘要 {r['ticker']}")
            except LLMUnavailable as exc:
                self.warnings.append(f"摘要 LLM 無法使用（{exc}）")
                break
            if res:
                new_rows.append({"key": key(r), "model": SUMMARY_MODEL, "prompt_version": SUMMARY_PROMPT_VERSION,
                                 "written_at": now, "summary": res["summary"].strip()})
        append_jsonl(SUMMARIES, new_rows)
        summaries.update({r["key"]: r for r in new_rows})
        for r in out:
            s = summaries.get(key(r), {})
            r["summary"] = s.get("summary", "")
            if r["summary"]:
                self.models_used.add(s["model"])
            del r["_evidence"]

        archived = {r["url"]: r["archive_url"] for r in read_jsonl(ARCHIVE)}
        news_items = {x["id"]: {k: x[k] for k in ("source", "authority", "published_at", "title", "url")}
                      | ({"archive_url": archived[x["url"]]} if x["url"] in archived else {}) for x in items}
        day.notes.extend(self.warnings)
        if not self.live:
            day.notes.append("彩排：新聞僅含 data/news/items.jsonl 已累積者，可能不完整")
        return pd.DataFrame(out).set_index("ticker"), news_items

    # ---------------------------------------------------------------- Wayback 存檔
    def archive(self, day, tickers: list[str], budget: int = 600) -> None:
        """可能引用的鉅亨網新聞送 Wayback 存檔（先查既有存檔，沒有才送存；429 立即停止）；只警告不中斷。"""
        if day.news is None:
            return
        started = time.time()
        urls = []
        for t in dict.fromkeys(tickers):
            if t not in day.news.index:
                continue
            ids = [i for i in str(day.news.loc[t, "evidence_ids"] or "").split() if i in day.news_items]
            urls += [day.news_items[i]["url"] for i in ids[:S.NEWS_PER_TICKER]
                     if day.news_items[i]["authority"] == "media" and "archive_url" not in day.news_items[i]]
        ua = {"User-Agent": "Mozilla/5.0"}
        # 用 Windows 憑證庫驗證（防毒軟體檢查加密連線時，Python 內建憑證清單會判定 archive.org 憑證無效）
        ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        for url in dict.fromkeys(urls):
            if time.time() - started + 45 > budget:
                day.notes.append(f"Wayback 存檔時間預算 {budget} 秒用完，其餘下次再存")
                break
            try:
                with urllib.request.urlopen(urllib.request.Request(
                        AVAILABLE.format(url=urllib.parse.quote(url, safe="")), headers=ua), timeout=30,
                        context=ctx) as r:
                    closest = json.loads(r.read()).get("archived_snapshots", {}).get("closest") or {}
                snap = (closest.get("url") or "").replace("http://", "https://", 1)
                method = "existing"
                if not (closest.get("available") and SNAPSHOT.match(snap)):
                    with urllib.request.urlopen(urllib.request.Request(SAVE.format(url=url), headers=ua),
                                                timeout=120, context=ctx) as r:
                        snap, method = r.geturl(), "saved"
                    if not SNAPSHOT.match(snap):
                        raise ValueError(f"回傳網址不是存檔頁：{snap}")
                    time.sleep(5)
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    day.notes.append("Wayback Machine 回應請求過多（429），其餘新聞下次再存")
                    break
                day.notes.append(f"新聞存檔失敗：{url}（HTTP {e.code}）")
                continue
            except Exception as e:  # noqa: BLE001 存檔為選填，失敗只警告
                day.notes.append(f"新聞存檔失敗：{url}（{type(e).__name__}）")
                continue
            append_jsonl(ARCHIVE, [{"url": url, "archive_url": snap, "method": method,
                                    "archived_at": datetime.now(TPE).replace(microsecond=0).isoformat()}])
            for x in day.news_items.values():
                if x["url"] == url:
                    x["archive_url"] = snap
