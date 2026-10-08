"""Run frozen documentation components on a newly prepared candidate."""
import argparse
import copy
import importlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from .benchmark import package_hash, safe_path
from .evaluation_worker import pinned_image, relocate, relocate_module, verify_job


def combine(values):
    if not values or any(v not in ('pass', 'fail', 'not_applicable') for v in values):
        return 'error'
    if all(v == 'not_applicable' for v in values):
        return 'error'
    return 'fail' if 'fail' in values else 'pass'


def relocate_values(value, job, plan):
    if isinstance(value, dict):
        return {k: relocate_values(v, job, plan) for k, v in value.items()}
    if isinstance(value, list):
        return [relocate_values(v, job, plan) for v in value]
    if isinstance(value, str) and value.startswith(plan['historical_path_prefix']):
        return str(relocate(job, plan, value))
    return value


def component(job, spec):
    """Each component has a fresh process and its own versioned import roots."""
    job, plan, candidate, mapping = verify_job(job)
    if sys.version_info < (3, 11):
        import tomli
        sys.modules.setdefault('tomllib', tomli)
    package = safe_path(job, spec['package'])
    root = relocate(job, plan, spec['root'])
    suite = relocate(job, plan, plan['historical_path_prefix'] + 'execution-contract350-20260908-v4/suite')
    paths = [root / 'runtime']
    if spec.get('overrides'):
        paths.append(relocate(job, plan, spec['overrides']))
    paths += [suite]
    if spec.get('legacy'):
        paths.append(relocate(job, plan, spec['legacy']))
    sys.path[:0] = [str(p) for p in paths]
    original_run = subprocess.run
    def run(args, *positional, **kwargs):
        if isinstance(args, (list, tuple)) and args and Path(str(args[0])).name == 'docker':
            args = list(args)
            for i, value in enumerate(args):
                if not isinstance(value, str) or 'src=' + plan['historical_path_prefix'] not in value:
                    continue
                start = value.index('src=') + 4
                end = value.index(',', start)
                if not Path(value[start:end]).is_relative_to(job):
                    args[i] = value[:start] + str(relocate(job, plan, value[start:end])) + value[end:]
        return original_run(args, *positional, **kwargs)
    subprocess.run = run
    import doc_fault_noevo_probe as runner
    original_read = runner.read
    config_path = relocate(job, plan, spec['plan'])
    if root.name == 'frozen_vcp_inline_document5_v2' and not config_path.exists():
        config_path = root.parent / 'frozen_vcp_modes_document5_v1/PLAN.json'
    config = json.loads(config_path.read_text())
    config = relocate_values(config, job, plan)
    runner.LEGACY.update({
        'frozen_uptrend_document7_v1': 'uptrend_document_runner',
        'frozen_prompt_document2_v2': 'prompt_tools_document_runner',
        'frozen_conversation_document2_v1': 'conversation_document_runner',
    })
    def read(path):
        path = Path(path)
        if path == root / 'PLAN.json':
            return config
        if root.name == 'frozen_vcp_inline_document5_v2' and path.name == 'PLAN.json' and path.parent.name == 'frozen_vcp_modes_document5_v1':
            return config
        if str(path).startswith(plan['historical_path_prefix']):
            path = relocate(job, plan, str(path))
        return original_read(path)
    runner.read = read
    original_backend_for = runner.backend_for
    def backend_for(config):
        backend = original_backend_for(config)
        relocate_module(backend, job, plan)
        return backend
    runner.backend_for = backend_for
    if root.name in runner.LEGACY:
        backend = importlib.import_module(runner.LEGACY[root.name])
        relocate_module(backend, job, plan)
    image = config.get('image', config.get('runtime_image'))
    if image:
        pinned_image(image)
    if root.name == 'frozen_replica_document60_v1':
        os.environ['REPLICA_PUBLIC_DEPENDENCIES'] = str(suite.parent / 'dependencies/replica-public-v1')
    from .document_routes import special_component
    if root.name == 'frozen_document_invocations2_v1':
        from document_invocation_runner import execute as execute_document
        behavior = mapping['behavior']
        owner = relocate(job, plan, behavior['executor_root'])
        behavior_plan = json.loads((owner / 'PLAN.json').read_text())
        task = relocate_values(behavior_plan['tasks'][spec['task_id']], job, plan)
        source = next(r['source_root'] for r in behavior_plan['source_verification']['rows'] if r['task_id'] == spec['task_id'])
        result = execute_document(task, relocate(job, plan, source), package)
    else:
        result = special_component(package, root, config, suite.parent)
    if result is None:
        result = runner.run_component({'task_id': spec['task_id']}, package, root, suite.parent)
    # Preserve the strict worker's empty-command applicability rules.
    release = relocate(job, plan, plan['historical_path_prefix'] + 'doc-fault-scoring-v3-20260914/release_code')
    sys.path.insert(0, str(release))
    from component_worker import empty_inline_evidence
    commands = result.get('commands')
    inline = 'inline' in root.name and any(name in root.name for name in ('vcp', 'trader'))
    if inline and result.get('status') in ('pass', 'fail') and isinstance(commands, list):
        evidence = empty_inline_evidence(root.name, package, commands)
        pending = result.get('pending_document_commands') or evidence['pending']
        result['applicability_evidence'] = evidence
        if pending:
            result.update(status='requires_diagnosis', pending_document_commands=pending)
        elif not commands:
            result.update(status='not_applicable')
    elif spec.get('strict') and result.get('status') == 'pass' and commands == []:
        result.update(status='requires_diagnosis', error='empty_execution_cannot_establish_pass')
    return result


