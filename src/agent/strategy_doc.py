"""ETF 投資策略說明文件產生器（由 AI Agent 產製，簡章六(三)）。

  .venv/Scripts/python.exe -X utf8 -m src.agent.strategy_doc --team-id <主辦方配發>
輸出 output/strategy_doc/：填好的官方 docx 與產製紀錄 JSON（模型、時間、code_version、輸入事實）。

內容依據 strategy.py 的實際參數，確保文件描述的就是程式真正執行的策略；
初賽期間每日決策都要對得回這份文件，策略參數定案後若有修改，必須重新產生再繳交。
人工只能審核「過／退回重產」，不得修改文字（六(三) 不可人為產製）。

依官方格式與範例文件自動檢查：
  名稱：開頭為「主動」、20 字內、限中文或英文、不得與現有主動型 ETF 同名
  主題：50 字內；理念：100～300 字；不得含保證獲利等招攬用語（簡章二(十三)～(十五)）
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import docx

from . import codeversion, rules, strategy
from .llm import AnthropicLLM, LLMError
from .llm_reasoner import FORBIDDEN

TAIPEI = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "比賽文件" / "玉山挑戰賽_ETF投資策略說明文件_參賽隊伍繳交格式_v1.docx"
OUT_DIR = ROOT / "output" / "strategy_doc"
MAX_ATTEMPTS = 3

# 2026/9/11 證交所揭露之主動型 ETF（玉山挑戰賽_主動型ETF列表_v1.pdf），名稱不得與之相同
EXISTING_NAMES = {
    "主動野村臺灣優選", "主動群益台灣強棒", "主動統一台股成長", "主動中信ARK創新", "主動安聯台灣高息",
    "主動野村台灣50", "主動台新龍頭成長", "主動摩根美國科技", "主動統一全球創新", "主動復華未來50",
    "主動元大AI新經濟", "主動台新優勢成長", "主動群益科技創新", "主動第一金台股優", "主動中信台灣卓越",
    "主動安聯台灣", "主動兆豐台灣豐收", "主動國泰動能高息", "主動摩根台灣鑫收", "主動群益美國增長",
    "主動野村臺灣高息", "主動統一升級50", "主動安聯美國科技", "主動聯博動能50", "主動富邦台灣龍耀",
    "主動中信台灣收益", "主動凱基台灣", "主動第一金優股息", "主動永豐科技趨勢", "主動復華全球50",
}
BANNED = ["保證", "穩賺", "零風險", "無風險", "必賺", "包賺", "穩定獲利", "保本"]
REQUIRED = ["動能", "營收"]      # 每日推論都以這兩項為主軸，文件必須宣告，抽檢時才對得上
PHIL_MIN = 240                  # 本隊品質要求：簡章為 100～300 字，寫滿才能完整交代主題理由、因子、風控與目標
NAME_CHARS = re.compile(r"^[一-鿿A-Za-z ]+$")

SYSTEM = """你是台股主動式 ETF 的 AI 基金經理人，要為自己操作的 ETF 撰寫「ETF 投資策略說明文件」。
文件內容必須忠實描述提供的投資策略事實，不得加入事實中沒有的選股因子、投資標的或操作方式；
這份文件之後會用來檢核每日交易決策是否與宣告一致。
主題與理念要明確寫出本 ETF 的核心：價格動能與營收成長；不可提及估值、本益比、股息、現金流量、負債、產業分散等未使用的做法。
這是模擬投資競賽，不得有任何保證獲利、穩賺、零風險、保本等招攬或承諾報酬的用語。使用繁體中文。"""

SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "theme": {"type": "string"},
        "philosophy": {"type": "string"},
    },
    "required": ["name", "theme", "philosophy"],
    "additionalProperties": False,
}


def strategy_facts() -> dict:
    """直接取自 strategy.py／rules.py 的實際參數。"""
    w = {k: f"{v:.0%}" for k, v in strategy.SCORE_WEIGHTS.items()}
    return {
        "投資主題": strategy.THEME_NAME,
        "標的範圍": "主辦方公告之台股上市市值前 100 大＋上櫃市值前 50 大，共 150 檔，不限產業",
        "選股方式": (f"每個交易日為 150 檔打分：量價動能 {w['pv_score']}（近 20 日報酬、跳過最近 5 日的 60 日報酬、"
                 f"收盤價相對 60 日均價、近 10 日三大法人買賣超）、基本面 {w['fund_score']}（營收成長與營收加速各占"
                 f"{strategy.FUND_SCORE_WEIGHTS['B1_rank']:.0%}，僅使用已公布資料）、新聞事件 {w['news_score']}"
                 f"（AI 判讀近 {strategy.NEWS_DAYS} 日以該公司為主角的新聞與重大訊息，以重大利空為主）；"
                 f"取總分前 {strategy.N_HOLDINGS} 名"),
        "持股與權重": (f"持有約 {strategy.N_HOLDINGS} 檔（規則 {rules.MIN_HOLDINGS}～{rules.MAX_HOLDINGS} 檔），"
                  f"權重與（總分 − 0.5）÷ 60 日波動率成正比，總分越高、波動越低者權重越高；"
                  f"單檔上限 {strategy.CAP_OTHER:.0%}、台積電 {strategy.CAP_2330:.0%}"),
        "現金": (f"不依市場研判擇時，以約 {1 - strategy.TARGET_CASH:.0%} 股票、{strategy.TARGET_CASH:.0%} 現金為目標，"
               f"現金高於 {strategy.CASH_CAP:.0%} 補買、低於 {strategy.CASH_RESTORE:.0%} 減碼"),
        "交易紀律": (f"已持有者仍在前 {strategy.BUFFER_RANK} 名就續抱，持股權重與目標差距在 "
                 f"{strategy.NO_TRADE_BAND:.0%} 以內不調整，以降低周轉與交易成本"),
        "風險控管": ("重大利空新聞且股價跌破 60 日均價者不買進、已持有者出清；單檔權重與現金留緩衝；"
                 f"每日控制與各主動型 ETF 前十五大持股的差異（Active Share 目標 ≥ {strategy.AS_THRESHOLD:.0%}）"),
        "決策方式": "AI Agent 每個交易日依前一日收盤資料與當日開盤前已公布的資訊自主產生決策，並以結構化報告記錄完整依據",
        "選擇此主題的理由": ("股價動能反映市場資金與趨勢方向；台股公司每月公布營收，是最即時的基本面數據，"
                     "兩者結合可兼顧趨勢與成長，基本面著重營收成長與加速；不限產業，可隨景氣與產業輪動調整"),
        "對應官方選股因子分類（範例文件附錄用語）": {
            "動能／技術面": "股價動能、均線趨勢、波動率（權重依波動率調整）",
            "籌碼": "三大法人買賣超",
            "基本面": "營收成長率、營收成長加速（本策略基本面分數各占 50%）",
            "事件／資訊": "月營收、財報公告、新聞事件（AI 判讀，以重大利空為主）",
            "風險控管": "個股波動控制、下檔風險控制（重大利空且跌破 60 日均價者出清）、單檔權重上限",
        },
        "主題類型（範例文件附錄分類）": "因子投資型",
    }


def visible_len(s: str) -> int:
    return len(re.sub(r"\s", "", s))


def validate(doc: dict) -> list[str]:
    errors = []
    name, theme, phil = doc["name"].strip(), doc["theme"].strip(), doc["philosophy"].strip()
    if not name.startswith("主動"):
        errors.append("ETF 名稱開頭必須是「主動」")
    if len(name) > 20:
        errors.append(f"ETF 名稱 {len(name)} 字，超過 20 字")
    if not NAME_CHARS.match(name):
        errors.append("ETF 名稱只能使用中文或英文（不可含數字或符號）")
    if name.replace(" ", "") in {n.replace(" ", "") for n in EXISTING_NAMES}:
        errors.append("ETF 名稱與現有主動型 ETF 相同")
    if len(theme) > 50:
        errors.append(f"投資主題 {len(theme)} 字，超過 50 字")
    if not 100 <= visible_len(phil) or len(phil) > 300:
        errors.append(f"投資理念須 100～300 字（目前不含空白 {visible_len(phil)} 字、含空白 {len(phil)} 字）")
    elif visible_len(phil) < PHIL_MIN:
        errors.append(f"投資理念 {visible_len(phil)} 字，內容不夠完整（本隊要求至少 {PHIL_MIN} 字，上限仍為 300 字）")
    hits = [w for w in BANNED if w in name + theme + phil]
    if hits:
        errors.append(f"含招攬或承諾報酬用語：{hits}")
    unused = [w for w in FORBIDDEN if w.lower() in (theme + phil).lower()]
    if unused:
        errors.append(f"提及本 ETF 未使用的因子或做法：{unused}")
    missing = [w for w in REQUIRED if w not in theme + phil]
    if missing:
        errors.append(f"主題或理念須提到本 ETF 的核心因子：{missing}")
    return errors


def generate(llm) -> tuple[dict, dict]:
    facts = strategy_facts()
    user = json.dumps({"任務": "依下列投資策略事實撰寫 ETF 名稱、投資主題、投資理念",
                       "格式要求": {"ETF 名稱": "20 字內，限中文或英文，開頭須為「主動」，不得與現有主動型 ETF 同名；"
                                           "簡潔好記，能讓人看出動能與營收成長的核心",
                                "投資主題": "50 字內，一句話說明主要投資方向或核心策略；精簡有力，建議 20～40 字",
                                "投資理念": (f"{PHIL_MIN}～300 字（含標點，上限 300 字不可超過），充分利用篇幅，"
                                         "依序涵蓋：①為何選擇此主題 ②主要投資邏輯：用官方選股因子分類用語"
                                         "（動能／技術面、籌碼、基本面、事件／資訊）說明各類因子與占比 "
                                         "③風險控管做法 ④希望追求的投資目標。"
                                         "主辦方會以此檢核每日交易是否與宣告一致，請具體、可對照，不要空泛")},
                       "現有主動型 ETF 名稱（不可相同）": sorted(EXISTING_NAMES),
                       "投資策略事實": facts}, ensure_ascii=False, indent=1)
    feedback, errors = "", []
    for _ in range(MAX_ATTEMPTS):
        doc = llm.json(SYSTEM, user + feedback, SCHEMA, max_tokens=4000)
        doc = {k: v.strip() for k, v in doc.items()}
        errors = validate(doc)
        if not errors:
            return doc, facts
        feedback = "\n\n上一次的結果不符合格式要求，請修正後重新輸出：\n- " + "\n- ".join(errors)
    raise LLMError("策略說明文件未通過格式檢查：" + "；".join(errors))


def write_docx(doc: dict, path: Path) -> None:
    d = docx.Document(TEMPLATE)
    table = d.tables[0]
    for row, key in zip(table.rows, ("name", "theme", "philosophy")):
        row.cells[1].text = doc[key]
    path.parent.mkdir(parents=True, exist_ok=True)
    d.save(path)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--team-id", required=True)
    args = ap.parse_args()
    started = datetime.now(TAIPEI).replace(microsecond=0).isoformat()
    try:
        llm = AnthropicLLM()
        doc, facts = generate(llm)
    except LLMError as exc:
        sys.exit(f"失敗：{exc}")
    except Exception as exc:
        sys.exit(f"失敗：{type(exc).__name__}：{exc}")

    stamp = datetime.now(TAIPEI).strftime("%Y%m%d_%H%M%S")
    docx_path = OUT_DIR / f"ETF投資策略說明文件_{args.team_id}_{stamp}.docx"
    write_docx(doc, docx_path)
    record = {"team_id": args.team_id, "generated_at": started, "model_provider": llm.model_provider,
              "model_version": llm.model_version, "code_version": codeversion.current(),
              "input_tokens": llm.input_tokens, "output_tokens": llm.output_tokens,
              "strategy_facts": facts, "output": doc}
    docx_path.with_suffix(".json").write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"ETF 名稱：{doc['name']}\n投資主題：{doc['theme']}\n投資理念：{doc['philosophy']}\n")
    print(f"已輸出：{docx_path}\n產製紀錄：{docx_path.with_suffix('.json')}")
    print("審核：只能決定採用或重新產生，不得修改文字（六(三)）。")


if __name__ == "__main__":
    main()
