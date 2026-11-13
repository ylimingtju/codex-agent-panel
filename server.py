#!/usr/bin/env python3
"""Local MCP Apps server. The only shared UI credential is the read-only view key."""
import argparse
import base64
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
import struct
import sys

import state

HERE = Path(__file__).resolve().parent
ASAR = Path('/Applications/ChatGPT.app/Contents/Resources/app.asar')
VERSION = '0.1.2'
MIME = 'text/html;profile=mcp-app'
TRACK = 'ui://savvy-progress/v2/track.html'
PANEL = 'ui://savvy-progress/v2/panel.html'
VIEWS = {TRACK: ('track', '任务进度'), PANEL: ('panel', '子代理')}
# Old tool results may still refer to the previous resource URIs.
LEGACY_VIEWS = {uri.replace('/v2/', '/'): uri for uri in VIEWS}
PROTOCOLS = ('2025-11-25', '2025-06-18', '2025-03-26', '2024-11-05')


def object_schema(properties, required=()):
    return {'type': 'object', 'properties': properties, 'required': list(required), 'additionalProperties': False}


STRING = {'type': 'string', 'minLength': 1}
IDS = {'type': 'array', 'items': STRING}
KEY = {'view_key': {**STRING, 'description': '当前工作流的只读配对码'}}
BINDING = {**STRING, 'description': '在当前代理自己的宿主 shell 运行 state.py authorize 为本次参数生成的单动作票据'}
STATUS = {'type': 'string', 'enum': ['running', 'waiting', 'ended', 'failed', 'interrupted', 'cancelled']}
PROGRESS = object_schema({'binding': BINDING, 'title': STRING, 'phase': STRING,
                          'tasks': {'type': 'array', 'items': object_schema({'id': STRING, 'title': STRING}, ('id', 'title'))},
                          'agents': {'type': 'array', 'items': object_schema({'id': STRING, 'task_id': STRING, 'status': STATUS}, ('id',))},
                          'accepted': IDS, 'reopen': IDS, 'remove': IDS, 'finished': {'type': 'boolean'}})
STEP = object_schema({'binding': BINDING, 'done': {'type': 'integer', 'minimum': 0},
                      'total': {'type': 'integer', 'minimum': 1}, 'status': STATUS, 'note': {'type': 'string'}, 'task_id': STRING})


def tool(name, title, description, schema, visibility, resource=None):
    ui = {'visibility': visibility}
    meta = {'ui': ui}
    if resource:
        ui['resourceUri'] = resource
        meta['openai/outputTemplate'] = resource
        meta['openai/ui'] = {'preferredModelDisplayMode': 'inline' if resource == TRACK else 'fullscreen'}
        if resource == PANEL:
            meta['openai/ui']['entrypoints'] = [{'type': 'thread'}]
    return {'name': name, 'title': title, 'description': description, 'inputSchema': schema,
            'annotations': {'readOnlyHint': name not in ('progress', 'step'), 'openWorldHint': False}, '_meta': meta}


TOOLS = {t['name']: t for t in (
    tool('progress', '报告总进度', '主代理声明任务、更新阶段、验收或重新打开任务。', PROGRESS, ['model']),
    tool('step', '报告代理步骤', '子代理上报自己的已完成步骤、总数及简短说明。', STEP, ['model']),
    tool('show_track', '任务进度', '在当前聊天显示实时总进度。传入本工作流的只读配对码。',
         object_schema(KEY), ['model', 'app'], TRACK),
    tool('show_panel', '子代理', '打开当前聊天的代理详情。首次打开时可输入此工作流的只读配对码。',
         object_schema(KEY), ['model', 'app'], PANEL),
    tool('pair', '配对当前工作流', '检查用户提供的只读配对码并显示该工作流。',
         object_schema(KEY, ('view_key',)), ['app']),
    tool('snapshot', '读取工作流快照', '使用只读配对码刷新同一工作流。',
         object_schema(KEY), ['app']),
)}


