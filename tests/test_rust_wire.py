"""Independent framing and synthetic process tests against compiled Rust code.

Build the binary and fixture-host example before running. No live cmux is used.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
TARGET = Path(os.environ.get('CARGO_TARGET_DIR', '/tmp/ahu-rust-cmux-target'))
METHODS = ['create_task_session', 'verify_task_session', 'select_task_session',
           'close_task_session', 'set_task_status', 'expand_task_session',
           'place_coordinator', 'set_agent_metadata', 'open_markdown']
HELLO = {'protocol_version': 2, 'id': 1, 'method': 'hello', 'params': {
    'protocol_versions': [2], 'host_api': 1, 'plugin_id': 'cmux-host', 'required_capabilities': METHODS}}

def frame(value):
    data = value if isinstance(value, bytes) else json.dumps(value, separators=(',', ':')).encode()
    return struct.pack('>I', len(data)) + data

def decode(data):
    result = []
    while data:
        assert len(data) >= 4
        size, = struct.unpack('>I', data[:4])
        assert 2 <= size <= 1024 * 1024 and len(data) >= size + 4
        result.append(json.loads(data[4:4+size]))
        data = data[4+size:]
    return result

class RustWireTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ahu-cmux-fixture-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / 'répo-😀'
        self.repo.mkdir()
        self.git = shutil.which('git')
        subprocess.run([self.git, 'init', '-q', str(self.repo)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.identity = hashlib.sha256(os.fsencode(str((self.repo / '.git').resolve()))).hexdigest()[:16]
        self.cmux = self.base / 'cmux'
        content = (ROOT / 'tests/fake_cmux.py').read_text().split('\n', 1)[1]
        self.cmux.write_text('#!' + sys.executable + '\n' + content)
        self.cmux.chmod(0o700)
        self.state = {'workspaces': {'win': [{'id': 'w', 'current_directory': str(self.repo), 'selected': True}]},
                      'groups': {'win': [{'id': 'g', 'name': 'repo', 'anchor_workspace_id': 'w', 'member_workspace_ids': ['w']}]}}
        self.driver = TARGET / 'debug/examples/fixture-host'
        self.assertTrue(self.driver.is_file(), 'build fixture-host example before running tests')

    def calls(self):
        path = self.base / 'calls.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def run_wire(self, requests=(), raw=None):
        (self.base / 'state.json').write_text(json.dumps(self.state))
        data = raw if raw is not None else frame(HELLO) + b''.join(frame({'protocol_version': 2, 'id': n, 'method': method, 'params': params}) for n, (method, params) in enumerate(requests, 2))
        result = subprocess.run([str(self.driver), str(self.cmux), self.git, str(self.base)], input=data,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=18,
                                env={**os.environ, 'CMUX_SOCKET_PATH': '/do-not-use', 'CMUX_WORKSPACE_ID': 'not-context', 'SECRET_SENTINEL': 'private'})
        self.assertEqual(result.stderr, b'')
        return result.returncode, decode(result.stdout)

    def ownership(self, **updates):
        scope = hashlib.sha256(json.dumps({'version': 2, 'repository_identity': self.identity, 'directory': str(self.repo)}, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return {'session_ref': 'cmux2:' + scope + ':w:win:g', 'repository_identity': self.identity,
                'expected_directory': str(self.repo), **updates}

    def create(self, **updates):
        return {'repository_identity': self.identity, 'repository_name': 'repo', 'repository_root': str(self.repo),
                'title': 'task', 'summary': 'summary', 'worktree': str(self.repo), 'startup_command': 'literal $(do-not-run); `anything`', 'focus': False, **updates}

    def test_handshake_and_all_nine_operations(self):
        own = self.ownership()
        requests = [('verify_task_session', own), ('select_task_session', own), ('close_task_session', own),
                    ('set_task_status', {**own, 'value': 'working; $(literal)'}), ('expand_task_session', own),
                    ('set_agent_metadata', {**own, 'agent': 'worker', 'harness': 'codex', 'model': 'test'}),
                    ('open_markdown', {'path': '/tmp/literal; $(file).md', 'focus': False}),
                    ('place_coordinator', {k: v for k, v in {**self.create(), 'label': 'repo', 'harness': 'codex', 'model': 'test'}.items() if k in ['repository_identity', 'repository_name', 'repository_root', 'label', 'harness', 'model', 'startup_command']}),
                    ('create_task_session', self.create())]
        status, responses = self.run_wire(requests)
        self.assertEqual(status, 0)
        self.assertEqual(len(responses), 10)
        self.assertEqual(responses[0]['result'], {'protocol_version': 2, 'host_api': 1, 'plugin_id': 'cmux-host', 'capabilities': METHODS, 'inherited_workspace': 'w'})
        self.assertEqual(responses[1]['result'], 'owned')
        self.assertEqual(responses[7]['result'], 'surface')
        self.assertEqual(responses[8]['result'], {'notes': [], 'opened_session': False})
        self.assertTrue(responses[9]['result']['session_ref'].endswith(':created:win:g'))
        for response in responses:
            self.assertNotIn('error', response)
        calls = self.calls()
        create = [c['args'] for c in calls if c['args'][0] == 'new-workspace']
        self.assertEqual(len(create), 1)
        self.assertEqual(create[0][create[0].index('--command') + 1], self.create()['startup_command'])
        for call in calls:
            self.assertNotIn('SECRET_SENTINEL', call['env'])
            self.assertNotIn('CMUX_SOCKET_PATH', call['env'])
            self.assertNotIn('CMUX_WORKSPACE_ID', call['env'])
            self.assertEqual(call['env']['HOME'], str(self.base))
            self.assertEqual(call['env']['PATH'], '/usr/bin:/bin')
            self.assertEqual(call['cwd'], '/')

    def test_invalid_hello_never_activates_provider(self):
        variants = []
        for key, value in [('protocol_version', 1), ('id', True), ('id', 1.0), ('method', 'open_markdown')]:
            variants.append({**HELLO, key: value})
        for key, value in [('host_api', True), ('host_api', 2), ('protocol_versions', [1, 2]), ('plugin_id', 'other'), ('required_capabilities', METHODS[::-1]), ('required_capabilities', METHODS + [METHODS[0]]), ('extra', None)]:
            variants.append({**HELLO, 'params': {**HELLO['params'], key: value}})
        variants += [{**HELLO, 'unknown': 1}, [], None]
        for hello in variants:
            with self.subTest(hello=hello):
                status, replies = self.run_wire(raw=frame(hello))
                self.assertEqual((status, replies), (2, []))
                self.assertEqual(self.calls(), [])

    def test_framing_duplicate_keys_and_partial_eof(self):
        for raw in [b'\0', struct.pack('>I', 1048577), struct.pack('>I', 1) + b'0', struct.pack('>I', 8) + b'{}', frame(b'{"x":1,"x":2}'), frame(b'{"x":{"a":1,"\\u0061":2}}'), frame(b'\xff')]:
            with self.subTest(raw=raw[:20]):
                self.assertEqual(self.run_wire(raw=raw), (2, []))
        status, replies = self.run_wire(raw=frame(HELLO) + b'\0')
        self.assertEqual(status, 2)
        self.assertEqual(len(replies), 1)

    def test_request_envelopes_and_sequence(self):
        for change in [{'id': 3}, {'id': True}, {'id': 2.0}, {'protocol_version': 3}, {'extra': 1}]:
            request = {'protocol_version': 2, 'id': 2, 'method': 'open_markdown', 'params': {}, **change}
            status, replies = self.run_wire(raw=frame(HELLO) + frame(request))
            self.assertEqual(status, 2)
            self.assertEqual(len(replies), 1)

    def test_unknown_parameters_rejected_on_every_method(self):
        requests = [(method, {'unexpected': True}) for method in METHODS]
        status, replies = self.run_wire(requests)
        self.assertEqual(status, 0)
        self.assertTrue(all(r['error']['code'] == 'failed' for r in replies[1:]))
        self.assertEqual(len(self.calls()), 3)  # Only handshake probes.

    def test_replacement_moved_changed_ambiguous_and_wrong_repository_refuse_close(self):
        baseline = copy.deepcopy(self.state)
        for scenario in ['replacement', 'window', 'directory', 'group', 'ambiguous', 'repository', 'scope']:
            with self.subTest(scenario=scenario):
                self.state = copy.deepcopy(baseline)
                own = self.ownership()
                if scenario == 'replacement': self.state['workspaces']['win'][0]['id'] = 'replacement'
                if scenario == 'window': self.state['workspaces']['other'] = self.state['workspaces'].pop('win')
                if scenario == 'directory': self.state['workspaces']['win'][0]['current_directory'] = '/other'
                if scenario == 'group': self.state['groups']['win'][0]['id'] = 'other'
                if scenario == 'ambiguous': self.state['workspaces']['win'].append({'id': 'other', 'current_directory': str(self.repo)})
                if scenario in ('repository', 'scope'):
                    own['repository_identity'] = '0' * 16
                    if scenario == 'repository':
                        digest = hashlib.sha256(json.dumps({'version': 2, 'repository_identity': '0' * 16, 'directory': str(self.repo)}, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                        own['session_ref'] = 'cmux2:' + digest + ':w:win:g'
                status, replies = self.run_wire([('verify_task_session', own), ('close_task_session', own)])
                self.assertEqual(status, 0)
                self.assertEqual(replies[1]['result'], 'mismatch')
                self.assertEqual(replies[2]['error']['code'], 'failed')
        self.assertFalse(any(c['args'][:2] == ['rpc', 'workspace.close'] for c in self.calls()))

    def test_unrecorded_recovery_and_absent_close(self):
        own = self.ownership(session_ref='')
        status, replies = self.run_wire([('verify_task_session', own), ('close_task_session', own)])
        self.assertEqual(status, 0)
        self.assertEqual([r['error']['code'] for r in replies[1:]], ['outcome_unknown'] * 2)
        self.state['workspaces']['win'] = []
        status, replies = self.run_wire([('verify_task_session', own), ('close_task_session', own)])
        self.assertEqual([r['result'] for r in replies[1:]], ['absent', None])
        self.assertFalse(any(c['args'][:2] == ['rpc', 'workspace.close'] for c in self.calls()))

    def test_duplicate_or_incomplete_inventories_refused(self):
        baseline = copy.deepcopy(self.state)
        for scenario in ['windows', 'workspace', 'group', 'anchor', 'members', 'selected', 'missing', 'duplicate-json']:
            with self.subTest(scenario=scenario):
                self.state = copy.deepcopy(baseline)
                if scenario == 'windows': self.state['windows'] = [{'id': 'win'}, {'id': 'win'}]
                if scenario == 'workspace': self.state['workspaces']['win'].append(self.state['workspaces']['win'][0])
                if scenario == 'group': self.state['groups']['win'].append(self.state['groups']['win'][0])
                if scenario == 'anchor': self.state['groups']['win'][0]['anchor_workspace_id'] = 'absent'
                if scenario == 'members': self.state['groups']['win'][0]['member_workspace_ids'] = ['w', 'w']
                if scenario == 'selected': self.state['workspaces']['win'].append({'id': 'other', 'selected': True})
                if scenario == 'missing': self.state['workspaces']['win'] = None
                if scenario == 'duplicate-json': self.state['mode'] = scenario
                status, replies = self.run_wire([('close_task_session', self.ownership())])
                if status == 0: self.assertIn('error', replies[-1])
                else: self.assertEqual(status, 2)
        self.assertFalse(any(c['args'][:2] == ['rpc', 'workspace.close'] for c in self.calls()))

    def test_create_unknown_outcomes_never_retry(self):
        for mode in ['lost-create', 'timeout-create', 'post-create-invalid', 'ambiguous-create', 'invalid-create-id']:
            with self.subTest(mode=mode):
                (self.base / 'calls.jsonl').unlink(missing_ok=True)
                self.state['mode'] = mode
                status, replies = self.run_wire([('create_task_session', self.create())])
                self.assertEqual(status, 0)
                self.assertEqual(replies[-1]['error']['code'], 'outcome_unknown')
                self.assertEqual(sum(c['args'][0] == 'new-workspace' for c in self.calls()), 1)
                self.assertFalse(any(c['args'][:2] == ['rpc', 'workspace.close'] for c in self.calls()))

    def test_bad_create_inputs_do_not_mutate(self):
        for updates in [{'focus': 'false'}, {'title': ''}, {'startup_command': '\n'}, {'worktree': str(self.base)}, {'repository_identity': '0' * 16}]:
            status, replies = self.run_wire([('create_task_session', self.create(**updates))])
            self.assertEqual(status, 0)
            self.assertIn('error', replies[-1])
        self.assertFalse(any(c['args'][0] == 'new-workspace' or c['args'][:2] == ['rpc', 'workspace.group.create'] for c in self.calls()))

    def test_repository_group_creation_selection_and_ambiguity(self):
        baseline = copy.deepcopy(self.state)
        self.state['groups']['win'] = []
        status, replies = self.run_wire([('create_task_session', self.create())])
        self.assertEqual(status, 0)
        self.assertTrue(replies[-1]['result']['session_ref'].endswith(':created:win:g'))
        self.assertEqual(sum(c['args'][:2] == ['rpc', 'workspace.group.create'] for c in self.calls()), 1)
        # Two matching repository groups without a selected member refuse placement.
        self.state = copy.deepcopy(baseline)
        self.state['workspaces']['win'][0]['selected'] = False
        self.state['workspaces']['win'].append({'id': 'other', 'current_directory': str(self.repo)})
        self.state['groups']['win'].append({'id': 'other-group', 'name': 'repo', 'anchor_workspace_id': 'other', 'member_workspace_ids': ['other']})
        (self.base / 'calls.jsonl').unlink()
        status, replies = self.run_wire([('create_task_session', self.create())])
        self.assertIn('error', replies[-1])
        self.assertFalse(any(c['args'][0] == 'new-workspace' for c in self.calls()))
        # Selected membership disambiguates those same candidates.
        self.state['workspaces']['win'][0]['selected'] = True
        status, replies = self.run_wire([('create_task_session', self.create())])
        self.assertTrue(replies[-1]['result']['session_ref'].endswith(':created:win:g'))

    def test_group_and_coordinator_partial_writes_are_unknown_without_retry(self):
        params = {'repository_identity': self.identity, 'repository_name': 'repo', 'repository_root': str(self.repo),
                  'label': 'repo', 'harness': 'codex', 'model': 'test', 'startup_command': 'literal command'}
        original = copy.deepcopy(self.state)
        for mode in ['lost-group-create', 'invalid-group-reply', 'post-group-invalid',
                     'post-add-invalid', 'post-add-metadata-failure']:
            with self.subTest(mode=mode):
                self.state = copy.deepcopy(original)
                self.state['mode'] = mode
                if 'group' in mode:
                    self.state['groups']['win'] = []
                (self.base / 'calls.jsonl').unlink(missing_ok=True)
                status, replies = self.run_wire([('place_coordinator', params), ('place_coordinator', {})])
                self.assertEqual(status, 0)
                self.assertEqual(replies[1]['error']['code'], 'outcome_unknown', replies)
                # Effect uncertainty is per operation, not sticky across requests.
                self.assertEqual(replies[2]['error']['code'], 'failed', replies)
                writes = [c['args'][1] for c in self.calls() if c['args'][0] == 'rpc']
                self.assertLessEqual(writes.count('workspace.group.create'), 1)
                self.assertLessEqual(writes.count('workspace.group.add'), 1)

    def test_coordinator_opens_session_when_caller_belongs_to_other_group(self):
        self.state['workspaces']['win'].append({'id': 'anchor', 'current_directory': str(self.repo)})
        self.state['groups']['win'] = [
            {'id': 'g', 'name': 'repo', 'anchor_workspace_id': 'anchor', 'member_workspace_ids': ['anchor']},
            {'id': 'other', 'name': 'different', 'anchor_workspace_id': 'w', 'member_workspace_ids': ['w']}]
        params = {'repository_identity': self.identity, 'repository_name': 'repo', 'repository_root': str(self.repo),
                  'label': 'repo', 'harness': 'codex', 'model': 'test', 'startup_command': 'literal command'}
        status, replies = self.run_wire([('place_coordinator', params)])
        self.assertEqual(status, 0)
        self.assertTrue(replies[-1]['result']['opened_session'])
        self.assertEqual(sum(c['args'][0] == 'new-workspace' for c in self.calls()), 1)
        self.assertFalse(any(c['args'][:2] == ['rpc', 'workspace.group.add'] for c in self.calls()))

    def test_git_output_and_duration_are_bounded(self):
        fake_git = self.base / 'git'
        self.git = str(fake_git)
        for body in ["import sys; sys.stdout.write('x' * (1024 * 1024 + 1))",
                     "import time; time.sleep(30)", "import sys; sys.stdout.write('invalid-path')"]:
            with self.subTest(body=body):
                fake_git.write_text('#!' + sys.executable + '\n' + body + '\n')
                fake_git.chmod(0o700)
                before = time.monotonic()
                status, replies = self.run_wire([('create_task_session', self.create())])
                self.assertEqual(status, 0)
                self.assertEqual(replies[-1]['error']['code'], 'failed')
                self.assertLess(time.monotonic() - before, 5)
        self.assertFalse(any(c['args'][0] == 'new-workspace' for c in self.calls()))

    def test_output_limit_and_process_group_cleanup(self):
        for mode in ['flood', 'descendant', 'closed-pipe', 'success-child']:
            with self.subTest(mode=mode):
                (self.base / 'child-ready').unlink(missing_ok=True)
                self.state['mode'] = mode
                before = time.monotonic()
                status, replies = self.run_wire()
                if mode == 'success-child':
                    self.assertEqual(status, 0)
                    self.assertEqual(len(replies), 1)
                else:
                    self.assertEqual((status, replies), (2, []))
                self.assertLess(time.monotonic() - before, 15)
                if mode != 'flood':
                    pid = int((self.base / 'child-ready').read_text())
                    # macOS/Linux may briefly retain an orphan as a zombie; it cannot execute.
                    for _ in range(100):
                        ps = subprocess.run(['/bin/ps', '-o', 'stat=', '-p', str(pid)], capture_output=True)
                        if not ps.stdout.strip() or ps.stdout.strip().startswith(b'Z'): break
                        time.sleep(.01)
                    else: self.fail('provider descendant survived process-group cleanup')

    def test_shipped_binary_rejects_unsupported_cli_without_provider_access(self):
        for args in [[], ['--help'], ['--ahu-host-protocol=1'], ['--ahu-host-protocol=2', '--extra']]:
            result = subprocess.run([str(TARGET / 'debug/session-host'), *args], input=b'', capture_output=True, timeout=2)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (2, b'', b''))

if __name__ == '__main__':
    unittest.main()
