---
okf_version: "0.2"
type: ahu:plugin
title: cmux session host
description: cmux workspace lifecycle backend for Ahu Host protocol V2
tags: [cmux, terminal, session-host]
schema_version: 1
id: cmux-host
package: ahu-cmux-session-host-plugin
host_api: 1
cargo_manifest: Cargo.toml
executable:
  path: bin/session-host
  protocol_version: 2
  timeout_ms: 15000
---

# cmux session host

Implements all Ahu interactive Host protocol V2 workspace operations through the cmux CLI.
See the repository README and SECURITY.md for its capability inventory.
