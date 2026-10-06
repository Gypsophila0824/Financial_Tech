"""Source adapters. Network endpoints are caller-supplied and never guessed."""
from __future__ import annotations

import csv
from pathlib import Path
from urllib.request import urlopen


def load_utf8_csv(path: str | Path) -> list[dict[str, str]]:
    """Read a UTF-8 CSV while retaining identifiers (such as ticker) as text."""
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def download_official_file(url: str, destination: str | Path, timeout_seconds: int = 30) -> Path:
    """Download an explicitly approved official source and persist its original bytes."""
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    with urlopen(url, timeout=timeout_seconds) as response:  # nosec B310: URL is explicit caller input
        target.write_bytes(response.read())
    return target