def run_components(job, plan, package, definitions, label, strict=False, config=None):
    output = job / 'document' / label
    output.mkdir(parents=True, exist_ok=False)
    rows = []
    for i, definition in enumerate(definitions):
        spec = dict(definition, package=package.relative_to(job).as_posix(), strict=strict)
        if config:
            spec.update(overrides=config['overrides'], legacy=config['legacy'])
        path = output / f'component-{i}.json'
        request = output / f'job-{i}.json'
        request.write_text(json.dumps(spec, indent=2) + '\n')
        command = [sys.executable, '-B', '-m', 'ssbench.document_worker', '--job', str(job),
                   '--component-spec', str(request), '--output', str(path)]
        with (output / f'component-{i}.log').open('w') as log:
            proc = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=600)
        result = json.loads(path.read_text()) if path.exists() else {'status': 'error', 'reason': 'component_no_result', 'returncode': proc.returncode}
        rows.append({'root': definition.get('original_root', definition['root']), 'execution': result})
    return {'status': combine([row['execution'].get('status') for row in rows]), 'components': rows}


def execute(job):
    job, plan, candidate, mapping = verify_job(job)
    state = mapping['state']
    strict = state in ('doc_fault', 'clean')
    if strict:
        source = mapping['source'] if state == 'doc_fault' else mapping['document']['source']
        tid = mapping['task_id'] if state == 'doc_fault' else mapping['document']['doc_adapter_task_id']
        frozen = json.loads((relocate(job, plan, source) / 'PLAN.json').read_text())
        task = frozen['tasks'][tid]
        definitions = [{'task_id': tid, 'original_root': root,
                        'root': source + '/runtime_overlay/' + Path(root).name,
                        'plan': source + '/runtime_overlay/' + Path(root).name + '/PLAN.json'}
                       for root in task['components']]
        config = frozen['config']
        packages = {'doc_fault' if state == 'doc_fault' else 'clean': candidate}
        reference = relocate(job, plan, task['pair']['packages']['clean'])
        if state == 'doc_fault':
            view = job / 'document-old-invocations'
            shutil.copytree(candidate, view)
            shutil.copy2(reference / 'SKILL.md', view / 'SKILL.md')
            packages['clean'] = view
        rows = []
        for name, package in packages.items():
            result = run_components(job, plan, package, definitions, name, True, config)
            rows.append({'state': name, 'status': result['status'], 'execution': result})
        # Scope policies use fresh executions and the immutable reference, not prior candidate outcomes.
        reference_result = None
        if Path(task['pair']['owner']).name in {'stage197_wordpress_inherited_scope_reassessment_v1',
                                              'stage200_deploy_target_contract_reassessment_v1'}:
            reference_result = run_components(job, plan, reference, definitions, 'reference', True, config)
        assessment = scope(job, plan, task['pair']['owner'], packages, rows, reference, reference_result)
        values = [v['status'] for v in assessment.values()]
        if state == 'clean':
            values += [row['status'] for row in rows]
        status = combine(values)
    else:
        doc = mapping.get('document', {})
        definitions = [{'task_id': mapping['task_id'], 'root': item['root'],
                        'plan': item.get('plan', {}).get('path', item['root'] + '/PLAN.json')}
                       for item in doc.get('components', [])]
        if mapping['kind'] == 'pr_dashboard_v2_behavior_and_document':
            root = str(Path(mapping['document_runtime']).parent)
            definitions = [{'task_id': mapping['task_id'], 'root': root, 'plan': mapping['document_plan']['path']}]
        if not definitions:
            raise ValueError('missing document component bindings')
        result = run_components(job, plan, candidate, definitions, 'candidate')
        rows = [{'state': 'clean', 'status': result['status'], 'execution': result}]
        owner = doc.get('owner', '')
        # Inherited-reference policies require a separately bound reference before scoring.
        reference = safe_path(job, 'evaluator/' + plan['input_reference']) if plan.get('input_reference') else None
        reference_result = None
        if Path(owner).name in {'stage197_wordpress_inherited_scope_reassessment_v1',
                               'stage200_deploy_target_contract_reassessment_v1'}:
            if reference is None:
                raise ValueError('immutable reference package required')
            reference_result = run_components(job, plan, reference, definitions, 'reference')
        assessment = scope(job, plan, owner, {'clean': candidate}, rows, reference, reference_result)
        status = combine([v['status'] for v in assessment.values()])
    unchanged = package_hash(candidate) == plan['candidate_tree_hash']
    if not unchanged:
        raise ValueError('documentation evaluation changed candidate')
    return {'task_id': mapping['task_id'], 'scope': 'documentation', 'status': status,
            'candidate_unchanged': unchanged, 'rows': rows, 'assessment': assessment,
            'model_calls': 0, 'whole_task_scored': False}


