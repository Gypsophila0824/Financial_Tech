from src.agent import validation_outputs as outputs
from src.agent.backtest import archive
import pytest


def test_index_links_run_and_keeps_legacy(monkeypatch, tmp_path):
    monkeypatch.setattr(outputs, 'ROOT', tmp_path)
    base = tmp_path / 'output/validation'
    monkeypatch.setattr(outputs, 'VALIDATION_ROOT', base)
    run = base / 'daily_workflow/run_1'
    run.mkdir(parents=True)
    (run / 'workflow.json').write_text('{}', encoding='utf-8')
    (tmp_path / 'output/backtest').mkdir()
    outputs.update_index()
    text = (base / 'README.md').read_text(encoding='utf-8')
    assert 'daily_workflow/run_1/workflow.json' in text
    assert '../backtest/' in text


def test_workflow_nested_backtest_allowed_but_live_forbidden(monkeypatch, tmp_path):
    base = tmp_path / 'output/validation'
    monkeypatch.setattr(archive, 'OUTPUT_ROOT', base)
    path = base / 'daily_workflow/run_1/backtest'
    assert archive.safe_output(path) == path.resolve()
    with pytest.raises(ValueError):
        archive.safe_output(tmp_path / 'output/live/run_1')


def test_print_output_paths_lists_run_and_reports(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(outputs, 'ROOT', tmp_path)
    monkeypatch.setattr(outputs, 'VALIDATION_ROOT', tmp_path / 'output/validation')
    run = outputs.VALIDATION_ROOT / 'daily_workflow/trade_2026-10-21_run'
    (run / 'backtest').mkdir(parents=True)
    (run / 'backtest/report.md').write_text('report', encoding='utf-8')
    outputs.print_output_paths('每日彩排與回測', run, files=(('回測報告', 'backtest/report.md'),))
    text = capsys.readouterr().out
    assert str(run.resolve()) in text
    assert str((run / 'backtest/report.md').resolve()) in text
    assert str((outputs.VALIDATION_ROOT / 'README.md').resolve()) in text
