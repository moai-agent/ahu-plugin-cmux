# Security inventory

This inventory describes the staged Rust package described in README.md. Ahu executes this
same-user program only after the project's committed plugin binding is enabled
with the `interactive-backend` grant and the exact installed package is verified.
The manifest is not a sandbox, and cmux itself is a separate same-user service.

| Capability | Effect | Boundary and control |
| --- | --- | --- |
| Inventory and lookup | Reads cmux groups, windows, and workspace directory/title/description metadata | Fixed `cmux rpc` method names; validated JSON; no arbitrary RPC input |
| Presentation | Moves a workspace between windows/groups, changes anchor/collapse/selection, opens a Markdown path, and sets Ahu status/agent labels | Requires an Ahu host operation; fixed RPC/CLI forms; values are passed as argv or JSON, never shell-expanded |
| Workspace lifecycle | Creates groups, anchors, and task workspaces; closes a specified workspace | The plugin returns a bounded reference binding a repository/worktree digest to the exact created workspace, window and group IDs. It is an identifier, not a secret. Every mutation rechecks that exact identity, current directory and group membership; replacements, moves and ambiguous matches are refused. An empty reference with a matching directory reports `outcome_unknown`, since directory equality cannot prove creation ownership. With no matching workspace it reports `absent`. Lost creation replies require operator reconciliation; the plugin never closes a guessed workspace |
| Task startup | Passes Ahu's `startup_command` to cmux's interactive shell | High impact: cmux interprets this string as shell code. Ahu must construct it from trusted, shell-quoted startup data and persist task inputs before dispatch. The plugin does not rewrite or log it. |
| Local process | Starts the fixed cmux executable with bounded time and output | Rust `Command` with explicit argv; fixed executable search paths; stdin closed; stderr discarded; clean environment and `/` working directory; private process group killed on every completion/failure; output capped at 1 MiB |
| Authentication/config | cmux uses its normal local socket/session | No token or socket path is stored in the package or returned by the plugin. Ahu's Host V2 process contract clears inherited environment; this adapter restores only `HOME` from the OS account database for CLI operation. |

The Rust adapter negotiates the nine provider-neutral Host V2 methods. cmux group,
window, and workspace identifiers remain private to this plugin and are not
part of Ahu's Rust task schema or public Host contract. It rejects duplicate JSON keys,
unknown request envelope fields, unknown method parameters, invalid IDs/strings,
oversized frames, and incomplete inventories. It does not retry requests. cmux
may still complete a mutation after a timeout or lost response; except for an
affirmative pre-dispatch refusal, Ahu must treat ambiguous creation as unknown.

Residual risks: same-user code can use other local capabilities available to the
user; cmux validates the caller independently; CLI/API behavior can change between
cmux versions; and this package cannot prove that another process did not race a
workspace operation. The host protocol does not sandbox startup commands or
protect against other code running as the same OS user. Review and pin the package
before granting interactive backend access. Tests use a fake cmux executable and
never mutate a live session.

Session references use the current `cmux2` format. Older directory-only references
are refused by this version; existing Ahu tasks keep their frozen package version.
Do not repoint an active task at a different package. Review uncertain creation
against provider inventory before an operator closes a confirmed owned workspace.

## Rust migration boundaries and verification

The standalone crate uses `serde`/`serde_json` for strict JSON parsing, `sha2` for
reference/repository hashes, and `libc` for OS-account lookup and bounded pipe
polling/process-group cleanup. There is no ahu core dependency, Python runtime,
install-time build hook, or dynamic executable selection from requests. Python
exists only in development tests and the historical oracle; neither is staged.

Both cmux and Git subprocesses have bounded output and duration, null stdin and
stderr, a fixed working directory, and a clean environment containing only PATH
and OS-account HOME. Git discovery uses fixed system/Homebrew paths. cmux retains
its original system/Homebrew and OS-account local-bin search order. The host does
not forward inherited cmux socket/authentication/context overrides. Successful
handshake still pings cmux, checks group/create capabilities, and derives inherited
workspace from the selected CLI inventory.

Each operation has a shared 12-second deadline across subprocesses, including
inventory and Git probes. Create dispatch is capped at four seconds, Git probes
at three seconds, and handshake capability discovery at five seconds. Each child
owns a private process group, killed and reaped after success or failure; this
also bounds descendants holding stdout open after the direct child exits. Host
stdin intentionally waits for the next frame; Ahu owns host cancellation and
connection lifetime. Subprocess bounds do not make the host an OS sandbox.

The migration tightens defective failure paths: exact hello version/type checks,
strict keys on every operation, duplicate inventory identities, validation before
mutations, worktree/root repository agreement, and unknown outcomes for every
failure after task create dispatch. General provider mutation errors do not prove
absence of effects. No implicit mutation retry or reconnect is performed.

Independent wire/process fixtures cover malformed/partial/oversized frames,
duplicate JSON keys, invalid hello and sequence IDs, all nine operations, unknown
parameters, identity replacement/movement/ambiguity, unrecorded recovery, lost or
malformed creation replies, output limits, and descendant cleanup. The test driver
injects synthetic provider paths through the library; the customer executable
exposes no such override. Fixtures do not verify live cmux compatibility. Live
provider/authentication and disposable creation/close checks remain the release
coordinator's responsibility.
