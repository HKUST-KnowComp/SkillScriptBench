"""Item-local evidence validation; unsupported edits never cancel unrelated repairs."""
import copy

import invocation_document_audit_v4 as previous
from invocation_inventory_v5 import inventory

METHOD = 'public-invocation-document-audit-v5'
tool_arguments = previous.tool_arguments
CONTRADICTION_ALIGNMENT_TOOL = previous.CONTRADICTION_ALIGNMENT_TOOL
apply_validated_edits = previous.apply_validated_edits
REFERENCE_KINDS = {'path_reference', 'unclassified_block', 'option_reference'}


def build_prompt(request_text, skill_text, scripts):
    prompt = previous.build_prompt(request_text, skill_text, scripts)
    prompt = prompt.replace('REFERENCE_ONLY is allowed only for path references or unclassified blocks.',
        'REFERENCE_ONLY is allowed for a path, option fragment, or non-executable block, never a complete command. '
        'Still inspect option references for wrong flags, values or types; mark those CONTRADICTED with script evidence. '
        'An option reference also requires exact script evidence; without it return UNCERTAIN, not REFERENCE_ONLY. '
        'Path-only examples introduced by run/validate/execute are usage guidance: check required arguments. '
        'Do not infer that a script exists from a Markdown mention. '
        'A repeated command is not by itself a defect. Never invent missing workflow stages. '
        'SKILL.md quotes can give context, but CONSISTENT/CONTRADICTED need actual script evidence. '
        'Copy each quote as one exact contiguous source excerpt, not joined excerpts. '
        'Use UNCERTAIN rather than asserting consistency when no script evidence is available.')
    # The IDs and order are unchanged; only the option-fragment type is refined.
    return prompt.replace('"kind": "inline_argument"', '"kind": "option_reference"')


def validate_assessment(payload, *, request_text, skill_text, scripts):
    missing_edit_submission = isinstance(payload, dict) and set(payload) == {'summary', 'reviews'}
    if missing_edit_submission:
        payload = dict(payload, edits=[])
    if not isinstance(payload, dict) or set(payload) != {'summary', 'reviews', 'edits'}:
        raise ValueError('invocation_review_schema_invalid')
    if not isinstance(payload['summary'], str) or not isinstance(payload['reviews'], list):
        raise ValueError('invocation_review_types_invalid')
    items = {r['id']: r for r in inventory(skill_text)}
    reviews = {}; diagnostics = []; rejected = []
    sources = dict(scripts, **{'SKILL.md': skill_text})
    for raw in payload['reviews']:
        if not isinstance(raw, dict) or set(raw) != {'id', 'status', 'reason', 'evidence'}:
            raise ValueError('invocation_review_item_invalid')
        r = copy.deepcopy(raw); identity = r['id']; status = r['status']
        if identity not in items or identity in reviews:
            raise ValueError('invocation_review_identity_invalid')
        if status not in ('CONSISTENT', 'CONTRADICTED', 'UNCERTAIN', 'REFERENCE_ONLY'):
            raise ValueError('invocation_review_status_invalid')
        if not isinstance(r['reason'], str) or not isinstance(r['evidence'], list) or len(r['evidence']) > 4:
            raise ValueError('invocation_review_evidence_schema_invalid')
        valid = []; invalid = 0
        for evidence in r['evidence']:
            if not isinstance(evidence, dict) or set(evidence) != {'path', 'quote'}:
                raise ValueError('invocation_script_evidence_invalid')
            path, quote = evidence['path'], evidence['quote']
            if not isinstance(path, str) or not isinstance(quote, str):
                raise ValueError('invocation_script_evidence_invalid')
            if path in sources and quote.strip() and quote in sources[path]:
                valid.append(evidence)
            else:
                invalid += 1
        grounded = [e for e in valid if e['path'] in scripts]
        problem = None
        if status == 'REFERENCE_ONLY' and items[identity]['kind'] not in REFERENCE_KINDS:
            problem = 'executable_invocation_cannot_be_dismissed_as_reference'
        elif status == 'REFERENCE_ONLY' and items[identity]['kind'] == 'option_reference' and (invalid or not grounded):
            problem = 'option_reference_requires_script_evidence'
        elif status in ('CONSISTENT', 'CONTRADICTED') and (invalid or not grounded):
            problem = 'invocation_script_evidence_not_grounded'
        elif invalid:
            problem = 'ungrounded_context_reference'
        r['evidence'] = valid
        if problem:
            r['status'] = 'UNCERTAIN'
            diagnostics.append(dict(id=identity, reason=problem, original_status=status))
        reviews[identity] = r
    if set(reviews) != set(items):
        raise ValueError('invocation_review_coverage_incomplete')
    if not isinstance(payload['edits'], list) or len(payload['edits']) > 8:
        raise ValueError('invocation_edit_budget_invalid')
    accepted = []
    # Reuse the strict line/scope/quote checks for each independently grounded edit.
    # A public-only packet is synthesized solely to validate that edit, not to call a model.
    for index, edit in enumerate(payload['edits']):
        if not isinstance(edit, dict):
            raise ValueError('invocation_edit_schema_invalid')
        ids = edit.get('evidence_ids', [])
        if not isinstance(ids, list) or not ids or any(not isinstance(i, str) or i not in reviews or reviews[i]['status'] != 'CONTRADICTED' for i in ids):
            rejected.append(dict(index=index, reason='invocation_edit_not_backed_by_contradiction'))
            continue
        probe_reviews = []
        for identity, r in reviews.items():
            probe = copy.deepcopy(r)
            probe['status'] = 'CONTRADICTED' if identity in ids else 'UNCERTAIN'
            probe['evidence'] = [e for e in probe['evidence'] if e['path'] in scripts] if identity in ids else []
            probe_reviews.append(probe)
        try:
            result = previous.validate_assessment(dict(summary=payload['summary'], reviews=probe_reviews, edits=[edit]),
                request_text=request_text, skill_text=skill_text, scripts=scripts)
            accepted.append((index, result['edits'][0]))
        except (ValueError, TypeError, KeyError) as exc:
            rejected.append(dict(index=index, reason=str(exc)))
    conflicts = set()
    for i, (index, edit) in enumerate(accepted):
        for other_index, other in accepted[i+1:]:
            if not (edit['end_line'] < other['start_line'] or edit['start_line'] > other['end_line']):
                conflicts.update((index, other_index))
    rejected.extend(dict(index=i, reason='invocation_edits_overlap') for i in sorted(conflicts))
    edits = [e for i, e in accepted if i not in conflicts]
    covered = {i for e in edits for i in e['evidence_ids']}
    unresolved = [i for i, r in reviews.items() if r['status'] == 'CONTRADICTED' and i not in covered]
    return dict(method=METHOD, summary=payload['summary'][:800], original_summary_length=len(payload['summary']),
        reviews=list(reviews.values()), edits=edits, script_evidence=[],
        status='CONTRADICTED' if edits else 'UNCERTAIN' if diagnostics or unresolved or any(r['status']!='CONSISTENT' for r in reviews.values()) else 'SATISFIED',
        coverage_complete=True, semantic_correctness_verified=False, source_scope='public_package_only',
        review_diagnostics=diagnostics, rejected_edits=rejected, unresolved_contradictions=unresolved,
        all_proposed_edits_applied=not rejected, item_local_validation=True,
        missing_edit_submission=missing_edit_submission)
