"""Browse task inputs, create workspaces, or invoke the revision runtime."""
import argparse
import json
import sys

from .benchmark import Benchmark


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == 'evaluation':
        from .evaluation import main as evaluation_main
        return evaluation_main(argv[1:])
    if argv and argv[0] == 'revise':
        from ssbench._runtime import run_revision
        original = sys.argv
        try:
            sys.argv = ['skillscriptbench revise', *argv[1:]]
            return run_revision.main()
        finally:
            sys.argv = original
    parser = argparse.ArgumentParser(description=__doc__,
        epilog='Revision: skillscriptbench revise --help; evaluation assets: skillscriptbench evaluation --help')
    parser.add_argument('--benchmark', default='benchmark', help='path to the benchmark directory')
    subs = parser.add_subparsers(dest='action', required=True)
    for name in ('list', 'verify'):
        sub = subs.add_parser(name)
        sub.add_argument('--split', choices=('all', 'main_results', 'clean'), default='all')
    show = subs.add_parser('show')
    show.add_argument('task_id')
    sources = subs.add_parser('sources', help='show upstream attribution, not task inputs')
    sources.add_argument('task_id')
    subs.add_parser('verify-sources', help='verify attribution, group mapping, and license notices')
    materialize = subs.add_parser('materialize')
    materialize.add_argument('task_id')
    materialize.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    try:
        bench = Benchmark(args.benchmark)
        if args.action == 'list':
            result = {'split': args.split, 'task_ids': [t.task_id for t in bench.tasks(args.split)]}
        elif args.action == 'verify':
            result = bench.verify(args.split)
        elif args.action == 'verify-sources':
            result = bench.verify_sources()
        elif args.action == 'sources':
            result = bench.sources()[args.task_id]
        else:
            task = bench.task(args.task_id)
            task.verify()
            if args.action == 'show':
                result = {'task_id': task.task_id, 'package': str(task.package),
                          'request': task.request.read_text()}
            else:
                result = {'task_id': task.task_id, 'workspace': str(task.materialize(args.output))}
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(2, f'error: {exc}\n')
    print(json.dumps(result, indent=2))
