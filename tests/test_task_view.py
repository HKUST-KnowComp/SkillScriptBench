"""Task views expose selected frozen routes without executing their code."""
import hashlib
import json
from types import SimpleNamespace

import pytest

from ssbench.evaluation import EvaluationBundle, main


@pytest.fixture
def bundle(tmp_path):
    root = tmp_path / 'bundle'
    (root / 'objects').mkdir(parents=True)
    (root / 'bindings').mkdir()
    behavior_image = 'sha256:' + 'a' * 64
    document_image = 'sha256:' + 'b' * 64
    unrelated_image = 'sha256:' + 'c' * 64
    blobs = {
        'behavior/PLAN.json': '{}',
        'behavior/runtime/run_behavior.py':
            f'IMAGE = "{behavior_image}"\nBROWSER_IMAGE = "{unrelated_image}"\n'
            'BROWSER_BASE = "other"\nraise RuntimeError("must not execute")\n',
        'strict/PLAN.json': json.dumps({'tasks': {
            'fault': {'components': ['/old/doc']},
            'unrelated': {'components': ['/old/unused']}}}),
        'strict/runtime_overlay/doc/PLAN.json': json.dumps({'image': document_image}),
        'strict/runtime_overlay/doc/runtime/check.py': 'raise RuntimeError("must not execute")\n',
        'strict/runtime_overlay/unused/PLAN.json': json.dumps({'image': unrelated_image}),
    }
    objects, paths = {}, {}
    for name, text in blobs.items():
        data = text.encode()
        sha = hashlib.sha256(data).hexdigest()
        (root / 'objects' / sha).write_bytes(data)
        objects[sha] = {'file': 'objects/' + sha, 'bytes': len(data)}
        paths[name] = sha
    tasks = {}
    for tid, state in [('clean', 'clean'), ('fault', 'doc_fault')]:
        mapping = {'task_id': tid, 'state': state, 'base_id': 'base', 'source': '/old/strict'}
        if state == 'clean':
            mapping.update(behavior={'executor_root': '/old/behavior'},
                           document={'source': '/old/strict', 'doc_adapter_task_id': 'fault'})
        binding = root / 'bindings' / (tid + '.json')
        binding.write_text(json.dumps(mapping))
        tasks[tid] = {'task_id': tid, 'track': 'controlled', 'state': state,
                      'binding_status': 'frozen_map_bound', 'required_images': [],
                      'files': {}, 'file_sets': ['shared'], 'blockers': [],
                      'execution_ready': True, 'mapping_file': 'bindings/' + tid + '.json',
                      'mapping_sha256': hashlib.sha256(binding.read_bytes()).hexdigest()}
    manifest = {'schema': 'skillscriptbench-evaluation-bundle-v1',
                'historical_path_prefix': '/old/', 'objects': objects, 'paths': paths,
                'tasks': tasks, 'file_sets': {'shared': paths}, 'fixture_paths': [],
                'summary': {'tasks_with_frozen_bindings': 2, 'execution_ready_tasks': 2}}
    (root / 'BUNDLE.json').write_text(json.dumps(manifest))
    (root / 'DEPENDENCIES.json').write_text(json.dumps({'image_ids': [behavior_image, document_image, unrelated_image]}))
    return EvaluationBundle(root), behavior_image, document_image, unrelated_image


def test_view_resolves_selected_document_and_behavior_without_importing(bundle):
    evaluator, behavior, document, unrelated = bundle
    view = evaluator.task_view('clean', files=True)
    assert view['required_images'] == [behavior, document]
    assert unrelated not in view['image_sources']
    assert view['scoring_components'] == ['behavior', 'document']
    assert any(row['plan'] == 'strict/runtime_overlay/doc/PLAN.json' for row in view['components'])
    assert all('unused' not in row['root'] for row in view['components'])
    assert {row['logical_path'] for row in view['files']} == set(evaluator.manifest['paths'])
    assert evaluator.task_view('fault')['required_images'] == [document]
    assert evaluator.task_view('fault')['scoring_components'] == ['document']


