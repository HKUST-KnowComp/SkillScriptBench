"""Check the instructions actually passed to the model without model calls."""
from pathlib import Path
from test_entrypoint import make_package
import run_revision as api


def test_published_revision_instruction_is_used_at_runtime(tmp_path):
    parent = make_package(tmp_path, 'parent')
    proposal = make_package(tmp_path, 'proposal')
    root = tmp_path / 'run'
    api.prepare(parent, proposal, 'Select the supplied index.', root)
    bundle = api.read_json(root / 'INPUT.json')
    packet = {'snapshot': 'parent', 'view': 'parent-signal'}
    actual = api.script_runner.bridge.revision_prompt(bundle, packet)
    published = (Path(__file__).parent / 'prompts/script_revision.txt').read_text().splitlines()[0]
    assert actual.split('\nPUBLIC_REVISION_INPUT.json\n')[0] == published
    assert bundle['legacy_shortlist_consumed'] is False
