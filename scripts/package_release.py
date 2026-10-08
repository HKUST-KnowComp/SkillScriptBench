"""Create source and benchmark archives from an explicit publication allowlist."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = {'README.md', 'VERSIONS.md', 'pyproject.toml', 'NOTICE',
              'CONTRIBUTING.md', 'SECURITY.md', '.gitignore', 'CITATION.cff'}
ROOT_DIRS = {'ssbench', 'benchmark', 'code', 'results', 'docs', 'tests', 'scripts'}
SKIP = {'.git', '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache',
        'node_modules', '.venv', '.DS_Store', 'build', 'dist'}


def checksum(path):
    value = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def members():
    for path in sorted(ROOT.rglob('*')):
        rel = path.relative_to(ROOT)
        if rel.parts[0] not in ROOT_FILES | ROOT_DIRS:
            continue
        if any(p in SKIP or p.endswith('.egg-info') for p in rel.parts):
            continue
        if path.is_symlink():
            raise ValueError(f'symlink: {rel}')
        if not path.is_file() or path.suffix in {'.pyc', '.pyo'}:
            continue
        if any(p in {'.ssh', 'credentials'} or
               (p.startswith('.env') and p != '.env.example') for p in rel.parts):
            raise ValueError(f'excluded publication path: {rel}')
        if path.suffix.lower() in {'.pem', '.key', '.p12', '.pfx'}:
            raise ValueError(f'review key material before packaging: {rel}')
        yield path


def archive(output, selected):
    with zipfile.ZipFile(output, 'x', zipfile.ZIP_DEFLATED) as z:
        for path in selected:
            rel = path.relative_to(ROOT).as_posix()
            info = zipfile.ZipInfo(rel, date_time=(2026, 10, 7, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o100755 if path.stat().st_mode & 0o111 else 0o100644) << 16
            z.writestr(info, path.read_bytes())
    with zipfile.ZipFile(output) as z:
        if z.testzip() is not None:
            raise ValueError('archive CRC check failed')
    return {'file': output.name, 'files': len(selected), 'bytes': output.stat().st_size,
            'sha256': checksum(output)}


def evaluator_archive(output, root):
    sys.path.insert(0, str(ROOT))
    from ssbench.evaluation import EvaluationBundle
    bundle = EvaluationBundle(root)
    bundle.verify()
    if any(Path(p).name == 'RESULT.json' for p in bundle.manifest['paths']):
        raise ValueError('historical outcomes must not be packaged as evaluation assets')
    names = {'BUNDLE.json', 'DEPENDENCIES.json', 'ASSEMBLY_CHANGES.json'}
    names.update(record['file'] for record in bundle.manifest['objects'].values())
    names.update(task['mapping_file'] for task in bundle.manifest['tasks'].values())
    if (bundle.root / 'VALIDATION.json').is_file():
        names.add('VALIDATION.json')
    with tarfile.open(output, 'x:gz', compresslevel=3) as archive:
        for name in sorted(names):
            path = bundle.root / name
            if path.is_symlink() or not path.resolve().is_relative_to(bundle.root):
                raise ValueError('invalid evaluator publication path')
            info = archive.gettarinfo(str(path), arcname='evaluator/' + name)
            info.uid = info.gid = 0
            info.uname = info.gname = ''
            info.mode = 0o644
            info.mtime = 1791417600
            with path.open('rb') as source:
                archive.addfile(info, source)
    return {'file': output.name, 'files': len(names), 'bytes': output.stat().st_size,
            'sha256': checksum(output)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='new output directory')
    parser.add_argument('--evaluator', type=Path, help='separate evaluated asset bundle')
    parser.add_argument('--runtime-manifest', type=Path, help='verified runtime-part manifest; image parts stay separate')
    args = parser.parse_args()
    selected = list(members())
    args.output.mkdir(parents=True, exist_ok=False)
    records = [archive(args.output / 'SkillScriptBench_source.zip', selected),
               archive(args.output / 'SkillScriptBench_benchmark350.zip',
                       [p for p in selected if p.relative_to(ROOT).parts[0] == 'benchmark'])]
    if args.evaluator:
        records.append(evaluator_archive(args.output / 'SkillScriptBench_evaluator.tar.gz', args.evaluator))
    if args.runtime_manifest:
        data = json.loads(args.runtime_manifest.read_text())
        if data.get('schema') != 'skillscriptbench-runtime-parts-v1':
            raise ValueError('unsupported runtime manifest')
        target = args.output / 'RUNTIME_PARTS.json'
        shutil.copyfile(args.runtime_manifest, target)
        records.append({'file': target.name, 'bytes': target.stat().st_size,
                        'sha256': checksum(target), 'runtime_parts_included': False})
    (args.output / 'CHECKSUMS.json').write_text(json.dumps(records, indent=2) + '\n')
    print(json.dumps(records, indent=2))


if __name__ == '__main__':
    main()
