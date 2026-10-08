"""Execute a prepared frozen behavior contract in its pinned Docker image.

This adapter reports behavior only. Documentation and whole-task scoring are
separate components. No historical verdict is used to grade a candidate.
"""
import argparse
import ast
import importlib
import json
from pathlib import Path
import subprocess
import sys

from .benchmark import package_hash, safe_path, sha256


def verify_job(job):
    """Validate the immutable inputs before importing an evaluator."""
    job = Path(job).resolve()
    plan = json.loads((job / 'EVALUATION_PLAN.json').read_text())
    candidate = safe_path(job, plan['candidate'])
    if package_hash(candidate) != plan['candidate_tree_hash']:
        raise ValueError('candidate changed since preparation')
    binding = job / 'FROZEN_BINDING.json'
    if sha256(binding) != plan['mapping_sha256']:
        raise ValueError('frozen binding changed')
    for relative, expected in plan['evaluator_files'].items():
        if sha256(safe_path(job, 'evaluator/' + relative)) != expected:
            raise ValueError('prepared evaluator asset changed')
    mapping = json.loads(binding.read_text())
    for logical, destination in plan.get('evaluator_symlinks', {}).items():
        path = job / 'evaluator' / logical
        if not path.is_symlink() or str(path.readlink()) != destination:
            raise ValueError('prepared runtime symlink changed')
    if mapping['task_id'] != plan['task_id']:
        raise ValueError('prepared task identity mismatch')
    return job, plan, candidate, mapping


def relocate(job, plan, value):
    if isinstance(value, str) and Path(value).is_absolute() and Path(value).is_relative_to(job):
        return Path(value)
    prefix = plan['historical_path_prefix']
    if not isinstance(value, str) or not value.startswith(prefix):
        raise ValueError('unsupported frozen asset path')
    return safe_path(job, 'evaluator/' + value.removeprefix(prefix))


def pinned_image(image):
    if not isinstance(image, str) or not image.startswith('sha256:') or len(image) != 71:
        raise ValueError('runtime must be a pinned image ID')
    check = subprocess.run(['docker', 'image', 'inspect', image, '--format', '{{.Id}}'],
                           capture_output=True, text=True, timeout=20)
    if check.returncode or check.stdout.strip() != image:
        raise ValueError('pinned_image_unavailable: ' + image)


def relocate_module(module, job, plan):
    """Relocate bound data paths, never fall back to a historical host tree."""
    for key, value in list(vars(module).items()):
        if isinstance(value, Path) and str(value).startswith(plan['historical_path_prefix']):
            setattr(module, key, relocate(job, plan, str(value)))


def literal_image(path, name):
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise ValueError('missing pinned runtime image')


def source_probe_key(path, task_id):
    """Read explicit literal routing records without traversing old workspaces."""
    matches = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if not isinstance(node, ast.Dict):
            continue
        try:
            record = ast.literal_eval(node)
        except (ValueError, TypeError):
            continue
        value = record.get(task_id)
        if isinstance(value, str):
            matches.add(value)
        elif isinstance(value, dict) and isinstance(value.get('base_id'), str):
            matches.add(value['base_id'])
    if len(matches) != 1:
        raise ValueError('missing or ambiguous explicit source probe routing')
    return matches.pop()


