"""Fetch checksum-pinned evaluator and runtime assets for selected tasks."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ssbench.evaluation import EvaluationBundle


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def asset_record(record, release=None):
    record = dict(record)
    if release is not None:
        record['release'] = release
    name = record['file']
    if (not isinstance(name, str) or Path(name).name != name or name in {'.', '..'}
            or '/' in name or '\\' in name or '*' in name or '?' in name or '[' in name):
        raise ValueError('asset file must be a literal basename')
    if (type(record['bytes']) is not int or record['bytes'] < 0
            or not re.fullmatch(r'[0-9a-f]{64}', record['sha256'])):
        raise ValueError('invalid asset size or SHA-256: ' + name)
    if not isinstance(record['release'], str) or not record['release'] or record['release'].startswith('-'):
        raise ValueError('invalid asset release: ' + name)
    return record


def installed_images(images):
    if not images:
        return []
    try:
        result = subprocess.run(['docker', 'image', 'inspect', *images, '--format', '{{.Id}}'],
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return []
    observed = set(result.stdout.splitlines())
    return sorted(set(images) & observed)


def make_plan(catalog, task_ids, ignore_installed=False):
    if catalog.get('schema') != 'skillscriptbench-assets-v1':
        raise ValueError('unsupported asset catalog schema')
    repo = catalog['repo']
    if not isinstance(repo, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
        raise ValueError('invalid catalog repository')
    task_ids = sorted(set(task_ids))
    if not task_ids:
        raise ValueError('no tasks selected')
    required = set()
    for task_id in task_ids:
        if task_id not in catalog['tasks']:
            raise ValueError('unknown task: ' + task_id)
        task = catalog['tasks'][task_id]
        if task.get('unresolved'):
            raise ValueError('unresolved runtime routes for task: ' + task_id)
        required.update(task['images'])
    if any(not re.fullmatch(r'sha256:[0-9a-f]{64}', image) for image in required):
        raise ValueError('catalog must specify full pinned image IDs')
    required = sorted(required)
    installed = [] if ignore_installed else installed_images(required)
    missing = sorted(set(required) - set(installed))
    assets = [asset_record(catalog['evaluator'])]
    if not missing:
        mode = 'none'
    elif all(image in catalog['images'] for image in missing):
        mode = 'independent'
        assets.extend(asset_record(catalog['images'][image]) for image in missing)
    else:
        mode = 'bulk'
        bulk = catalog['bulk']
        if not set(missing).issubset(bulk['runtime']['images']):
            raise ValueError('bulk archive does not cover selected image IDs')
        assets.append(asset_record(bulk['manifest'], bulk['release']))
        assets.extend(asset_record(part, bulk['release']) for part in bulk['runtime']['parts'])
    names = [asset['file'] for asset in assets]
    if len(names) != len(set(names)):
        raise ValueError('asset names must be unique in the download plan')
    return {'repo': repo, 'tasks': task_ids, 'required_images': required,
            'installed_images': installed, 'missing_images': missing, 'mode': mode,
            'assets': assets, 'maximum_download_bytes': sum(asset['bytes'] for asset in assets),
            'download_size_note': 'Maximum before reusing checksum-verified cached files.'}


def verify_asset(path, record):
    if (path.is_symlink() or not path.is_file() or path.stat().st_size != record['bytes']
            or sha256(path) != record['sha256']):
        raise ValueError('cached/downloaded asset failed size or SHA-256 verification: ' + str(path))


def fetch_asset(repo, record, destination):
    target = destination / record['file']
    if target.exists() or target.is_symlink():
        verify_asset(target, record)
        print('Using verified cache: ' + str(target), flush=True)
        return target
    with tempfile.TemporaryDirectory(prefix='.download-', dir=destination) as temporary:
        result = subprocess.run([
            'gh', 'release', 'download', record['release'], '--repo', repo,
            '--pattern', record['file'], '--dir', temporary,
        ], capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError('gh release download failed for ' + record['file']
                               + '; check repository access with gh auth status')
        downloaded = Path(temporary) / record['file']
        verify_asset(downloaded, record)
        # Publish the complete verified file atomically, refusing a concurrent overwrite.
        os.link(downloaded, target)
    print('Downloaded and verified: ' + str(target), flush=True)
    return target


def verify_evaluator(path, expected_hash):
    manifest = path / 'BUNDLE.json'
    if path.is_symlink() or manifest.is_symlink() or sha256(manifest) != expected_hash:
        raise ValueError('evaluator BUNDLE.json does not match the catalog')
    EvaluationBundle(path).verify()


def install_evaluator(archive, destination, expected_hash):
    target = destination / 'evaluator'
    if target.exists() or target.is_symlink():
        verify_evaluator(target, expected_hash)
        return target
    with tempfile.TemporaryDirectory(prefix='.extract-', dir=destination) as temporary:
        extracted = Path(temporary) / 'contents'
        extracted.mkdir()
        with tarfile.open(archive, 'r:gz') as source:
            source.extractall(extracted, filter='data')
        candidate = extracted / 'evaluator' if (extracted / 'evaluator').is_dir() else extracted
        verify_evaluator(candidate, expected_hash)
        if target.exists() or target.is_symlink():
            raise ValueError('evaluator destination appeared during extraction; refusing overwrite')
        candidate.rename(target)
    return target


def check_docker_platform():
    result = subprocess.run(['docker', 'info', '--format', '{{.OSType}}/{{.Architecture}}'],
                            capture_output=True, text=True, timeout=30)
    if result.returncode or result.stdout.strip() not in {'linux/amd64', 'linux/x86_64'}:
        raise ValueError('--load requires a reachable Linux amd64 Docker engine')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument('--demo', action='store_true', help='fetch the two installation-example tasks')
    selection.add_argument('--task', action='append', help='task ID; repeat to select several tasks')
    selection.add_argument('--all', action='store_true', help='fetch assets for all tasks')
    parser.add_argument('--catalog', type=Path, default=ROOT / 'distribution' / 'assets.json')
    parser.add_argument('--dest', type=Path, default=Path('assets'))
    parser.add_argument('--plan', action='store_true', help='show the offline plan without writing files')
    parser.add_argument('--load', action='store_true', help='load runtimes into Linux amd64 Docker')
    parser.add_argument('--ignore-installed', action='store_true', help='fetch even if exact images exist in Docker')
    args = parser.parse_args(argv)
    try:
        catalog = json.loads(args.catalog.read_text())
        tasks = catalog['demo'] if args.demo else list(catalog['tasks']) if args.all else args.task
        plan = make_plan(catalog, tasks, args.ignore_installed)
        if args.plan:
            print(json.dumps(plan, indent=2), flush=True)
            return 0
        destination = args.dest.resolve()
        for protected in (ROOT / 'benchmark', ROOT / 'distribution'):
            protected = protected.resolve()
            if destination.is_relative_to(protected) or protected.is_relative_to(destination):
                raise ValueError('--dest must not overlap benchmark or distribution source trees')
        print(f"Selected {len(plan['tasks'])} tasks; {len(plan['missing_images'])} missing images; "
              f"mode={plan['mode']}; maximum download {plan['maximum_download_bytes'] / 1e9:.3f} GB "
              '(before verified cache reuse).', flush=True)
        if args.load:
            check_docker_platform()
        destination.mkdir(parents=True, exist_ok=True)
        existing = destination / 'evaluator'
        if existing.exists() or existing.is_symlink():
            verify_evaluator(existing, catalog['evaluator']['bundle_sha256'])
        downloads = {record['file']: fetch_asset(plan['repo'], record, destination)
                     for record in plan['assets']}
        evaluator = install_evaluator(downloads[catalog['evaluator']['file']], destination,
                                      catalog['evaluator']['bundle_sha256'])
        if plan['mode'] == 'bulk':
            manifest = downloads[catalog['bulk']['manifest']['file']]
            if json.loads(manifest.read_text()) != catalog['bulk']['runtime']:
                raise ValueError('runtime manifest content does not match the catalog')
            command = [sys.executable, '-B', str(ROOT / 'scripts' / 'load_runtimes.py'),
                       '--manifest', str(manifest)]
            if args.load:
                command.append('--load')
            subprocess.run(command, check=True)
        elif args.load:
            for image in plan['missing_images']:
                archive = downloads[catalog['images'][image]['file']]
                result = subprocess.run(['docker', 'image', 'load', '--input', str(archive)],
                                        capture_output=True, text=True)
                if result.returncode:
                    raise RuntimeError('Docker could not load runtime asset: ' + archive.name)
        if args.load and installed_images(plan['required_images']) != plan['required_images']:
            raise ValueError('selected pinned image IDs are still missing after Docker load')
        print('Verified evaluator: ' + str(evaluator))
        print('Selected runtimes loaded and verified.' if args.load else
              'Assets verified; use --load to import runtimes into Docker.')
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, tarfile.TarError) as exc:
        parser.exit(2, f'error: {exc}\n')
    except RuntimeError as exc:
        parser.exit(2, f'error: {exc}\n')


if __name__ == '__main__':
    raise SystemExit(main())
