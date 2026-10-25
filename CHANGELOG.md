# 0.2.1

The cmux Host V2 package now contains a standalone Rust executable. The wire
protocol, nine operations and opaque session reference format are unchanged.
Runtime requires native cmux and Git, but no Python. Python remains development
fixture/oracle tooling only.

Build from locked source on the target platform and stage into a fresh directory
before computing its package digest. Source directories are not installable
executable packages. Finish tasks using the old frozen host before updating its
binding. See README.md for build, upgrade and security guidance.
