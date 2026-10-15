"""本地提交證據登錄；不連線、不提交，也不宣稱能驗證平台回執真偽。"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_new(path: Path, value: dict) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)


def day_dir(out: Path, team: str, day: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", team):
        raise ValueError("team_id 格式不合法")
    if datetime.fromisoformat(day).date().isoformat() != day:
        raise ValueError("trade_date 須為 YYYY-MM-DD")
    return out / "submissions" / team / day


def seal_run(run: Path, team: str, day: str, passed: bool) -> None:
    """每次執行獨立目錄、禁止覆寫；SHA-256 可偵測封存內容被改動。"""
    files = [f"D-Plan_{team}_{day}.json", f"inputs_{team}_{day}.json"]
    write_new(run / "run_record.json", {
        "team_id": team, "trade_date": day, "checks_passed": passed,
        "files": {name: digest((run / name).read_bytes()) for name in files},
    })


def confirm_submission(out: Path, run: Path, team: str, day: str,
                       receipt: Path, submitted_at: str) -> Path:
    stamp = datetime.fromisoformat(submitted_at)
    if stamp.tzinfo is None or stamp > datetime.now(timezone.utc):
        raise ValueError("成功提交時間必須含時區，且不得晚於現在")
    from .compliance import submit_window
    from datetime import timedelta
    start, end = submit_window(datetime.fromisoformat(day).date(), timezone(timedelta(hours=8)))
    if not start <= stamp <= end:
        raise ValueError("成功提交時間不在該交易日繳交時窗")
    run = run.resolve()
    if not run.is_relative_to((out / "runs").resolve()):
        raise ValueError("只能登錄 output/live/runs 內的獨立執行版本")
    record = json.loads((run / "run_record.json").read_text(encoding="utf-8"))
    if record["team_id"] != team or record["trade_date"] != day or not record["checks_passed"]:
        raise ValueError("執行版本日期、隊伍不符，或本地檢查未通過")
    payload = {}
    for name in (f"D-Plan_{team}_{day}.json", f"inputs_{team}_{day}.json"):
        raw = (run / name).read_bytes()
        if digest(raw) != record["files"].get(name):
            raise ValueError("執行版本已被修改，不能登錄")
        payload[name] = raw
    plan = json.loads(payload[f"D-Plan_{team}_{day}.json"])
    if plan["trade_date"] != day or plan["team_id"] != team:
        raise ValueError("D-Plan 日期或隊伍不符")
    evidence = receipt.read_bytes()
    if not evidence:
        raise ValueError("回執不得為空；必須提供平台成功提交證據")
    target = day_dir(out, team, day)
    target.mkdir(parents=True, exist_ok=True)
    # 同一平台時間若已登錄，拒絕歧義；先確認實際回執，不以登錄時間決定交易版本。
    for old in target.glob("*/submission.json"):
        if datetime.fromisoformat(json.loads(old.read_text(encoding="utf-8"))["submitted_at"]) == stamp:
            raise ValueError("此平台提交時間已登錄，請確認是否重複或有時間歧義")
    event = target / uuid4().hex
    event.mkdir()
    for name, raw in payload.items():
        with (event / name).open("xb") as stream:
            stream.write(raw)
    with (event / "receipt.bin").open("xb") as stream:
        stream.write(evidence)
    write_new(event / "submission.json", {
        "team_id": team, "trade_date": day, "submitted_at": stamp.isoformat(),
        "recorded_at": datetime.now(timezone.utc).isoformat(), "run_dir": str(run),
        "receipt_name": receipt.name, "confirmed_by_operator": True,
        "files": {**{name: digest(raw) for name, raw in payload.items()}, "receipt.bin": digest(evidence)},
    })
    return event


def latest_submission(out: Path, team: str, day: str):
    entries = []
    for directory in day_dir(out, team, day).glob("*"):
        if not directory.is_dir():
            continue
        record = json.loads((directory / "submission.json").read_text(encoding="utf-8"))
        if record["team_id"] != team or record["trade_date"] != day:
            raise ValueError("提交紀錄身份不符")
        stamp = datetime.fromisoformat(record["submitted_at"])
        if stamp.tzinfo is None:
            raise ValueError("提交紀錄缺少時區")
        entries.append((stamp, directory, record))
    if not entries:
        raise ValueError("沒有成功提交紀錄；不得以本地 D-Plan 當作已成交")
    entries.sort(key=lambda x: x[0])
    if len(entries) > 1 and entries[-1][0] == entries[-2][0]:
        raise ValueError("最新提交時間有歧義")
    _, directory, record = entries[-1]
    for name in (f"D-Plan_{team}_{day}.json", f"inputs_{team}_{day}.json", "receipt.bin"):
        if digest((directory / name).read_bytes()) != record["files"].get(name):
            raise ValueError("提交封存或回執已被修改")
    return directory, record


def reconcile(state, official: dict, team: str, day: str) -> None:
    """官方資料由值班人自平台匯出／轉錄；嚴格股數與一元金額誤差，差異停止。"""
    if official.get("team_id") != team or official.get("as_of") != day:
        raise ValueError("官方帳本日期或隊伍不符")
    holdings = official.get("holdings")
    if not isinstance(holdings, dict) or any(type(n) is not int or n < 0 for n in holdings.values()):
        raise ValueError("官方股數須為非負整數")
    if {t: n for t, n in holdings.items() if n} != state.holdings:
        raise ValueError("推估股數與官方庫存不同，停止寫入帳本；請查明提交／成交狀態")
    for name in ("cash", "nav"):
        value = official.get(name)
        if type(value) not in (float, int) or not math.isfinite(value) or value < 0 or (name == "nav" and value == 0):
            raise ValueError(f"官方 {name} 無效")
        if abs(getattr(state, name) - value) > 1:
            raise ValueError(f"推估 {name} 與官方相差超過 1 元，停止寫入；請查明費稅／成交資料差異")


def state_digest(state) -> str:
    return digest(json.dumps(asdict(state), sort_keys=True, ensure_ascii=False).encode("utf-8"))
