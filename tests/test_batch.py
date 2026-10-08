import json
from types import SimpleNamespace

import pytest

from ssbench.batch import load_candidates, summarize, score_one


def test_manifest_resolves_paths_and_rejects_duplicates(tmp_path):
    candidate = tmp_path / 'package'
    candidate.mkdir()
    (candidate / 'SKILL.md').write_text('# Skill')
    bundle = SimpleNamespace(task=lambda tid: {'state': 'clean'})
    manifest = tmp_path / 'candidates.json'
    row = {'task_id': 'toy', 'candidate': 'package', 'run': 1}
    manifest.write_text(json.dumps([row]))
    selected = load_candidates(bundle, manifest=manifest)
    assert selected[0]['candidate'] == str(candidate)
    assert selected[0]['run'] == '1'
    manifest.write_text(json.dumps([row, row]))
    with pytest.raises(ValueError, match='duplicate'):
        load_candidates(bundle, manifest=manifest)


def test_repeat_metrics_and_incomplete_groups():
    rows = [{'task_id': task, 'run': str(i + 1), 'status': status}
            for task, statuses in [('a', ['pass', 'pass', 'pass']),
                                   ('b', ['pass', 'fail', 'fail'])]
            for i, status in enumerate(statuses)]
    summary = summarize(rows)
    assert summary['avg_pct'] == pytest.approx(400 / 6)
    assert summary['p_at_3_pct'] == 100
    assert summary['hit_3_pct'] == 50
    assert summarize(rows[:-1])['hit_3_pct'] is None
    rows[0]['status'] = 'error'
    assert summarize(rows)['success_rate_pct'] is None
    assert summarize([])['avg_pct'] is None


def test_score_one_rejects_incomplete_receipt(tmp_path, monkeypatch):
    def prepare(task, candidate, job):
        job.mkdir(parents=True)
        (job / 'SCORE.json').write_text(json.dumps({
            'task_id': task, 'status': 'pass', 'candidate_unchanged': True,
            'whole_task_scored': False}))
    bundle = SimpleNamespace(prepare=prepare)
    monkeypatch.setattr('ssbench.batch.subprocess.run',
                        lambda *a, **k: SimpleNamespace(returncode=0))
    row = {'task_id': 'toy', 'run': '1', 'candidate': '/unused', 'state': 'clean'}
    result = score_one(bundle, row, tmp_path)
    assert result['status'] == 'error'
    assert result['reason'] == 'invalid scoring receipt'
