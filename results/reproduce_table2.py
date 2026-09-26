"""Recompute every Table 2 value from the frozen run outcomes (stdlib only)."""
from collections import defaultdict
import csv
from fractions import Fraction
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent
MODELS = {'gpt-5.5': 'GPT-5.5', 'gpt-5.6-sol': 'GPT-5.6-Sol',
          'claude-opus-5': 'Claude Opus 5', 'deepseek-v4-pro': 'DeepSeek V4 Pro'}
METHODS = {'md-only': 'MD-only', 'raw-package': 'Raw', 'native-ast': 'Raw+AST',
           'coevoskills': 'CoEvo', 'coevoskills-ast': 'CoEvo+AST'}
GROUPS = {'In-the-Wild': 150, 'Doc': 50, 'Script': 50, 'Joint': 50, 'Overall': 300}


def calculate():
    records = list(csv.DictReader((ROOT / 'outcomes.csv').open()))
    assert len(records) == 18000
    index, tasks = {}, {}
    for r in records:
        key = r['model'], r['method'], r['task_id'], int(r['repeat'])
        assert key not in index and r['status'] in {'pass', 'fail'}
        assert key[0] in MODELS and key[1] in METHODS and key[3] in {1, 2, 3}
        index[key] = r['status'] == 'pass'
        group = 'In-the-Wild' if r['track'] == 'SourceRepair150' else {
            'doc_fault': 'Doc', 'script_fault': 'Script', 'joint_fault': 'Joint'}[r['state']]
        assert r['task_id'] not in tasks or tasks[r['task_id']] == group
        tasks[r['task_id']] = group
    assert len(tasks) == 300
    ids = {g: sorted(t for t, v in tasks.items() if v == g or g == 'Overall') for g in GROUPS}
    assert {g: len(v) for g, v in ids.items()} == GROUPS
    table, summary = {}, []
    for model in MODELS:
        for method in METHODS:
            displayed = []
            for group, tids in ids.items():
                runs = [[index[(model, method, tid, repeat)] for repeat in (1, 2, 3)] for tid in tids]
                values = [Fraction(100 * sum(map(sum, runs)), 3 * len(tids)),
                          Fraction(100 * sum(map(any, runs)), len(tids)),
                          Fraction(100 * sum(map(all, runs)), len(tids))]
                shown = [f'{float(v):.1f}' for v in values]
                displayed.extend(shown)
                summary.append(dict(model=model, method=method, group=group, tasks=len(tids),
                                    avg=shown[0], pass_at_3=shown[1], hit_all_3=shown[2]))
            table[MODELS[model], METHODS[method]] = displayed
    return table, summary


def main():
    computed, summary = calculate()
    expected, model = {}, None
    for line in (ROOT / 'table2.tex').read_text().splitlines():
        for name in MODELS.values():
            if '\\textbf{' + name + '}' in line:
                model = name
        if ' & ' in line:
            method, cells = line.split(' & ', 1)
            if method in METHODS.values():
                expected[model, method] = re.findall(r'\d+\.\d+', cells)
    assert len(expected) == 20 and expected == computed, 'Table 2 mismatch'
    print(json.dumps({'records': 18000, 'tasks': 300, 'verified_table_cells': 300,
                      'table2_matches': True, 'model_calls': 0}, indent=2))
    return summary


if __name__ == '__main__':
    main()
