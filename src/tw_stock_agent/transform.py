"""Pure normalization helpers for imported source rows."""
from __future__ import annotations

from datetime import date


def normalize_ticker(value: object) -> str:
    raw = str(value).strip()
    if raw.endswith(".0") and raw[:-2].isdigit():
        raw = raw[:-2]
    if not raw.isdigit() or not 4 <= len(raw) <= 6:
        raise ValueError(f"invalid ticker: {value!r}")
    return raw.zfill(4)


def normalize_market(value: object) -> str:
    aliases = {"TWSE": "TWSE", "上市": "TWSE", "TPEX": "TPEX", "TPEx": "TPEX", "上櫃": "TPEX"}
    try:
        return aliases[str(value).strip()]
    except KeyError as exc:
        raise ValueError(f"invalid market: {value!r}") from exc


def normalize_date(value: object) -> str:
    parsed = date.fromisoformat(str(value).strip())
    return parsed.isoformat()


def optional_decimal(value: object) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    return float(str(value).replace(",", ""))
