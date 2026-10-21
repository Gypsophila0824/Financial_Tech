"""ETF 投資策略說明文件產生器：格式檢查、重試、docx 輸出。"""

from __future__ import annotations

import docx
import pytest

from src.agent.llm import FakeLLM, LLMError
from src.agent.strategy_doc import generate, validate, write_docx

PHILOSOPHY = ("股價動能反映市場資金與趨勢方向，而每月公布的營收是台股最即時的基本面數據，兩者結合可兼顧趨勢與成長。"
              "本 ETF 不限產業，AI Agent 每個交易日為 150 檔標的評分：量價動能占 40%，包含短中期報酬與均線趨勢，"
              "並納入三大法人籌碼；基本面占 50%，其中營收成長率與營收成長加速各占一半；事件／資訊占 10%，"
              "由 AI 判讀新聞以辨識重大利空。持有總分前段約 25 檔，依分數與波動度配置權重，"
              "並對重大利空且跌破均線者出清以控制下檔風險，以單檔權重上限與換股緩衝控制個股風險與交易成本，"
              "追求參與動能與成長兼具企業的長期投資成果。")
GOOD = {"name": "主動動能成長精選", "theme": "以價格動能與營收成長為核心，從台股大型股中精選成長企業",
        "philosophy": PHILOSOPHY}


def test_good_document_passes():
    assert validate(GOOD) == []


@pytest.mark.parametrize("field,value,msg", [
    ("name", "台灣AI科技鏈", "開頭必須是「主動」"),
    ("name", "主動台灣AI科技鏈領航五十檔精選成長優質組合", "超過 20 字"),
    ("name", "主動AI50", "只能使用中文或英文"),
    ("name", "主動 統一台股成長", "與現有主動型 ETF 相同"),
    ("theme", "聚" * 51, "超過 50 字"),
    ("philosophy", "太短的理念", "100～300 字"),
    ("philosophy", PHILOSOPHY[:150], "內容不夠完整"),
    ("philosophy", PHILOSOPHY.replace("追求參與", "保證參與"), "招攬"),
    ("philosophy", PHILOSOPHY.replace("營收成長加速", "本益比"), "未使用的因子"),
])
def test_rule_violations(field, value, msg):
    assert any(msg in e for e in validate({**GOOD, field: value}))


def test_core_factors_required():
    doc = {k: v.replace("動能", "趨勢") for k, v in GOOD.items()}
    assert any("核心因子" in e for e in validate(doc))


def test_retry_then_success():
    answers = iter([{**GOOD, "name": "台灣AI科技鏈"}, GOOD])
    llm = FakeLLM(lambda s, u, schema: next(answers))
    doc, facts = generate(llm)
    assert doc == GOOD and llm.calls == 2 and facts["投資主題"]


def test_persistent_failure_raises():
    with pytest.raises(LLMError):
        generate(FakeLLM(lambda s, u, schema: {**GOOD, "philosophy": "太短"}))


def test_docx_cells_filled(tmp_path):
    path = tmp_path / "doc.docx"
    write_docx(GOOD, path)
    rows = docx.Document(path).tables[0].rows
    assert [r.cells[1].text for r in rows] == [GOOD["name"], GOOD["theme"], GOOD["philosophy"]]
    assert rows[0].cells[0].text.startswith("ETF 名稱")
