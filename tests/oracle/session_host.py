#!/usr/bin/python3
"""Ahu Host V2 adapter for cmux's CLI/RPC interface."""

import json
import hashlib
import os
import pwd
import selectors
import signal
import subprocess
import sys
import time
import shutil

PLUGIN_ID = "cmux-host"
VERSION = 2
MAX_FRAME = 1024 * 1024
MAX_OUTPUT = 1024 * 1024
TIMEOUT = 12.0
METHODS = [
    "create_task_session", "verify_task_session", "select_task_session",
    "close_task_session", "set_task_status", "expand_task_session",
    "place_coordinator", "set_agent_metadata", "open_markdown",
]


class HostError(Exception):
    def __init__(self, message="operation failed", code="failed"):
        super().__init__(message)
        self.code = code


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def read_exact(size):
    chunks = []
    while size:
        chunk = sys.stdin.buffer.read(size)
        if not chunk:
            raise EOFError
        chunks.append(chunk)
        size -= len(chunk)
    return b"".join(chunks)


def read_frame():
    header = read_exact(4)
    size = int.from_bytes(header, "big")
    if size < 2 or size > MAX_FRAME:
        raise ValueError("invalid frame size")
    return json.loads(read_exact(size), object_pairs_hook=unique_object)


def write_frame(value):
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_FRAME:
        raise HostError("response too large")
    sys.stdout.buffer.write(len(raw).to_bytes(4, "big") + raw)
    sys.stdout.buffer.flush()


def home_dir():
    return pwd.getpwuid(os.getuid()).pw_dir


def cmux_path():
    home = home_dir()
    for path in ("/opt/homebrew/bin/cmux", "/usr/local/bin/cmux", "/usr/bin/cmux",
                 "/bin/cmux", "/home/linuxbrew/.linuxbrew/bin/cmux",
                 os.path.join(home, ".local/bin/cmux"), os.path.join(home, "bin/cmux")):
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    raise HostError("cmux executable unavailable", "failed")


def run(argv, timeout=TIMEOUT, allow_nonzero=False):
    try:
        child = subprocess.Popen([cmux_path(), *argv], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 env={"PATH": "/usr/bin:/bin", "HOME": home_dir()},
                                 start_new_session=True)
    except OSError:
        raise HostError("cannot start cmux") from None
    selector = selectors.DefaultSelector()
    selector.register(child.stdout, selectors.EVENT_READ)
    output = bytearray()
    deadline = time.monotonic() + timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise HostError("cmux operation timed out")
            chunk = os.read(child.stdout.fileno(), 8192)
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > MAX_OUTPUT:
                raise HostError("cmux response exceeded limit")
        if child.wait(timeout=max(0.01, deadline - time.monotonic())) != 0 and not allow_nonzero:
            raise HostError("cmux operation failed")
    except (OSError, subprocess.TimeoutExpired):
        raise HostError("cmux operation failed") from None
    finally:
        selector.close()
        if child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except OSError:
                pass
            child.wait()
        child.stdout.close()
    return bytes(output)


def rpc(method, params, timeout=TIMEOUT):
    try:
        value = json.loads(run(["rpc", method, json.dumps(params, separators=(",", ":"))], timeout=timeout),
                           object_pairs_hook=unique_object)
    except (ValueError, UnicodeError):
        raise HostError("cmux returned invalid JSON") from None
    if not isinstance(value, dict):
        raise HostError("cmux returned invalid response")
    return value


def string(value, name):
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 4096 or any(ord(c) < 32 for c in value):
        raise HostError("invalid " + name)
    return value


def optional_string(value, name):
    if value is None:
        return None
    return string(value, name)


def group(item):
    if not isinstance(item, dict):
        raise HostError("invalid group")
    gid = string(item.get("id"), "group id")
    anchor = string(item.get("anchor_workspace_id"), "anchor id")
    members = item.get("member_workspace_ids")
    if not isinstance(members, list) or any(not isinstance(x, str) for x in members):
        raise HostError("invalid group members")
    members = [string(member, "workspace id") for member in members]
    if len(set(members)) != len(members) or anchor not in members:
        raise HostError("incomplete group membership")
    return {"id": gid, "name": item.get("name") if isinstance(item.get("name"), str) else "",
            "anchor_workspace_id": anchor, "member_workspace_ids": members,
            "is_collapsed": item.get("is_collapsed") is True}


