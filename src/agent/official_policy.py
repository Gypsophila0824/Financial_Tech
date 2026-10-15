"""Explicit release gate; simulation assumptions are never official confirmation."""
import json
from pathlib import Path

POLICY = Path(__file__).resolve().parents[2] / 'config' / 'official_policy.json'


def require_formal_release(path=POLICY):
    policy = json.loads(path.read_text(encoding='utf-8'))
    if (policy.get('schema') != 'official-policy-v1' or policy.get('pending')
            or policy.get('formal_release_approved') is not True
            or not isinstance(policy.get('approval_evidence'), dict)):
        raise ValueError('正式使用尚未核准；請先確認 config/official_policy.json 的規則與功能待辦，不得以研究測試通過代替')
    evidence = policy['approval_evidence']
    if not all(evidence.get(k) for k in ('official_sources', 'reviewer', 'reviewed_at', 'validation_report')):
        raise ValueError('正式核准缺少官方來源、審核人、日期或驗收報告')
    return policy
