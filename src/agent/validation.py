"""One-shot, hash-locked retrospective/OOS comparison. No live promotion."""
import argparse
import hashlib
import json
from datetime import date
from pathlib import Path

from . import codeversion, rules
from .backtest import archive
from .backtest.runner import run_formal_backtest, write_result
from .inputs import StoreInputs
from .news_contract import NewsSnapshots
from .strategy_interface import ResearchPortfolio

ROOT = Path(__file__).resolve().parents[2]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_spec(spec):
    periods = [spec[k] for k in ('development', 'validation', 'test')]
    for start, end in periods:
        date.fromisoformat(start); date.fromisoformat(end)
        if start > end:
            raise ValueError('期間起迄錯誤')
    if not periods[0][1] < periods[1][0] <= periods[1][1] < periods[2][0]:
        raise ValueError('研究、驗證、測試期間必須依時間隔離')
    if spec.get('news_policy') not in ('neutral_ablation', 'point_in_time_snapshot'):
        raise ValueError('必須明確指定新聞政策')
    if spec['news_policy'] == 'point_in_time_snapshot' and not spec.get('news_snapshot'):
        raise ValueError('缺少新聞快照')
    if spec['news_policy'] == 'neutral_ablation' and spec.get('news_snapshot'):
        raise ValueError('中性新聞設定與快照衝突')
    if not 20 <= spec['research_selected_count'] <= 30:
        raise ValueError('競賽研究投組須 20–30 檔')
    if not spec.get('prior_exposure_disclosure'):
        raise ValueError('必須揭露哪些測試資料曾用於調參／選型')
    # No code can prove human non-exposure. Attestation must remain explicit.
    if spec.get('claims_unseen_oos') and not spec.get('unseen_attestation'):
        raise ValueError('樣本外宣告缺少事前未使用證明')


def seal(spec: dict, path: Path):
    validate_spec(spec)
    files = list(codeversion._files())
    files += [ROOT / 'data/market/daily.csv', ROOT / 'data/market/twse_exright.csv',
              ROOT / 'data/history/fundamental/fund_history.csv']
    files += list((ROOT / 'data/etf').glob('active_etf_holdings_*.csv'))
    for key in ('news_snapshot', 'corporate_actions'):
        if spec.get(key):
            files.append(Path(spec[key]).resolve())
    payload = {'spec': spec, 'files': {str(p.resolve()): digest(p) for p in files if p.is_file()},
               'execution': {'capital': rules.INITIAL_CAPITAL, 'fee': rules.FEE_RATE,
                             'tax': rules.TAX_RATE, 'rounding': 'floor_unconfirmed',
                             'fee_basis': 'daily_gross_buy_plus_sell_not_net',
                             'tax_basis': 'per_sell_order_unconfirmed',
                             'fill': 'daily_average', 'valuation': 'daily_close',
                             'turnover': 'gross_two_sided_over_prior_nav',
                             'baseline': 'available_equal_weight_price_only_not_total_return'},
               'formal_promotion': False}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as f:
        f.write(archive.json_bytes(payload))
    with path.with_suffix('.sha256').open('x') as f:
        f.write(digest(path))


def verify(path):
    if path.with_suffix('.sha256').read_text().strip() != digest(path):
        raise ValueError('鎖定設定已修改')
    payload = json.loads(path.read_text(encoding='utf-8'))
    validate_spec(payload['spec'])
    for name, expected in payload['files'].items():
        p = Path(name)
        if not p.is_file() or digest(p) != expected:
            raise ValueError(f'鎖定後資料／程式已變動：{name}')
    return payload


def run_locked(path: Path, out: Path):
    payload = verify(path)
    spec = payload['spec']
    out = archive.safe_output(out)
    # Reserve before revealing results; a failed attempt remains consumed.
    with path.with_suffix('.attempt.json').open('xb') as f:
        f.write(archive.json_bytes({'lock_sha256': digest(path), 'out': str(out), 'status': 'started'}))
    out.mkdir(parents=True)
    (out / 'validation_lock.json').write_bytes(path.read_bytes())
    snapshot, _ = archive.capture_sources(real_data=True)
    (out / 'common_source_snapshot.zip').write_bytes(snapshot['source_snapshot.zip'])
    (out / 'common_source_manifest.json').write_bytes(snapshot['source_manifest.json'])
    for key in ('news_snapshot', 'corporate_actions'):
        if spec.get(key):
            (out / f'{key}_source.bin').write_bytes(Path(spec[key]).read_bytes())
    from .backtest.corporate_actions import load_actions
    actions = load_actions(Path(spec['corporate_actions']))[0] if spec.get('corporate_actions') else []
    news = NewsSnapshots(Path(spec['news_snapshot'])) if spec.get('news_snapshot') else None
    summaries = {}
    intervals = []
    for name, adapter in [('production', None), ('research_equal', ResearchPortfolio(spec['research_selected_count']))]:
        verify(path)
        try:
            result = run_formal_backtest(StoreInputs(history=True, strict_history=True),
                start=spec['test'][0], end=spec['test'][1], strategy_adapter=adapter,
                news_loader=news, corporate_actions=actions, terminal_date=spec.get('terminal_date'))
        except Exception as exc:
            (out / 'failure.json').write_bytes(archive.json_bytes({
                'strategy': name, 'reason': str(exc), 'completed': list(summaries),
                'fair_comparison_complete': False, 'attempt_consumed': True}))
            raise
        write_result(result, out / name, config=payload)
        summaries[name] = result.summary
        intervals.append(list(result.daily_nav.date))
    if intervals[0] != intervals[1]:
        raise ValueError('兩策略實際完成交易日期不同；不得視為公平績效比較，保留失敗輸出')
    verify(path)
    status = {'results': summaries, 'claims_unseen_oos': bool(spec.get('claims_unseen_oos')),
              'scope': 'portfolio comparison, not full D-Plan/platform validation',
              'formal_promotion': False}
    (out / 'comparison.json').write_bytes(archive.json_bytes(status))
    return status


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('lock'); p.add_argument('--spec', type=Path, required=True); p.add_argument('--lock', type=Path, required=True)
    p = sub.add_parser('run'); p.add_argument('--lock', type=Path, required=True); p.add_argument('--out-dir', type=Path, required=True)
    args = ap.parse_args()
    base = (ROOT / 'output/research').resolve()
    if not args.lock.resolve().is_relative_to(base):
        ap.error('鎖定設定只能放 output/research 下')
    if args.cmd == 'lock':
        seal(json.loads(args.spec.read_text(encoding='utf-8')), args.lock)
    else:
        run_locked(args.lock, args.out_dir)


if __name__ == '__main__':
    main()
