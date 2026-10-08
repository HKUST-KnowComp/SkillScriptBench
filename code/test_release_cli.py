"""Offline transport boundary checks; all credentials and responses are synthetic."""
import io
import http.client
import json
from pathlib import Path
import sys
import urllib.request

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_revision as api
from skillscriptbench import discovery_transport_v1108 as transport


@pytest.mark.parametrize('target', ['http://other.invalid/collect', 'https://other.invalid/collect',
                                   'https://provider.invalid/moved'])
def test_bearer_redirect_is_refused(target):
    request = urllib.request.Request('https://provider.invalid/v1/chat/completions',
                                     data=b'{}', headers={'Authorization': 'Bearer synthetic'})
    with pytest.raises(ValueError, match='provider_redirect_refused'):
        transport.NoRedirect().redirect_request(request, None, 302, 'Found', {}, target)


@pytest.mark.parametrize('body', [[], {'choices': [None]}, {'choices': [{'delta': []}]},
                                {'choices': [{'delta': {'tool_calls': [None]}}]}])
def test_malformed_stream_is_rejected(body):
    class Response(io.BytesIO):
        headers = {'Content-Type': 'text/event-stream'}
    response = Response(('data: ' + json.dumps(body) + '\n').encode())
    with pytest.raises(ValueError):
        transport.decode(response, {})


def test_invalid_provider_json_falls_back_and_finalizes(tmp_path, monkeypatch):
    for name in ('parent', 'proposal'):
        package = tmp_path / name
        package.mkdir()
        (package / 'SKILL.md').write_text('# Toy\nNo scripts.\n', encoding='utf-8')
    root = tmp_path / 'run'
    api.prepare(tmp_path / 'parent', tmp_path / 'proposal', 'Preserve the document.', root)

    class Response(io.BytesIO):
        headers = {'Content-Type': 'application/json'}

    class Opener:
        def open(self, request, timeout):
            return Response(b'[]')

    monkeypatch.setattr(transport.urllib.request, 'build_opener', lambda *handlers: Opener())
    callback = api.script_runner.provider_callback(root, 'synthetic', base_url='https://provider.invalid/v1')
    result = api.run_prepared(root, callback)
    assert result['document_status'] == 'fallback_unchanged_document'
    assert api.hash_tree(root / 'final/package') == api.hash_tree(tmp_path / 'proposal')
    assert (root / 'FINAL.json').is_file()
    assert api.read_json(root / 'provider/discovery/TRANSPORT_FAILURE.json')['reason'] == 'invalid_provider_response_shape'


def test_streamed_tool_fragments_and_usage_are_accepted():
    class Response(io.BytesIO):
        headers = {'Content-Type': 'text/event-stream'}

    chunks = [
        {'id': 'completion-1', 'model': 'synthetic-model', 'choices': [{'index': 0,
         'delta': {'tool_calls': [{'index': 0, 'id': 'tool-1', 'function': {
             'name': 'example_tool', 'arguments': '{"ok":'}}]}}]},
        {'id': 'completion-1', 'model': 'synthetic-model', 'choices': [{'index': 0,
         'delta': {'tool_calls': [{'index': 0, 'function': {'arguments': 'true}'}}]},
         'finish_reason': 'tool_calls'}]},
        {'choices': [], 'usage': {'completion_tokens': 12}},
    ]
    body = ''.join('data: ' + json.dumps(chunk) + '\n\n' for chunk in chunks) + 'data: [DONE]\n'
    metadata = {}
    result = transport.decode(Response(body.encode()), metadata)
    assert result['model'] == 'synthetic-model'
    assert result['usage'] == {'completion_tokens': 12}
    call = result['choices'][0]['message']['tool_calls'][0]
    assert call['function'] == {'name': 'example_tool', 'arguments': '{"ok":true}'}
    assert metadata['done'] is True


def test_interrupted_http_response_is_retried_once(tmp_path, monkeypatch):
    api.write_json(tmp_path / 'PROTOCOL.json', {'model': 'synthetic-model', 'temperature': 0,
                   'extraction_token_limit': 100, 'revision_token_limit': 100})
    attempts = []

    class Response(io.BytesIO):
        headers = {'Content-Type': 'application/json'}

    class Opener:
        def open(self, request, timeout):
            attempts.append(request.full_url)
            if len(attempts) == 1:
                raise http.client.IncompleteRead(b'', 10)
            response = {'model': 'synthetic-model', 'choices': [{'finish_reason': 'tool_calls',
                'message': {'tool_calls': [{'function': {'name': 'example_tool', 'arguments': '{}'}}]}}]}
            return Response(json.dumps(response).encode())

    monkeypatch.setattr(transport.urllib.request, 'build_opener', lambda *handlers: Opener())
    callback = api.script_runner.provider_callback(tmp_path, 'synthetic', base_url='https://provider.invalid/v1')
    assert callback('Toy prompt', {'function': {'name': 'example_tool'}}, 'discovery') == {}
    assert len(attempts) == 2
    assert api.read_json(tmp_path / 'provider/discovery/ATTEMPT_1.json')['status'] == 'rejected'
    assert api.read_json(tmp_path / 'provider/discovery/ATTEMPT_2.json')['status'] == 'accepted'
