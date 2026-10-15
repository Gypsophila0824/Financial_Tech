"""帳本核對：正確帳本通過；抄錯股數會被擋下；人工確認綁定帳本內容，修改後失效。"""

from __future__ import annotations

import json

from src.agent import ledger_check

BUSY = 4


def setup(mock_run, tmp_path):
    ledger = tmp_path / "ledger.json"
    mock_run.final.save(ledger)
    assert mock_run.final.as_of == mock_run.days[BUSY][0]
    return ledger


def test_correct_ledger_passes(mock_run, tmp_path):
    ledger = setup(mock_run, tmp_path)
    errors, warnings, notes = ledger_check.check(ledger, mock_run.out_dir, "MOCK", mock_run.inputs.daily)
    assert not errors and not warnings and any("股數與推算一致" in n for n in notes)


def test_wrong_shares_caught_and_confirm_bound_to_content(mock_run, tmp_path):
    ledger = setup(mock_run, tmp_path)
    data = json.loads(ledger.read_text(encoding="utf-8"))
    t = next(iter(data["holdings"]))
    data["holdings"][t] += 1000
    ledger.write_text(json.dumps(data), encoding="utf-8")
    errors, _, _ = ledger_check.check(ledger, mock_run.out_dir, "MOCK", mock_run.inputs.daily)
    assert errors and t in errors[0]
    as_of = data["as_of"]
    assert not ledger_check.confirmed(ledger, as_of, False, errors)
    assert ledger_check.confirmed(ledger, as_of, True, errors)
    assert ledger_check.confirmed(ledger, as_of, False, errors)
    data["holdings"][t] += 1000                        # 確認後又修改 → 需重新確認
    ledger.write_text(json.dumps(data), encoding="utf-8")
    assert not ledger_check.confirmed(ledger, as_of, False, errors)


def test_format_errors(mock_run, tmp_path):
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({"as_of": "2026-10-27", "nav": 1e9, "cash": -5,
                                  "holdings": {"9999": 1000, "2330": 1500}}), encoding="utf-8")
    errors, _, _ = ledger_check.check(ledger, mock_run.out_dir, "MOCK", mock_run.inputs.daily)
    text = "\n".join(errors)
    assert "150 檔以外" in text and "1000 的非負整數倍" in text and "現金須為非負" in text