def codey_bytes(archive):
    """Read the known desktop Codey v6 atlas without extracting the ASAR or executing vendor code."""
    with archive.open('rb') as f:
        header_size, header, payload, tree_size = struct.unpack('<IIII', f.read(16))
        if not (header_size == 4 and header == payload + 4 and 4 <= tree_size <= 32 << 20 and
                header - 12 < tree_size <= header - 8):
            raise ValueError('Invalid ASAR header')
        tree = json.loads(f.read(tree_size))['files']['webview']['files']['assets']['files']
        names = [n for n in tree if re.fullmatch(r'codex-spritesheet-v6\.[a-f0-9]{6,64}\.webp', n)]
        if len(names) != 1:
            raise ValueError('Missing or ambiguous Codey v6 atlas')
        item = tree[names[0]]
        size, offset = item['size'], int(item['offset'])
        if item.get('unpacked') or not (0 < size <= 8 << 20 and 0 <= offset <= f.seek(0, 2) - (8 + header) - size):
            raise ValueError('Invalid atlas range')
        f.seek(8 + header + offset)
        data = f.read(size)
    digest = item['integrity']
    if digest.get('algorithm') != 'SHA256' or hashlib.sha256(data).hexdigest() != digest.get('hash'):
        raise ValueError('Codey atlas failed integrity check')
    # v6 ships a lossless WebP, 8x11 cells, 192x208 pixels each. Check canvas and alpha flag.
    if len(data) < 25 or data[:4] != b'RIFF' or data[8:16] != b'WEBPVP8L' or data[20] != 0x2f:
        raise ValueError('Unexpected WebP atlas encoding')
    flags = int.from_bytes(data[21:25], 'little')
    if (int.from_bytes(data[4:8], 'little') != len(data) - 8 or (flags & 0x3fff) + 1 != 1536 or
            ((flags >> 14) & 0x3fff) + 1 != 2288 or not flags & (1 << 28)):
        raise ValueError('Unexpected Codey frame layout')
    return data


@lru_cache(maxsize=1)
def sprite_uri():
    try:
        return 'data:image/webp;base64,' + base64.b64encode(codey_bytes(ASAR)).decode('ascii')
    except (OSError, ValueError, KeyError, TypeError, struct.error):
        return ''  # UI keeps the text status if this desktop version has no matching atlas.


def page(view):
    template = (HERE / 'assets' / 'panel.html').read_text(encoding='utf-8')
    return (template.replace('__SAVVY_VIEW__', view).replace('__SAVVY_VERSION__', VERSION)
            .replace('__SAVVY_SPRITE__', sprite_uri()))


def brief(data):
    if not data.get('flow_id'):
        return '请输入当前工作流的只读配对码。'
    title = data.get('title') or '工作流'
    accepted = data.get('accepted', 0)
    total = data.get('total', 0)
    progress = f'已验收 {accepted}/{total}' if data.get('total_known', bool(total)) else '总任务数未知'
    status_text = {'active': '进行中', 'completed': '已结束', 'interrupted': '已中断', 'running': '进行中',
                   'waiting': '等待中', 'ended': '已结束', 'failed': '失败', 'cancelled': '已取消'}
    details = [title, progress, data.get('phase'), status_text.get(data.get('status'), data.get('status'))]
    rows = [' · '.join(str(item) for item in details if item)]
    for agent in data.get('agents', []):
        steps = f"{agent['done']}/{agent['total']} 步" if agent.get('total') is not None else '步骤未知'
        status = status_text.get(agent.get('status'), agent.get('status'))
        rows.append(' · '.join(str(v) for v in (agent.get('name'), status, steps, agent.get('note')) if v))
    return '\n'.join(rows)


def call(name, args, meta):
    # Codex inserts threadId in the MCP envelope; a tool argument cannot override it.
    # A present but unrecognized ID must never fall back to another chat's read key.
    thread = meta.get('threadId') if isinstance(meta, dict) else None
    if thread is not None and (not isinstance(thread, str) or not thread):
        raise ValueError('宿主线程标识无效。')
    if name in ('progress', 'step'):
        if thread:
            payload = {k: v for k, v in args.items() if k != 'binding'}
            return state.write_for_thread(thread, name, payload)
        if 'binding' not in args:
            raise ValueError('缺少宿主线程标识。请用当前代理生成的单动作票据。')
        return state.progress(**args) if name == 'progress' else state.step(**args)
    key = args.get('view_key')
    if thread:
        view = state.view_for_thread(thread, key)
        if not view and key:
            raise ValueError('配对码与当前聊天不匹配。')
        key = view['view_key'] if view else None
    if not key:
        return {'paired': False}
    data = state.snapshot(key)
    if name == 'show_track':
        state.mark_shown(key)
    return {**data, 'view_key': key}


