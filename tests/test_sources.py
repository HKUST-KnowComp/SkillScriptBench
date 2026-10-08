"""Source inventories stay separate from task inputs and remain verifiable."""
import hashlib
import json
from pathlib import Path

import pytest

from ssbench import Benchmark


@pytest.fixture
def source_benchmark(tmp_path):
    states = ('clean', 'doc_fault', 'script_fault', 'joint_fault')
    notice = tmp_path / 'licenses/source.txt'
    notice.parent.mkdir()
    notice.write_text('Original source notice\n')
    sha = hashlib.sha256(notice.read_bytes()).hexdigest()
    origin = {'source_repository': 'https://github.com/example/package',
              'source_commit': 'a' * 40, 'license': 'MIT'}
    tasks = [{'task_id': state, 'request': f'tasks/{state}/REQUEST.md',
              'package': f'tasks/{state}/package', 'expected_request_sha256': '',
              'expected_package_tree_hash': ''} for state in states]
    sources = [{'task_id': state, **origin, 'license_file': 'licenses/source.txt',
                'license_sha256': sha} for state in states]
    metadata = [{'task_id': state, 'track': 'controlled', 'state': state,
                 'base_id': 'base'} for state in states]
    groups = [{'base_id': 'base', **origin, 'tasks': {s: s for s in states}}]
    for name, content in [('TASKS.json', {'tasks': tasks}),
                          ('SOURCES.json', {'tasks': sources}),
                          ('metadata.json', {'tasks': metadata}),
                          ('CONTROLLED_GROUPS.json', {'groups': groups})]:
        (tmp_path / name).write_text(json.dumps(content))
    return Benchmark(tmp_path)


def test_sources_and_group_mapping(source_benchmark):
    result = source_benchmark.verify_sources()
    assert result['attributed_tasks'] == 4
    assert result['controlled_groups'] == 1
    assert result['verified_license_files'] == 1


def test_modified_notice_rejected(source_benchmark):
    (source_benchmark.root / 'licenses/source.txt').write_text('changed')
    with pytest.raises(ValueError, match='license notice hash mismatch'):
        source_benchmark.verify_sources()


def test_missing_source_rejected(source_benchmark):
    path = source_benchmark.root / 'SOURCES.json'
    data = json.loads(path.read_text())
    data['tasks'].pop()
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='cover each task exactly once'):
        source_benchmark.verify_sources()


def test_incorrect_group_source_rejected(source_benchmark):
    path = source_benchmark.root / 'CONTROLLED_GROUPS.json'
    data = json.loads(path.read_text())
    data['groups'][0]['source_commit'] = 'b' * 40
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='group source disagreement'):
        source_benchmark.verify_sources()
