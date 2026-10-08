"""Bounded per-example review with public quote evidence and line-addressed edits."""
import copy
import json

from bvi_skill_evo import package_contradiction_alignment_v2 as previous
from bvi_skill_evo.document_markers import BEGIN_MARKER, END_MARKER
from invocation_inventory_v4 import inventory, python_interfaces

METHOD = 'public-invocation-document-audit-v4'
tool_arguments = previous.tool_arguments


def obj(fields, required=None):
    return dict(type='object', additionalProperties=False, properties=fields,
        required=list(fields) if required is None else required)


TEXT = dict(type='string')
EVIDENCE = obj(dict(path=TEXT, quote=TEXT))
REVIEW = obj(dict(id=TEXT, status=dict(type='string', enum=['CONSISTENT', 'CONTRADICTED', 'UNCERTAIN', 'REFERENCE_ONLY']),
    reason=TEXT, evidence=dict(type='array', maxItems=4, items=EVIDENCE)))
EDIT = obj(dict(start_line=dict(type='integer', minimum=1), end_line=dict(type='integer', minimum=1),
    old_text=TEXT, new_text=TEXT, evidence_ids=dict(type='array', minItems=1, items=TEXT)))
CONTRADICTION_ALIGNMENT_TOOL = dict(type='function', function=dict(
    name='submit_package_contradiction_assessment',
    description='Review every numbered public invocation, then return only grounded document repairs.',
    parameters=obj(dict(summary=TEXT, reviews=dict(type='array', items=REVIEW),
        edits=dict(type='array', maxItems=8, items=EDIT)))))


def build_prompt(request_text, skill_text, scripts):
    return ("Review every numbered invocation in SKILL.md against the actual scripts. "
        "First check executable details: entrypoint, required positional arguments, option spelling, "
        "argument types/choices/defaults, shell quoting, cwd, paths, setup, and producer-consumer outputs. "
        "Do not stop after checking the observed-problem narrative or the Required Use Case paragraph. "
        "A plausible purpose description does not prove an example can execute. "
        "For every inventory ID return exactly one review. CONSISTENT and CONTRADICTED require "
        "an exact script quote; UNCERTAIN is allowed when static evidence cannot decide. "
        "REFERENCE_ONLY is allowed only for path references or unclassified blocks. "
        "Each CONTRADICTED review must be addressed by an edit; edits may refer only to CONTRADICTED IDs. "
        "Do not rewrite abstract descriptions while leaving known invalid commands unchanged. "
        "Use at most eight nonoverlapping line-addressed edits. Copy old_text exactly from those lines, "
        "without line-number prefixes. "
        "Edit only within the Markdown block spans (line through end_line) of the cited invocation IDs; "
        "every cited span must intersect the edit. Inline spans include their explanatory paragraph. "
        "Do not remove workflows or weaken public requirements. "
        "Do not edit protected Required Use Case markers or their contents. "
        "The program validates references, not behavioral correctness. No task tests, gold, oracle or reward are provided. "
        "Keep reasons and summary concise; summary length never determines patch acceptance. "
        "Call submit_package_contradiction_assessment once.\n\nPUBLIC_REQUEST\n" + request_text
        + '\n\nINVOCATION_INVENTORY\n' + json.dumps(inventory(skill_text), ensure_ascii=True)
        + '\n\nPYTHON_INTERFACE_FACTS\n' + json.dumps(python_interfaces(scripts), ensure_ascii=True)
        + '\n\nSKILL.md (one-based line numbers)\n'
        + '\n'.join(f'{i}: {line}' for i, line in enumerate(skill_text.splitlines(), 1))
        + '\n\nPUBLIC_SCRIPTS\n' + json.dumps(scripts, ensure_ascii=True))


