"""Portable task access. Loading and verification never execute package code."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import shutil

IGNORED = {'.git', '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache',
           'node_modules', '.DS_Store'}


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def safe_path(root, relative):
    rel = Path(relative)
    if rel.is_absolute() or '..' in rel.parts:
        raise ValueError('manifest path must stay inside benchmark root')
    path = root / rel
    if any(root.joinpath(*rel.parts[:i]).is_symlink() for i in range(1, len(rel.parts) + 1)):
        raise ValueError('symlink in task input path')
    if not path.resolve().is_relative_to(root):
        raise ValueError('task input outside benchmark root')
    return path


def package_files(root):
    for path in sorted(root.rglob('*')):
        if any(x in IGNORED for x in path.relative_to(root).parts):
            continue
        if path.is_symlink():
            raise ValueError('symlink in task package')
        if path.is_file() and path.suffix not in {'.pyc', '.pyo'}:
            yield path


def package_hash(root):
    hashes = {p.relative_to(root).as_posix(): sha256(p) for p in package_files(root)}
    data = json.dumps(hashes, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
    return hashlib.sha256(data.encode()).hexdigest()


@dataclass(frozen=True)
class Task:
    task_id: str
    request: Path
    package: Path
    expected_request_sha256: str
    expected_package_tree_hash: str

    def verify(self):
        if not self.package.is_dir() or not self.request.is_file():
            raise ValueError(f'{self.task_id}: missing input')
        if sha256(self.request) != self.expected_request_sha256:
            raise ValueError(f'{self.task_id}: request hash mismatch')
        if package_hash(self.package) != self.expected_package_tree_hash:
            raise ValueError(f'{self.task_id}: package hash mismatch')
        return True

    def materialize(self, destination):
        """Copy only the request and input package to a new task workspace."""
        self.verify()
        destination = Path(destination).resolve()
        if destination.is_relative_to(self.package) or self.package.is_relative_to(destination):
            raise ValueError('workspace must not overlap the source package')
        destination.mkdir(parents=True, exist_ok=False)
        shutil.copy2(self.request, destination / 'REQUEST.md')
        for source in package_files(self.package):
            target = destination / 'package' / source.relative_to(self.package)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        return destination


class Benchmark:
    def __init__(self, root='benchmark'):
        self.root = Path(root).resolve()
        rows = json.loads((self.root / 'TASKS.json').read_text())['tasks']
        self._tasks = {}
        for row in rows:
            tid = row['task_id']
            if tid in self._tasks:
                raise ValueError(f'duplicate task: {tid}')
            self._tasks[tid] = Task(tid, safe_path(self.root, row['request']),
                                    safe_path(self.root, row['package']),
                                    row['expected_request_sha256'], row['expected_package_tree_hash'])

    def task(self, task_id):
        try:
            return self._tasks[task_id]
        except KeyError:
            raise ValueError(f'unknown task: {task_id}') from None

    def tasks(self, split='all'):
        if split not in {'all', 'main_results', 'clean'}:
            raise ValueError(f'unknown split: {split}')
        ids = json.loads((self.root / 'splits' / f'{split}.json').read_text())['task_ids']
        if len(ids) != len(set(ids)):
            raise ValueError('duplicate task in split')
        return tuple(self.task(tid) for tid in ids)

    def verify(self, split='all'):
        tasks = self.tasks(split)
        for task in tasks:
            task.verify()
        return {'split': split, 'verified_tasks': len(tasks), 'status': 'ok'}

    def sources(self):
        """Read upstream attribution separately from method-visible inputs."""
        rows = json.loads((self.root / 'SOURCES.json').read_text())['tasks']
        indexed = {row['task_id']: row for row in rows}
        if len(indexed) != len(rows) or set(indexed) != set(self._tasks):
            raise ValueError('source inventory must cover each task exactly once')
        return indexed

    def verify_sources(self):
        sources = self.sources()
        notices = set()
        for tid, row in sources.items():
            for key in ('source_repository', 'source_commit', 'license'):
                if not row.get(key):
                    raise ValueError(f'{tid}: missing source field {key}')
            if row.get('license_file'):
                notice = safe_path(self.root, row['license_file'])
                if sha256(notice) != row.get('license_sha256'):
                    raise ValueError(f'{tid}: license notice hash mismatch')
                notices.add(row['license_file'])
        metadata_rows = json.loads((self.root / 'metadata.json').read_text())['tasks']
        metadata = {row['task_id']: row for row in metadata_rows}
        if len(metadata) != len(metadata_rows) or set(metadata) != set(self._tasks):
            raise ValueError('metadata must cover each task exactly once')
        controlled = {tid for tid, row in metadata.items() if row['track'] == 'controlled'}
        groups = json.loads((self.root / 'CONTROLLED_GROUPS.json').read_text())['groups']
        seen = set()
        bases = set()
        expected = {'clean', 'doc_fault', 'script_fault', 'joint_fault'}
        for group in groups:
            if group['base_id'] in bases or set(group['tasks']) != expected:
                raise ValueError('Controlled groups must have unique base IDs and four states')
            bases.add(group['base_id'])
            for state, tid in group['tasks'].items():
                if tid in seen or tid not in controlled:
                    raise ValueError('duplicate or unknown Controlled task')
                seen.add(tid)
                meta = metadata[tid]
                if (meta['state'], meta['base_id']) != (state, group['base_id']):
                    raise ValueError(f'{tid}: group metadata disagreement')
                if not sources[tid].get('license_file'):
                    raise ValueError(f'{tid}: missing Controlled license notice')
                for key in ('source_repository', 'source_commit'):
                    if group[key] != sources[tid][key]:
                        raise ValueError(f'{tid}: group source disagreement')
        if seen != controlled:
            raise ValueError('Controlled group inventory is incomplete')
        return {'status': 'ok', 'attributed_tasks': len(sources),
                'controlled_tasks': len(controlled), 'controlled_groups': len(groups),
                'verified_license_files': len(notices)}