def execute(job):
    job, plan, candidate, mapping = verify_job(job)
    behavior = mapping.get('behavior', {})
    owner = behavior.get('executor_root') or str(Path(behavior.get('runtime', '')).parent)
    root = relocate(job, plan, owner)
    config = json.loads((root / 'PLAN.json').read_text()) if (root / 'PLAN.json').exists() else {}
    module_name = config.get('contract_module', 'public_mask_contract')
    runtime = root / 'runtime'
    # A fresh worker process prevents module-cache collisions between frozen versions.
    sys.path.insert(0, str(runtime))
    judgment = None
    if behavior.get('adapter') == 'deploy_boundary':
        backend = importlib.import_module('deploy_candidate_layout')
        relocate_module(backend, job, plan)
        pinned_image(backend.IMAGE)
        result = backend.execute(candidate, [{'preview_status': n} for n in (200, 399, 400, 401, 403)], runtime, backend.IMAGE)
        judgment = result
    elif (runtime / 'run_behavior.py').is_file():
        image = literal_image(runtime / 'run_behavior.py', 'IMAGE')
        backend = importlib.import_module('run_behavior')
        relocate_module(backend, job, plan)
        original_argv = backend.docker_argv
        def docker_argv(*args, **kwargs):
            values = original_argv(*args, **kwargs)
            for i, value in enumerate(values):
                marker = 'src=' + plan['historical_path_prefix']
                if marker in value:
                    start = value.index('src=') + 4
                    end = value.index(',', start)
                    if Path(value[start:end]).is_relative_to(job):
                        continue
                    values[i] = value[:start] + str(relocate(job, plan, value[start:end])) + value[end:]
            return values
        backend.docker_argv = docker_argv
        base = behavior.get('base') or mapping.get('base_id')
        if not base:
            base = source_probe_key(runtime / 'source_labels.py', mapping['task_id'])
        if base == getattr(backend, 'BROWSER_BASE', None):
            image = backend.BROWSER_IMAGE
        pinned_image(image)
        result = backend.execute(candidate, base)
        judgment = result.get('behavior', {})
    elif 'contract' in config and (runtime / 'public_cli_worker.py').is_file():
        if not isinstance(module_name, str) or not module_name.isidentifier():
            raise ValueError('invalid contract module')
        pinned_image(config['runtime_image'])
        backend = importlib.import_module(module_name)
        transport = importlib.import_module('python_contract_runner')
        result = transport.execute(candidate, config['contract']['calls'], image=config['runtime_image'],
                                   worker_file=runtime / 'public_cli_worker.py')
        if result.get('execution_status') == 'completed':
            judgment = backend.judge(config['contract'], result['observed'])
    elif (runtime / 'contract_checks.py').exists() and ((root / 'CONTRACTS.json').exists() or behavior.get('contract')):
        image = behavior.get('image', config.get('runtime_image'))
        pinned_image(image)
        backend = importlib.import_module('contract_checks')
        transport = importlib.import_module('python_contract_runner')
        contract = behavior.get('contract') or json.loads((root / 'CONTRACTS.json').read_text())[mapping['task_id']]
        result = transport.execute(candidate, backend.worker_calls(contract), image=image,
                                   worker_file=runtime / 'python_calls.py')
        if result.get('execution_status') == 'completed':
            judgment = backend.judge(contract, result['observed'], result['receipt'])
    elif 'task_calls' in config:
        pinned_image(config['runtime_image'])
        transport = importlib.import_module('python_contract_runner')
        backend = importlib.import_module('advisory_action_contract')
        calls = config['task_calls'][mapping['task_id']]
        result = transport.execute(candidate, calls, image=config['runtime_image'], worker_file=runtime / 'python_calls.py')
        judgment = backend.grade(calls, result)
    elif 'source_verification' in config:
        pinned_image(config['runtime_image'])
        backend = importlib.import_module('source_test_runner')
        relocate_module(backend, job, plan)
        tid = mapping['task_id']
        task = dict(config['tasks'][tid])
        task['public_parent'] = str(relocate(job, plan, task['public_parent']))
        source = next(row['source_root'] for row in config['source_verification']['rows'] if row['task_id'] == tid)
        result = backend.execute(task, relocate(job, plan, source), candidate, image=config['runtime_image'])
        judgment = result
    else:
        raise ValueError('unsupported_frozen_behavior_adapter')
    unchanged = package_hash(candidate) == plan['candidate_tree_hash']
    if not unchanged:
        raise ValueError('evaluation changed the candidate snapshot')
    status = judgment.get('status', 'error') if judgment else 'error'
    if status not in {'pass', 'fail'}:
        status = 'error'
    return {'task_id': plan['task_id'], 'scope': 'behavior_only', 'status': status,
            'candidate_unchanged': unchanged, 'candidate_tree_hash': plan['candidate_tree_hash'],
            'judgment': judgment, 'execution': result, 'model_calls': 0,
            'whole_task_scored': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job', type=Path, required=True)
    args = parser.parse_args(argv)
    if not args.job.is_dir() or not (args.job / 'EVALUATION_PLAN.json').is_file():
        parser.exit(2, 'error: --job must be an existing prepared evaluation directory\n')
    output = args.job.resolve() / 'BEHAVIOR_RESULT.json'
    if output.exists():
        parser.exit(2, 'error: behavior result already exists; prepare a new job\n')
    try:
        result = execute(args.job)
    except (OSError, ValueError, KeyError, ImportError, subprocess.SubprocessError) as exc:
        result = {'status': 'error', 'scope': 'behavior_only',
                  'error_type': type(exc).__name__, 'reason': str(exc), 'whole_task_scored': False}
    output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({k: v for k, v in result.items() if k not in {'execution', 'judgment'}}, indent=2))
    return 2 if result['status'] == 'error' else 0


if __name__ == '__main__':
    raise SystemExit(main())
