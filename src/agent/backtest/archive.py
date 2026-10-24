"""獨立回測輸入封存、重播與完整性檢查；不依賴現行 data/ 主檔。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from io import BytesIO, StringIO
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

import pandas as pd

from .. import codeversion
from ..inputs import DayInputs, SourceSpec
from ..validation_outputs import VALIDATION_ROOT

ROOT = Path(__file__).resolve().parents[3]
OUTPUT_ROOT = VALIDATION_ROOT


def json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')


def safe_output(path: Path) -> Path:
    path = path.resolve()
    base = OUTPUT_ROOT.resolve()
    if base != OUTPUT_ROOT.absolute():
        raise ValueError('回測輸出根目錄不得由連結轉向其他位置')
    if path == base or not path.is_relative_to(base):
        raise ValueError(f'回測輸出僅允許 {base} 下的新子目錄')
    if path.exists():
        raise FileExistsError(f'禁止覆寫既有回測：{path}')
    return path


def capture_sources(real_data: bool) -> tuple[dict[str, bytes], dict[Path, str]]:
    files = codeversion._files()
    versioned = list(files)
    if real_data:
        files += [ROOT / p for p in ('data/market/daily.csv', 'data/market/twse_exright.csv',
                                    'data/history/fundamental/fund_history.csv')]
        files += [ROOT / p for p in ('data/market/twse_market_cap_top20.csv',
                                    'data/market/twse_market_cap_top20_manifest.json')]
        files += sorted((ROOT / 'data/etf').glob('active_etf_holdings_*.csv'))
    contents = {p: p.read_bytes() for p in files if p.is_file()}
    buffer = BytesIO()
    with ZipFile(buffer, 'w', ZIP_DEFLATED) as z:
        for p, data in contents.items():
            z.writestr(p.relative_to(ROOT).as_posix(), data)
    version_hash = hashlib.sha256()
    for p in versioned:
        version_hash.update(p.relative_to(ROOT).as_posix().encode())
        version_hash.update(contents[p])
    hashes = {p: hashlib.sha256(b).hexdigest() for p, b in contents.items()}
    manifest = {'code_version': f'pkg:{version_hash.hexdigest()[:12]}',
                'files': {p.relative_to(ROOT).as_posix(): h for p, h in hashes.items()}}
    return {'source_snapshot.zip': buffer.getvalue(), 'source_manifest.json': json_bytes(manifest)}, hashes


def check_unchanged(hashes: dict[Path, str]) -> None:
    for p, digest in hashes.items():
        if not p.exists() or hashlib.sha256(p.read_bytes()).hexdigest() != digest:
            raise ValueError(f'回測期間來源已改變，請重新執行：{p}')


def capture_day(day, fills, closes, previous_closes) -> dict:
    frames = {k: getattr(day, k).to_json(orient='split', double_precision=15, force_ascii=False)
              for k in ('master', 'pv', 'fund')}
    market_cap_score = (None if day.market_cap_score is None else
                        {str(k): float(v) for k, v in day.market_cap_score.items()})
    for key in ('news', 'research_ranking'):
        if getattr(day, key, None) is not None:
            frames[key] = getattr(day, key).to_json(orient='split', double_precision=15, force_ascii=False)
    return {'trade_date': day.trade_date, 'data_date': day.data_date, 'frames': frames,
            'market_cap_score': market_cap_score,
            'benches': {k: v.to_dict() for k, v in day.benches.items()},
            'close': day.close.to_dict(), 'tradable': sorted(day.tradable),
            'etf_date': day.etf_date, 'etf_dates': day.etf_dates, 'notes': list(day.notes),
            'market_sources': {k: asdict(v) for k, v in day.market_sources.items()},
            'fund_sources': {k: asdict(v) for k, v in day.fund_sources.items()},
            'fill_prices': fills, 'close_prices': closes, 'previous_close_prices': previous_closes}


def verify_archive(path: Path) -> dict:
    path = path.resolve()
    manifest = json.loads((path / 'integrity.json').read_text(encoding='utf-8'))
    expected = set(manifest['files'])
    actual = {p.relative_to(path).as_posix() for p in path.rglob('*') if p.is_file()} - {'integrity.json'}
    if actual != expected:
        raise ValueError('封存檔案清單不一致（有遺失或額外檔案）')
    for name, digest in manifest['files'].items():
        p = (path / name).resolve()
        if not p.is_relative_to(path) or hashlib.sha256(p.read_bytes()).hexdigest() != digest:
            raise ValueError(f'封存完整性驗證失敗：{name}')
    return manifest


class ArchivedInputs:
    """重播已封存的因子與價格；來源檔 ZIP 另供核對因子生成，不是正式輸入。"""
    is_mock = True

    def __init__(self, path: Path):
        verify_archive(path)
        manifest = json.loads((path / 'source_manifest.json').read_text(encoding='utf-8'))
        # 股票主檔及行情使用封存，不要求現行 data/ 相同；程式與依賴則必須還原版本。
        for name, digest in manifest['files'].items():
            if name.startswith(('src/', 'config/')) or name == 'requirements.txt':
                p = ROOT / name
                if not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest() != digest:
                    raise ValueError(f'重播程式版本不一致，請先還原 source_snapshot.zip：{name}')
        self.payload = json.loads((path / 'replay_inputs.json').read_text(encoding='utf-8'))
        self.rows = {r['trade_date']: r for r in self.payload['days']}

    def dates(self):
        rows = list(self.rows.values())
        return [rows[0]['data_date']] + list(self.rows)

    def day(self, trade_date):
        r = self.rows[trade_date]
        frames = {k: pd.read_json(StringIO(v), orient='split', dtype=False, convert_axes=False)
                  for k, v in r['frames'].items()}
        for f in frames.values():
            f.index = f.index.astype(str)
        news = frames.pop('news', None)
        return DayInputs(trade_date=r['trade_date'], data_date=r['data_date'], **frames,
                         news=news, news_items={}, benches={k: pd.Series(v) for k, v in r['benches'].items()},
                         market_cap_score=(None if r.get('market_cap_score') is None else
                                           pd.Series(r['market_cap_score'], dtype='float64')),
                         etf_date=r['etf_date'], close=pd.Series(r['close']), tradable=set(r['tradable']),
                         market_sources={k: SourceSpec(**v) for k, v in r['market_sources'].items()},
                         fund_sources={k: SourceSpec(**v) for k, v in r['fund_sources'].items()},
                         notes=r['notes'], etf_dates=r['etf_dates'])

    def fill_prices(self, day):
        return self.rows[day]['fill_prices']

    def evaluation_benchmarks(self, day):
        r = self.rows[day]
        return ({k: pd.Series(v) for k, v in r.get('evaluation_benches', r['benches']).items()},
                r.get('evaluation_etf_dates', r['etf_dates']))

    def close_prices(self, day):
        if day in self.rows:
            return self.rows[day]['close_prices']
        return next(r['previous_close_prices'] for r in self.rows.values() if r['data_date'] == day)
