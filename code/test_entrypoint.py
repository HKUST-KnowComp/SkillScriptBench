from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_revision as api


def make_package(tmp_path, name):
    root = tmp_path / name
    (root / 'scripts').mkdir(parents=True)
    (root / 'SKILL.md').write_text('# Selection\nSelect the record requested by the caller.\n')
    (root / 'scripts/pick.py').write_text('def pick(items, index=0):\n    return items[0]\n')
    return root


def test_llm_discovery_reaches_real_node_editor_and_final_package(tmp_path):
    parent, proposal = make_package(tmp_path, 'parent'), make_package(tmp_path, 'proposal')
    request = 'Use the caller supplied index for record selection; retain the default.'
    root = tmp_path / 'run'
    api.prepare(parent, proposal, request, root)
    bridge = api.script_runner.bridge
    bundle = api.read_json(root / 'INPUT.json')
    node = next(n for n in bundle['nodes'].values()
                if n['snapshot'] == 'parent' and n['source'] == 'items[0]' and n['editable'])
    _, catalog = bridge.records.evidence_catalog(request, bundle['documents'])
    evidence = next(k for k, v in catalog.items() if v['source_id'] == 'request')
    payload = {'abstain': False, 'abstain_reason': '', 'records': [{
        'contract': {'subject': 'supplied index', 'predicate': 'uses_for', 'object': 'record selection',
                     'polarity': 'affirmed', 'condition': '', 'condition_evidence': [],
                     'source_kind': 'explicit_request', 'evidence': [{'evidence_id': evidence}],
                     'confidence': 'high', 'documentation_required': False},
        'document_assessments': [], 'code_assessments': {'parent': 'REPAIR', 'proposal': 'SATISFIED'},
        'bindings': [{'location': {'source_id': node['source_id'], 'node_type': node['node_type'],
                                  'quote': node['source'], 'symbol': node['symbol'], 'line_hint': 0},
                      'role': 'subject', 'use': 'EDIT'}]}]}
    stages = []

    def callback(prompt, tool, stage):
        stages.append(stage)
        if stage == 'discovery':
            return payload
        if stage == 'document-alignment':
            return {'summary': 'No invocation contradiction.', 'reviews': [], 'edits': []}
        return {'decision': 'EDIT_ALL', 'summary': 'Use supplied index.',
                'edits': [{'target_node_id': node['node_id'], 'replacement': 'items[index]'}]}

    result = api.run_prepared(root, callback)
    assert result['status'] == 'frozen'
    assert not result['behavioral_success_evaluated']
    assert (root / 'final/package/scripts/pick.py').read_text().endswith('return items[index]\n')
    assert stages == ['discovery', 'parent-signal', 'document-alignment']
    assert not api.read_json(root / 'INPUT.json')['legacy_shortlist_consumed']
    assert 'return items[0]' in (parent / 'scripts/pick.py').read_text()


def test_discovery_abstain_preserves_proposal_without_regex_fallback(tmp_path):
    parent, proposal = make_package(tmp_path, 'parent'), make_package(tmp_path, 'proposal')
    (proposal / 'scripts/pick.py').write_text('def pick(items, index=0):\n    return items[index]\n')
    root = tmp_path / 'run'
    api.prepare(parent, proposal, 'Preserve working behavior.', root)
    stages = []

    def callback(prompt, tool, stage):
        stages.append(stage)
        if stage == 'discovery':
            return {'abstain': True, 'abstain_reason': 'No grounded defect.', 'records': []}
        return {'summary': 'No contradiction.', 'reviews': [], 'edits': []}

    result = api.run_prepared(root, callback)
    assert stages == ['discovery', 'document-alignment']
    assert api.hash_tree(root / 'final/package') == api.hash_tree(proposal)
    assert result['script_result']['legacy_detectors_invoked'] is False


def test_invalid_document_edit_does_not_overwrite_code_or_document(tmp_path):
    parent, proposal = make_package(tmp_path, 'parent'), make_package(tmp_path, 'proposal')
    root = tmp_path / 'run'
    api.prepare(parent, proposal, 'Preserve working behavior.', root)

    def callback(prompt, tool, stage):
        if stage == 'discovery':
            return {'abstain': True, 'abstain_reason': 'No defect.', 'records': []}
        return {'invalid': 'unstructured response'}

    result = api.run_prepared(root, callback)
    assert result['document_status'] == 'fallback_unchanged_document'
    assert api.hash_tree(root / 'final/package') == api.hash_tree(proposal)


def test_private_input_rejected_before_model_call(tmp_path):
    parent, proposal = make_package(tmp_path, 'parent'), make_package(tmp_path, 'proposal')
    (parent / '_oracle').mkdir()
    import pytest
    with pytest.raises(ValueError, match='nonpublic'):
        api.prepare(parent, proposal, 'Request.', tmp_path / 'run')
