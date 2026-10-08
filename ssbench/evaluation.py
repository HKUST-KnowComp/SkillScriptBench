"""Inspect and materialize frozen evaluator assets without executing package code."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys

from .benchmark import package_files, package_hash, safe_path


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class EvaluationBundle:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.manifest = json.loads((self.root / 'BUNDLE.json').read_text())
        if self.manifest.get('schema') != 'skillscriptbench-evaluation-bundle-v1':
            raise ValueError('unsupported evaluation bundle schema')

    def task(self, task_id):
        if task_id not in self.manifest['tasks']:
            raise ValueError(f'unknown task: {task_id}')
        task = self.manifest['tasks'][task_id]
        if not task.get('file_sets'):
            return task
        files = dict(task['files'])
        for key in task['file_sets']:
            for path, sha in self.manifest['file_sets'][key].items():
                if path in files and files[path] != sha:
                    raise ValueError('conflicting task file sets')
                files[path] = sha
        return dict(task, files=files)

    def _object(self, sha):
        record = self.manifest['objects'].get(sha)
        if not record:
            raise ValueError('unknown frozen object')
        path = safe_path(self.root, record['file'])
        if path.stat().st_size != record['bytes'] or digest(path) != sha:
            raise ValueError('frozen evaluator object changed')
        return path

    def verify(self):
        for sha in self.manifest['objects']:
            self._object(sha)
        for logical, sha in self.manifest['paths'].items():
            safe_path(self.root, 'layout/' + logical)
            if Path(logical).is_absolute() or sha not in self.manifest['objects']:
                raise ValueError('invalid logical file binding')
        for tid in self.manifest['tasks']:
            task = self.task(tid)
            if task['task_id'] != tid:
                raise ValueError('task identity disagreement')
            for path, sha in task['files'].items():
                if self.manifest['paths'].get(path) != sha:
                    raise ValueError('task/file binding disagreement')
            if task.get('mapping_file'):
                if digest(safe_path(self.root, task['mapping_file'])) != task['mapping_sha256']:
                    raise ValueError('frozen task mapping changed')
        for path in self.manifest['fixture_paths']:
            if path not in self.manifest['paths']:
                raise ValueError('unresolved fixture binding')
        return {'status': 'ok', **self.manifest['summary'], 'check': 'asset_integrity_only'}

    def inspect(self, task_id):
        task = self.task(task_id)
        return {k: v for k, v in task.items() if k not in {'files', 'mapping_file'}} | {
            'bound_files': len(task['files'])}

    def prepare(self, task_id, candidate, output):
        task = self.task(task_id)
        if task['binding_status'] != 'frozen_map_bound':
            raise ValueError('no frozen evaluator binding for this task')
        candidate = Path(candidate).resolve()
        output = Path(output).resolve()
        if not candidate.is_dir() or not (candidate / 'SKILL.md').is_file():
            raise ValueError('candidate must be a package directory containing SKILL.md')
        for source in (candidate, self.root):
            if output.is_relative_to(source) or source.is_relative_to(output):
                raise ValueError('output must not overlap candidate or evaluation bundle')
        files = dict(task['files'])
        # Include only fixtures in this task's frozen runtime directories.
        parents = {str(Path(p).parent) for p in files}
        for path in self.manifest['fixture_paths']:
            if str(Path(path).parent) in parents:
                files[path] = self.manifest['paths'][path]
        for logical, sha in files.items():
            if self.manifest['paths'].get(logical) != sha:
                raise ValueError('task/file binding disagreement')
            safe_path(self.root, 'layout/' + logical)
            self._object(sha)
        binding = safe_path(self.root, task['mapping_file'])
        if digest(binding) != task['mapping_sha256']:
            raise ValueError('frozen task mapping changed')
        before = package_hash(candidate)
        output.mkdir(parents=True, exist_ok=False)
        for source in package_files(candidate):
            target = output / 'candidate' / source.relative_to(candidate)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        if package_hash(output / 'candidate') != before or package_hash(candidate) != before:
            raise ValueError('candidate changed during snapshot creation')
        for logical, sha in files.items():
            # Prior run outcomes are not inputs to scoring a new candidate.
            if Path(logical).name == 'RESULT.json':
                continue
            target = safe_path(output, 'evaluator/' + logical)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self._object(sha), target)
            mode = self.manifest.get('file_modes', {}).get(logical, 0o644)
            target.chmod(0o755 if mode & 0o111 else 0o644)
        for logical in task.get('directories', []):
            safe_path(output, 'evaluator/' + logical).mkdir(parents=True, exist_ok=True)
        for logical, destination in task.get('container_symlinks', {}).items():
            # The upstream test runner writes its timing cache inside container /tmp.
            # This is an exact frozen runtime link, never a method-input link.
            if not logical.endswith('/repository/test_durations.json') or destination != '/tmp/unbroker-runner-durations.json':
                raise ValueError('unsupported container-only symlink')
            target = safe_path(output, 'evaluator/' + logical)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(destination)
        for logical, destination in task.get('symlinks', {}).items():
            target = safe_path(output, 'evaluator/' + logical)
            if Path(destination).is_absolute() or not (target.parent / destination).resolve().is_relative_to(output / 'evaluator'):
                raise ValueError('runtime symlink escapes evaluator')
            if not (target.parent / destination).resolve().is_file():
                raise ValueError('runtime symlink target missing')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(destination)
        shutil.copyfile(binding, output / 'FROZEN_BINDING.json')
        plan = {'task_id': task_id, 'candidate': 'candidate', 'candidate_tree_hash': before,
                'input_reference': task.get('input_reference'),
                'evaluator_symlinks': dict(task.get('symlinks', {}), **task.get('container_symlinks', {})),
                'historical_path_prefix': self.manifest.get('historical_path_prefix', ''),
                'evaluator_files': {p: h for p, h in files.items() if Path(p).name != 'RESULT.json'},
                'mapping_sha256': task['mapping_sha256'],
                'status': 'prepared_not_executed', 'execution_ready': False,
                'blockers': task['blockers'], 'required_images': task.get('required_images', []),
                'candidate_code_executed': False, 'historical_scores_used': False}
        (output / 'EVALUATION_PLAN.json').write_text(json.dumps(plan, indent=2) + '\n')
        return plan

    def doctor(self):
        try:
            result = subprocess.run(['docker', 'info', '--format', '{{.ServerVersion}}'],
                                    capture_output=True, text=True, timeout=15)
            docker = 'available' if result.returncode == 0 and result.stdout.strip() else 'unavailable'
        except (OSError, subprocess.TimeoutExpired):
            docker = 'unavailable'
        bound = self.manifest['summary']['tasks_with_frozen_bindings']
        dependency_file = self.root / 'DEPENDENCIES.json'
        images = json.loads(dependency_file.read_text()).get('image_ids', []) if dependency_file.exists() else []
        missing_images = []
        if docker == 'available':
            for image in images:
                check = subprocess.run(['docker', 'image', 'inspect', image, '--format', '{{.Id}}'],
                                       capture_output=True, text=True, timeout=20)
                if check.returncode or check.stdout.strip() != image:
                    missing_images.append(image)
        else:
            missing_images = images
        missing_modules = [name for name in ('markdown_it', 'pptx', 'lxml', 'PIL', 'xlsxwriter', 'yaml')
                           if importlib.util.find_spec(name) is None]
        remaining = []
        if docker != 'available':remaining.append('start a compatible Docker engine')
        if missing_images:remaining.append('load the fixed runtime image archive')
        if not images:remaining.append('supply the runtime dependency manifest')
        if missing_modules:remaining.append('install the evaluation optional dependencies')
        if bound < len(self.manifest['tasks']):
            remaining.append('complete outstanding task bindings')
        return {'docker_engine': docker, 'runtime_available': not remaining,
                'bound_tasks': bound, 'missing_images': missing_images,
                'missing_python_modules': missing_modules,
                'execution_validated_tasks': self.manifest['summary'].get('execution_ready_tasks', 0),
                'check': 'runtime_availability_only', 'remaining': remaining}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in {'behavior', 'document', 'score'}:
        # Isolate imports for every task, including calls made via the Python API.
        module = {'behavior': 'evaluation_worker', 'document': 'document_worker', 'score': 'score_worker'}[argv[0]]
        return subprocess.call([sys.executable, '-B', '-m', 'ssbench.' + module, *argv[1:]])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', required=True, help='path to the separately assembled evaluator bundle')
    commands = parser.add_subparsers(dest='action', required=True)
    commands.add_parser('verify')
    commands.add_parser('doctor')
    inspect = commands.add_parser('inspect')
    inspect.add_argument('task_id')
    prepare = commands.add_parser('prepare')
    prepare.add_argument('task_id')
    prepare.add_argument('--candidate', required=True)
    prepare.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    try:
        bundle = EvaluationBundle(args.bundle)
        if args.action == 'inspect':
            result = bundle.inspect(args.task_id)
        elif args.action == 'prepare':
            result = bundle.prepare(args.task_id, args.candidate, args.output)
        else:
            result = getattr(bundle, args.action)()
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(2, f'error: {exc}\n')
    print(json.dumps(result, indent=2))
