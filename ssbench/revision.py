"""Public revision API, backed by the reviewed LLM-discovery runtime."""


def _api():
    from ssbench._runtime import run_revision
    return run_revision


def prepare(parent, proposal, request, output, model='gpt-5.6-sol'):
    """Prepare a complete parent/proposal pair without contacting a model."""
    return _api().prepare(parent, proposal, request, output, model=model)


def run_prepared(root, callback):
    """Run LLM discovery, parser-bound script revision and Markdown alignment."""
    return _api().run_prepared(root, callback)
