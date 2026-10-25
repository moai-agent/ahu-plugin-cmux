# Ahu cmux session host

This repository packages an optional cmux backend that implements the nine
provider-neutral operations in Ahu's public Host protocol V2 contract. The
plugin privately owns cmux group/window/workspace inventory, task workspace
creation, opaque session references, selection and close, metadata/status, and
Markdown viewing. It does not replace Ahu's task records, worktree ownership,
agent supervision, approval policy, or cleanup decisions. See [SECURITY.md](SECURITY.md)
for the exact capability and trust inventory.

The implementation is a Python standard-library executable that speaks Ahu's
length-prefixed Host protocol V2 protocol and invokes only fixed cmux CLI/RPC operations.
It requires a working cmux installation and session. Headless and provider-free
Ahu use remain available when this plugin is absent or disabled.


## Runtime language policy and migration status

Ahu-maintained customer-facing executables must be Rust and must not require
Python at installation or runtime. Python is permitted for development tooling,
tests, and fixtures only. Skills remain text, and the external CLI retains its
own implementation.

The adapters in this checkout currently use Python and do not yet meet this
policy. Their next supported release requires Rust replacements with equivalent
protocol behavior, security checks, and verified live integration. They remain
external, optional plugins; migration does not move provider code into ahu core.

## Install from a checkout

Install ahu 0.11.0 or a compatible later version. Clone this repository at the
reviewed release tag, then compute the package digest from that checkout:

```sh
ahu plugins digest "$(pwd -P)/package"
```

Use the result as the pinned revision and SHA-256 in a project plugin binding
with `source.kind: local`, `source.repository` set to this checkout,
`source.subdirectory: package`, `id: cmux-host`, version `0.2.0`, and
`grants: [interactive-backend]`. Keep the binding disabled while reviewing it,
then install and enable it using the normal Ahu plugin lifecycle. Selection and
execution require a committed, reviewed binding. Consult Ahu's executable host
documentation for the complete OKF binding shape and compatibility fields.

The host process receives Ahu's fixed clean environment. This executable looks
up cmux in common system/Homebrew locations and restores only `HOME` from the OS
account database for CLI use; it does not read a socket path from plugin input.
The startup command is supplied by Ahu and interpreted by cmux's interactive
shell, so Ahu must continue to construct and shell-quote it safely.

## Validate

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

The unit tests use mocked RPC/CLI calls and do not modify a live cmux session.
Live creation and close checks require a dedicated disposable repository/worktree;
verify the returned ownership reference and remove only the workspace created by
that run. Never use a user's existing workspace as a test target. Ahu's
external-host fixture and lifecycle tests remain necessary for protocol and
failure-path coverage; package tests alone do not establish live compatibility.

Keep the source pinned to the reviewed package digest and inspect the security
inventory before granting session-host access.

## Session ownership and recovery

Task references bind the exact created workspace, window and group, together with
the repository/worktree scope. A replacement workspace at the same path is not
the same session. Changes to placement or ambiguous matches refuse mutations.
If a creation reply was lost, a directory match alone is insufficient ownership
proof: recovery reports uncertainty and requires operator reconciliation. No
matching workspace is reported absent. See SECURITY.md for the same-user and
concurrent-change limits. The current reference format is `cmux2`; directory-only
references from earlier local candidates are not accepted by this package.