class RpcError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def valid(value, schema):
    kind = schema['type']
    if kind == 'object':
        fields = schema['properties']
        return (isinstance(value, dict) and value.keys() <= fields.keys() and
                set(schema['required']) <= value.keys() and all(valid(v, fields[k]) for k, v in value.items()))
    if kind == 'array':
        return isinstance(value, list) and all(valid(v, schema['items']) for v in value)
    if kind == 'string':
        return (isinstance(value, str) and len(value) >= schema.get('minLength', 0) and
                ('enum' not in schema or value in schema['enum']))
    if kind == 'boolean':
        return isinstance(value, bool)
    return type(value) is int and value >= schema['minimum']


def handle(method, params):
    if not isinstance(params, dict):
        raise RpcError(-32602, 'Invalid params')
    if method == 'initialize':
        protocol = params.get('protocolVersion')
        return {'protocolVersion': protocol if protocol in PROTOCOLS else PROTOCOLS[0],
                'serverInfo': {'name': 'savvy-progress', 'title': 'Codex Agent Panel', 'version': VERSION},
                'capabilities': {'tools': {}, 'resources': {}}}
    if method == 'ping':
        return {}
    if method == 'tools/list':
        return {'tools': list(TOOLS.values())}
    if method == 'resources/templates/list':
        return {'resourceTemplates': []}
    if method == 'resources/list':
        return {'resources': [{'uri': uri, 'name': title, 'mimeType': MIME} for uri, (_, title) in VIEWS.items()]}
    if method == 'resources/read':
        uri = params.get('uri')
        resolved = LEGACY_VIEWS.get(uri, uri) if isinstance(uri, str) else None
        if resolved not in VIEWS:
            raise RpcError(-32002, 'Unknown resource')
        meta = {'ui': {'prefersBorder': False, 'csp': {'connectDomains': [], 'resourceDomains': ['data:']}}}
        if resolved == TRACK:
            meta['openai/ui'] = {'availableDisplayModes': ['inline', 'fullscreen'], 'preferredDisplayMode': 'inline'}
            meta.update({'openai/widgetShowCodexWidgetInline': True, 'openai/widgetHeightHint': 100,
                         'openai/widgetHideAttribution': True})
        else:
            meta['openai/ui'] = {'availableDisplayModes': ['fullscreen'], 'preferredDisplayMode': 'fullscreen'}
        return {'contents': [{'uri': uri, 'mimeType': MIME, 'text': page(VIEWS[resolved][0]), '_meta': meta}]}
    if method != 'tools/call':
        raise RpcError(-32601, 'Method not found')
    name, args = params.get('name'), params.get('arguments', {})
    if not isinstance(name, str) or name not in TOOLS:
        raise RpcError(-32602, 'Unknown tool')
    schema = TOOLS[name]['inputSchema']
    if not valid(args, schema):
        raise RpcError(-32602, 'Invalid tool arguments')
    try:
        data = call(name, args, params.get('_meta'))
    except (ValueError, KeyError, PermissionError) as exc:
        return {'isError': True, 'content': [{'type': 'text', 'text': str(exc)}]}
    return {'structuredContent': data, 'content': [{'type': 'text', 'text': brief(data)}]}


def serve():
    for line in sys.stdin:
        req_id = None
        try:
            try:
                msg = json.loads(line)
            except ValueError:
                raise RpcError(-32700, 'Parse error')
            if not isinstance(msg, dict) or msg.get('jsonrpc') != '2.0' or not isinstance(msg.get('method'), str):
                raise RpcError(-32600, 'Invalid request')
            if 'id' not in msg:
                continue
            req_id = msg['id']
            result = handle(msg['method'], msg.get('params', {}))
            reply = {'result': result}
        except RpcError as exc:
            reply = {'error': {'code': exc.code, 'message': str(exc)}}
        except Exception:
            # Do not send local paths, capabilities or inbound parameters to the model.
            reply = {'error': {'code': -32603, 'message': 'Local progress data or UI resource is unavailable'}}
        print(json.dumps({'jsonrpc': '2.0', 'id': req_id, **reply}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Codex Agent Panel · 本机子代理进度')
    parser.add_argument('command', nargs='?', choices=['status'], help='status 从标准输入读取只读配对码，输出文字摘要')
    if parser.parse_args().command == 'status':
        try:
            print(brief(state.snapshot(input().strip())))
        except (ValueError, KeyError, PermissionError, EOFError) as error:
            parser.exit(1, str(error) + '\n')
    else:
        serve()
