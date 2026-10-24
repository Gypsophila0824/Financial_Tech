"""code_version：依程式碼、策略參數、股票主檔的內容雜湊自動產生。

D-Plan 指南 ⑦：影響決策的參數與提示詞模板都屬程式碼，改了就要換版本識別；
晉級後 12/2 資格審查須拿得出該版本程式碼。所以每個新版本都會打包到 output/code_archive/。
"""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

from . import rules

ROOT = Path(__file__).resolve().parents[2]
VERSIONED = [ROOT / "src", ROOT / "config", ROOT / "data" / "security_master.csv", ROOT / "requirements.txt",
             ROOT / rules.SCHEMA_RELATIVE_PATH]     # 合規檢查需要當前 schema，重現時要一起還原
ARCHIVE_DIR = ROOT / "output" / "code_archive"


def _files() -> list[Path]:
    files = []
    for p in VERSIONED:
        if p.is_dir():
            files += [f for f in p.rglob("*") if f.is_file() and "__pycache__" not in f.parts]
        elif p.exists():
            files.append(p)
    return sorted(files)


def current() -> str:
    h = hashlib.sha256()
    for f in _files():
        h.update(f.relative_to(ROOT).as_posix().encode())
        h.update(f.read_bytes())
    version = f"pkg:{h.hexdigest()[:12]}"
    _archive(version)
    return version


def _archive(version: str) -> None:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    target = ARCHIVE_DIR / f"{version.replace(':', '_')}.zip"
    if target.exists():
        return
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for f in _files():
            z.write(f, f.relative_to(ROOT).as_posix())
