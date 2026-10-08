import csv
import json

import pytest

from examples.check_installation import ROOT, prepare_manifest
from examples.check_results import EXPECTED, check_results


def test_example_manifest_uses_verified_originals_and_refuses_overwrite(tmp_path):
    output = tmp_path / 'demo'
    manifest = prepare_manifest(output)
    rows = json.loads(manifest.read_text())
    assert {row['task_id'] for row in rows} == set(EXPECTED)
    assert all(row['candidate'] == str(ROOT / 'benchmark' / 'tasks' / row['task_id'] / 'package')
               and row['run'] == 1 for row in rows)
    before = manifest.read_bytes()
    with pytest.raises(FileExistsError):
        prepare_manifest(output)
    assert manifest.read_bytes() == before
    with pytest.raises(ValueError, match='overlap'):
        prepare_manifest(ROOT / 'benchmark' / 'example-output')


@pytest.mark.parametrize('fault_status', ['pass', 'error', 'fail'])
def test_example_checker_requires_expected_completed_outcomes(tmp_path, fault_status):
    scores = tmp_path / 'scores.csv'
    with scores.open('w', newline='') as file:
        writer = csv.DictWriter(file, fieldnames=['task_id', 'run', 'state', 'status'])
        writer.writeheader()
        for task_id, (state, expected) in EXPECTED.items():
            writer.writerow({'task_id': task_id, 'run': 1, 'state': state,
                             'status': fault_status if state == 'script_fault' else expected})
    if fault_status == 'fail':
        assert len(check_results(scores)) == 2
    else:
        with pytest.raises(ValueError, match='expected run 1'):
            check_results(scores)
