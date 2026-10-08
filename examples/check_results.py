"""Check the two expected outcomes from the installation example."""
import argparse
import csv
from pathlib import Path


EXPECTED = {
    'ssb-deep60-case-18b5b153d97659d7': ('clean', 'pass'),
    'ssb-deep60-case-f8e43276eccd8365': ('script_fault', 'fail'),
}


def check_results(scores):
    with Path(scores).open(newline='') as file:
        rows = list(csv.DictReader(file))
    if len(rows) != len(EXPECTED) or {row.get('task_id') for row in rows} != set(EXPECTED):
        raise ValueError('expected exactly one result for each of the two example tasks')
    for row in rows:
        state, status = EXPECTED[row['task_id']]
        if (row.get('run'), row.get('state'), row.get('status')) != ('1', state, status):
            raise ValueError(
                f"{row['task_id']}: expected run 1, state {state}, status {status}; "
                f"got run {row.get('run')}, state {row.get('state')}, status {row.get('status')}"
            )
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('scores', type=Path, help='evaluation/scores.csv from check_installation.py')
    args = parser.parse_args(argv)
    try:
        rows = check_results(args.scores)
    except (OSError, ValueError, csv.Error) as exc:
        parser.exit(1, f'Installation example did not match expectations: {exc}\n')
    for row in rows:
        print(f"{row['task_id']}: {row['status']} (expected)")
    print('Both recorded outcomes match the installation example expectations.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
