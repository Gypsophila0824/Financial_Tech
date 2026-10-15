"""Point-in-time scored news contract. Never fetch or relabel historical news."""
from datetime import datetime
from pathlib import Path
import hashlib
from io import BytesIO
from datetime import timezone, timedelta

import pandas as pd

FIELDS = {'trade_date', 'ticker', 'news_score', 'worst_event', 'published_at',
          'available_at', 'analyzed_at', 'snapshot_cutoff', 'source', 'snapshot_version',
          'model_version', 'prompt_version', 'content_sha256'}


def timestamp(value):
    t = datetime.fromisoformat(str(value))
    if t.tzinfo is None:
        raise ValueError('新聞時間必須含時區')
    return t


def validate_news(frame: pd.DataFrame, trade_date: str, decision_at: str) -> pd.DataFrame:
    missing = FIELDS - set(frame.columns)
    if missing:
        raise ValueError(f'新聞快照缺少欄位：{sorted(missing)}')
    cutoff = timestamp(decision_at)
    local = cutoff.astimezone(timezone(timedelta(hours=8)))
    if local.date().isoformat() != trade_date or cutoff > timestamp(f'{trade_date}T08:55:00+08:00'):
        raise ValueError('決策日期不一致')
    rows = frame.loc[frame.trade_date.astype(str) == trade_date].copy()
    if rows.empty:
        raise ValueError(f'{trade_date} 缺少可信新聞快照；不得默默改成中性')
    rows['ticker'] = rows.ticker.astype(str)
    if rows.ticker.duplicated().any():
        raise ValueError('新聞快照股票重複')
    if not pd.to_numeric(rows.news_score, errors='raise').between(0, 1).all():
        raise ValueError('新聞分數無效')
    for r in rows.to_dict('records'):
        p, a, l, s = [timestamp(r[k]) for k in ('published_at', 'available_at', 'analyzed_at', 'snapshot_cutoff')]
        if not p <= a <= l <= s <= cutoff:
            raise ValueError('新聞發布／可得／分析／快照時間超出決策時點或順序錯誤')
        for k in ('source', 'snapshot_version', 'model_version', 'prompt_version'):
            if pd.isna(r[k]) or not str(r[k]).strip():
                raise ValueError(f'新聞缺少追蹤資訊：{k}')
        import re
        if not re.fullmatch('[0-9a-f]{64}', str(r['content_sha256'])):
            raise ValueError('新聞內容雜湊無效')
    return rows.set_index('ticker')


class NewsSnapshots:
    def __init__(self, path: Path):
        self.raw = path.read_bytes()
        self.sha256 = hashlib.sha256(self.raw).hexdigest()
        self.frame = pd.read_csv(BytesIO(self.raw), dtype={'ticker': str})

    def __call__(self, trade_date: str):
        return validate_news(self.frame, trade_date, f'{trade_date}T08:55:00+08:00')
