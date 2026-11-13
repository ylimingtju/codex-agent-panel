"""Run with python3 -B test_server.py. All writable state is isolated in a temporary directory."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import uuid

import server

HERE = Path(__file__).resolve().parent
MANAGER, WORKER, OTHER = [str(uuid.uuid4()) for _ in range(3)]


def check(folder):
    db = sqlite3.connect(folder / 'state_5.sqlite')
    db.executescript('''
        CREATE TABLE threads (id TEXT PRIMARY KEY, thread_source TEXT, agent_path TEXT,
            agent_nickname TEXT, model TEXT, reasoning_effort TEXT);
        CREATE TABLE thread_spawn_edges (parent_thread_id TEXT, child_thread_id TEXT PRIMARY KEY);
    ''')
    db.executemany('INSERT INTO threads VALUES (?,?,?,?,?,?)', [
        (MANAGER, 'user', None, None, 'sample-model', 'high'),
        (OTHER, 'user', None, None, 'sample-model', 'high'),
        (WORKER, 'subagent', '/root/reviewer', 'Reviewer', 'sample-model', 'medium'),
    ])
    db.execute('INSERT INTO thread_spawn_edges VALUES (?,?)', (MANAGER, WORKER))
    db.commit()
    db.close()
    env = dict(os.environ, SAVVY_PROGRESS_HOME=str(folder / 'records'), CODEX_HOME=str(folder), PYTHONDONTWRITEBYTECODE='1')

    def admin(caller, *args):
        output = subprocess.check_output([sys.executable, '-B', str(HERE / 'state.py'), *args],
                                         text=True, env=dict(env, CODEX_THREAD_ID=caller))
        return json.loads(output)

    proc = subprocess.Popen([sys.executable, '-B', str(HERE / 'server.py')], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    ids = iter(range(1, 1000))

    def ask(method, params=None):
        i = next(ids)
        proc.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': i, 'method': method, 'params': params or {}}) + '\n')
        proc.stdin.flush()
        reply = json.loads(proc.stdout.readline())
        assert reply['id'] == i
        return reply

    def call(name, args=None, meta=None):
        return ask('tools/call', {'name': name, 'arguments': args or {}, '_meta': meta or {}})

    try:
        assert 'tools' in ask('initialize', {'protocolVersion': '2025-11-25'})['result']['capabilities']
        assert ask('initialize', {'protocolVersion': 'unrecognized'})['result']['protocolVersion'] == server.PROTOCOLS[0]
        proc.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        descriptors = {t['name']: t for t in ask('tools/list')['result']['tools']}
        assert descriptors.keys() == {'progress', 'step', 'show_track', 'show_panel', 'pair', 'snapshot'}
        assert descriptors['show_panel']['_meta']['openai/ui']['entrypoints'] == [{'type': 'thread'}]
        assert all(descriptors[t]['_meta']['ui']['visibility'] == ['model'] for t in ('progress', 'step'))
        assert all(descriptors[t]['_meta']['ui']['visibility'] == ['app'] for t in ('snapshot', 'pair'))
        assert call('show_panel')['result']['structuredContent'] == {'paired': False}
        assert call('pair')['error']['code'] == -32602
        assert call('progress', {'binding': 'n/a', 'caller': 'manager'})['error']['code'] == -32602
        assert call('step', {'binding': 'n/a', 'done': True, 'total': 1})['error']['code'] == -32602
        assert call('snapshot', {'view_key': 'n/a'})['result']['isError']
        assert ask('resources/read', {'uri': 'file:///etc/passwd'})['error']['code'] == -32002
        for view in ask('resources/list')['result']['resources']:
            resource = ask('resources/read', {'uri': view['uri']})['result']['contents'][0]
            modes = resource['_meta']['openai/ui']
            assert modes['preferredDisplayMode'] == ('inline' if view['uri'] == server.TRACK else 'fullscreen')
            assert 'fullscreen' in modes['availableDisplayModes']
            legacy = view['uri'].replace('/v2/', '/')
            old = ask('resources/read', {'uri': legacy})['result']['contents'][0]
            assert old['uri'] == legacy and old['_meta'] == resource['_meta']
            assert resource['mimeType'] == 'text/html;profile=mcp-app'
            assert '__SAVVY_' not in resource['text'] and 'ui/initialize' in resource['text']
            assert resource['_meta']['ui']['csp']['connectDomains'] == []
            if server.sprite_uri():
                assert 'data:image/webp;base64,' in resource['text']
        if server.ASAR.exists() and server.sprite_uri():
            assert server.codey_bytes(server.ASAR).startswith(b'RIFF')
            assert len(server.sprite_uri()) < 12_000_000
        key = admin(MANAGER, 'open')['view_key']
        other_key = admin(OTHER, 'open')['view_key']
        assert other_key != key
        assert call('pair', {'view_key': key})['result']['structuredContent']['view_key'] == key
        args = {'title': '验收演示', 'tasks': [{'id': 'review', 'title': '复核'}], 'phase': 'delegate'}
        ticket = admin(MANAGER, 'authorize', 'progress', json.dumps(args))['binding']
        assert call('progress', {**args, 'title': '不匹配', 'binding': ticket})['result']['isError']
        for _ in range(2):
            written = call('progress', {**args, 'binding': ticket})['result']
            assert not written.get('isError'), written
        args = {'done': 1, 'total': 2, 'note': '交叉检查', 'task_id': 'review'}
        ticket = admin(WORKER, 'authorize', 'step', json.dumps(args))['binding']
        assert not call('step', {**args, 'binding': ticket})['result'].get('isError')
        result = call('show_track', {'view_key': key})['result']
        snapshot = result['structuredContent']
        assert snapshot['total'] == 1 and snapshot['accepted'] == 0 and snapshot['title'] == '验收演示'
        assert any(a['done'] == 1 and a['total'] == 2 for a in snapshot['agents'])
        assert '0/1' in result['content'][0]['text']
        assert not any('binding' in json.dumps(row) for row in [result, snapshot])
        assert call('snapshot', {'view_key': other_key})['result']['structuredContent']['total'] == 0
        assert call('pair', {'view_key': other_key}, {'threadId': MANAGER})['result']['isError']
        assert call('snapshot', {}, {'threadId': MANAGER})['result']['structuredContent']['view_key'] == key
        assert call('progress', {'phase': 'review'}, {'threadId': WORKER})['result']['isError']
        assert not call('progress', {'phase': 'review'}, {'threadId': MANAGER})['result'].get('isError')
        assert call('show_panel', {}, {'threadId': MANAGER})['result']['structuredContent']['view_key'] == key
        assert call('show_panel', {}, {'threadId': 'unknown'})['result']['structuredContent'] == {'paired': False}
        assert ask('unregistered')['error']['code'] == -32601 and ask('ping')['result'] == {}
        text = subprocess.check_output([sys.executable, '-B', str(HERE / 'server.py'), 'status'],
                                       input=key+'\n', text=True, env=env)
        assert '验收演示' in text and '0/1' in text and '1/2 步' in text
        assert not call('progress', {'accepted': ['review'], 'finished': True}, {'threadId': MANAGER})['result'].get('isError')
        next_key = admin(MANAGER, 'open', '--new')['view_key']
        assert next_key != key
        assert call('snapshot', {'view_key': key}, {'threadId': MANAGER})['result']['structuredContent']['status'] == 'completed'
        assert call('snapshot', {}, {'threadId': MANAGER})['result']['structuredContent']['view_key'] == next_key
    finally:
        proc.stdin.close()
        proc.wait(timeout=8)
    assert proc.returncode == 0 and not proc.stderr.read()


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='savvy-protocol-test-') as tmp:
        check(Path(tmp))
    print('PASS: stdio, two UI resources, read-only pairing, per-action identity, session isolation and CLI summary')
