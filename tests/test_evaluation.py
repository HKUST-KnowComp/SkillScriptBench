import hashlib
import json

import pytest

from ssbench.evaluation import EvaluationBundle


@pytest.fixture
def fixture_bundle(tmp_path):
    root = tmp_path / 'bundle'
    root.mkdir()
    objects, paths = {}, {}
    for name, text in [('runtime/fixture.py', 'VALUE = 1\n'),
                       ('PLAN.json', '{}'), ('RESULT.json', '{"status":"pass"}')]:
        data = text.encode()
        sha = hashlib.sha256(data).hexdigest()
        path = root / 'objects' / sha
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(data)
        objects[sha] = {'file': 'objects/' + sha, 'bytes': len(data)}
        paths['frozen/' + name] = sha
    binding = root / 'bindings/toy.json'
    binding.parent.mkdir()
    binding.write_text('{"kind":"test_fixture_only"}')
    task = {'task_id': 'toy', 'track': 'controlled', 'state': 'clean', 'base_id': 'base',
            'binding_status': 'frozen_map_bound', 'files': paths,
            'mapping_file': 'bindings/toy.json',
            'mapping_sha256': hashlib.sha256(binding.read_bytes()).hexdigest(),
            'required_images': [], 'execution_ready': False, 'blockers': ['fixture_only']}
    manifest = {'schema': 'skillscriptbench-evaluation-bundle-v1',
                'summary': {'tasks_with_frozen_bindings': 1},
                'objects': objects, 'paths': paths, 'tasks': {'toy': task},
                'fixture_paths': ['frozen/runtime/fixture.py']}
    (root / 'BUNDLE.json').write_text(json.dumps(manifest))
    package = tmp_path / 'candidate'
    package.mkdir()
    (package / 'SKILL.md').write_text('# Toy\n')
    return EvaluationBundle(root), package


def test_prepare_preserves_separation_and_does_not_reuse_scores(fixture_bundle, tmp_path):
    bundle, package = fixture_bundle
    assert bundle.verify()['check'] == 'asset_integrity_only'
    output = tmp_path / 'job'
    plan = bundle.prepare('toy', package, output)
    assert plan['status'] == 'prepared_not_executed'
    assert not plan['execution_ready'] and not plan['historical_scores_used']
    assert sorted(p.name for p in (output / 'candidate').iterdir()) == ['SKILL.md']
    assert (output / 'evaluator/frozen/runtime/fixture.py').exists()
    assert not (output / 'evaluator/frozen/RESULT.json').exists()


def test_changed_object_rejected(fixture_bundle, tmp_path):
    bundle, package = fixture_bundle
    record = next(iter(bundle.manifest['objects'].values()))
    (bundle.root / record['file']).write_text('modified')
    with pytest.raises(ValueError, match='object changed'):
        bundle.prepare('toy', package, tmp_path / 'job')
    assert not (tmp_path / 'job').exists()


def test_changed_mapping_rejected(fixture_bundle):
    bundle, _ = fixture_bundle
    (bundle.root / 'bindings/toy.json').write_text('{}')
    with pytest.raises(ValueError, match='mapping changed'):
        bundle.verify()


def test_unbound_task_cannot_prepare(fixture_bundle, tmp_path):
    bundle, package = fixture_bundle
    bundle.manifest['tasks']['toy']['binding_status'] = 'not_bound'
    with pytest.raises(ValueError, match='no frozen evaluator binding'):
        bundle.prepare('toy', package, tmp_path / 'job')
    assert not (tmp_path / 'job').exists()


def test_worker_rejects_candidate_drift_before_docker(fixture_bundle, tmp_path, monkeypatch):
    from ssbench.evaluation_worker import execute
    bundle, package = fixture_bundle
    job = tmp_path / 'job'
    bundle.prepare('toy', package, job)
    (job / 'candidate/SKILL.md').write_text('changed')
    monkeypatch.setattr('ssbench.evaluation_worker.subprocess.run',
                        lambda *a, **k: pytest.fail('Docker must not run on changed inputs'))
    with pytest.raises(ValueError, match='candidate changed'):
        execute(job)


def test_worker_rejects_evaluator_drift_before_docker(fixture_bundle, tmp_path, monkeypatch):
    from ssbench.evaluation_worker import execute
    bundle, package = fixture_bundle
    job = tmp_path / 'job'
    bundle.prepare('toy', package, job)
    (job / 'evaluator/frozen/runtime/fixture.py').write_text('changed')
    monkeypatch.setattr('ssbench.evaluation_worker.subprocess.run',
                        lambda *a, **k: pytest.fail('Docker must not run on changed assets'))
    with pytest.raises(ValueError, match='evaluator asset changed'):
        execute(job)


def test_runtime_links_keep_original_relative_layout(fixture_bundle, tmp_path):
    bundle, package = fixture_bundle
    bundle.manifest['tasks']['toy']['symlinks'] = {'frozen/bin/tool': '../runtime/fixture.py'}
    job = tmp_path / 'job'
    bundle.prepare('toy', package, job)
    link = job / 'evaluator/frozen/bin/tool'
    assert link.is_symlink()
    assert str(link.readlink()) == '../runtime/fixture.py'
    assert link.read_text() == 'VALUE = 1\n'


def test_path_relocation_is_idempotent(tmp_path):
    from ssbench.evaluation_worker import relocate
    job = tmp_path / 'job'
    plan = {'historical_path_prefix': '/historical/'}
    first = relocate(job, plan, '/historical/runtime/file')
    assert relocate(job, plan, str(first)) == first