def scope(job, plan, owner, packages, rows, reference, reference_result=None):
    names = {'comfy_document_acceptance7_v1', 'ai_news_document_acceptance5_v1',
             'stage205_jetson_portable_acceptance240_v1'}
    name = Path(owner).name
    suite = relocate(job, plan, plan['historical_path_prefix'] + 'execution-contract350-20260908-v4/suite')
    sys.path.insert(0, str(suite))
    if name == 'stage197_wordpress_inherited_scope_reassessment_v1':
        if reference_result is None or reference_result['status'] == 'error':
            return {row['state']: {'status': 'error'} for row in rows}
        from wordpress_document_acceptance import audit_execution
        from inherited_failure_contract import score_calls
        ref_components = reference_result['components']
        if len(ref_components) != 1:
            raise ValueError('WordPress reference component count')
        _, reference_calls = audit_execution((reference / 'SKILL.md').read_text(), ref_components[0]['execution'])
        result = {}
        for row in rows:
            if row['status'] == 'error':
                result[row['state']] = {'status': 'error'}
                continue
            components = row['execution']['components']
            if len(components) != 1:
                raise ValueError('WordPress component count')
            _, calls = audit_execution((packages[row['state']] / 'SKILL.md').read_text(), components[0]['execution'])
            result[row['state']] = score_calls(calls, reference_calls, non_target_stages={'create_file'})
        return result
    if name == 'stage200_deploy_target_contract_reassessment_v1':
        from inherited_failure_contract import inherited_missing_node_entry
        result = {}
        for row in rows:
            if row['status'] == 'error' or reference_result is None or reference_result['status'] == 'error':
                result[row['state']] = {'status': 'error'}
                continue
            statuses = []
            for i, c in enumerate(row['execution']['components']):
                if 'layout' in Path(c['root']).name:
                    statuses.append(c['execution']['status'])
                    continue
                parent = reference_result['components'][i]['execution']
                ref = next(x for x in parent['commands'] if x['stage'] == 'diff')
                for call in c['execution']['commands']:
                    exempt = call['stage'] == 'diff' and inherited_missing_node_entry(ref, call,
                        parent_package=str(reference), candidate_package=str(packages[row['state']]), non_target=True)
                    statuses.append('pass' if exempt else call['status'])
            result[row['state']] = {'status': combine(statuses)}
        return result
    if name == 'stage191_jetson_artifact_document_acceptance240_v2':
        path = relocate(job, plan, owner + '/checker/jetson_artifact_document_worker.py')
        spec = importlib.util.spec_from_file_location('frozen_jetson_acceptance', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for row in rows:
            if row['status'] == 'error':continue
            statuses = []
            for c in row['execution']['components']:
                for call in c['execution']['commands']:
                    checks = module.assess(call)
                    statuses.append('pass' if all(checks.values()) else 'fail')
            row['status'] = combine(statuses)
    if name not in names:
        return {row['state']: {'status': row['status']} for row in rows}
    if any(row['status'] == 'error' for row in rows):
        return {row['state']: {'status': 'error'} for row in rows}
    from analyze_doc_fault_noevo_probe import scoped_pair
    return scoped_pair({'owner': owner, 'packages': {k: str(v) for k, v in packages.items()}}, {'rows': rows})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job', type=Path, required=True)
    parser.add_argument('--component-spec', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    output = args.output or args.job / 'DOCUMENT_RESULT.json'
    if not output.resolve().is_relative_to(args.job.resolve()) or output.exists():
        parser.exit(2, 'output must be a new file inside the prepared job\n')
    try:
        result = component(args.job, json.loads(args.component_spec.read_text())) if args.component_spec else execute(args.job)
    except Exception as exc:
        result = {'status': 'error', 'error_type': type(exc).__name__, 'reason': str(exc), 'whole_task_scored': False}
    output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({k:v for k,v in result.items() if k not in {'rows','assessment','commands'}}))
    return 2 if result.get('status') == 'error' else 0


if __name__ == '__main__':
    raise SystemExit(main())
