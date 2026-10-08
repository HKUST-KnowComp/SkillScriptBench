"""Evaluate one original Clean/Script pair to check a scoring installation."""
import argparse
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from examples.check_results import EXPECTED, main as check_main
from ssbench import Benchmark


def prepare_manifest(output, bundle=None):
    output = Path(output).resolve()
    protected = [ROOT / 'benchmark']
    if bundle is not None:
        protected.append(Path(bundle).resolve())
    for source in protected:
        if output.is_relative_to(source) or source.is_relative_to(output):
            raise ValueError('output must not overlap benchmark or evaluator assets')
    benchmark = Benchmark(ROOT / 'benchmark')
    rows = []
    for task_id in EXPECTED:
        task = benchmark.task(task_id)
        task.verify()
        rows.append({'task_id': task_id, 'candidate': str(task.package), 'run': 1})
    output.mkdir(parents=True, exist_ok=False)
    manifest = output / 'candidates.json'
    manifest.write_text(json.dumps(rows, indent=2) + '\n')
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, help='extracted evaluator directory')
    parser.add_argument('--output', type=Path, required=True, help='new directory for this example')
    parser.add_argument('--prepare-only', action='store_true',
                        help='verify the two inputs and write the manifest without evaluation')
    args = parser.parse_args(argv)
    if not args.prepare_only and args.bundle is None:
        parser.error('--bundle is required unless --prepare-only is used')
    try:
        manifest = prepare_manifest(args.output, args.bundle)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f'Candidate manifest: {manifest}', flush=True)
    if args.prepare_only:
        print('Inputs verified; evaluation has not run.')
        return 0
    output = manifest.parent / 'evaluation'
    process = subprocess.run([
        sys.executable, '-B', str(ROOT / 'evaluate.py'),
        '--bundle', str(args.bundle.resolve()), '--manifest', str(manifest),
        '--output', str(output), '--workers', '1',
    ], cwd=ROOT)
    if process.returncode:
        print(f'Evaluation did not complete successfully. Inspect {output}', file=sys.stderr)
        return process.returncode
    return check_main([str(output / 'scores.csv')])


if __name__ == '__main__':
    raise SystemExit(main())