def groups(window=None, timeout=TIMEOUT):
    params = {} if window is None else {"window_id": window}
    value = rpc("workspace.group.list", params, timeout)
    rows = value.get("groups")
    if not isinstance(rows, list):
        raise HostError("cmux group inventory incomplete")
    return [group(row) for row in rows]


def workspaces(window=None, timeout=TIMEOUT):
    value = rpc("workspace.list", {"window_id": window}, timeout)
    rows = value.get("workspaces")
    if not isinstance(rows, list):
        raise HostError("cmux workspace inventory incomplete")
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            raise HostError("invalid workspace inventory")
        wid = string(row.get("id"), "workspace id")
        if wid in result:
            raise HostError("duplicate workspace identity")
        result[wid] = {"directory": row.get("current_directory") if isinstance(row.get("current_directory"), str) else "",
                       "title": row.get("custom_title") if isinstance(row.get("custom_title"), str) else None,
                       "description": row.get("description") if isinstance(row.get("description"), str) else None}
    return result


def all_workspaces():
    deadline = time.monotonic() + 12
    rows = rpc("window.list", {}, 3.0).get("windows")
    if not isinstance(rows, list):
        raise HostError("cmux window inventory incomplete")
    result = {}
    seen_windows = set()
    for row in rows:
        if not isinstance(row, dict):
            raise HostError("invalid window inventory")
        wid = string(row.get("id"), "window id")
        if wid in seen_windows:
            raise HostError("duplicate window identity")
        seen_windows.add(wid)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HostError("cmux workspace inventory timed out")
        found = workspaces(wid, remaining)
        if set(result).intersection(found):
            raise HostError("duplicate workspace identity")
        result.update(found)
    return result


def require(params, keys):
    if not isinstance(params, dict) or set(params) != set(keys):
        raise HostError("invalid method parameters")


def created_workspace(params):
    gid, title, summary, cwd, command = (string(params[k], k) for k in
        ("group_id", "title", "summary", "cwd", "startup_command"))
    window = params["window_id"]
    if window is not None:
        window = string(window, "window_id")
    focus = params["focus"]
    if type(focus) is not bool:
        raise HostError("invalid focus")
    initial = next((g for g in groups(window, timeout=2.0) if g["id"] == gid), None)
    if initial is None:
        raise HostError("group not found", "not_created")
    before = set(initial["member_workspace_ids"])
    argv = ["new-workspace", "--name", title, "--description", summary, "--cwd", cwd,
            "--command", command, "--group", gid, "--group-placement", "end",
            "--focus", "true" if focus else "false"]
    if window is not None:
        argv += ["--window", window]
    try:
        run(argv, timeout=4.0)
    except HostError as error:
        error.code = "outcome_unknown"
        raise
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        current = next((g for g in groups(window, timeout=min(3.0, remaining)) if g["id"] == gid), None)
        if current is None:
            raise HostError("group disappeared after workspace request", "outcome_unknown")
        added = set(current["member_workspace_ids"]) - before
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        inventory = workspaces(window, remaining)
        matches = [wid for wid in added if inventory.get(wid, {}).get("directory") == cwd]
        if len(matches) == 1:
            actual_window = dispatch("window_for_workspace", {"workspace_id": matches[0]})
            if window is not None and actual_window != window:
                raise HostError("created workspace appeared in an unexpected window", "outcome_unknown")
            return {"workspace_id": matches[0], "window_id": actual_window, "group_id": gid}
        time.sleep(0.15)
    raise HostError("workspace creation outcome unknown", "outcome_unknown")


