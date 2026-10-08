"""Inspect and materialize frozen evaluator assets without executing package code."""
import argparse
import ast
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

    def read_file(self, task_id, logical_path):
        """Read a bound UTF-8 asset by name, validating its frozen bytes."""
        task = self.task(task_id)
        sha = task['files'].get(logical_path)
        if sha is None or self.manifest['paths'].get(logical_path) != sha:
            raise ValueError('file is not bound to this task: ' + logical_path)
        return self._object(sha).read_text(encoding='utf-8')

    def task_view(self, task_id, files=False):
        """Describe frozen routes without importing or executing their code."""
        task = self.task(task_id)
        mapping_path = safe_path(self.root, task['mapping_file'])
        if digest(mapping_path) != task['mapping_sha256']:
            raise ValueError('frozen task mapping changed')
        mapping = json.loads(mapping_path.read_text())
        if mapping.get('task_id') != task_id:
            raise ValueError('task identity disagreement')
        prefix = self.manifest.get('historical_path_prefix', '')

        def logical(value):
            value = value.removeprefix(prefix) if prefix else value
            safe_path(self.root, 'layout/' + value)
            if Path(value).is_absolute():
                raise ValueError('unsupported frozen asset path')
            return value.rstrip('/')

        def asset(path):
            sha = task['files'].get(path)
            if sha is None or self.manifest['paths'].get(path) != sha:
                raise ValueError('task/file binding disagreement: ' + path)
            return self._object(sha)

        def config(path):
            return json.loads(asset(path).read_text())

        def constants(path):
            result = {}
            for node in ast.parse(asset(path).read_text()).body:
                if isinstance(node, ast.Assign):
                    try:
                        value = ast.literal_eval(node.value)
                    except (ValueError, TypeError):
                        continue
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            result[target.id] = value
            return result

        images = set(task.get('required_images', []))
        image_sources = {image: ['task metadata'] for image in images}
        unresolved = []

        def add_image(image, source):
            if isinstance(image, str) and image.startswith('sha256:') and len(image) == 71:
                images.add(image)
                image_sources.setdefault(image, []).append(source)
                return True
            return False

        components = []

        def component(role, root, plan=None, selected_id=task_id):
            root = logical(root)
            plan = logical(plan) if plan else root + '/PLAN.json'
            # This documented worker fallback is part of the frozen adapter.
            if plan not in task['files'] and root.endswith('/frozen_vcp_inline_document5_v2'):
                plan = str(Path(root).parent / 'frozen_vcp_modes_document5_v1/PLAN.json')
            cfg = config(plan) if plan in task['files'] else {}
            related = sorted(p for p in task['files'] if p == plan or
                             (p.startswith(root + '/runtime/') and Path(p).suffix in {'.py', '.js', '.mjs'}) or
                             p == root + '/CONTRACTS.json')
            row = {'role': role, 'root': root, 'plan': plan if plan in task['files'] else None,
                   'task_key': selected_id, 'files': related}
            components.append(row)
            if role == 'document':
                found_image = add_image(cfg.get('image', cfg.get('runtime_image')), plan)
                suite = 'execution-contract350-20260908-v4/suite/'
                probe = root + '/runtime/doc_fault_noevo_probe.py'
                if probe not in task['files']:
                    probe = suite + 'doc_fault_noevo_probe.py'
                legacy = dict(constants(probe).get('LEGACY', {})) if probe in task['files'] else {}
                legacy.update({'frozen_uptrend_document7_v1': 'uptrend_document_runner',
                               'frozen_prompt_document2_v2': 'prompt_tools_document_runner',
                               'frozen_conversation_document2_v1': 'conversation_document_runner'})
                backend_name = legacy.get(Path(root).name, cfg.get('backend'))
                if isinstance(backend_name, str):
                    entry = root + '/runtime/' + backend_name + '.py'
                    if entry in related:
                        related.remove(entry)
                        related.insert(0, entry)
                if Path(root).name in set(legacy) | {'frozen_document_invocations2_v1'}:
                    # These backends use execute_commands' default IMAGE. Each
                    # frozen legacy runtime contains its own source_test_runner.
                    backend = root + '/runtime/source_test_runner.py'
                    if backend in task['files']:
                        found_image = add_image(constants(backend).get('IMAGE'), backend + ':IMAGE') or found_image
                if not found_image:
                    unresolved.append(root)
            return root, cfg

        behavior = mapping.get('behavior', {})
        if behavior:
            owner = behavior.get('executor_root') or str(Path(behavior['runtime']).parent)
            root, cfg = component('behavior', owner)
            runtime = root + '/runtime/'
            runner = runtime + 'run_behavior.py'
            priority = ['run_behavior.py', 'deploy_candidate_layout.py', 'contract_checks.py',
                        cfg.get('contract_module', 'public_mask_contract') + '.py', 'source_test_runner.py']
            components[-1]['files'].sort(key=lambda p: (Path(p).name not in priority, p))
            if behavior.get('adapter') == 'deploy_boundary':
                path = runtime + 'deploy_candidate_layout.py'
                found_image = add_image(constants(path).get('IMAGE'), path)
            elif runner in task['files']:
                values = constants(runner)
                base = behavior.get('base') or mapping.get('base_id')
                if not base:
                    from .evaluation_worker import source_probe_key
                    base = source_probe_key(asset(runtime + 'source_labels.py'), task_id)
                name = 'BROWSER_IMAGE' if base == values.get('BROWSER_BASE') else 'IMAGE'
                found_image = add_image(values.get(name), runner + ':' + name)
            else:
                found_image = add_image(behavior.get('image', cfg.get('runtime_image')), 'behavior binding / ' + root)
            if not found_image:
                unresolved.append(root)

        doc = mapping.get('document', {})
        if mapping.get('state') in {'doc_fault', 'clean'}:
            source = mapping['source'] if mapping['state'] == 'doc_fault' else doc['source']
            selected_id = task_id if mapping['state'] == 'doc_fault' else doc['doc_adapter_task_id']
            root = logical(source)
            selected = config(root + '/PLAN.json')['tasks'][selected_id]
            components.append({'role': 'document routing', 'root': root, 'plan': root + '/PLAN.json',
                               'task_key': selected_id, 'files': [root + '/PLAN.json']})
            for original in selected['components']:
                component('document', source + '/runtime_overlay/' + Path(original).name,
                          selected_id=selected_id)
        elif mapping.get('kind') == 'pr_dashboard_v2_behavior_and_document':
            component('document', str(Path(mapping['document_runtime']).parent), mapping['document_plan']['path'])
        else:
            for item in doc.get('components', []):
                component('document', item['root'], item.get('plan', {}).get('path'))

        result = {'task_id': task_id, 'track': task.get('track'), 'state': task.get('state'),
                  'binding_status': task.get('binding_status'), 'binding': str(mapping_path),
                  'input_reference': task.get('input_reference'), 'bound_files': len(task['files']),
                  'scoring_components': ['document'] if mapping.get('state') == 'doc_fault' else ['behavior', 'document'],
                  'required_images': sorted(images), 'image_sources': image_sources,
                  'unresolved_image_sources': sorted(set(unresolved)),
                  'components': components, 'blockers': task.get('blockers', []),
                  'check': 'static_task_inspection_only'}
        if files:
            result['files'] = [{'logical_path': path, 'object_path': str(safe_path(self.root, self.manifest['objects'][sha]['file'])),
                                'sha256': sha, 'prepared': Path(path).name != 'RESULT.json'}
                               for path, sha in sorted(task['files'].items())]
        return result

    def inspect_text(self, task_id, files=False):
        view = self.task_view(task_id, files)
        lines = [f"Task: {view['task_id']}", f"Track / state: {view['track']} / {view['state']}",
                 f"Binding: {view['binding_status']} ({view['binding']})",
                 f"Input reference: {view['input_reference']}",
                 'Scoring components: ' + ', '.join(view['scoring_components']),
                 f"Bound files: {view['bound_files']}", 'Runtime images (metadata and frozen routes):']
        lines.extend('  ' + image for image in view['required_images'])
        if not view['required_images']:
            lines.append('  No pinned image found in the selected routes.')
        lines += ['Checker entrypoints (after prepare):',
                  '  skillscriptbench evaluation score --job JOB',
                  *('  skillscriptbench evaluation ' + name + ' --job JOB' for name in view['scoring_components']),
                  'Frozen evaluator routes (paths relative to prepared JOB/evaluator):']
        for item in view['components']:
            lines += [f"  {item['role']}: {item['root']}", f"    task key: {item['task_key']}"]
            if item['plan']:
                lines.append('    plan: ' + item['plan'])
            paths = [path for path in item['files'] if path != item['plan']]
            lines.extend('    file: ' + path for path in paths[:4])
            if len(paths) > 4:
                lines.append(f'    ... {len(paths) - 4} more runtime files; use --files for the full index')
        if view['unresolved_image_sources']:
            lines.append('Unresolved image sources: ' + ', '.join(view['unresolved_image_sources']))
        if view['blockers']:
            lines.append('Blockers: ' + ', '.join(view['blockers']))
        if files:
            lines.append('All bound files (logical path -> existing object; no asset copies):')
            for item in view['files']:
                suffix = ' [historical result; excluded from prepare]' if not item['prepared'] else ''
                lines += ['  ' + item['logical_path'] + suffix, '    -> ' + item['object_path']]
        lines.append('Read/export an asset: skillscriptbench evaluation --bundle BUNDLE inspect TASK --read-file LOGICAL_PATH')
        lines.append('Static inspection only; this does not execute or validate task scoring.')
        return '\n'.join(lines)

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

    def doctor(self, task_ids=None):
        selected = list(dict.fromkeys(task_ids or []))
        views = [self.task_view(task_id) for task_id in selected]
        try:
            result = subprocess.run(['docker', 'info', '--format', '{{.ServerVersion}}'],
                                    capture_output=True, text=True, timeout=15)
            docker = 'available' if result.returncode == 0 and result.stdout.strip() else 'unavailable'
        except (OSError, subprocess.TimeoutExpired):
            docker = 'unavailable'
        bound = (sum(view['binding_status'] == 'frozen_map_bound' for view in views) if selected
                 else self.manifest['summary']['tasks_with_frozen_bindings'])
        dependency_file = self.root / 'DEPENDENCIES.json'
        images = (sorted({image for view in views for image in view['required_images']}) if selected else
                  json.loads(dependency_file.read_text()).get('image_ids', []) if dependency_file.exists() else [])
        missing_images = []
        if docker == 'available':
            for image in images:
                try:
                    check = subprocess.run(['docker', 'image', 'inspect', image, '--format', '{{.Id}}'],
                                           capture_output=True, text=True, timeout=20)
                    present = check.returncode == 0 and check.stdout.strip() == image
                except (OSError, subprocess.TimeoutExpired):
                    present = False
                if not present:
                    missing_images.append(image)
        else:
            missing_images = images
        missing_modules = [name for name in ('markdown_it', 'pptx', 'lxml', 'PIL', 'xlsxwriter', 'yaml')
                           if importlib.util.find_spec(name) is None]
        remaining = []
        if docker != 'available':remaining.append('start a compatible Docker engine')
        if missing_images:remaining.append('load the fixed runtime image archive')
        if not selected and not images:remaining.append('supply the runtime dependency manifest')
        if missing_modules:remaining.append('install the evaluation optional dependencies')
        unresolved = sorted({source for view in views for source in view['unresolved_image_sources']})
        if unresolved:remaining.append('resolve task runtime image sources before treating this check as complete')
        if bound < (len(selected) if selected else len(self.manifest['tasks'])):
            remaining.append('complete outstanding task bindings')
        return {'docker_engine': docker, 'runtime_available': not remaining,
                'bound_tasks': bound, 'missing_images': missing_images,
                'scope': 'selected_tasks' if selected else 'bundle', 'task_ids': selected,
                'required_images': images,
                'unresolved_image_sources': unresolved,
                'missing_python_modules': missing_modules,
                'execution_validated_tasks': (sum(bool(self.task(tid).get('execution_ready')) for tid in selected)
                                              if selected else self.manifest['summary'].get('execution_ready_tasks', 0)),
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
    doctor = commands.add_parser('doctor')
    doctor.add_argument('--task', action='append', dest='task_ids', help='check only this task; repeat to select more')
    inspect = commands.add_parser('inspect')
    inspect.add_argument('task_id')
    inspect.add_argument('--format', choices=('json', 'text'), default='json')
    inspect.add_argument('--files', action='store_true', help='include logical paths and existing object locations')
    inspect.add_argument('--read-file', metavar='LOGICAL_PATH', help='print a hash-verified UTF-8 asset for reading or export')
    prepare = commands.add_parser('prepare')
    prepare.add_argument('task_id')
    prepare.add_argument('--candidate', required=True)
    prepare.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    try:
        bundle = EvaluationBundle(args.bundle)
        if args.action == 'inspect':
            if args.read_file:
                if args.files or args.format != 'json':
                    parser.error('--read-file cannot be combined with --files or --format text')
                print(bundle.read_file(args.task_id, args.read_file), end='')
                return
            if args.format == 'text':
                print(bundle.inspect_text(args.task_id, args.files))
                return
            result = bundle.task_view(args.task_id, files=True) if args.files else bundle.inspect(args.task_id)
        elif args.action == 'doctor':
            result = bundle.doctor(args.task_ids)
        elif args.action == 'prepare':
            result = bundle.prepare(args.task_id, args.candidate, args.output)
        else:
            result = getattr(bundle, args.action)()
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(2, f'error: {exc}\n')
    print(json.dumps(result, indent=2))
    return 2 if args.action == 'doctor' and not result['runtime_available'] else 0
