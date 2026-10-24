import copy
import json
import zipfile

import pytest
from jsonschema import Draft202012Validator

from src.agent import codeversion, rules
from src.agent.compliance import SCHEMA_PATH


def schema():
    return json.loads(SCHEMA_PATH.read_text(encoding='utf-8'))


@pytest.mark.parametrize('layer', ['orders', 'decisions'])
def test_full_rotation_fits_sixty_entries_but_not_sixty_one(layer):
    s = schema()
    example = json.loads((SCHEMA_PATH.parent / 'D-Plan_TEAM_042_2026-10-28.json')
                         .read_text(encoding='utf-8'))[layer][0]
    validator = Draft202012Validator({'$defs': s['$defs'], **s['properties'][layer]})
    validator.validate([copy.deepcopy(example) for _ in range(60)])
    assert any(e.validator == 'maxItems' for e in validator.iter_errors([example] * 61))
    assert rules.LAYER_LIMITS[layer] == s['properties'][layer]['maxItems']


@pytest.mark.parametrize('node,field,prefix,limit', [
    ('observation', 'source_ref', 'S', 10), ('decision', 'inference_refs', 'I', 8)])
def test_expanded_reference_limits_preserve_upper_boundary(node, field, prefix, limit):
    validator = Draft202012Validator(schema()['$defs'][node]['properties'][field])
    validator.validate([f'{prefix}{i}' for i in range(1, limit + 1)])
    assert any(e.validator == 'maxItems' for e in
               validator.iter_errors([f'{prefix}{i}' for i in range(1, limit + 2)]))


def test_generated_plan_and_archived_validator_use_same_schema(mock_run):
    assert all(day.plan['schema_version'] == '4.3' for _, _, day in mock_run.days)
    validator = Draft202012Validator(schema())
    for _, _, day in mock_run.days:
        validator.validate(day.plan)
    version = codeversion.current()
    with zipfile.ZipFile(codeversion.ARCHIVE_DIR / f"{version.replace(':', '_')}.zip") as z:
        assert z.read(rules.SCHEMA_RELATIVE_PATH) == SCHEMA_PATH.read_bytes()


def test_version_pattern_is_four_x_not_exact_four_three():
    validator = Draft202012Validator(schema()['properties']['schema_version'])
    for version in ['4.2', '4.3']:
        validator.validate(version)
    assert not validator.is_valid('5.0')
    assert not validator.is_valid(4.3)
