"""Universe Loader：讀取由官方 150 檔清單建立的 data/security_master.csv。"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SECURITY_MASTER = ROOT / "data" / "security_master.csv"


@dataclass(frozen=True)
class Security:
    ticker: str
    name: str
    market: str          # TWSE / TPEX
    industry_code: str
    industry: str
    max_weight: float


def load_universe(path: Path = SECURITY_MASTER) -> dict[str, Security]:
    if not path.exists():
        raise FileNotFoundError(f"找不到 {path}，請先執行 src/universe/build_security_master.py")
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    universe = {
        r["ticker"]: Security(r["ticker"], r["name"], r["market"], r["industry_code"],
                              r["industry"], float(r["max_weight"]))
        for r in rows if r["is_eligible"] == "true"
    }
    if len(universe) != 150:
        raise ValueError(f"股票主檔應有 150 檔，實際 {len(universe)} 檔")
    return universe
