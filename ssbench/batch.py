"""Batch orchestration around the existing whole-package scoring protocol."""
import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
import json
from pathlib import Path
import re
import subprocess
import sys

from .evaluation import EvaluationBundle


def load_candidates(bundle, *, task=None, candidate=None, manifest=None):
    if manifest:
        manifest = Path(manifest).resolve()
        rows = json.loads(manifest.read_text())
        base = manifest.parent
        if not isinstance(rows, list) or not rows:
            raise ValueError('manifest must be a nonempty JSON array')
    else:
        rows = [{'task_id': task, 'candidate': str(Path(candidate).resolve()), 'run': '1'}]
        base = Path.cwd()
    selected, seen = [], set()
    for row in rows:
        tid, run = row['task_id'], str(row.get('run', '1'))
        if not isinstance(tid, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+', tid) or tid in {'.', '..'}:
            raise ValueError('invalid task ID')
        if run not in {'1', '2', '3'}:
            raise ValueError('run must be 1, 2, or 3')
        entry = bundle.task(tid)
        key = (tid, run)
        if key in seen:
            raise ValueError('duplicate task/run pair: ' + str(key))
        seen.add(key)
        path = (base / row['candidate']).resolve()
        if not path.is_dir() or not (path / 'SKILL.md').is_file():
            raise ValueError('candidate package missing: ' + str(path))
        selected.append({'task_id': tid, 'run': run, 'candidate': str(path),
                         'state': entry['state']})
    return selected


def summarize(rows):
    counts = Counter(row['status'] for row in rows)
    complete = bool(rows) and not counts['error']
    by_task = defaultdict(list)
    for row in rows:
        by_task[row['task_id']].append(row)
    three = complete and all({r['run'] for r in items} == {'1', '2', '3'}
                             and len(items) == 3 for items in by_task.values())
    return {'tasks': len(by_task), 'runs': len(rows),
            'pass': counts['pass'], 'fail': counts['fail'], 'error': counts['error'],
            'success_rate_pct': 100 * counts['pass'] / len(rows) if complete else None,
            'avg_pct': 100 * counts['pass'] / len(rows) if three else None,
            'p_at_3_pct': 100 * sum(any(r['status'] == 'pass' for r in items)
                                  for items in by_task.values()) / len(by_task) if three else None,
            'hit_3_pct': 100 * sum(all(r['status'] == 'pass' for r in items)
                                 for items in by_task.values()) / len(by_task) if three else None}


def score_one(bundle, row, output):
    job = output / 'jobs' / row['task_id'] / ('run-' + row['run'])
    result = dict(row, status='error', score_file=str(job.relative_to(output) / 'SCORE.json'))
    try:
        bundle.prepare(row['task_id'], row['candidate'], job)
        with (job / 'score.log').open('w') as log:
            process = subprocess.run([sys.executable, '-B', '-m', 'ssbench.score_worker',
                                      '--job', str(job)], stdout=log, stderr=subprocess.STDOUT,
                                     timeout=5400)
        receipt = json.loads((job / 'SCORE.json').read_text())
        status = receipt.get('status')
        if process.returncode or status not in {'pass', 'fail'}:
            result['reason'] = receipt.get('reason', 'scoring did not complete')
        elif (receipt.get('task_id') != row['task_id'] or not receipt.get('candidate_unchanged')
              or not receipt.get('whole_task_scored')):
            result['reason'] = 'invalid scoring receipt'
        else:
            result['status'] = status
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        result['reason'] = str(exc)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--task', help='one benchmark task ID')
    parser.add_argument('--candidate', type=Path, help='complete candidate package for --task')
    parser.add_argument('--manifest', type=Path, help='JSON array of task_id, candidate, run')
    parser.add_argument('--output', type=Path, help='new output directory')
    parser.add_argument('--workers', type=int, choices=range(1, 5), default=1)
    parser.add_argument('--dry-run', action='store_true', help='validate inputs and list jobs; execute nothing')
    args = parser.parse_args(argv)
    if bool(args.manifest) == bool(args.task or args.candidate):
        parser.error('use either --manifest or --task with --candidate')
    if not args.manifest and not (args.task and args.candidate):
        parser.error('--task and --candidate are required together')
    if not args.dry_run and not args.output:
        parser.error('--output is required for execution')
    try:
        bundle = EvaluationBundle(args.bundle)
        rows = load_candidates(bundle, task=args.task, candidate=args.candidate, manifest=args.manifest)
        if args.dry_run:
            print(json.dumps({'jobs': rows, 'executed': False}, indent=2))
            return 0
        output = args.output.resolve()
        for source in [bundle.root, *(Path(row['candidate']) for row in rows)]:
            if output.is_relative_to(source) or source.is_relative_to(output):
                raise ValueError('output must not overlap candidate or evaluator assets')
        output.mkdir(parents=True, exist_ok=False)
        (output / 'INPUTS.json').write_text(json.dumps(rows, indent=2) + '\n')
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        pending = [pool.submit(score_one, bundle, row, output) for row in rows]
        for future in as_completed(pending):
            result = future.result()
            results.append(result)
            print(f"[{len(results)}/{len(rows)}] {result['task_id']} run {result['run']}: {result['status']}", flush=True)
    results.sort(key=lambda row: (row['task_id'], row['run']))
    with (output / 'scores.csv').open('w', newline='') as file:
        writer = csv.DictWriter(file, fieldnames=['task_id', 'run', 'state', 'status',
                                                 'candidate', 'score_file', 'reason'])
        writer.writeheader()
        writer.writerows(results)
    groups = {state: summarize([r for r in results if r['state'] == state])
              for state in sorted({r['state'] for r in results})}
    summary = {'all': summarize(results),
               'repair': summarize([r for r in results if r['state'] != 'clean']),
               'clean': summarize([r for r in results if r['state'] == 'clean']),
               'by_state': groups}
    (output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary, indent=2))
    return 2 if any(r['status'] == 'error' for r in results) else 0
