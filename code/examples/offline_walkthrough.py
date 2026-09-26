"""Demonstrate the callback API with fixed toy responses and no model calls."""
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run_revision as api


def demonstrate(workspace):
    for name in ('parent', 'proposal'):
        package = workspace / name
        (package / 'scripts').mkdir(parents=True)
        (package / 'SKILL.md').write_text('# Selection\nSelect the requested record.\n')
        (package / 'scripts/pick.py').write_text(
            'def pick(items, index=0):\n    return items[0]\n')
    request = 'Use the caller supplied index for record selection; retain the default.'
    run = workspace / 'run'
    api.prepare(workspace / 'parent', workspace / 'proposal', request, run)
    bundle = api.read_json(run / 'INPUT.json')
    node = next(n for n in bundle['nodes'].values()
                if n['snapshot'] == 'parent' and n['source'] == 'items[0]' and n['editable'])
    _, catalog = api.script_runner.bridge.records.evidence_catalog(request, bundle['documents'])
    evidence = next(k for k, v in catalog.items() if v['source_id'] == 'request')
    # These fixed responses illustrate the API. A real run obtains them from an LLM.
    discovery = {'abstain': False, 'abstain_reason': '', 'records': [{
        'contract': {'subject': 'supplied index', 'predicate': 'uses_for',
                     'object': 'record selection', 'polarity': 'affirmed',
                     'condition': '', 'condition_evidence': [],
                     'source_kind': 'explicit_request', 'evidence': [{'evidence_id': evidence}],
                     'confidence': 'high', 'documentation_required': False},
        'document_assessments': [],
        'code_assessments': {'parent': 'REPAIR', 'proposal': 'REPAIR'},
        'bindings': [{'location': {'source_id': node['source_id'],
                                  'node_type': node['node_type'], 'quote': node['source'],
                                  'symbol': node['symbol'], 'line_hint': 0},
                      'role': 'subject', 'use': 'EDIT'}]}]}
    stages = []

    def callback(prompt, tool_schema, stage):
        stages.append(stage)
        if stage == 'discovery':
            return discovery
        if stage == 'document-alignment':
            return {'summary': 'No executable example to align.', 'reviews': [], 'edits': []}
        return {'decision': 'EDIT_ALL', 'summary': 'Use the requested index.',
                'edits': [{'target_node_id': node['node_id'], 'replacement': 'items[index]'}]}

    result = api.run_prepared(run, callback)
    source = (run / 'final/package/scripts/pick.py').read_text()
    assert 'return items[index]' in source
    print(json.dumps({'example': 'fixed-response API walkthrough', 'model_calls': 0,
                      'stages': stages, 'status': result['status']}, indent=2))
    print(source)


if __name__ == '__main__':
    with TemporaryDirectory(prefix='skillscriptbench-example-') as directory:
        demonstrate(Path(directory))