def test_inspect_keeps_default_json_and_text_includes_readable_routes(bundle, capsys):
    evaluator, _, _, _ = bundle
    args = ['--bundle', str(evaluator.root), 'inspect', 'clean']
    main(args)
    assert json.loads(capsys.readouterr().out) == evaluator.inspect('clean')
    main(args + ['--format', 'text', '--files'])
    output = capsys.readouterr().out
    assert 'skillscriptbench evaluation document --job JOB' in output
    assert 'strict/runtime_overlay/doc/PLAN.json' in output
    assert str(evaluator.root / 'objects') in output
    assert 'Static inspection only' in output


def test_task_view_rejects_changed_mapping_and_objects(bundle):
    evaluator, _, _, _ = bundle
    path = evaluator.root / 'bindings/clean.json'
    original = path.read_bytes()
    path.write_text('{}')
    with pytest.raises(ValueError, match='mapping changed'):
        evaluator.task_view('clean')
    path.write_bytes(original)
    sha = evaluator.manifest['paths']['strict/runtime_overlay/doc/PLAN.json']
    (evaluator.root / evaluator.manifest['objects'][sha]['file']).write_text('{}')
    with pytest.raises(ValueError, match='object changed'):
        evaluator.task_view('clean')


def test_read_file_checks_membership_and_preserves_bytes(bundle, capsys):
    evaluator, _, _, _ = bundle
    main(['--bundle', str(evaluator.root), 'inspect', 'clean', '--read-file', 'behavior/PLAN.json'])
    assert capsys.readouterr().out == '{}'
    with pytest.raises(ValueError, match='not bound'):
        evaluator.read_file('clean', '../BUNDLE.json')


def test_legacy_default_image_and_unresolved_routes(bundle):
    evaluator, _, document, _ = bundle

    def bind(name, text):
        data = text.encode()
        sha = hashlib.sha256(data).hexdigest()
        (evaluator.root / 'objects' / sha).write_bytes(data)
        evaluator.manifest['objects'][sha] = {'file': 'objects/' + sha, 'bytes': len(data)}
        evaluator.manifest['paths'][name] = sha
        evaluator.manifest['file_sets']['shared'][name] = sha

    root = 'strict/runtime_overlay/doc'
    bind(root + '/PLAN.json', '{}')
    assert evaluator.task_view('fault')['unresolved_image_sources'] == [root]
    bind(root + '/runtime/doc_fault_noevo_probe.py', 'LEGACY = {"doc": "legacy_backend"}\n')
    bind(root + '/runtime/source_test_runner.py', f'IMAGE = "{document}"\n')
    view = evaluator.task_view('fault')
    assert not view['unresolved_image_sources']
    assert view['required_images'] == [document]


def test_doctor_selected_images_and_deduplication(bundle, monkeypatch):
    evaluator, behavior, document, unrelated = bundle
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout='27.0' if args[1] == 'info' else args[3])

    monkeypatch.setattr('ssbench.evaluation.subprocess.run', run)
    monkeypatch.setattr('ssbench.evaluation.importlib.util.find_spec', lambda name: object())
    result = evaluator.doctor(['clean', 'fault', 'clean'])
    assert result['task_ids'] == ['clean', 'fault']
    assert result['required_images'] == [behavior, document]
    assert result['runtime_available']
    assert [call[3] for call in calls if call[1] == 'image'] == [behavior, document]
    calls.clear()
    assert unrelated in evaluator.doctor()['required_images']


def test_doctor_rejects_unknown_task_before_docker(bundle, monkeypatch):
    evaluator, _, _, _ = bundle
    monkeypatch.setattr('ssbench.evaluation.subprocess.run', lambda *a, **k: pytest.fail('Docker must not run'))
    with pytest.raises(ValueError, match='unknown task'):
        evaluator.doctor(['missing'])


def test_doctor_cli_returns_nonzero_when_runtime_unavailable(monkeypatch, capsys):
    from ssbench import evaluation
    monkeypatch.setattr(evaluation, 'EvaluationBundle', lambda root: SimpleNamespace(
        doctor=lambda selected: {'runtime_available': False, 'remaining': ['missing image']}))
    assert evaluation.main(['--bundle', 'unused', 'doctor']) == 2
    assert 'missing image' in capsys.readouterr().out
