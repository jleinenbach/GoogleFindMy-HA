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
Why the poll branch records the rejection without failing the coordinator UPDATE (it does fail the cycle): in the
cycle's `finally` block `cycle_failed` and `last_exception` drive two different things. `cycle_failed` only writes
the `last_poll_result` diagnostic attribute that `binary_sensor.py` exposes; `last_exception` drives
`async_set_update_error`, and `GoogleFindMyEntity.available` follows the coordinator's `last_update_success`.
Recording a per-device client rejection as `last_exception` therefore marked EVERY tracker entity unavailable, and
because a tracker deleted from the account never recovers the way a 5xx does, the outage repeated on every poll for
as long as that tracker stayed in the cached device list -- trading a spurious re-auth prompt for a permanent
availability outage. The branch keeps `cycle_failed` and leaves `last_exception` to the failures that are actually
about the account. It is the ONLY branch in that loop which sets one without the other, and that is deliberate, not
an oversight: every other branch there reports a condition that says something about the account, this one does not.
(The `OwnerKeyLookupTransientError` branch is close in spirit -- also an ordinary per-device skip -- but sets
neither flag, so it is not the same shape.) A side effect worth naming: while the client branch held the
`last_exception` slot, a rejected tracker polled BEFORE a genuinely expired one made the coordinator report the
harmless 404 and hide the 401.
The all-rejected case is handled after the loop, not in the branch: whether a rejection is per-device or
account-wide is only knowable once every device has been tried, the same reasoning by which
`_finalize_cycle_decrypt_state` defers the decrypt verdict. If `cycle_rejected_devices == len(devices)` every device
was refused, which is account-wide on the rejections' own terms, and the cycle surfaces an `UpdateFailed`. Do NOT
replace that check with "the device list would have caught it one layer up": `async_get_basic_device_list` is a
different RPC (`nbe_list_devices`) from the per-device location request, and `DEVICE_LIST_POLL_INTERVAL` (300s,
`const.py`) means most cycles reuse the cached list without calling it at all. That claim was written here once and
was wrong.
Read the check for exactly what it tests, and no more. It does NOT say that a cycle failing the equality had a sibling
success. It is one of TWO post-loop guards, and the other one is what closed the gap this paragraph used to describe as
open. `cycle_unaccepted_devices` counts the devices whose locate request was never accepted, and the sum guard
(`unaccepted + rejected == len(devices)`, with at least one unaccepted) surfaces a cycle in which no device's request
got through. The two are kept apart so the reported message names the condition: a deleted tracker is a configuration
change, a cycle in which no request was accepted is an outage.
That difference is NOT flow position, and writing it that way would be measurably wrong. `location_request.py`
re-raises the non-credential 4xx from the same `try` that produces the 5xx and the 429, all of them BEFORE the
`Location request accepted` line, so neither kind reached the accept point. The difference is what the outcome
says: a rejection carries the SERVER'S answer ABOUT THAT DEVICE -- it was asked, and it answered -- while a
non-accepted request carries no answer FROM THE SERVER about that device. (That is not the forbidden "about the
device versus not about the device" compression below: it is about who spoke, not about whom the failure concerns.)
Say it that way and nothing more.
Three compressions are wrong and all three have been written here before. NOT "the question never arrived": for the
429 and the 5xx it did arrive and the server did answer, only about ITSELF -- one refusing to serve, the other
reporting its own failure -- while `no_fcm_token` and the failed registration never left this integration. What the
seven stages share is the ACCEPT POINT, not the wire: none of them got past `Location request accepted`, which is why
the class is named for acceptance and not for delivery. Writing it as "never reached the server" invites a later
handler to assume pre-dispatch semantics that two of the stages do not have.
NOT "permanent versus transient": a later paragraph measures where that breaks (`no_fcm_token`), and an earlier
revision of THIS sentence asserted the compression while that paragraph already forbade it. NOT "about the device versus not about the device" either: `no_fcm_token` is
non-acceptance and IS device-specific, it is just not the server saying so. What the two share is only that this device
contributed no evidence.
What an empty sibling proves is narrower than it looks, and the guard is built so that it never has to prove much. The
transport failures that used to flatten into `{}` -- the 5xx, the 429, the `aiohttp.ClientError`, the generic Nova
failure -- now raise `LocationRequestNotAcceptedError`. FOUR pre-accept failures still arrive as an empty dict,
because they are raised BEFORE the outer handler that would convert them: an unregistered FCM receiver provider
(`RuntimeError`), a provider that returns `None` (`RuntimeError`), a missing token cache
(`MissingTokenCacheError`), and a failure while binding the lazily imported decrypt / eid-info modules (`ImportError` /
`AttributeError`), whose binding sits ABOVE the outer `try` even though the same import inside the FCM callback is
guarded. Count the FAILURE MODES above that `try`, not the clauses of this sentence and not the `raise` statements
either: an earlier revision grouped the two provider failures into one clause, wrote "three", and then said "all four"
two lines later. Three of the four are an explicit `raise` (the two provider guards and the token cache); the fourth is
implicit, raised by the module bindings themselves. A fourth explicit `raise` does sit above that `try` -- the `Cache
accessors could not be initialized` guard -- and is deliberately NOT one of the four: both branches preceding it
assign the accessors when they are unset, so it is defensive and constructively unreachable. Counting `raise`
statements therefore yields four with the WRONG membership, which is why the unit of the count is named here. This
number is prose only: unlike the extent counts guarded by
`tests/test_nova_request.py::TestTheDocumentedExtentStaysTrue`, no AST test derives it, so it is on whoever adds a
pre-accept failure to come back here and to the enumeration at the post-loop guard in `coordinator/polling.py` that
this paragraph restates. `api.py` flattens all four, and for the first it does so deliberately, as a
documented cold-boot race that retries on the next cycle. An empty dict is therefore WEAK
evidence of acceptance, never proof, and the sum guard is conservative for exactly that reason: an unrecognised failure
makes it stay silent, never fire wrongly.
Do not sharpen the rejection/non-acceptance distinction into "permanent versus transient" either. It holds for the
common cases and breaks on the `no_fcm_token` stage, whose source returns `None` for a canonic id the receiver does not
know -- device-specific and not transient. What the two counts share is only "this device contributed no evidence".
This is a regression against the pre-change behaviour, named here rather than left to be discovered: before the
status-based classification the mixed cycle DID surface, because the 4xx took the transient-auth branch and set
`last_exception` there -- the same branch that fed the counter behind the false sign-in prompt. The signal was a
side effect of the misclassification, not a contract, and it cannot be kept without keeping the defect. Buying it
back by gating the guard on positive success does not work at this layer either, and the reason is mechanical rather
than a forecast about accounts. The only positive marker on the REQUEST path is `_any_device_got_data`; the
neighbouring `cycle_had_successful_decrypt` is a positive proof as well, but about the shared key, and both are False
for either kind of empty result. `_any_device_got_data` records that a device COMMITTED data, not that its request
was accepted. Gating on it turns `test_a_rejected_device_does_not_make_every_tracker_unavailable` red, because
the sibling in that test returns exactly `{}` -- the stricter guard withdraws the very fix that test was written for.
One rejection plus one empty sibling would have to stay silent for that test and surface for the mixed-failure one,
from the same observable state. That is not a judgement call to be argued either way; it is an ambiguity, and only the
layer that produced the empty result can resolve it.
What the earlier wording got right must not be thrown out with its wrong quantifier: an account with one deleted
tracker and otherwise idle BLE tags WOULD go unavailable on every cycle under a stricter gate. That was never true of
"every healthy BLE-only account" -- the guard is conjunctive with a rejection, so an account without one is untouched
-- but it is exactly true of that one shape, and it is the concrete price of tightening here.
Where the resolution belongs was measured before it was built, and the measurement still governs how it may be
changed. `location_request.py` logs `Location request accepted` once the RPC is through, and the `return []` paths
reachable only BEFORE that log (no FCM token, FCM registration failure, 429, 5xx, `aiohttp.ClientError`, the generic
guard around the Nova request) are failures. They now raise `LocationRequestNotAcceptedError` instead. The split is
read off the `request_accepted` flag set AT the log line, NEVER off a position in the file, and that is not a style
preference: the outer surfacing handler wraps the WHOLE body, so its own `return []` sits after the log while the
exceptions it catches come from either side of it. Not every empty result after the log is benign either -- the
unexpected-device branch logs a WARNING and returns empty -- and those were deliberately left alone, because the line
drawn here is `accepted` against `not accepted`, not `succeeded` against `failed`.
Note where the empty dict actually comes from, and note what is NOT in that list. `location_request` catches the 5xx
and the 429 itself, so `api.py`'s handlers for those never run on the locate path. The protobuf decode failure and the
Nova logic error do not belong in the same sentence, measured: WITHIN `location_request.py`, the only call of
`parse_device_update_protobuf` sits in the FCM callback, whose own `except Exception` sets `ctx.data = []` after the
accept line, and no `NovaLogicError` is raised anywhere in the tree. The scope matters and was wrong here once: that
decoder has further callers in `Auth/fcm_receiver_ha.py` and a `__main__` self-check in `decrypt_locations.py`, none
of them on the locate request, so the unscoped claim reads as a tree-wide statement and is false as one. On the
locate path the dict is produced by the no-location fallthrough instead, which is why an intervention confined to
`api.py` would have been inert: the outcome had to be carried ACROSS the `location_request.py` boundary, not
reconstructed downstream once the empty collection had flattened it.
Note what the mixed cycle is NOT: silent everywhere. The rejection still sets `cycle_failed`, so `last_poll_result`
reports `failed` and the diagnostic binary sensor shows it; only entity availability is left alone.
Eight tests pin this: `test_a_rejected_device_does_not_make_every_tracker_unavailable`,
`test_a_rejected_device_still_marks_the_poll_result_failed`,
`test_a_rejected_device_does_not_hide_a_later_credential_failure`,
`test_a_cycle_where_every_device_is_rejected_still_reports_an_error`,
`test_a_cycle_of_only_empty_results_still_reports_success`,
`test_a_mixed_cycle_of_rejection_and_empty_siblings_stays_silent`,
`test_a_cycle_where_no_request_was_accepted_reports_an_error` and
`test_a_mixed_cycle_of_rejection_and_unaccepted_siblings_now_surfaces`.
What is still NOT fixed there, stated so it is not mistaken for solved: that success path still treats every empty
result as proof of working credentials. The 5xx and the 429 no longer reach it -- they raise before they can -- but
every idle BLE tag still does, and so do the four pre-accept failures named above. Deciding what an
empty result may prove about credentials is a behaviour change of its own with a far wider blast radius (every healthy
idle poll takes the same path); it is tracked separately (`PLAN_GFMY_AUTH_RESET_POSITIVE_PROOF`) and must not be assumed done. Three tests pin the
current state so it cannot drift silently: `test_an_empty_return_still_clears_the_counter` characterises the reset that
stays, `test_an_unaccepted_request_no_longer_clears_the_counter` is its contract pair for the requests that no longer
reach it, and `test_a_non_credential_4xx_location_is_passed_through` pins the seam that keeps a rejection out of it.
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
That class has since been given two further duties, because the same shape kept recurring. It derives the guard count
stated in `LocationRequestNotAcceptedError`'s docstring, and it resolves the test names cited in every `AGENTS.md`
under this component against the test tree, class names included. The name half is only PART new, and saying otherwise
would repeat the defect: `TestTheDocumentedRejectionGuardStaysTrue` has always read the "Eight tests pin this:"
sentence above and checked both its number and its names. What was loose until now is the rest -- the second list
("Three tests pin the current state"), one-off citations outside any list, the class names this file leans on, and the
other contracts under this component. A name may therefore not be written into any of them before the test exists.
Citations of a FILE (`tests/foo.py`) stay unchecked by design, so renaming a test module still dangles silently.

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
