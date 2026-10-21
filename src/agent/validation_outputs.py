"""彩排與回測輸出的共同位置及瀏覽索引。"""
from pathlib import Path
from datetime import datetime
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
VALIDATION_ROOT = ROOT / 'output' / 'validation'


def dated_run_name(prefix: str, start_date: str, end_date: str | None = None,
                   *, stamp: str | None = None) -> str:
    """執行資料夾名稱明示其涵蓋日期，並保留同日多次執行的唯一性。"""
    period = start_date if not end_date or end_date == start_date else f'{start_date}_to_{end_date}'
    stamp = stamp or (datetime.now().strftime('%H%M%S_') + uuid4().hex[:8])
    return f'{prefix}_{period}_{stamp}'


def update_index():
    VALIDATION_ROOT.mkdir(parents=True, exist_ok=True)
    lines = ['# 彩排與回測結果', '', '正式可送審 D-Plan 另存 `output/live/runs/`。', '']
    for name, label in [('acceptance', '實跑驗收與總報告'), ('daily_workflow', '每日完整流程'), ('backtest', '獨立回測'),
                        ('replay', '歷史彩排'), ('replay_api', 'API 彩排'), ('mock', '合成模擬')]:
        folder = VALIDATION_ROOT / name
        lines += [f'## {label}', '']
        if folder.exists():
            lines.append(f'- [{name}/]({name}/)')
            for child in sorted(folder.iterdir(), key=lambda p: p.name, reverse=True):
                if child.is_dir():
                    target = next((child / name for name in ('README.md', 'workflow.json', 'report.md')
                                   if (child / name).exists()), child)
                    target = target if target.exists() else child
                    lines.append(f'- [{child.name}]({target.relative_to(VALIDATION_ROOT).as_posix()})')
        else:
            lines.append('尚無執行結果。')
        lines.append('')
    lines += ['## 舊結果（原路徑保留）', '']
    for name in ('backtest', 'replay', 'replay_api', 'mock', 'daily_workflow', 'test_runs'):
        if (ROOT / 'output' / name).exists():
            lines.append(f'- [{name}](../{name}/)')
    (VALIDATION_ROOT / 'README.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def print_output_paths(label: str, run_dir: Path, *, files: tuple[tuple[str, str], ...] = ()) -> None:
    """在命令列列出本次輸出位置，方便直接複製給組員。"""
    run_dir = Path(run_dir).resolve()
    print(f'{label}資料夾：{run_dir}')
    for title, relative in files:
        path = run_dir / relative
        if path.exists():
            print(f'{title}：{path.resolve()}')
    print(f'彩排與回測總索引：{(VALIDATION_ROOT / "README.md").resolve()}')
