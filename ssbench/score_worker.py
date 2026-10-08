"""Score a prepared package using fresh behavior and documentation executions."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

from .benchmark import package_hash
from .evaluation_worker import verify_job


def execute(job):
    job, plan, candidate, mapping = verify_job(job)
    components = {}
    names = ['document'] if mapping['state'] == 'doc_fault' else ['behavior', 'document']
    for name in names:
        filename = 'BEHAVIOR_RESULT.json' if name == 'behavior' else 'DOCUMENT_RESULT.json'
        if (job / filename).exists():
            raise ValueError('prepare a new job; prior component results are not reused')
        module = 'evaluation_worker' if name == 'behavior' else 'document_worker'
        with (job / (name + '.log')).open('w') as log:
            proc = subprocess.run([sys.executable, '-B', '-m', 'ssbench.' + module, '--job', str(job)],
                                  stdout=log, stderr=subprocess.STDOUT, timeout=2400)
        if (job / filename).exists():
            result = json.loads((job / filename).read_text())
            components[name] = {'status': result.get('status', 'error'), 'file': filename}
        else:
            components[name] = {'status': 'error', 'reason': 'component_no_result', 'returncode': proc.returncode}
    unchanged = package_hash(candidate) == plan['candidate_tree_hash']
    if not unchanged:
        raise ValueError('evaluation changed candidate')
    statuses = [v['status'] for v in components.values()]
    status = 'error' if any(s not in {'pass', 'fail'} for s in statuses) else 'pass' if all(s == 'pass' for s in statuses) else 'fail'
    return {'task_id': mapping['task_id'], 'candidate_tree_hash': plan['candidate_tree_hash'],
            'status': status, 'components': components, 'candidate_unchanged': True,
            'whole_task_scored': status in {'pass', 'fail'}, 'model_calls': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job', type=Path, required=True)
    args = parser.parse_args()
    output = args.job / 'SCORE.json'
    if not args.job.is_dir() or output.exists():
        parser.exit(2, 'use a new prepared job directory\n')
    try:
        result = execute(args.job)
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as exc:
        result = {'status': 'error', 'reason': str(exc), 'whole_task_scored': False}
    output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
    return 2 if result['status'] == 'error' else 0


if __name__ == '__main__':
    raise SystemExit(main())
