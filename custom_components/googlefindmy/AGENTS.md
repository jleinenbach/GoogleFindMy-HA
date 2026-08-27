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
Extent today, stated so it is not mistaken for coverage: six call sites in four files read it (`api.py` three times,
`coordinator/polling.py`, `coordinator/locate.py`, `location_request.py`), and they serve seven handlers, because the two
sound handlers share the `_classify_nova_auth_error` adapter. In order: the two sound handlers via
`_classify_nova_auth_error`; the device-list handler (`api.py`, `except NovaAuthError` before `NovaProtobufDecodeError`),
which now raises `UpdateFailed` for a non-credential rejection; the location handler, which passes the error on to its
callers instead of returning `{}` -- and which, unlike the device-list handler, also passes on a plain
NON-permanent credential rejection (a 403) rather than converting it to `ConfigEntryAuthFailed`, because
`polling.py` counts consecutive transient auth failures and escalates at its own threshold instead of
prompting on the first one; only a PERMANENT auth failure (or an HTTP 401/403 `NovaHTTPError`) converts
there. Both cases therefore leave that handler as `NovaAuthError`, which is precisely why a caller must ask
the predicate and not the type; `coordinator/polling.py`, whose `except NovaAuthError` branch leaves the
transient-auth counter alone in both directions AND does not record the rejection as the cycle's `last_exception`;
and `coordinator/locate.py`, whose branch does not flip the account-wide auth state. `location_request.py` names the status in its log records instead of calling every 4xx an
authentication error, but still only re-raises -- it does not classify.
Why the location handler raises rather than returning `{}`, since the empty return looks like the gentler option: both
callers treat ANY non-raising return as positive proof that the credentials work. `polling.py` runs "Success path:
ensure any previous auth error is cleared" and `locate.py` runs "Success path: clear any auth error state", and both run
BEFORE the `if not location` guard. An empty return for a rejected device would therefore have RESET the transient-auth
counter and cleared the auth state, which is the opposite of what those branches say. Worse, it would have done so
PERMANENTLY: a 5xx clears up, a device deleted from the account does not, so one deleted tracker in the list would reset
the counter every cycle and a genuinely expired sign-in on another tracker would never reach
`_MAX_TRANSIENT_AUTH_FAILURES`. The user would never see the reauth prompt at all -- a worse outcome than the defect
this change exists to fix. Raising keeps a rejected device out of that reset.
Why the poll branch records the rejection without failing the cycle: in the cycle's `finally` block `cycle_failed`
and `last_exception` drive two different things. `cycle_failed` only writes the `last_poll_result` diagnostic
attribute that `binary_sensor.py` exposes; `last_exception` drives `async_set_update_error`, and
`GoogleFindMyEntity.available` follows the coordinator's `last_update_success`. Recording a per-device client
rejection as `last_exception` therefore marked EVERY tracker entity unavailable, and because a tracker deleted from
the account never recovers the way a 5xx does, the outage repeated on every poll for as long as that tracker stayed
in the cached device list -- trading a spurious re-auth prompt for a permanent availability outage. The branch keeps
`cycle_failed` and leaves `last_exception` to the failures that are actually about the account, the same shape the
`OwnerKeyLookupTransientError` branch below it already uses. A side effect worth naming: while the client branch held
the `last_exception` slot, a rejected tracker polled BEFORE a genuinely expired one made the coordinator report the
harmless 404 and hide the 401. Residual gap, stated so it is not mistaken for covered: if EVERY device is rejected
the cycle now reports no error at all. That window is narrow, because an account-wide rejection already fails
`async_get_basic_device_list` with `UpdateFailed` one layer up, and it is not silent (`last_poll_result` reads
"failed", `note_error` records it, every rejected device gets its own WARNING). Three tests pin this:
`test_a_rejected_device_does_not_make_every_tracker_unavailable`,
`test_a_rejected_device_still_marks_the_poll_result_failed` and
`test_a_rejected_device_does_not_hide_a_later_credential_failure`.
What is still NOT fixed there, stated so it is not mistaken for solved: that success path still treats every empty
result as proof of working credentials, and every 5xx, every 429 and every idle BLE tag reaches it. Deciding what an
empty result may prove about credentials is a behaviour change of its own with a far wider blast radius (every healthy
idle poll takes the same path); it is tracked separately and must not be assumed done. Two tests pin the current state
so it cannot drift silently: `test_an_empty_return_still_clears_the_counter` characterises the reset that stays, and
`test_a_non_credential_4xx_location_is_passed_through` pins the seam that keeps a rejection out of it.
What is NOT fixed, stated so the rule is not mistaken for a solved problem: `nova_request.py` still raises a type named
"auth" for all of them, so a new handler that reads the type repeats the defect, and this paragraph is the only thing
standing in its way. Giving the non-credential case its own exception class is the open follow-up; it needs an
exhaustiveness test over `NovaError.__subclasses__()` first. Measured over the AST: ten `try` blocks catch
`NovaAuthError`. Eight carry a broad `except Exception` in the same block and would swallow a new class under a name that
hides the status; the two sound-request handlers catch it in a tuple with no broad handler, so a new class would
propagate uncaught there instead. Two opposite failure modes in one change. Narrowing a handler is a behaviour change that needs its own regression test, not a
drive-by edit; do not assume a green suite proves the rest of the tree already follows this rule.
Every count in the two paragraphs above (six call sites in four files, ten `try` blocks, eight of them with a broad
handler) is enforced by `tests/test_nova_request.py::TestTheDocumentedExtentStaysTrue`, which derives them from the AST
rather than from grep, following the rule `tests/AGENTS.md` states for shared tuples. Prose that carries a number and
calls itself "the only thing standing in its way" must not be the only copy of that number: change the extent and this
paragraph in the same commit, and let the test tell you when one of them went stale.

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