def validate_assessment(payload, *, request_text, skill_text, scripts):
    if not isinstance(payload, dict) or set(payload) != {'summary', 'reviews', 'edits'}:
        raise ValueError('invocation_review_schema_invalid')
    if not isinstance(payload['summary'], str) or not isinstance(payload['reviews'], list):
        raise ValueError('invocation_review_types_invalid')
    items = {r['id']: r for r in inventory(skill_text)}
    reviews = {}
    for review in payload['reviews']:
        if not isinstance(review, dict) or set(review) != {'id', 'status', 'reason', 'evidence'}:
            raise ValueError('invocation_review_item_invalid')
        identity, status = review['id'], review['status']
        if identity not in items or identity in reviews:
            raise ValueError('invocation_review_identity_invalid')
        if status not in ('CONSISTENT', 'CONTRADICTED', 'UNCERTAIN', 'REFERENCE_ONLY'):
            raise ValueError('invocation_review_status_invalid')
        if not isinstance(review['reason'], str) or not isinstance(review['evidence'], list) or len(review['evidence']) > 4:
            raise ValueError('invocation_review_evidence_schema_invalid')
        if status == 'REFERENCE_ONLY' and items[identity]['kind'] not in ('path_reference', 'unclassified_block'):
            raise ValueError('executable_invocation_cannot_be_dismissed_as_reference')
        if status in ('CONSISTENT', 'CONTRADICTED') and not review['evidence']:
            raise ValueError('invocation_script_evidence_required')
        for evidence in review['evidence']:
            if not isinstance(evidence, dict) or set(evidence) != {'path', 'quote'}:
                raise ValueError('invocation_script_evidence_invalid')
            quote = evidence['quote']
            if evidence['path'] not in scripts or not isinstance(quote, str) or not quote.strip() or quote not in scripts[evidence['path']]:
                raise ValueError('invocation_script_quote_not_grounded')
        reviews[identity] = review
    if set(reviews) != set(items):
        raise ValueError('invocation_review_coverage_incomplete')
    if not isinstance(payload['edits'], list) or len(payload['edits']) > 8:
        raise ValueError('invocation_edit_budget_invalid')
    lines = skill_text.splitlines(); edits = []; covered = set(); occupied = []
    begin = next((i for i, line in enumerate(lines, 1) if BEGIN_MARKER in line), None)
    end = next((i for i, line in enumerate(lines, 1) if END_MARKER in line), len(lines))
    for edit in payload['edits']:
        if not isinstance(edit, dict) or set(edit) != {'start_line', 'end_line', 'old_text', 'new_text', 'evidence_ids'}:
            raise ValueError('invocation_edit_schema_invalid')
        a, b = edit['start_line'], edit['end_line']
        if type(a) is not int or type(b) is not int or not 1 <= a <= b <= len(lines):
            raise ValueError('invocation_edit_range_invalid')
        if edit['old_text'] != '\n'.join(lines[a - 1:b]):
            raise ValueError('invocation_edit_original_lines_mismatch')
        if not isinstance(edit['new_text'], str) or len(edit['new_text'].encode()) > 16000:
            raise ValueError('invocation_edit_replacement_invalid')
        if not edit['new_text'].strip() or ''.join(edit['old_text'].split()) == ''.join(edit['new_text'].split()):
            raise ValueError('invocation_workflow_deletion_or_noop')
        if (begin and not (b < begin or a > end)) or BEGIN_MARKER in edit['new_text'] or END_MARKER in edit['new_text']:
            raise ValueError('invocation_edit_overlaps_required_contract')
        if any(not (b < x or a > y) for x, y in occupied):
            raise ValueError('invocation_edits_overlap')
        ids = edit['evidence_ids']
        if not isinstance(ids, list) or not ids or any(i not in reviews or reviews[i]['status'] != 'CONTRADICTED' for i in ids):
            raise ValueError('invocation_edit_not_backed_by_contradiction')
        spans = [(items[i]['line'], items[i]['end_line']) for i in ids]
        if any(b < start or a > end for start, end in spans) or any(
            not any(start <= line <= end for start, end in spans)
            for line in range(a, b + 1)
        ):
            raise ValueError('invocation_edit_outside_cited_blocks')
        occupied.append((a, b)); covered.update(ids); edits.append(copy.deepcopy(edit))
    if covered != {i for i, r in reviews.items() if r['status'] == 'CONTRADICTED'}:
        raise ValueError('identified_contradiction_not_addressed')
    return dict(method=METHOD, summary=payload['summary'][:800], original_summary_length=len(payload['summary']),
        reviews=list(reviews.values()), edits=edits, script_evidence=[],
        status='CONTRADICTED' if edits else ('UNCERTAIN' if any(r['status']=='UNCERTAIN' for r in reviews.values()) else 'SATISFIED'),
        coverage_complete=True, semantic_correctness_verified=False, source_scope='public_package_only')


def apply_validated_edits(skill_text, assessment):
    lines = skill_text.splitlines(keepends=True)
    for edit in sorted(assessment['edits'], key=lambda e: e['start_line'], reverse=True):
        replacement = edit['new_text']
        if not replacement.endswith('\n'):
            replacement += '\n'
        lines[edit['start_line'] - 1:edit['end_line']] = [replacement]
    return ''.join(lines)
