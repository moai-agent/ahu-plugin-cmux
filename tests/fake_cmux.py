#!/usr/bin/python3
"""Synthetic provider only; never shipped. State and logs live in a temp directory."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

base = Path(__file__).parent
if sys.argv[1:] == ['--child']:
    (base / 'child-ready').write_text(str(os.getpid()))
    while True:
        time.sleep(1)
state = json.loads((base / 'state.json').read_text())
args = sys.argv[1:]
with (base / 'calls.jsonl').open('a') as stream:
    stream.write(json.dumps({'args': args, 'env': dict(os.environ), 'cwd': os.getcwd()}) + '\n')
mode = state.get('mode')
if args == ['ping']:
    if mode == 'flood':
        sys.stdout.write('x' * (1024 * 1024 + 1))
    elif mode in ('descendant', 'closed-pipe', 'hang', 'success-child'):
        child = subprocess.Popen([sys.executable, __file__, '--child'], stdout=subprocess.DEVNULL if mode == 'success-child' else None)
        while not (base / 'child-ready').exists():
            time.sleep(.005)
        if mode in ('descendant', 'success-child'):
            sys.exit(0)
        if mode == 'closed-pipe':
            os.close(1)
        time.sleep(30)
    sys.exit(0)
if args == ['capabilities']:
    print(json.dumps({'capabilities': ['workspace.groups.v1', 'workspace.group_create.v1', 'workspace.create_in_group.v1']}))
    sys.exit(0)
if args[0] == 'new-workspace':
    window = args[args.index('--window') + 1]
    directory = args[args.index('--cwd') + 1]
    state['workspaces'][window].append({'id': 'created', 'current_directory': directory})
    state['groups'][window][0]['member_workspace_ids'].append('created')
    if mode == 'ambiguous-create':
        state['workspaces'][window].append({'id': 'created-other', 'current_directory': directory})
        state['groups'][window][0]['member_workspace_ids'].append('created-other')
    if mode == 'invalid-create-id':
        state['workspaces'][window][-1]['id'] = 'invalid:id'
        state['groups'][window][0]['member_workspace_ids'][-1] = 'invalid:id'
    (base / 'state.json').write_text(json.dumps(state))
    if mode == 'lost-create':
        sys.exit(1)
    if mode == 'timeout-create':
        time.sleep(30)
    sys.exit(0)
if args[0] == 'set-status':
    sys.exit(0)
assert args[0] == 'rpc', args
method, params = args[1], json.loads(args[2])
if mode == 'duplicate-json' and method == 'workspace.list':
    print('{"workspaces":[],"workspaces":[]}')
    sys.exit(0)
if mode == 'post-create-invalid' and any(w['id'] == 'created' for ws in state['workspaces'].values() for w in ws):
    print('not json')
    sys.exit(0)
window = params.get('window_id') or 'win'
if method == 'workspace.list':
    result = {'window_id': window, 'workspaces': state['workspaces'].get(window, [])}
elif method == 'window.list':
    result = {'windows': state.get('windows', [{'id': w} for w in state['workspaces']])}
elif method == 'workspace.group.list':
    result = {'groups': state['groups'].get(window, [])}
elif method == 'workspace.group.create':
    group = {'id': 'g', 'name': params['name'], 'anchor_workspace_id': 'anchor', 'member_workspace_ids': ['anchor']}
    state['groups'][window].append(group)
    state['workspaces'][window].append({'id': 'anchor', 'current_directory': params['cwd']})
    (base / 'state.json').write_text(json.dumps(state))
    result = {'group': group}
elif method == 'workspace.group.add':
    for group in state['groups'][window]:
        if group['id'] == params['group_id'] and params['workspace_id'] not in group['member_workspace_ids']:
            group['member_workspace_ids'].append(params['workspace_id'])
    (base / 'state.json').write_text(json.dumps(state))
    result = {}
elif method == 'markdown.open':
    result = {'surface_id': 'surface'}
else:
    assert method in ('workspace.select', 'workspace.close', 'workspace.group.expand', 'workspace.move_to_window'), method
    result = {}
print(json.dumps(result))