def repository_identity(directory):
    git = shutil.which("git")
    if not git:
        raise HostError("git executable unavailable")
    result = subprocess.run([git, "-C", directory, "rev-parse", "--path-format=absolute", "--git-common-dir"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=3, check=False)
    if result.returncode:
        return None
    common = os.path.realpath(os.fsdecode(result.stdout).strip())
    return hashlib.sha256(os.fsencode(common)).hexdigest()[:16]


def ensure_repository_group(params):
    require(params, ["repository_identity", "repository_name", "repository_root"])
    identity = string(params["repository_identity"], "repository identity")
    name = string(params["repository_name"], "repository name")
    root = string(params["repository_root"], "repository root")
    if not os.path.isabs(root) or len(identity) != 16:
        raise HostError("invalid repository identity or root")
    window = dispatch("current_window", {})
    current = dispatch("current_workspace", {})
    available = groups(window)
    inventory = workspaces(window)
    candidates = [g for g in available if g["name"] == name and any(
        repository_identity(item["directory"]) == identity
        for wid in g["member_workspace_ids"]
        if (item := inventory.get(wid)) is not None
    )]
    current_group = next((g for g in candidates if current in g["member_workspace_ids"]), None)
    if current_group is not None:
        selected = current_group
    elif len(candidates) == 1:
        selected = candidates[0]
    elif len(candidates) > 1:
        raise HostError("multiple cmux groups match this repository; run Ahu from the group to use")
    else:
        value = rpc("workspace.group.create", {"name": name, "cwd": root, "window_id": window})
        selected = group(value.get("group"))
    return {"group": selected, "window_id": window}


def verify_task_workspace(params):
    require(params, ["workspace_id", "group_id", "window_id", "expected_directory"])
    wid = string(params["workspace_id"], "workspace id")
    gid = string(params["group_id"], "group id")
    window = string(params["window_id"], "window id")
    directory = string(params["expected_directory"], "expected directory")
    inventory = workspaces(window)
    if wid not in inventory:
        return "absent"
    if inventory[wid].get("directory") != directory:
        return "mismatch"
    if dispatch("window_for_workspace", {"workspace_id": wid}) != window:
        return "mismatch"
    if [g["id"] for g in groups(window) if wid in g["member_workspace_ids"]] != [gid]:
        return "mismatch"
    return "owned"


def session_scope(identity, directory):
    raw = json.dumps({"version": 2, "repository_identity": identity,
                      "directory": directory}, sort_keys=True,
                     separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def session_token(value):
    identity = string(value.get("repository_identity"), "repository identity")
    directory = string(value.get("directory"), "worktree directory")
    ids = [string(value.get(key), key) for key in
           ("workspace_id", "window_id", "group_id")]
    reference = ":".join(["cmux2", session_scope(identity, directory), *ids])
    return session_value(reference)


def session_value(reference):
    text = string(reference, "session reference")
    parts = text.split(":")
    if len(text) > 256 or len(parts) != 5 or parts[0] != "cmux2":
        raise HostError("invalid session reference")
    if len(parts[1]) != 64 or any(c not in "0123456789abcdef" for c in parts[1]):
        raise HostError("invalid session reference")
    if any(not item or len(item) > 64 or any(
            c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-" for c in item)
            for item in parts[2:]):
        raise HostError("invalid session reference")
    return text


def session_ownership(params):
    keys = {"session_ref", "repository_identity", "expected_directory"}
    if not isinstance(params, dict) or set(params) not in (keys, keys | {"value"}):
        raise HostError("invalid method parameters")
    raw_reference = params["session_ref"]
    reference = "" if raw_reference == "" else session_value(raw_reference)
    identity = string(params["repository_identity"], "repository identity")
    directory = string(params["expected_directory"], "expected directory")
    value = None
    if reference:
        _, scope, workspace_id, window_id, group_id = reference.split(":")
        if scope != session_scope(identity, directory):
            return "mismatch", None
        value = {"repository_identity": identity, "directory": directory,
                 "workspace_id": workspace_id, "window_id": window_id, "group_id": group_id}
    rows = rpc("window.list", {}).get("windows")
    if not isinstance(rows, list):
        raise HostError("cmux window inventory incomplete")
    candidates = []
    for row in rows:
        if not isinstance(row, dict):
            raise HostError("invalid cmux window inventory")
        actual_window = string(row.get("id"), "window id")
        for wid, workspace in workspaces(actual_window).items():
            if workspace.get("directory") == directory or (value and wid == value["workspace_id"]):
                candidates.append((actual_window, wid, workspace.get("directory")))
    if not candidates:
        return "absent", None
    if not reference:
        # Directory equality cannot prove who created a workspace after a lost reply.
        raise HostError("unrecorded workspace ownership requires operator reconciliation",
                        "outcome_unknown")
    if candidates != [(value["window_id"], value["workspace_id"], directory)]:
        return "mismatch", None
    if repository_identity(directory) != identity:
        return "mismatch", None
    state = verify_task_workspace({"workspace_id": value["workspace_id"],
        "group_id": value["group_id"], "window_id": value["window_id"],
        "expected_directory": directory})
    return state, value


def require_owned(params):
    ownership = {key: params[key] for key in
                 ("session_ref", "repository_identity", "expected_directory")
                 if key in params}
    state, value = session_ownership(ownership)
    if state != "owned":
        raise HostError("session is absent or ownership does not match the task worktree")
    return value


def create_task_session(params):
    require(params, ["repository_identity", "repository_name", "repository_root", "title",
                     "summary", "worktree", "startup_command", "focus"])
    identity = string(params["repository_identity"], "repository identity")
    root = string(params["repository_root"], "repository root")
    worktree = string(params["worktree"], "worktree")
    if len(identity) != 16 or not os.path.isabs(root) or not os.path.isabs(worktree):
        raise HostError("invalid repository or worktree identity")
    if repository_identity(root) != identity:
        raise HostError("repository identity does not match its root")
    placement = ensure_repository_group({"repository_identity": identity,
        "repository_name": string(params["repository_name"], "repository name"),
        "repository_root": root})
    created = created_workspace({"group_id": placement["group"]["id"],
        "window_id": placement["window_id"], "title": string(params["title"], "title"),
        "summary": string(params["summary"], "summary"), "cwd": worktree,
        "startup_command": string(params["startup_command"], "startup command"),
        "focus": params["focus"]})
    try:
        reference = session_token({"version": 2, "repository_identity": identity,
            "directory": worktree, "group_id": created["group_id"],
            "window_id": created["window_id"], "workspace_id": created["workspace_id"]})
    except HostError as error:
        error.code = "outcome_unknown"
        raise
    return {"session_ref": reference}


def place_coordinator(params):
    require(params, ["repository_identity", "repository_name", "repository_root", "label",
                     "harness", "model", "startup_command"])
    identity = string(params["repository_identity"], "repository identity")
    root = string(params["repository_root"], "repository root")
    caller = dispatch("current_workspace", {})
    if caller is None:
        return {"notes": [], "opened_session": False}
    placement = ensure_repository_group({"repository_identity": identity,
        "repository_name": string(params["repository_name"], "repository name"),
        "repository_root": root})
    group_id, window_id = placement["group"]["id"], placement["window_id"]
    caller_window = dispatch("window_for_workspace", {"workspace_id": caller})
    other_group = next((g for g in groups(caller_window)
        if g["id"] != group_id and caller in g["member_workspace_ids"]), None)
    label = string(params["label"], "label")
    harness = string(params["harness"], "harness")
    model = string(params["model"], "model")
    if other_group is not None:
        created = created_workspace({"group_id": group_id, "window_id": window_id,
            "title": label + " coordinator", "summary": label + " coordinator",
            "cwd": root, "startup_command": string(params["startup_command"], "startup command"),
            "focus": True})
        metadata(created["workspace_id"], "director", harness, model)
        return {"notes": ["the invoking session already belongs to another group; the provider opened a coordinator session under this repository"],
                "opened_session": True}
    actual_window = dispatch("window_for_workspace", {"workspace_id": caller})
    if actual_window != window_id:
        rpc("workspace.move_to_window", {"workspace_id": caller, "window_id": window_id})
    rpc("workspace.group.add", {"group_id": group_id, "workspace_id": caller, "window_id": window_id})
    if not any(caller in g["member_workspace_ids"] for g in groups(window_id) if g["id"] == group_id):
        raise HostError("coordinator group membership was not confirmed", "outcome_unknown")
    metadata(caller, "director", harness, model)
    rpc("workspace.group.expand", {"group_id": group_id})
    return {"notes": [], "opened_session": False}


def metadata(workspace_id, agent, harness, model):
    value = " · ".join(string(x, "agent metadata") for x in (agent, harness, model))
    run(["set-status", "ahu.agent", value, "--workspace", string(workspace_id, "session id")])


def dispatch(method, p):
    if method == "window_for_workspace":
        require(p, ["workspace_id"])
        workspace_id = string(p["workspace_id"], "workspace id")
        rows = rpc("window.list", {}).get("windows")
        if not isinstance(rows, list):
            raise HostError("cmux window inventory incomplete")
        matches = []
        for row in rows:
            if not isinstance(row, dict):
                raise HostError("invalid cmux window inventory")
            window_id = string(row.get("id"), "window id")
            if workspace_id in workspaces(window_id):
                matches.append(window_id)
        if len(matches) != 1:
            raise HostError("cmux workspace window is absent or ambiguous")
        return matches[0]
    if method == "current_window":
        value = rpc("workspace.list", {})
        return string(value.get("window_id"), "window id")
    if method == "current_workspace":
        rows = rpc("workspace.list", {}).get("workspaces")
        if not isinstance(rows, list):
            raise HostError("cmux workspace inventory incomplete")
        selected = [row for row in rows if isinstance(row, dict) and row.get("selected") is True]
        if len(selected) > 1:
            raise HostError("cmux selected workspace is ambiguous")
        return string(selected[0].get("id"), "workspace id") if selected else None
    if method == "create_task_session":
        return create_task_session(p)
    if method == "verify_task_session":
        state, _ = session_ownership(p)
        return state
    if method == "select_task_session":
        value = require_owned(p)
        rpc("workspace.select", {"workspace_id": value["workspace_id"]})
        return None
    if method == "close_task_session":
        state, value = session_ownership(p)
        if state == "owned":
            rpc("workspace.close", {"workspace_id": value["workspace_id"]})
        elif state == "mismatch":
            raise HostError("session ownership does not match the task worktree")
        return None
    if method == "set_task_status":
        value = require_owned(p)
        run(["set-status", "ahu.task", string(p["value"], "status"), "--workspace", value["workspace_id"]])
        return None
    if method == "expand_task_session":
        value = require_owned(p)
        rpc("workspace.group.expand", {"group_id": value["group_id"]})
        return None
    if method == "set_agent_metadata":
        require(p, ["session_ref", "repository_identity", "expected_directory",
                    "agent", "harness", "model"])
        value = require_owned(p)
        metadata(value["workspace_id"], string(p.get("agent"), "agent"),
                 string(p.get("harness"), "harness"), string(p.get("model"), "model"))
        return None
    if method == "place_coordinator":
        return place_coordinator(p)
    if method == "open_markdown":
        require(p, ["path", "focus"])
        if type(p["focus"]) is not bool:
            raise HostError("invalid focus")
        value = rpc("markdown.open", {"path": string(p["path"], "path"), "focus": p["focus"]})
        return string(value.get("surface_id"), "surface id")
    raise HostError("unsupported method")


def main():
    if sys.argv[1:] != ["--ahu-host-protocol=2"]:
        return 2
    try:
        hello = read_frame()
        if set(hello) != {"protocol_version", "id", "method", "params"} or hello["method"] != "hello" or hello["id"] != 1:
            return 2
        p = hello["params"]
        if p.get("protocol_versions") != [2] or p.get("host_api") != 1 or p.get("plugin_id") != PLUGIN_ID:
            return 2
        caps = p.get("required_capabilities")
        if not isinstance(caps, list) or any(c not in METHODS for c in caps) or len(set(caps)) != len(caps):
            return 2
        if caps != METHODS:
            return 2
        run(["ping"])
        advertised = json.loads(run(["capabilities"], timeout=5), object_pairs_hook=unique_object).get("capabilities", [])
        required = {"workspace.groups.v1", "workspace.group_create.v1", "workspace.create_in_group.v1"}
        if not isinstance(advertised, list) or not required.issubset(set(advertised)):
            return 2
        write_frame({"protocol_version": 2, "id": 1, "result": {"protocol_version": 2, "host_api": 1,
            "plugin_id": PLUGIN_ID, "capabilities": METHODS,
            "inherited_workspace": dispatch("current_workspace", {})}})
        expected_id = 2
        while True:
            try:
                request = read_frame()
            except EOFError:
                return 0
            if not isinstance(request, dict) or set(request) != {"protocol_version", "id", "method", "params"} or request["protocol_version"] != 2:
                return 2
            rid = request["id"]
            if type(rid) is not int or rid != expected_id:
                return 2
            expected_id += 1
            try:
                result = dispatch(request["method"], request["params"])
                response = {"protocol_version": 2, "id": rid, "result": result}
            except HostError as error:
                response = {"protocol_version": 2, "id": rid, "error": {"code": error.code, "message": str(error)[:512]}}
            except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
                response = {"protocol_version": 2, "id": rid, "error": {"code": "failed", "message": "invalid request or provider response"}}
            write_frame(response)
    except (EOFError, ValueError, TypeError, OSError, UnicodeError, RecursionError, HostError):
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
