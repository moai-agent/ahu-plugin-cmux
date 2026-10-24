import importlib.util
from importlib.machinery import SourceFileLoader
import io
import json
import pathlib
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
ENTRY = pathlib.Path(__file__).parents[1] / "package/bin/session-host"
spec = importlib.util.spec_from_loader("cmux_host", SourceFileLoader("cmux_host", str(ENTRY)))
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)


class HostTests(unittest.TestCase):
    def test_contract_is_small_and_provider_neutral(self):
        self.assertEqual(len(host.METHODS), 9)
        self.assertEqual(len(set(host.METHODS)), 9)
        self.assertEqual(host.METHODS, [
            "create_task_session", "verify_task_session", "select_task_session",
            "close_task_session", "set_task_status", "expand_task_session",
            "place_coordinator", "set_agent_metadata", "open_markdown",
        ])
        self.assertNotIn("list_groups", host.METHODS)
        self.assertIn("create_task_session", host.METHODS)
        self.assertIn("place_coordinator", host.METHODS)

    def test_session_references_are_opaque_and_strict(self):
        value = {"version": 1, "repository_identity": "0123456789abcdef",
                 "directory": "/work/task", "group_id": "group",
                 "window_id": "window", "workspace_id": "workspace"}
        encoded = host.session_token(value)
        self.assertLess(len(encoded), 256)
        self.assertTrue(encoded.startswith("cmux2:"))
        long_path = "/" + "long-component/" * 80 + "task"
        self.assertLess(len(host.session_token({
            **value, "directory": long_path})), 256)
        self.assertEqual(host.session_value(encoded), encoded)
        for bad in ("", "not-base64", "cmux1:bad", "cmux1:" + "z" * 64):
            with self.assertRaises(host.HostError):
                host.session_value(bad)

    def test_replacement_workspace_cannot_be_closed_by_original_reference(self):
        from unittest.mock import patch
        original = {"repository_identity": "0123456789abcdef", "directory": "/work/task",
                    "group_id": "g", "window_id": "win", "workspace_id": "original"}
        params = {"session_ref": host.session_token(original),
                  "repository_identity": original["repository_identity"],
                  "expected_directory": original["directory"]}
        calls = []
        def rpc(method, args):
            calls.append(method)
            return {"windows": [{"id": "win"}]}
        with patch.object(host, "rpc", rpc), patch.object(host, "repository_identity", return_value=original["repository_identity"]), patch.object(host, "workspaces", return_value={"replacement": {"directory": "/work/task"}}), patch.object(host, "groups", return_value=[{"id": "g", "member_workspace_ids": ["replacement"]}]):
            with self.assertRaises(host.HostError):
                host.dispatch("close_task_session", params)
        self.assertNotIn("workspace.close", calls)

    def test_recorded_session_rejects_moved_changed_or_ambiguous_workspace(self):
        from unittest.mock import patch
        value = {"repository_identity": "0123456789abcdef", "directory": "/work/task",
                 "group_id": "g", "window_id": "win", "workspace_id": "w"}
        params = {"session_ref": host.session_token(value),
                  "repository_identity": value["repository_identity"], "expected_directory": value["directory"]}
        scenarios = [
            ("other-window", {"w": {"directory": "/work/task"}}, "g"),
            ("win", {"w": {"directory": "/changed"}}, "g"),
            ("win", {"w": {"directory": "/work/task"}}, "other-group"),
            ("win", {"w": {"directory": "/work/task"}, "other": {"directory": "/work/task"}}, "g"),
        ]
        for window, inventory, group in scenarios:
            with self.subTest(window=window, inventory=inventory, group=group), patch.object(host, "rpc", return_value={"windows": [{"id": window}]}), patch.object(host, "repository_identity", return_value=value["repository_identity"]), patch.object(host, "workspaces", return_value=inventory), patch.object(host, "groups", return_value=[{"id": group, "member_workspace_ids": list(inventory)}]):
                self.assertEqual(host.session_ownership(params)[0], "mismatch")
                with self.assertRaises(host.HostError):
                    host.dispatch("close_task_session", params)

    def test_unrecorded_session_never_claims_directory_match(self):
        from unittest.mock import patch
        params = {"session_ref": "", "repository_identity": "0123456789abcdef", "expected_directory": "/work/task"}
        calls = []
        def rpc(method, args):
            calls.append(method)
            return {"windows": [{"id": "win"}]}
        with patch.object(host, "rpc", rpc), patch.object(host, "workspaces", return_value={"w": {"directory": "/work/task"}}):
            with self.assertRaises(host.HostError) as error:
                host.dispatch("close_task_session", params)
            self.assertEqual(error.exception.code, "outcome_unknown")
        self.assertEqual(calls, ["window.list"])
        with patch.object(host, "rpc", rpc), patch.object(host, "workspaces", return_value={}):
            self.assertEqual(host.session_ownership(params)[0], "absent")

    def test_repository_group_resolution_is_plugin_owned(self):
        old_dispatch, old_groups, old_workspaces, old_identity, old_rpc = (
            host.dispatch, host.groups, host.workspaces, host.repository_identity, host.rpc)
        existing = {"id": "g", "name": "repo", "anchor_workspace_id": "w",
                    "member_workspace_ids": ["w"], "is_collapsed": False}
        calls = []
        try:
            host.dispatch = lambda method, params: {"current_window": "win", "current_workspace": "w"}[method]
            host.groups = lambda window=None: [existing]
            host.workspaces = lambda window=None: {"w": {"directory": "/repo"}}
            host.repository_identity = lambda directory: "0123456789abcdef"
            host.rpc = lambda method, params: calls.append((method, params))
            result = host.ensure_repository_group({"repository_identity": "0123456789abcdef",
                "repository_name": "repo", "repository_root": "/repo"})
            self.assertEqual(result, {"group": existing, "window_id": "win"})
            self.assertEqual(calls, [])
            host.groups = lambda window=None: []
            host.workspaces = lambda window=None: {}
            created = {**existing, "id": "new", "anchor_workspace_id": "anchor",
                       "member_workspace_ids": ["anchor"]}
            host.rpc = lambda method, params: calls.append((method, params)) or {"group": created}
            result = host.ensure_repository_group({"repository_identity": "0123456789abcdef",
                "repository_name": "repo", "repository_root": "/repo"})
            self.assertEqual(result["group"], created)
            self.assertEqual(calls[-1][0], "workspace.group.create")
        finally:
            host.dispatch, host.groups, host.workspaces, host.repository_identity, host.rpc = (
                old_dispatch, old_groups, old_workspaces, old_identity, old_rpc)

    def test_session_ownership_checks_repo_path_group_and_window(self):
        old_groups, old_workspaces, old_dispatch, old_rpc, old_identity = (
            host.groups, host.workspaces, host.dispatch, host.rpc, host.repository_identity)
        group = {"id": "g", "name": "repo", "anchor_workspace_id": "w",
                 "member_workspace_ids": ["w"], "is_collapsed": False}
        ref = host.session_token({"version": 1, "repository_identity": "0123456789abcdef",
            "directory": "/work/task", "group_id": "g", "window_id": "win", "workspace_id": "w"})
        params = {"session_ref": ref, "repository_identity": "0123456789abcdef",
                  "expected_directory": "/work/task"}
        try:
            host.groups = lambda window=None: [group]
            host.workspaces = lambda window=None: {"w": {"directory": "/work/task"}}
            host.rpc = lambda method, params: {"windows": [{"id": "win"}]}
            host.repository_identity = lambda directory: "0123456789abcdef"
            self.assertEqual(host.dispatch if False else host.session_ownership(params)[0], "owned")
            with self.assertRaises(host.HostError):
                host.session_ownership({**params, "session_ref": ""})
            self.assertEqual(host.session_ownership({**params, "expected_directory": "/other"})[0], "mismatch")
            host.workspaces = lambda window=None: {}
            self.assertEqual(host.session_ownership(params)[0], "absent")
        finally:
            host.groups, host.workspaces, host.dispatch, host.rpc, host.repository_identity = (
                old_groups, old_workspaces, old_dispatch, old_rpc, old_identity)

    def test_task_creation_builds_reference_after_confirming_workspace(self):
        old = (host.repository_identity, host.ensure_repository_group, host.created_workspace, host.verify_task_workspace)
        created = {"workspace_id": "w", "window_id": "win", "group_id": "g"}
        try:
            host.repository_identity = lambda directory: "0123456789abcdef"
            host.ensure_repository_group = lambda p: {"group": {"id": "g"}, "window_id": "win"}
            host.created_workspace = lambda p: created
            host.verify_task_workspace = lambda p: "owned"
            result = host.create_task_session({"repository_identity": "0123456789abcdef",
                "repository_name": "repo", "repository_root": "/repo", "title": "task",
                "summary": "summary", "worktree": "/work/task", "startup_command": "ahu run-task",
                "focus": False})
            self.assertEqual(result["session_ref"], host.session_token({
                "repository_identity": "0123456789abcdef", "directory": "/work/task", **created}))
            self.assertLess(len(result["session_ref"]), 256)
            host.created_workspace = lambda p: {**created, "workspace_id": "invalid:identity"}
            with self.assertRaises(host.HostError) as error:
                host.create_task_session({"repository_identity": "0123456789abcdef",
                    "repository_name": "repo", "repository_root": "/repo", "title": "task",
                    "summary": "summary", "worktree": "/work/task", "startup_command": "ahu run-task",
                    "focus": False})
            self.assertEqual(error.exception.code, "outcome_unknown")
        finally:
            host.repository_identity, host.ensure_repository_group, host.created_workspace, host.verify_task_workspace = old

    def test_actions_reverify_ownership_before_provider_mutation(self):
        ref = host.session_token({"version": 1, "repository_identity": "0123456789abcdef",
            "directory": "/work/task", "group_id": "g", "window_id": "win", "workspace_id": "w"})
        old_rpc, old_run, old_ownership = host.rpc, host.run, host.session_ownership
        calls = []
        try:
            host.rpc = lambda method, params: calls.append(("rpc", method, params))
            host.run = lambda argv, **kwargs: calls.append(("cli", argv)) or b""
            host.session_ownership = lambda p: ("owned", {
                "workspace_id": "w", "group_id": "g", "window_id": "win"})
            params = {"session_ref": ref, "repository_identity": "0123456789abcdef",
                      "expected_directory": "/work/task"}
            host.dispatch("select_task_session", params)
            host.dispatch("close_task_session", params)
            host.dispatch("set_task_status", {**params, "value": "working"})
            host.dispatch("expand_task_session", params)
            host.dispatch("set_agent_metadata", {**params, "agent": "worker",
                "harness": "codex", "model": "test-model"})
            self.assertEqual([x[1] for x in calls], [
                "workspace.select", "workspace.close",
                ["set-status", "ahu.task", "working", "--workspace", "w"],
                "workspace.group.expand",
                ["set-status", "ahu.agent", "worker · codex · test-model", "--workspace", "w"]])
            calls.clear()
            host.session_ownership = lambda p: ("mismatch", None)
            with self.assertRaises(host.HostError):
                host.dispatch("close_task_session", params)
            self.assertEqual(calls, [])
        finally:
            host.rpc, host.run, host.session_ownership = old_rpc, old_run, old_ownership

    def test_unknown_parameters_fail_closed(self):
        with self.assertRaises(host.HostError):
            host.dispatch("close_task_session", {"session_ref": "x", "force": True})

    def test_current_workspace_and_window_come_from_selected_cmux_inventory(self):
        old_rpc = host.rpc
        calls = []
        try:
            host.rpc = lambda method, params: calls.append((method, params)) or {
                "window_id": "window-1",
                "workspaces": [{"id": "workspace-1", "selected": False},
                               {"id": "workspace-2", "selected": True}],
            }
            self.assertEqual(host.dispatch("current_window", {}), "window-1")
            self.assertEqual(host.dispatch("current_workspace", {}), "workspace-2")
            self.assertEqual(calls, [("workspace.list", {}), ("workspace.list", {})])
        finally:
            host.rpc = old_rpc

    def test_current_workspace_rejects_ambiguous_or_incomplete_inventory(self):
        old_rpc = host.rpc
        try:
            host.rpc = lambda method, params: {"workspaces": [
                {"id": "workspace-1", "selected": True},
                {"id": "workspace-2", "selected": True},
            ]}
            with self.assertRaises(host.HostError):
                host.dispatch("current_workspace", {})
            host.rpc = lambda method, params: {"workspaces": None}
            with self.assertRaises(host.HostError):
                host.dispatch("current_workspace", {})
        finally:
            host.rpc = old_rpc

    def test_window_for_workspace_requires_one_verified_window_membership(self):
        old_rpc, old_workspaces = host.rpc, host.workspaces
        calls = []
        try:
            host.rpc = lambda method, params: {"windows": [{"id": "window-1"},
                                                              {"id": "window-2"}]}
            host.workspaces = lambda window=None: calls.append(window) or (
                {"workspace-1": {}} if window == "window-2" else {})
            self.assertEqual(host.dispatch("window_for_workspace", {"workspace_id": "workspace-1"}), "window-2")
            self.assertEqual(calls, ["window-1", "window-2"])
            host.workspaces = lambda window=None: {"workspace-1": {}}
            with self.assertRaises(host.HostError):
                host.dispatch("window_for_workspace", {"workspace_id": "workspace-1"})
        finally:
            host.rpc, host.workspaces = old_rpc, old_workspaces

    def test_duplicate_json_keys_are_rejected(self):
        with self.assertRaises(ValueError):
            json.loads('{"id":1,"id":2}', object_pairs_hook=host.unique_object)

    def test_main_negotiates_v2_and_advertises_only_contract_methods(self):
        def frame(value):
            raw = json.dumps(value, separators=(",", ":")).encode()
            return len(raw).to_bytes(4, "big") + raw
        hello = {"protocol_version": 2, "id": 1, "method": "hello", "params": {
            "protocol_versions": [2], "host_api": 1, "plugin_id": host.PLUGIN_ID,
            "required_capabilities": host.METHODS}}
        request = {"protocol_version": 2, "id": 2, "method": "open_markdown",
                   "params": {"path": "/tmp/a.md", "focus": False}}
        old_stdin, old_stdout, old_argv = host.sys.stdin, host.sys.stdout, host.sys.argv
        old_run, old_dispatch = host.run, host.dispatch
        host.sys.stdin = io.TextIOWrapper(io.BytesIO(frame(hello) + frame(request)))
        output = io.BytesIO()
        host.sys.stdout = type("Out", (), {"buffer": output})()
        host.sys.argv = ["session-host", "--ahu-host-protocol=2"]
        host.run = lambda args, **kwargs: b'{"capabilities":["workspace.groups.v1","workspace.group_create.v1","workspace.create_in_group.v1"]}' if args == ["capabilities"] else b""
        host.dispatch = lambda method, params: None if method == "current_workspace" else "surface"
        try:
            self.assertEqual(host.main(), 0)
        finally:
            host.sys.stdin, host.sys.stdout, host.sys.argv = old_stdin, old_stdout, old_argv
            host.run, host.dispatch = old_run, old_dispatch
        data = output.getvalue()
        decoded = []
        while data:
            size = int.from_bytes(data[:4], "big")
            decoded.append(json.loads(data[4:4 + size]))
            data = data[4 + size:]
        self.assertEqual(decoded[0]["result"]["capabilities"], host.METHODS)
        self.assertEqual(decoded[1]["result"], "surface")


if __name__ == "__main__":
    unittest.main()
