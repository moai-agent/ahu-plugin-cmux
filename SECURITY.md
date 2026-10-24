# Security inventory

This inventory describes `package/ahu-plugin.md` as shipped. Ahu executes this
same-user program only after the project's committed plugin binding is enabled
with the `interactive-backend` grant and the exact installed package is verified.
The manifest is not a sandbox, and cmux itself is a separate same-user service.

| Capability | Effect | Boundary and control |
| --- | --- | --- |
| Inventory and lookup | Reads cmux groups, windows, and workspace directory/title/description metadata | Fixed `cmux rpc` method names; validated JSON; no arbitrary RPC input |
| Presentation | Moves a workspace between windows/groups, changes anchor/collapse/selection, opens a Markdown path, and sets Ahu status/agent labels | Requires an Ahu host operation; fixed RPC/CLI forms; values are passed as argv or JSON, never shell-expanded |
| Workspace lifecycle | Creates groups, anchors, and task workspaces; closes a specified workspace | The plugin returns a bounded reference binding a repository/worktree digest to the exact created workspace, window and group IDs. It is an identifier, not a secret. Every mutation rechecks that exact identity, current directory and group membership; replacements, moves and ambiguous matches are refused. An empty reference with a matching directory reports `outcome_unknown`, since directory equality cannot prove creation ownership. With no matching workspace it reports `absent`. Lost creation replies require operator reconciliation; the plugin never closes a guessed workspace |
| Task startup | Passes Ahu's `startup_command` to cmux's interactive shell | High impact: cmux interprets this string as shell code. Ahu must construct it from trusted, shell-quoted startup data and persist task inputs before dispatch. The plugin does not rewrite or log it. |
| Local process | Starts the fixed cmux executable with bounded time and output | Standard-library Python; fixed executable search paths; stdin closed; stderr discarded; process group killed on timeout; response capped at 1 MiB |
| Authentication/config | cmux uses its normal local socket/session | No token or socket path is stored in the package or returned by the plugin. Ahu's Host V2 process contract clears inherited environment; this adapter restores only `HOME` from the OS account database for CLI operation. |

The adapter negotiates the nine provider-neutral Host V2 methods. cmux group,
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
