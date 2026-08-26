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

### FHNA frame slicing reminder

BLE FHNA service data places the frame type at octet 7 with the EID starting at octet 8. Resolver updates must keep these offsets authoritative and only fall back to the 1-byte header layout when the service-data pattern does not apply.

The frame byte does not carry the EID length. Per the [Find Hub Network Accessory Specification](https://developers.google.com/nearby/fast-pair/specifications/extensions/fmdn) (retrieved 2026-08-05) it tracks unwanted tracking protection mode: `0x41` while the mode is active, `0x40` otherwise. `0x40` therefore correlates with 20-byte legacy EIDs and `0x41` with 32-byte P-256 ones only as a field observation, never as a rule — a legacy beacon in tracking-protection mode and a modern beacon outside it are both conformant. Derive the slice length primarily from the payload length (`_framed_eid_lengths`, shared by both framed geometries), and probe the 32-byte reading first: the first 20 bytes of a 32-byte EID are a precomputed lookup entry of their own (`MODERN_P256_X20_TRUNC_*`), so the shorter reading otherwise matches first and puts the hashed-flags byte on EID material. The frame byte still decides exactly two documented asymmetries, both spelled out in that function's docstring; read it before changing a band.

"Does not apply" is decided by the lookup result, not by byte 7 alone: byte 7 of a raw-header payload is EID material and matches 0x40/0x41 once in 256 rotation windows. Probe both geometries and let the successful candidate define the geometry, including the position of the optional hashed-flags byte (`EidCandidate.offset + len(eid)`). Do not re-derive that position from the payload afterwards — the match already answered it, and re-deriving it guesses a second time. `EidCandidate.layout` is the discriminator for whether the position is known at all: `"framed"` and `"bare"` know it (for `"bare"` the answer is "there is no flags byte"), `"window"` does not, because a sliding-window offset is a find position rather than a parsed layout.

### SPOT/gRPC client reminder

When reusing the shared grpclib transport (`SpotGrpcTransport`), keep SSL context creation lazy and ensure ALPN includes `h2`. The transport helper already sets the protocol list and should be closed on unload so new channels negotiate HTTP/2 cleanly.

## Cross-reference index

* [`tests/AGENTS.md`](../../tests/AGENTS.md) — Discovery and reconfigure test stubs, including the lightweight `ConfigEntry` doubles referenced across the topical guides above.
  * Tests often monkeypatch `hass.async_create_task` with lightweight stand-ins. When authoring platform code, either guard direct calls (for example, verify the attribute exists before invoking it) or update the runtime-patterns guide with the expected stub signature so regressions like the coordinator listener crash do not resurface.
  * Keep the coordinator stub in `tests/conftest.py` aligned with new runtime helpers (for example, visibility-wait utilities) to avoid missing-attribute regressions during setup.
  * The `_async_create_task` helper in `custom_components/googlefindmy/__init__.py` intentionally delegates directly to `hass.async_create_task` with the optional `name` argument. Avoid reintroducing alternate scheduling paths that enqueue coroutines multiple times; update tests instead if new task semantics are required.
* [`docs/CONFIG_SUBENTRIES_HANDBOOK.md`](../../docs/CONFIG_SUBENTRIES_HANDBOOK.md) — Canonical reference for config subentry setup/unload flows.
  * When changing config entry or subentry behavior (flows, platform forwarding, `runtime_data` layout), cross-check the handbook and cite the relevant sections in PR descriptions or code comments that rely on guarantees such as data-only `ConfigSubentry` objects or the absence of `config_subentry_id` in `async_forward_entry_setups`.

When adding new guidance, prefer creating another `agents/<topic>/AGENTS.md` file instead of expanding this index. This keeps
updates like the subentry unload reminder easy to place without scrolling through unrelated instructions.

### Version bump touches three files (manifest.json + const.py + pyproject.toml)

The integration version lives in **three** places that MUST be bumped together on every release:
`manifest.json` `"version"`, `const.py` `INTEGRATION_VERSION` (the latter feeds diagnostics,
device `sw_version`, and logs), and `pyproject.toml` `[tool.poetry] version` (the distribution
version). `semantic-release` now rewrites **all three** automatically on any release line
(`main` and every `X.Y` maintenance branch, e.g. `1.7`) via `[tool.semantic_release] version_toml`
(pyproject) plus `version_variables` (`const.py` + `manifest.json`); a manual three-file bump is
only a fallback for out-of-band edits. Historically, before `version_variables` was wired up,
`pyproject.toml` silently drifted to `1.7.1` while the integration shipped `1.7.4`. `manifest.json`
is strict JSON validated by hassfest, so it cannot carry an inline cross-reference comment or an
extra key; this note is the canonical anchor for the manifest side. `const.py` and `pyproject.toml`
carry reciprocal cross-reference comments naming the other two files. Note: `const.py`'s
`INTEGRATION_VERSION` deliberately has **no** `: str` annotation, because the `version_variables`
regex matches `NAME = "x"` but not `NAME: str = "x"`; do not re-add the annotation.
A release that bumps only some files ships an inconsistent version string.
Before tagging a release, grep all three:
`git grep -nE '"version"|INTEGRATION_VERSION|^version = ' custom_components/googlefindmy/manifest.json custom_components/googlefindmy/const.py pyproject.toml`.

### Quick-start reminder: a new tracker is an entity, not a discovery

A tracker that appears in the account is added silently: no discovery flow, no card, no user click. There is no cloud
discovery trigger in `device_tracker.py` any more, and no "X devices found" notification for trackers. Discovery is reserved
for a new account and for refreshed credentials of an existing one. The registry check **after** entities are scheduled is
still there, but it serves a different purpose: it is the input to a single self-healing reload for what is missing **and**
the moment the trackers that did register are re-derived into the polling set, and it is only judged once a
grace period has passed, because scheduling an entity and registering it are not the same instant. Cross-link:
[`agents/runtime_patterns/AGENTS.md`](agents/runtime_patterns/AGENTS.md#tracker-registry-gating)
carries the canonical contract; that file wins if the two ever drift.

### Async test execution contract

Within `tests/`, **never** call `asyncio.run()` to drive coroutines. Home Assistant’s
test harness already provides a managed event loop via `pytest-asyncio`; starting a
new loop inside a test causes fixture clashes and resource leaks. Mark coroutine tests
with `@pytest.mark.asyncio` (or set `pytestmark = pytest.mark.asyncio` in the module)
and `await` the coroutine directly.

### Nova API cache provider registration

When decrypting FCM background location payloads, **always** register the active entry cache with
`nova_request.register_cache_provider` immediately before calling the Nova async decryptor and **always**
unregister it in a `finally` block. The decryptor resolves credentials via this provider, so skipping registration or
running decryption in an executor without the surrounding context will cause multi-account setups to fail silently.
Handle `StaleOwnerKeyError` from the decryptor by logging and skipping the update instead of crashing the pipeline so key
rotation can proceed without interrupting other accounts.

Normalize FCM canonic IDs before validation (for example, compare `response_canonic_id.lower()` to
`canonic_device_id.lower()` and store the lowercase string on decrypted payloads) so tracker updates are not discarded due
to server-provided hex casing differences.

### Hybrid Low-Accuracy Polling

When a poll response fails the accuracy threshold, `coordinator.py` preserves the previous coordinates and accuracy but still
updates the new `last_seen` timestamp. This keeps map pins stable (no "jumping" to poor fixes) while reflecting that the device
recently reported. The cold-start drop path (no cached coordinates available) strips `_report_hint` before returning; mirror that
hint-stripping step in any new helpers that short-circuit low-quality updates so internal metadata never leaks into entity
state.

### Authentication failure propagation

When a location decrypt/FCM callback encounters `SpotApiEmptyResponseError`, store the exception on the callback context and
re-raise it after the waiter resumes so the coordinator can translate it into `ConfigEntryAuthFailed`. This keeps invalid
sessions flowing into Home Assistant's reauthentication UI instead of being swallowed in background threads.

Classify an auth rejection by the HTTP **status**, never by the exception type. `NovaAuthError` is raised for every
non-retryable 4xx (`nova_request.py` raises it for 400, 404, 405, 409, 422 as well; `HTTP_RETRY_ELIGIBLE` holds no 4xx
besides 408 and 429), so a type check reads "device not found" as "your sign-in expired". A 401 that survived the refresh
sequence arrives flagged `is_permanent=True`, which leaves 403 as the only plain credential rejection a handler sees.
`nova_request.is_credential_rejection` is the shared predicate: permanent first, then 401/403, an unreadable status keeps
the conservative verdict, everything else is a server-side rejection. `api._classify_nova_auth_error` is its sound-path
adapter and must not re-derive the status test -- a second copy is how the handlers drifted apart in the first place.
Extent today, stated so it is not mistaken for coverage: six sites read it. The two sound handlers via
`_classify_nova_auth_error`; the device-list handler (`api.py`, `except NovaAuthError` before `NovaProtobufDecodeError`),
which now raises `UpdateFailed` for a non-credential rejection; the location handler, which returns `{}`;
`coordinator/polling.py`, whose `except NovaAuthError` branch leaves the transient-auth counter alone in both directions;
and `coordinator/locate.py`, whose branch does not flip the account-wide auth state. `location_request.py` names the
status in its log records instead of calling every 4xx an authentication error, but still only re-raises -- it does not
classify.
Read those last two claims narrowly, they are about the BRANCH and not about the path: since `api.py` returns `{}` for
such a status, neither branch is reachable in production any more, and what the device actually takes is the SUCCESS
path -- `polling.py` "Success path: ensure any previous auth error is cleared" and `locate.py` "Success path: clear any
auth error state", both of which run BEFORE the `if not location` guard. So a 404 now RESETS the transient-auth counter
and clears the auth state, which is the opposite of what the branch says and can mask a genuine 401 on another tracker in
the same cycle. That reset is not new (every 5xx and 429 already returns `{}` and takes it), but it is newly reachable
for a client rejection. Fixing it means deciding what an empty result is allowed to prove about credentials, which is a
behaviour change of its own; it is tracked separately and must not be assumed done.
What is NOT fixed, stated so the rule is not mistaken for a solved problem: `nova_request.py` still raises a type named
"auth" for all of them, so a new handler that reads the type repeats the defect, and this paragraph is the only thing
standing in its way. Giving the non-credential case its own exception class is the open follow-up; it needs an
exhaustiveness test over `NovaError.__subclasses__()` first: measured, all eight `try` blocks that catch `NovaAuthError`
carry a broad `except Exception` in the same block, so a new class would be swallowed at every one of them under a name
that hides the status. Narrowing a handler is a behaviour change that needs its own regression test, not a
drive-by edit; do not assume a green suite proves the rest of the tree already follows this rule.

### Import deferral reminder

Heavyweight runtime dependencies (for example, browser drivers such as `undetected_chromedriver`) must be imported lazily inside
the helpers that use them. Avoid module-level imports that execute expensive discovery logic during Home Assistant startup—wrap
the import in a small getter and call it only from the executor-backed runtime path.

When adding a lazy import helper, **keep the corresponding `import_module` (or other loader) imported in the module** so static
analysis tools like `ruff` retain full visibility into the call site. Dropping the import and relying solely on dynamic
resolution causes undefined-name lint failures the next time the file is checked.

### Network Status Codes & Privacy Mapping

Use the proto/network label mapping below when interpreting report provenance or introducing new UI strings so contributor
privacy semantics remain aligned with Google's contribution settings.

| Proto Enum | Integration Label | Real-world Meaning |
| --- | --- | --- |
| `Status.CROWDSOURCED = 2` | `'crowdsourced'` | Location report from a finder contributing with network in **all areas** ("Contribution Settings: With network in all areas"). |
| `Status.AGGREGATED = 3` | `'aggregated'` | Location report from a finder contributing with network in **high-traffic areas only** ("Contribution Settings: With network in high-traffic areas only"). |
| `EncryptedReport.isOwnReport = true` or `Status.LAST_KNOWN = 1` | `'owner'` | Owner-sourced location report from the device itself. |
