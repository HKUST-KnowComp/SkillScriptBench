"""Offline regressions for target identity and Markdown edit authority."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'code'))
import run_revision as api


def project(raw):
    parent = 'def target():\n    return 1\n\ndef other():\n    return 2\n'
    candidate = parent.replace('return 1', 'return 3')
    return api.script_runner.bridge.rebase._project_file(
        path='scripts/a.py', parent_source=parent, raw_source=raw,
        candidate_source=candidate,
        edits=[{'line': 2, 'end_line': 2, 'target_node_id': 'target-return'}])


def test_parent_delta_cannot_move_to_unrelated_proposal_function():
    raw = 'def target():\n    return 9\n\ndef other():\n    return 1\n'
    result, receipts, findings = project(raw)
    assert result == raw
    assert receipts == []
    assert findings == ['scripts/a.py:changed_raw_file_requires_node_identity']


def test_parent_delta_still_applies_to_unchanged_file():
    parent = 'def target():\n    return 1\n\ndef other():\n    return 2\n'
    result, receipts, findings = project(parent)
    assert result == parent.replace('return 1', 'return 3')
    assert not findings
    assert receipts[0]['action'] == 'applied_parent_delta'


def test_completed_parent_delta_is_recognized_without_reapplication():
    candidate = 'def target():\n    return 3\n\ndef other():\n    return 2\n'
    result, receipts, findings = project(candidate)
    assert result == candidate
    assert not findings
    assert receipts[0]['action'] == 'already_applied_in_raw'


def test_completed_delta_does_not_reapply_to_surviving_old_text():
    parent = 'def target():\n    return 1\n\ndef other():\n    return 1\n'
    candidate = parent.replace('return 1', 'return 3', 1)
    result, receipts, findings = api.script_runner.bridge.rebase._project_file(
        path='scripts/a.py', parent_source=parent, raw_source=candidate,
        candidate_source=candidate,
        edits=[{'line': 2, 'end_line': 2, 'target_node_id': 'target-return'}])
    assert result == candidate
    assert not findings
    assert receipts[0]['action'] == 'already_applied_in_raw'


def test_unchanged_parent_with_repeated_lines_keeps_exact_target():
    parent = 'def target():\n    return 1\n\ndef other():\n    return 1\n'
    candidate = parent.replace('return 1', 'return 3', 1)
    result, receipts, findings = api.script_runner.bridge.rebase._project_file(
        path='scripts/a.py', parent_source=parent, raw_source=parent,
        candidate_source=candidate,
        edits=[{'line': 2, 'end_line': 2, 'target_node_id': 'target-return'}])
    assert result == candidate
    assert not findings
    assert receipts[0]['projected_line_start'] == 2


def test_published_document_prompt_matches_runtime():
    from pathlib import Path
    actual = api.document.build_prompt('Request.', '# Skill\n', {})
    published = (api.HERE / 'prompts/markdown_alignment.txt').read_text().splitlines()[0]
    assert actual.split('\n\nPUBLIC_REQUEST\n')[0] == published


def assess(skill, start, end, new_text):
    items = api.document.inventory(skill)
    payload = {'summary': 'Repair documented option.', 'reviews': [
        {'id': item['id'], 'status': 'CONTRADICTED', 'reason': 'Option mismatch.',
         'evidence': [{'path': 'scripts/a.py', 'quote': 'parser.add_argument("--good")'}]}
        for item in items], 'edits': [
        {'start_line': start, 'end_line': end,
         'old_text': '\n'.join(skill.splitlines()[start - 1:end]),
         'new_text': new_text, 'evidence_ids': [item['id'] for item in items]}]}
    return api.document.validate_assessment(payload, request_text='Repair the usage example.',
        skill_text=skill, scripts={'scripts/a.py': 'parser.add_argument("--good")'})


def test_document_contradiction_does_not_authorize_unrelated_paragraph():
    skill = '# Skill\n\nKeep all reports permanently.\n\nRun `python scripts/a.py --bad`.\n'
    report = assess(skill, 3, 3, 'Delete all reports immediately.')
    assert report['edits'] == []
    assert report['rejected_edits'][0]['reason'] == 'invocation_edit_outside_cited_blocks'
    assert report['unresolved_contradictions'] == ['invocation-1']
    assert api.document.apply_validated_edits(skill, report) == skill


def test_document_command_repair_can_include_its_explanatory_paragraph():
    skill = '# Skill\n\nRun `python scripts/a.py --bad`\nto select the incorrect mode.\n'
    report = assess(skill, 3, 4, 'Run `python scripts/a.py --good`\nto select the requested mode.')
    assert len(report['edits']) == 1
    assert not report['unresolved_contradictions']


def test_document_fenced_command_repair_is_permitted():
    skill = '# Skill\n\n```bash\npython scripts/a.py --bad\n```\n'
    report = assess(skill, 4, 4, 'python scripts/a.py --good')
    assert len(report['edits']) == 1
    assert api.document.apply_validated_edits(skill, report).endswith('--good\n```\n')
