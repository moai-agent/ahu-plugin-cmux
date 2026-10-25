# Ahu cmux session host

This optional, standalone Rust plugin implements all nine provider-neutral Ahu
Host V2 operations: task session creation, verification, selection, close, status,
expansion, coordinator placement, agent metadata, and Markdown viewing. It owns
cmux inventory and placement, not Ahu task records, worktrees, supervision,
approval policy, or cleanup decisions. No ahu core dependency is linked.

Installation and runtime require no Python. Runtime requires Git and a working
cmux installation/session with `workspace.groups.v1`, `workspace.group_create.v1`,
and `workspace.create_in_group.v1`. The original Python implementation is retained
only under `tests/oracle` as a development oracle. Headless/provider-free Ahu
continues to work without this optional plugin.

## Build and package

Use Rust/Cargo 1.88 or later (validated with Rust 1.96.0) on macOS or
Linux. From the reviewed checkout, build explicitly with the committed lockfile,
then stage a package in the ignored `dist` directory:

```sh
export CARGO_TARGET_DIR=/tmp/ahu-rust-cmux-target
cargo build --manifest-path package/Cargo.toml --release --locked --bin session-host
mkdir -p dist/cmux-host/bin
install -m 755 "$CARGO_TARGET_DIR/release/session-host" dist/cmux-host/bin/session-host
# The installed Cargo manifest declares package identity; it is not a build hook.
sed '/^\[\[bin\]\]/,$d' package/Cargo.toml > dist/cmux-host/Cargo.toml
install -m 644 package/ahu-plugin.md LICENSE README.md SECURITY.md dist/cmux-host/
ahu plugins digest "$(pwd -P)/dist/cmux-host"
```

The staging directory contains only the executable, Cargo identity, manifest,
license, and documentation. Neither the source `package` directory nor a Cargo
source archive is an installable binary package. Install never runs a build.
Build separately for each OS/architecture; digest the final package **after**
building and staging it. The commands are repeatable with the locked dependencies;
byte-for-byte cross-toolchain/platform reproducibility is not claimed.

Use Ahu 0.11.0 or a compatible later version. Bind the staged package with
`source.kind: local`, `source.repository` set to this checkout, and
`source.subdirectory: dist/cmux-host`, using `id: cmux-host`, version `0.2.0`,
and `grants: [interactive-backend]`. Pin the digest/revision returned by Ahu.
Keep the binding disabled during review; install and enable through the normal
Ahu plugin lifecycle. Execution requires a committed, reviewed binding. Consult
Ahu's executable-host contract for the complete binding shape. Do not replace
an active task's frozen package with this build.

## Transport and ownership

The executable accepts only `--ahu-host-protocol=2`. It uses four-byte big-endian
length-prefixed UTF-8 JSON, at most 1 MiB per frame, exact V2 handshake capabilities,
and sequential integer request IDs starting with hello ID 1. Duplicate JSON keys,
unknown envelope/parameter fields, partial frames, and invalid types are rejected.
Only EOF between frames is clean. stdout contains protocol frames only.

Task references retain the `cmux2` format and original hash encoding, binding the
exact workspace, window, and group to repository/worktree scope. Every task action
checks inventory and repository identity again. Replacements, moved sessions,
changed directories/groups, and ambiguous matches refuse mutations. An empty
reference with a matching directory returns `outcome_unknown`; no match returns
`absent`. Directory equality alone cannot recover ownership after a lost reply.
Older directory-only references are refused.

Create validates inputs and root/worktree Git identity before mutation. After a
create command is dispatched, failed confirmation is `outcome_unknown`, including
timeout, nonzero exit, malformed provider output, or an invalid returned identity.
Creation is never retried. Read-only inventory may be polled to observe the result.
The coordinator/operator must reconcile uncertain creation with provider inventory.

The CLI uses fixed cmux search locations and OS-account `HOME`, preserving cmux's
normal local authentication and selected-workspace context. It does not accept
socket, executable, or token overrides from protocol input/environment. Startup
commands are passed unchanged as an argv value to cmux, which interprets them in
its interactive shell; Ahu must construct and quote them safely.

## Validate

```sh
export CARGO_TARGET_DIR=/tmp/ahu-rust-cmux-target
cargo fmt --manifest-path package/Cargo.toml --all --check
cargo clippy --manifest-path package/Cargo.toml --all-targets --locked -- -D warnings
cargo clippy --manifest-path package/Cargo.toml --no-default-features --all-targets --locked -- -D warnings
cargo test --manifest-path package/Cargo.toml --locked
cargo build --manifest-path package/Cargo.toml --bins --examples --locked
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

Python is development-only. Tests independently encode wire frames and drive a
compiled Rust fixture host using the same transport, domain, and subprocess code
with explicit temporary provider paths. The shipped executable's CLI refusal
paths are also tested. The fixture driver and fake cmux are never packaged. Tests
use temporary Git repositories, synthetic provider state, and bounded process
fixtures; they never invoke live cmux or mutate network providers. The oracle's
unit tests remain separate from Rust implementation coverage.

Live cmux creation/close and authentication compatibility have **not** been
validated by this migration. The coordinator must run those checks in a dedicated
disposable repository/worktree before release, inspect the ownership reference,
and remove only a workspace created by that run. Fixture success is not proof of
live compatibility. See [SECURITY.md](SECURITY.md) before granting access.
