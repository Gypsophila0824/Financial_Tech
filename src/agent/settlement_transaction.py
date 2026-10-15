"""Write-ahead settlement journal. Recovery never recomputes trades or fees.

OS-held single writer lock; the OS releases it on process death. The lock file
itself is deliberately retained to avoid races between different file handles.
"""
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4
from contextlib import contextmanager
from dataclasses import asdict

from .state import PortfolioState


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode('utf-8')


@contextmanager
def writer_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        if path.stat().st_size == 0:
            stream.write(b'0'); stream.flush()
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def atomic_write(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    try:
        with temp.open('xb') as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def _apply(ledger, payload):
    history = ledger.parent / 'history'
    day = payload['day']
    current = encoded(asdict(PortfolioState.load(ledger)))
    before, after = encoded(payload['before']), encoded(payload['after'])
    if current not in (before, after):
        raise ValueError('恢復帳本與 journal 前後狀態皆不符；禁止覆蓋')
    for name, raw in payload['evidence'].items():
        allowed = {f'official_{day}.json', f'receipt_{day}.bin', f'settlement_{day}.json', f'ledger_before_{day}.json'}
        if name not in allowed:
            raise ValueError('journal 證據檔名不合法')
        path, data = history / name, bytes.fromhex(raw)
        if path.exists():
            if path.read_bytes() != data:
                raise ValueError(f'既有證據不同：{name}')
        else:
            atomic_write(path, data)
    if current != after:
        atomic_write(ledger, after)
    atomic_write(history / f'committed_{day}.json', encoded({'day': day, 'after_sha256': hashlib.sha256(after).hexdigest()}))


def recover(ledger: Path, day: str, team: str):
    from .submissions import day_dir
    day_dir(ledger.parent, team, day)  # validate identifiers before using paths
    history = ledger.parent / 'history'
    journal = json.loads((history / f'journal_{day}.json').read_text(encoding='utf-8'))
    payload = journal['payload']
    if hashlib.sha256(encoded(payload)).hexdigest() != journal['sha256']:
        raise ValueError('結算 journal 雜湊不符')
    if payload['team'] != team or payload['day'] != day:
        raise ValueError('結算 journal 身份不符')
    lock = ledger.parent / 'settlement.lock'
    with writer_lock(lock):
        _apply(ledger, payload)


def commit(ledger: Path, day: str, team: str, before: dict, after: dict, evidence: dict[str, bytes]):
    from .submissions import day_dir
    day_dir(ledger.parent, team, day)
    history = ledger.parent / 'history'
    history.mkdir(parents=True, exist_ok=True)
    payload = {'day': day, 'team': team, 'before': before, 'after': after,
               'evidence': {k: v.hex() for k, v in evidence.items()}}
    journal = history / f'journal_{day}.json'
    lock = ledger.parent / 'settlement.lock'
    with writer_lock(lock):
        if journal.exists():
            raise ValueError('已有結算 journal，請執行 recover，不重新計算')
        atomic_write(journal, encoded({'payload': payload, 'sha256': hashlib.sha256(encoded(payload)).hexdigest()}))
        _apply(ledger, payload)
