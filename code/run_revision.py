"""Portable entry point for the later LLM discovery and document alignment code.

The callback API permits offline tests and different model providers. The CLI
uses the existing OpenLux transport and reads credentials only from a supplied fd.
"""
import argparse
import json
import os
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE / 'runtime/ASTSkill'), str(HERE / 'runtime/SkillScriptBench'),
               str(HERE / 'runtime/SkillScriptBench/doc_fault_v2')]
import bvi_skill_evo
bvi_skill_evo.__path__.append(str(HERE / 'runtime/SkillScriptBench/bvi_skill_evo'))
from skillscriptbench import semantic_discovery_native_runner_v1110 as script_runner
from skillscriptbench.io_utils import read_json, write_json, copy_tree_clean, hash_tree, canonical_json_hash
import invocation_document_audit_v5 as document


def prepare(parent, proposal, request, output, model='gpt-5.5'):
    result = script_runner.prepare(parent, proposal, request, output, model=model)
    (Path(output) / 'REQUEST.md').write_text(request)
    write_json(Path(output) / 'PIPELINE.json', {
        'name': 'AST-Guided Skill Revision', 'implementation': 'llm-discovery-plus-document-alignment',
        'scripts': 'semantic_discovery_native_runner_v1110', 'document': document.METHOD,
        'semantic_discovery': 'LLM', 'regex_semantic_miner': False,
        'historical_main_result_reproduction': False})
    return result


def run_prepared(root, callback):
    root = Path(root)
    if (root / 'FINAL.json').exists():
        raise FileExistsError('already_finalized')
    request = (root / 'REQUEST.md').read_text()
    if request != read_json(root / 'INPUT.json')['request']:
        raise ValueError('prepared_request_changed')
    script_result = script_runner.run_prepared(root, callback)
    candidate = root / 'selected/package'
    final = root / 'final/package'
    copy_tree_clean(candidate, final)
    sources = script_runner.bridge.syntax.public_sources(candidate)
    scripts = {p: text for p, text in sources.items() if p != 'SKILL.md'}
    prompt = document.build_prompt(request, sources['SKILL.md'], scripts)
    (root / 'DOCUMENT_PROMPT.txt').write_text(prompt)
    try:
        payload = callback(prompt, document.CONTRADICTION_ALIGNMENT_TOOL, 'document-alignment')
        write_json(root / 'DOCUMENT_RESPONSE.json', payload)
        report = document.validate_assessment(payload, request_text=request,
                    skill_text=sources['SKILL.md'], scripts=scripts)
        updated = document.apply_validated_edits(sources['SKILL.md'], report)
        (final / 'SKILL.md').write_text(updated)
    except (ValueError, KeyError, TypeError, RuntimeError, OSError) as exc:
        report = {'status': 'fallback_unchanged_document', 'error_class': type(exc).__name__}
    write_json(root / 'DOCUMENT_REPORT.json', report)
    result = {'status': 'frozen', 'package': str(final),
              'script_result': script_result, 'document_status': report['status'],
              'tree_hash': canonical_json_hash(hash_tree(final)),
              'behavioral_success_evaluated': False}
    write_json(root / 'FINAL.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'run'])
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--parent', type=Path)
    parser.add_argument('--proposal', type=Path)
    parser.add_argument('--request', type=Path)
    parser.add_argument('--model', choices=['gpt-5.5', 'gpt-5.6-sol'], default='gpt-5.5')
    parser.add_argument('--credential-fd', type=int, default=3)
    args = parser.parse_args()
    if args.action == 'prepare':
        if not all((args.parent, args.proposal, args.request)):
            parser.error('prepare needs --parent --proposal --request')
        result = prepare(args.parent, args.proposal, args.request.read_text(), args.output, args.model)
    else:
        with os.fdopen(os.dup(args.credential_fd)) as stream:
            key = stream.readline(4096).strip()
        if not key:
            parser.error('empty credential fd')
        result = run_prepared(args.output, script_runner.provider_callback(args.output, key))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
