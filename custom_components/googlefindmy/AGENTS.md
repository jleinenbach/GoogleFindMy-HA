# custom_components/googlefindmy/AGENTS.md

This directory now exposes focused AGENT files grouped by topic so contributors can jump directly to the guidance they need.
Each linked file below applies to **every** module under `custom_components/googlefindmy/` unless a more specific AGENT in a
child directory overrides it.

## Topical index

| Topic | File |
| --- | --- |
| Config flows, reconfigure hooks, and service validation | [`agents/config_flow/AGENTS.md`](agents/config_flow/AGENTS.md) |
| Runtime lifecycle patterns, platform forwarding, and subentry helpers (**entity lifecycle requirements live here**) | [`agents/runtime_patterns/AGENTS.md`](agents/runtime_patterns/AGENTS.md) |
| Typing reminders, stub imports, and strict mypy expectations | [`agents/typing_guidance/AGENTS.md`](agents/typing_guidance/AGENTS.md) |

## Cross-reference index

* [`tests/AGENTS.md`](../../tests/AGENTS.md) — Discovery and reconfigure test stubs, including the lightweight `ConfigEntry` doubles referenced across the topical guides above.
* [`docs/CONFIG_SUBENTRIES_HANDBOOK.md`](../../docs/CONFIG_SUBENTRIES_HANDBOOK.md) — Canonical reference for config subentry setup/unload flows.

When adding new guidance, prefer creating another `agents/<topic>/AGENTS.md` file instead of expanding this index. This keeps
updates like the subentry unload reminder easy to place without scrolling through unrelated instructions.
