import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from ssbench import Benchmark
from ssbench.benchmark import package_hash


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'benchmark'
        package = self.root / 'tasks/toy/package'
        package.mkdir(parents=True)
        (package / 'SKILL.md').write_text('# Toy\n')
        request = package.parent / 'REQUEST.md'
        request.write_text('Preserve the toy skill.\n')
        self.row = {'task_id': 'toy', 'request': 'tasks/toy/REQUEST.md',
                    'package': 'tasks/toy/package',
                    'expected_request_sha256': hashlib.sha256(request.read_bytes()).hexdigest(),
                    'expected_package_tree_hash': package_hash(package)}
        self.write_manifest()
        (self.root / 'splits').mkdir()
        (self.root / 'splits/all.json').write_text(json.dumps({'task_ids': ['toy']}))

    def write_manifest(self):
        (self.root / 'TASKS.json').write_text(json.dumps({'tasks': [self.row]}))

    def test_verify_and_materialize(self):
        b = Benchmark(self.root)
        self.assertEqual(b.verify()['verified_tasks'], 1)
        dest = Path(self.tmp.name) / 'work'
        b.task('toy').materialize(dest)
        self.assertEqual(sorted(p.name for p in dest.iterdir()), ['REQUEST.md', 'package'])
        self.assertEqual(package_hash(dest / 'package'), self.row['expected_package_tree_hash'])
        with self.assertRaises(FileExistsError):
            b.task('toy').materialize(dest)

    def test_tamper_is_rejected(self):
        (self.root / self.row['request']).write_text('tampered')
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            Benchmark(self.root).verify()

    def test_traversal_is_rejected(self):
        self.row['request'] = '../secret'
        self.write_manifest()
        with self.assertRaises(ValueError):
            Benchmark(self.root)

    def test_symlink_is_rejected(self):
        (self.root / self.row['package'] / 'linked').symlink_to(self.root / self.row['request'])
        with self.assertRaisesRegex(ValueError, 'symlink'):
            Benchmark(self.root).verify()

    def test_unknown_split_and_task(self):
        b = Benchmark(self.root)
        with self.assertRaises(ValueError):
            b.task('missing')
        with self.assertRaises(ValueError):
            b.tasks('../all')


if __name__ == '__main__':
    unittest.main()
