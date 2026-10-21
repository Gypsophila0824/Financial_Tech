"""正式策略的可稽核清單與封存工具。

這個模組不決定任何交易。它將會影響決策的策略常數整理為一份帶雜湊的
manifest，隨每日輸入包封存，讓賽後可辨識當日使用的策略定義。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import rules, strategy


MANIFEST_VERSION = "production-strategy-manifest-v1"


def production_manifest() -> dict:
    """回傳正式策略的穩定、JSON 可序列化描述與內容雜湊。"""
    body = {
        "manifest_version": MANIFEST_VERSION,
        "theme": strategy.THEME_NAME,
        "production_model": {
            "score_weights": strategy.SCORE_WEIGHTS,
            "fundamental_score_weights": strategy.FUND_SCORE_WEIGHTS,
            "price_volume_factors": [
                "A1: 近20日還原報酬",
                "A2: 跳過最近5日的60日還原報酬",
                "A3: 相對60日均價",
                "A4: 近10日法人買賣超金額／20日平均成交金額",
            ],
            "selection": {
                "target_holdings": strategy.N_HOLDINGS,
                "holding_buffer_rank": strategy.BUFFER_RANK,
                "target_cash": strategy.TARGET_CASH,
                "no_trade_band": strategy.NO_TRADE_BAND,
            },
            "risk_controls": {
                "target_active_share": strategy.AS_THRESHOLD,
                "post_order_active_share": strategy.POST_AS_THRESHOLD,
                "official_active_share_minimum": rules.ACTIVE_SHARE_MIN,
                "official_active_share_top_n": rules.ACTIVE_SHARE_TOP_N,
                "cash_restore": strategy.CASH_RESTORE,
                "cash_cap": strategy.CASH_CAP,
                "price_buffer": strategy.PRICE_BUFFER,
                "veto_materiality": strategy.VETO_MATERIALITY,
                "veto_trend_max": strategy.VETO_TREND_MAX,
            },
        },
        "contest_limits": {
            "universe_size": rules.UNIVERSE_SIZE,
            "min_holdings": rules.MIN_HOLDINGS,
            "max_holdings": rules.MAX_HOLDINGS,
            "cash_max": rules.CASH_MAX,
            "tsmc_max_weight": rules.max_weight(rules.TSMC),
            "other_max_weight": rules.max_weight("0000"),
        },
        "shadow_model_policy": "研究用途；不得產生、覆寫或挑選正式交易委託",
    }
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {**body, "manifest_sha256": hashlib.sha256(encoded).hexdigest()}


def write_manifest(path: Path) -> dict:
    """寫出格式化的策略清單，並回傳相同內容供呼叫端封存。"""
    manifest = production_manifest()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest
