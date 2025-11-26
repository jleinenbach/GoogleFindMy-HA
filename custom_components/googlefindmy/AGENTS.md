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
  * Tests often monkeypatch `hass.async_create_task` with lightweight stand-ins. When authoring platform code, either guard direct calls (for example, verify the attribute exists before invoking it) or update the runtime-patterns guide with the expected stub signature so regressions like the coordinator listener crash do not resurface.
  * Keep the coordinator stub in `tests/conftest.py` aligned with new runtime helpers (for example, visibility-wait utilities) to avoid missing-attribute regressions during setup.
  * The `_async_create_task` helper in `custom_components/googlefindmy/__init__.py` intentionally delegates directly to `hass.async_create_task` with the optional `name` argument. Avoid reintroducing alternate scheduling paths that enqueue coroutines multiple times; update tests instead if new task semantics are required.
* [`docs/CONFIG_SUBENTRIES_HANDBOOK.md`](../../docs/CONFIG_SUBENTRIES_HANDBOOK.md) — Canonical reference for config subentry setup/unload flows.
  * When changing config entry or subentry behavior (flows, platform forwarding, `runtime_data` layout), cross-check the handbook and cite the relevant sections in PR descriptions or code comments that rely on guarantees such as data-only `ConfigSubentry` objects or the absence of `config_subentry_id` in `async_forward_entry_setups`.

When adding new guidance, prefer creating another `agents/<topic>/AGENTS.md` file instead of expanding this index. This keeps
updates like the subentry unload reminder easy to place without scrolling through unrelated instructions.

### Quick-start reminder: avoid false-positive tracker discovery

When restoring `device_tracker` entities on startup, confirm the cloud discovery trigger only fires for **truly new** tracker
entities. Reuse the coordinator's registry helpers (for example, `find_tracker_entity_entry`) **after** entities are scheduled
to detect whether each scheduled entity already exists in the entity registry and skip the discovery flow when all restored
devices are known. Centralizing this post-scheduling gate prevents redundant pre-checks and keeps the "X devices found"
notification from reappearing after restarts when no new hardware has been added. Cross-link:
[`agents/runtime_patterns/AGENTS.md`](agents/runtime_patterns/AGENTS.md#tracker-registry-gating)
tracks the canonical post-scheduling gate that platform guides should mirror.

### Nova API cache provider registration

When decrypting FCM background location payloads, **always** register the active entry cache with
`nova_request.register_cache_provider` immediately before calling the Nova async decryptor and **always**
unregister it in a `finally` block. The decryptor resolves credentials via this provider, so skipping registration or
running decryption in an executor without the surrounding context will cause multi-account setups to fail silently.
Handle `StaleOwnerKeyError` from the decryptor by logging and skipping the update instead of crashing the pipeline so key
rotation can proceed without interrupting other accounts.

### Import deferral reminder

Heavyweight runtime dependencies (for example, browser drivers such as `undetected_chromedriver`) must be imported lazily inside
the helpers that use them. Avoid module-level imports that execute expensive discovery logic during Home Assistant startup—wrap
the import in a small getter and call it only from the executor-backed runtime path.
