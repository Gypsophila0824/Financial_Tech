"""HTTP 共用：節流、重試、原始回應快取。

- 證交所有請求頻率限制（過快會被暫時封鎖），同一主機的請求至少間隔 MIN_INTERVAL 秒。
- 原始回應存到 data/market/raw/，供事後查核 D-Plan 引用的資料內容；
  歷史日期的資料不會再變，命中快取就不重抓。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parents[3]
RAW_DIR = ROOT / "data" / "market" / "raw"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
MIN_INTERVAL = {"www.twse.com.tw": 3.0, "openapi.twse.com.tw": 1.0, "www.tpex.org.tw": 2.0}
DEFAULT_INTERVAL = 0.5

_last_call: dict[str, float] = {}


def _throttle(host: str) -> None:
    wait = MIN_INTERVAL.get(host, DEFAULT_INTERVAL) - (time.monotonic() - _last_call.get(host, 0.0))
    if wait > 0:
        time.sleep(wait)
    _last_call[host] = time.monotonic()


def fetch_json(url: str, cache_key: str | None = None, retries: int = 4, timeout: int = 120):
    """取得 JSON。給 cache_key 時先查快取，抓到後寫入快取。"""
    path = RAW_DIR / f"{cache_key}.json" if cache_key else None
    if path and path.exists():
        return json.loads(path.read_text(encoding="utf-8"))

    host = urlparse(url).netloc
    last_err = None
    for attempt in range(1, retries + 1):
        _throttle(host)
        try:
            resp = requests.get(url, headers=HEADERS, timeout=timeout)
            resp.raise_for_status()
            data = json.loads(resp.content.decode("utf-8-sig"))
            break
        except (requests.RequestException, ValueError) as exc:
            last_err = exc
            time.sleep(3 * attempt)
    else:
        raise RuntimeError(f"無法取得 {url}：{last_err}")

    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data
