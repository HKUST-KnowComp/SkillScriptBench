import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import fetch_assets


IMAGE_A = 'sha256:' + 'a' * 64
IMAGE_B = 'sha256:' + 'b' * 64


def record(name, payload=b'asset'):
    return {'file': name, 'bytes': len(payload), 'sha256': hashlib.sha256(payload).hexdigest(),
            'release': 'test-release'}


def catalog():
    return {'schema': 'skillscriptbench-assets-v1', 'repo': 'example/benchmark',
            'evaluator': record('evaluator.tar.gz') | {'bundle_sha256': 'c' * 64},
            'images': {IMAGE_A: record('image-a.tar.gz')},
            'tasks': {'a': {'images': [IMAGE_A], 'unresolved': []},
                      'b': {'images': [IMAGE_B], 'unresolved': []}},
            'demo': ['a'],
            'bulk': {'release': 'test-release', 'manifest': record('RUNTIME_PARTS.json'),
                     'runtime': {'images': [IMAGE_A, IMAGE_B],
                                 'parts': [record('runtime.part00'), record('runtime.part01')]}}}


def test_task_plan_uses_independent_images_then_explicit_bulk(monkeypatch):
    monkeypatch.setattr(fetch_assets, 'installed_images', lambda images: [])
    data = catalog()
    demo = fetch_assets.make_plan(data, ['a', 'a'])
    assert demo['tasks'] == ['a']
    assert demo['mode'] == 'independent'
    assert [asset['file'] for asset in demo['assets']] == ['evaluator.tar.gz', 'image-a.tar.gz']
    bulk = fetch_assets.make_plan(data, ['a', 'b'])
    assert bulk['mode'] == 'bulk'
    assert [asset['file'] for asset in bulk['assets']] == [
        'evaluator.tar.gz', 'RUNTIME_PARTS.json', 'runtime.part00', 'runtime.part01']
    assert bulk['maximum_download_bytes'] == 20


def test_exact_installed_images_avoid_runtime_download(monkeypatch):
    monkeypatch.setattr(fetch_assets, 'installed_images', lambda images: sorted(images))
    plan = fetch_assets.make_plan(catalog(), ['b'])
    assert plan['mode'] == 'none'
    assert plan['missing_images'] == []
    assert len(plan['assets']) == 1
    assert fetch_assets.make_plan(catalog(), ['b'], ignore_installed=True)['mode'] == 'bulk'


def test_plan_creates_nothing_and_requires_no_gh(tmp_path, monkeypatch, capsys):
    path = tmp_path / 'catalog.json'
    path.write_text(json.dumps(catalog()))
    monkeypatch.setattr(fetch_assets, 'installed_images', lambda images: [])
    monkeypatch.setattr(fetch_assets.subprocess, 'run',
                        lambda *a, **k: pytest.fail('offline plan invoked a subprocess'))
    destination = tmp_path / 'not-created'
    assert fetch_assets.main(['--demo', '--plan', '--catalog', str(path),
                              '--dest', str(destination)]) == 0
    assert not destination.exists()
    assert json.loads(capsys.readouterr().out)['mode'] == 'independent'


def test_unknown_and_unresolved_tasks_fail_before_download(monkeypatch):
    monkeypatch.setattr(fetch_assets, 'installed_images', lambda images: [])
    with pytest.raises(ValueError, match='unknown task'):
        fetch_assets.make_plan(catalog(), ['unknown'])
    data = catalog()
    data['tasks']['a']['unresolved'] = ['route']
    with pytest.raises(ValueError, match='unresolved'):
        fetch_assets.make_plan(data, ['a'])


@pytest.mark.parametrize('relative', ['.', 'benchmark/tasks/package', 'distribution/downloads'])
def test_destination_cannot_overlap_source_trees(tmp_path, monkeypatch, relative, capsys):
    root = tmp_path / 'repo'
    path = tmp_path / 'catalog.json'
    path.write_text(json.dumps(catalog()))
    monkeypatch.setattr(fetch_assets, 'ROOT', root)
    monkeypatch.setattr(fetch_assets, 'installed_images', lambda images: [])
    monkeypatch.setattr(fetch_assets, 'fetch_asset',
                        lambda *a, **k: pytest.fail('unsafe destination reached download'))
    with pytest.raises(SystemExit) as error:
        fetch_assets.main(['--demo', '--catalog', str(path), '--dest', str(root / relative)])
    assert error.value.code == 2
    assert 'must not overlap' in capsys.readouterr().err
    assert not root.exists()


def test_download_is_verified_before_cache_publication(tmp_path, monkeypatch):
    asset = record('image.tar.gz', b'correct')
    payload = b'corrupt'

    def download(command, **kwargs):
        assert kwargs['capture_output'] is True
        (Path(command[command.index('--dir') + 1]) / asset['file']).write_bytes(payload)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(fetch_assets.subprocess, 'run', download)
    with pytest.raises(ValueError, match='verification'):
        fetch_assets.fetch_asset('example/repo', asset, tmp_path)
    assert not (tmp_path / asset['file']).exists()
    assert not list(tmp_path.iterdir())
    payload = b'correct'
    cached = fetch_assets.fetch_asset('example/repo', asset, tmp_path)
    assert cached.read_bytes() == payload
    assert list(tmp_path.iterdir()) == [cached]


def test_matching_cache_reused_and_invalid_cache_preserved(tmp_path, monkeypatch):
    asset = record('image.tar.gz')
    target = tmp_path / asset['file']
    target.write_bytes(b'asset')
    monkeypatch.setattr(fetch_assets.subprocess, 'run',
                        lambda *a, **k: pytest.fail('cache reuse invoked a download'))
    assert fetch_assets.fetch_asset('example/repo', asset, tmp_path) == target
    target.write_bytes(b'bad')
    with pytest.raises(ValueError, match='verification'):
        fetch_assets.fetch_asset('example/repo', asset, tmp_path)
    assert target.read_bytes() == b'bad'


def test_existing_evaluator_requires_pinned_manifest(tmp_path, monkeypatch):
    target = tmp_path / 'evaluator'
    target.mkdir()
    (target / 'BUNDLE.json').write_bytes(b'changed')
    monkeypatch.setattr(fetch_assets, 'EvaluationBundle',
                        lambda path: pytest.fail('untrusted manifest reached bundle verification'))
    with pytest.raises(ValueError, match='BUNDLE.json'):
        fetch_assets.install_evaluator(tmp_path / 'unused.tar.gz', tmp_path, '0' * 64)
    assert (target / 'BUNDLE.json').read_bytes() == b'changed'


def test_load_requires_linux_amd64_engine(monkeypatch):
    monkeypatch.setattr(fetch_assets.subprocess, 'run',
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout='linux/arm64\n'))
    with pytest.raises(ValueError, match='Linux amd64'):
        fetch_assets.check_docker_platform()
