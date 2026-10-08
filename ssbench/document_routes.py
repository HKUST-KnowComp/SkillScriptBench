"""Entry-point adapters for frozen document components with specialized APIs.

Selection and execution use the original versioned modules, with fresh commands
parsed from the submitted package rather than archived candidate inventories.
"""
from pathlib import Path


def special_component(package, root, config, evidence):
    name = root.name
    recognized = ('frozen_vercel_candidate_pipeline17_v2',
                  'frozen_vercel_json_component17_reviewed_v2',
                  'frozen_vercel_document_extensions17_v1',
                  'frozen_edge_local_document7_v3', 'frozen_conformance_document7_v1')
    if name not in recognized:
        return None
    from inventory_document_invocations import parse_document
    from inventory_candidate_document_invocations import commands
    from vpe_document_runner import execute_commands
    examples = parse_document((package / 'SKILL.md').read_text())
    found = [c for e in examples for c in commands(e)]
    runtime = root / 'runtime'
    image = config['image']
    if name == 'frozen_vercel_candidate_pipeline17_v2':
        from vercel_candidate_pipeline_plan import plan, jobs
        from run_vercel_candidate_pipeline import SUPPORT
        planned = plan(examples, found)
        result = execute_commands(package, jobs(planned), {}, runtime / 'vercel_pipeline_worker.py',
                                  [runtime / n for n in SUPPORT], image=image)
        result['document_pipeline_plan'] = planned
        return result
    if name == 'frozen_vercel_json_component17_reviewed_v2':
        # The reviewed component retains the original runtime; absence is NA.
        runtime = evidence / 'frozen_vercel_json_component17_v1/runtime'
        selected = [c for c in found if c['text'].startswith('node -e ')]
        if not selected:
            return {'status': 'not_applicable', 'commands': [], 'commands_executed': 0}
        # Admitted command forms are supplied as a separate calibration fixture.
        import json
        forms = json.loads((runtime.parent / 'ADMITTED_COMMANDS.json').read_text())
        if any(c['text'] not in forms for c in selected):
            return {'status': 'requires_diagnosis', 'error': 'uncalibrated_json_document_form'}
        matrix = [dict(c, fixture_index=i) for c in selected for i in (0, 1)]
        return execute_commands(package, matrix, {}, runtime / 'vercel_json_validation_worker.py', image=image)
    if name == 'frozen_vercel_document_extensions17_v1':
        from vercel_document_extensions import extension_plan
        from vercel_document_bindings import requested_budget
        from calibration_vercel_document_extensions import SUPPORT, WORKERS, budget_properties
        planned = extension_plan(examples, found)
        groups = {}
        for category in ('collectors', 'budgets', 'helpers'):
            jobs = planned[category]
            if not jobs:
                continue
            worker = WORKERS[category]
            result = execute_commands(package, jobs, {}, runtime / worker,
                                      [runtime / n for n in SUPPORT if n != worker], image=image)
            if category == 'budgets' and result['status'] == 'pass':
                modes = {str(requested_budget(j['step_commands']['gate'])) for j in jobs}
                result['budget_properties'] = budget_properties(result['commands'], modes)
                if not result['budget_properties']['all_matched']:
                    result['status'] = 'fail'
            groups[category] = result
            if result['status'] != 'pass':
                break
        return {'status': next((r['status'] for r in groups.values() if r['status'] != 'pass'), 'pass'),
                'groups': groups, 'plan': planned}
    if name == 'frozen_edge_local_document7_v3':
        from edge_local_document_worker import local_plan
        selected, pending = local_plan(found)
        result = execute_commands(package, selected, {}, runtime / 'edge_local_document_worker.py', image=image)
        result['pending_document_commands'] = pending
        return result
    from conformance_document_worker import FAMILY, plan
    return execute_commands(package, plan(found), {}, runtime / 'conformance_document_worker.py',
                            fixture_mounts=[(evidence / 'source_envelope_audit_v2/archives/synthesis-skills', '/workspace/repo')],
                            package_mount_paths=['/workspace/repo/skills/' + FAMILY], image=image)
