"""Display completed internal backtests, or explicit failures, as Markdown tables.

Reads existing archives only. Never runs trades, fills gaps, or invents NAV.
Monthly figures describe one continuous account, not monthly capital resets.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

import pandas as pd

from . import archive


def read_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def escape(value) -> str:
    return str(value).replace('|', '\\|').replace('\n', ' ')


def money(value: float) -> str:
    return f'{value / 10000:+,.1f} 萬'


def summarize_pair(baseline: pd.DataFrame, experiment: pd.DataFrame):
    """Validate aligned continuous accounts and compute actual NAV differences."""
    for frame in (baseline, experiment):
        if not {'date', 'nav', 'prior_nav'} <= set(frame.columns) or frame.empty:
            raise ValueError('缺少非空 daily_nav 的 date、nav、prior_nav')
        dates = pd.to_datetime(frame.date, errors='raise')
        if dates.dt.strftime('%Y-%m-%d').tolist() != frame.date.tolist():
            raise ValueError('日期須為 YYYY-MM-DD')
        if frame.date.duplicated().any() or frame.date.tolist() != sorted(frame.date):
            raise ValueError('日期重複或未依序排列')
        for col in ('nav', 'prior_nav'):
            if not frame[col].map(lambda x: isinstance(x, (int, float)) and math.isfinite(x) and x > 0).all():
                raise ValueError('NAV 與 prior_nav 須為正的有限數值')
        if not ((frame.prior_nav.iloc[1:].to_numpy() - frame.nav.iloc[:-1].to_numpy()).__abs__() <= 1).all():
            raise ValueError('每日 NAV 帳本不連續，不可當成同一資金曲線')
    if baseline.date.tolist() != experiment.date.tolist():
        raise ValueError('兩版交易日期不一致，不能比較')
    if abs(float(baseline.prior_nav.iloc[0]) - float(experiment.prior_nav.iloc[0])) > 1:
        raise ValueError('兩版初始本金不同')

    def one(frame):
        initial = float(frame.prior_nav.iloc[0])
        final = float(frame.nav.iloc[-1])
        return {'initial_nav': initial, 'final_nav': final, 'profit': final-initial,
                'return': final/initial-1, 'days': len(frame)}

    monthly = []
    months = baseline.date.str[:7]
    for month in months.unique():
        old, new = one(baseline.loc[months == month]), one(experiment.loc[months == month])
        monthly.append({'month': month, 'days': old['days'],
                        'baseline_profit': old['profit'], 'experiment_profit': new['profit'],
                        'baseline_final_nav': old['final_nav'], 'experiment_final_nav': new['final_nav'],
                        'baseline_return': old['return'], 'experiment_return': new['return'],
                        'difference_percentage_points': (new['return']-old['return'])*100})
    return {'baseline': one(baseline), 'experiment': one(experiment)}, pd.DataFrame(monthly)


def load_pair(comparison: Path, value: dict):
    """Require intact archives and the complete requested historical date range."""
    root = comparison.parent
    old_dir, new_dir = root / 'baseline', root / 'experiment'
    for directory in (old_dir, new_dir):
        archive.verify_archive(directory)
    old_config, new_config = [read_json(d/'config_snapshot.json') for d in (old_dir, new_dir)]
    if old_config.get('source_snapshot_sha256') != new_config.get('source_snapshot_sha256'):
        raise ValueError('兩版來源快照不同，請先確認共同資料與設定')
    start, end = value['spec']['start'], value['spec']['end']
    old = pd.read_csv(old_dir/'daily_nav.csv')
    new = pd.read_csv(new_dir/'daily_nav.csv')
    # Trading dates come from the archived data, not today's changed calendar.
    from zipfile import ZipFile
    with ZipFile(old_dir/'source_snapshot.zip') as z:
        with z.open('data/market/daily.csv') as f:
            market = pd.read_csv(f, usecols=['date'])
    expected = sorted(d for d in market.date.unique() if start <= d <= end)
    if not expected or old.date.tolist() != expected or new.date.tolist() != expected:
        raise ValueError('回測沒有涵蓋完整指定交易日期，不可將略過日當作完整績效')
    return summarize_pair(old, new)


def render(comparison: Path, out_dir: Path) -> Path:
    value = read_json(comparison)
    spec = value['spec']
    lines = ['# 回測策略比較報告', '',
             f'指定期間：{escape(spec["start"])}～{escape(spec["end"])}', '',
             '名稱依 comparison.json：原方案 baseline、新方案 experiment。月份採連續資金帳戶，不是每月重置本金。', '']
    result = {'requested_start': spec['start'], 'requested_end': spec['end'],
              'benchmark_0050': 'not_provided', 'monthly_mode': 'continuous_account',
              'formal_promotion': False}
    monthly = None
    errors = []
    if value.get('fair_comparison_complete') is not True:
        for variant, row in value.get('results', {}).items():
            errors.append(f'{variant}：{row.get("error", row.get("status", "未完成"))}')
        if not errors:
            errors.append('比較紀錄未標記為完整回測')
    else:
        try:
            totals, monthly = load_pair(comparison, value)
        except (OSError, ValueError, KeyError) as exc:
            errors.append(str(exc))
    if errors:
        result.update(status='incomplete', errors=errors)
        lines += ['**回測未完整完成：不顯示推算損益、期末 NAV 或月報績效。**', '',
                  '| 方案 | 損益 | 期末淨值 |', '|---|---:|---:|',
                  '| 原方案 | 無完整結果 | — |', '| 新方案 | 無完整結果 | — |', '',
                  '原因：', ''] + [f'- {escape(e)}' for e in errors]
        coverage = comparison.parent/'coverage_preflight.json'
        if coverage.exists():
            c = read_json(coverage)
            lines += ['', f'ETF 資料覆蓋：{c.get("requested_trade_date_count", "—")} 個請求交易日中，'
                      f'{c.get("missing_etf_count", "—")} 日缺少當時可用快照。']
    else:
        result.update(status='complete', totals=totals, monthly=monthly.to_dict('records'))
        lines += ['| 方案 | 損益 | 期末淨值 | 報酬 |', '|---|---:|---:|---:|']
        for name, key in [('原方案','baseline'), ('新方案','experiment')]:
            row=totals[key]
            lines.append(f'| {name} | {money(row["profit"])} | {row["final_nav"]/1e8:.4f} 億 | {row["return"]:.2%} |')
        lines += ['', '## 月份比較（連續資金，非每月重置）', '',
                  '| 月份 | 交易日數 | 新方案損益 | 原方案損益 | 新－舊月報酬（百分點） |',
                  '|---|---:|---:|---:|---:|']
        for row in monthly.to_dict('records'):
            lines.append(f'| {row["month"]} | {row["days"]} | {money(row["experiment_profit"])} | '
                         f'{money(row["baseline_profit"])} | {row["difference_percentage_points"]:+.4f} |')
    lines += ['', '0050：未提供獨立、可追溯的同期比較資料，本報告不以其他基準冒充 0050。', '',
              '報表只展示既有回測，未重新抓資料、未執行策略、未更動競賽規則；回測與資料完整性仍須依原封存說明判讀。', '']
    out_dir = archive.safe_output(out_dir)
    out_dir.mkdir(parents=True, exist_ok=False)
    (out_dir/'report.md').write_text('\n'.join(lines),encoding='utf-8')
    (out_dir/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    if monthly is not None:
        monthly.to_csv(out_dir/'monthly_comparison.csv',index=False,encoding='utf-8-sig')
    (out_dir/'source_comparison.json').write_bytes(comparison.read_bytes())
    meta={'input':str(comparison.resolve()),
          'comparison_sha256':hashlib.sha256(comparison.read_bytes()).hexdigest(),
          'files':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in out_dir.iterdir() if p.is_file()}}
    (out_dir/'report_integrity.json').write_text(json.dumps(meta,ensure_ascii=False,indent=2),encoding='utf-8')
    return out_dir/'report.md'


def main():
    p=argparse.ArgumentParser(description='將內部雙策略回測結果顯示為比較／月報表格；不重新交易或補值')
    p.add_argument('--comparison',type=Path,required=True,help='既有雙策略 comparison.json')
    p.add_argument('--out-dir',type=Path,required=True,help='output/backtest 下尚不存在的新目錄')
    args=p.parse_args()
    print(render(args.comparison,args.out_dir))


if __name__=='__main__':
    main()
