# Google FindMy Device (Find Hub) - Home Assistant Integration <img src="https://github.com/BSkando/GoogleFindMy-HA/blob/main/icon.png" width="30">

>[!CAUTION]
> ## **V1.7 Semi-Breaking Change**
>
> After installing this update, you must delete your existing configuration and re-add the integration.  This is due to major architectural changes. Location history should not be affected.

---

A comprehensive Home Assistant custom integration for Google's FindMy Device network, enabling real-time(ish) tracking and control of FindMy devices directly within Home Assistant!

>[!TIP]
>**Check out my companion Lovelace card, designed to work perfectly with this integration!**
>
>**[Google FindMy Card!](https://github.com/BSkando/GoogleFindMy-Card)**

## Come join our Discord for real time help and chat!

[Google FindMy Discord Server](https://discord.gg/U3MkcbGzhc)

---
<img src="https://github.com/BSkando/GoogleFindMy-HA/blob/main/icon.png" width="30"> [![GitHub Repo stars](https://img.shields.io/github/stars/BSkando/GoogleFindMy-HA?style=for-the-badge&logo=github)](https://github.com/BSkando/GoogleFindMy-HA) [![Home Assistant Community Forum](https://img.shields.io/badge/Home%20Assistant-Community%20Forum-blue?style=for-the-badge&logo=home-assistant)](https://community.home-assistant.io/t/google-findmy-find-hub-integration/931136) [![Continuous integration status](https://github.com/BSkando/GoogleFindMy-HA/actions/workflows/ci.yml/badge.svg)](https://github.com/BSkando/GoogleFindMy-HA/actions/workflows/ci.yml) [![Buy me a coffee](https://img.shields.io/badge/Coffee-Addiction!-yellow?style=for-the-badge&logo=buy-me-a-coffee)](https://www.buymeacoffee.com/bskando) <img src="https://github.com/BSkando/GoogleFindMy-HA/blob/main/icon.png" width="30">

>[!TIP]
>**Home Assistant Core 2025.10 or newer is recommended.** The functional minimum is **2025.9.1** (enforced in `hacs.json` and `pyproject.toml`), the empirically determined floor at which all bundled integration dependencies resolve (verified with `script/check_ha_compatibility.py --find-minimum`). The config subentry flow maturity and the `async_added_to_hass` behavior the tracker/service subentries depend on landed earlier, in 2025.8, and the Core-managed config subentry model itself has been available since the 2025.3 cycle. Running 2025.10 or newer is recommended for the bug fixes and stability improvements made since 2025.9.1, not because of a hard API requirement.

### Continuous integration checks

Our GitHub Actions pipeline now validates manifests with hassfest, runs the HACS integration checker, and executes Ruff, Codespell, Bandit, `mypy --strict`, and `pytest -q --cov` on Python 3.13 to protect code quality before merges.

For the quickest way to bootstrap Home Assistant test stubs before running `pytest -q`, see the Environment verification bullets in [AGENTS.md](AGENTS.md#environment-verification).

#### Quickstart checks

- **Clean caches**: Run `make clean` (or the equivalent `find … '__pycache__' -prune` command from [AGENTS.md](AGENTS.md#environment-verification)) after test runs to avoid stale bytecode interfering with CI results.
- **Connectivity probe**: Capture a quick HTTP/HTTPS check (for example, `python -m pip install --dry-run --no-deps pip`) before longer installs so summaries document network status.
- **Home Assistant stubs**: Run `make install-dev` to install Poetry dev/test dependencies (including `homeassistant` and `pytest-homeassistant-custom-component`) before running `pytest -q`.

#### Local verification commands

- `mypy --strict` — run the full strict type-checker locally to mirror CI expectations before opening a pull request.
- `make lint` — invoke `ruff check . --fix` across the entire repository (auto-fixes safe issues). CI runs the same check without `--fix`.
- `make test-unload` — run the focused parent-unload rollback regression (`tests/test_unload_subentry_cleanup.py`) so you can confirm the recovery guardrails without executing the entire suite.
- `make test-ha` — execute the targeted regression smoke tests (`tests/test_entity_recovery_manager.py`, `tests/test_homeassistant_callback_stub_helper.py`) and then run `pytest -q --cov` for the full suite while teeing detailed output to `pytest_output.log`. Append flags such as `--maxfail=1 -k recovery` with `make test-ha PYTEST_ARGS="…"` when you need custom pytest options, or override the coverage summary with `make test-ha PYTEST_COV_FLAGS="--cov-report=term"` for slimmer output.
- `make test-cov` — run `pytest -q --cov` with coverage reporting (output teed to `pytest_output.log`).
- `make test-single TEST=<path>` — run a single test file with optional `PYTEST_ARGS`.
- `make translation-check` — check for missing translation keys across all locale files.
- `make check-ha-compat` — check dependency compatibility with Home Assistant.
- `script/bootstrap_ssot_cached.sh` — stage the Home Assistant Single Source of Truth (SSoT) wheels in `.wheelhouse/ssot` and install them from the local cache. Pass `SKIP_WHEELHOUSE_REFRESH=1` to reuse the cached artifacts on subsequent bootstrap runs or `PYTHON=python3.12` to target an alternate interpreter. The helper also validates `.wheelhouse/ssot` against `script/ssot_wheel_manifest.txt` (override with `SSOT_MANIFEST=…`) so repeated runs can confirm the primary wheels are cached without re-listing the full directory.
- `python script/list_wheelhouse.py` — print a grouped index of cached wheels (optionally against `--manifest script/ssot_wheel_manifest.txt`) before running lengthy installs so you can confirm the cache satisfies the manifest without scrolling through pip logs. Pass `--allow-missing` to preview the formatter when `.wheelhouse/ssot` has not been generated yet.

### Installing Home Assistant test dependencies on demand

The repository uses [Poetry](https://python-poetry.org/) to manage all
development and test dependencies. Run `make install-dev` from the project root
to install `homeassistant`, `pytest-homeassistant-custom-component`, and the
remaining dev/test packages into your Poetry-managed environment. This is the
quickest way to unblock `pytest` after cloning the repository or when a CI run
reports missing Home Assistant packages.

Alternatively, `make test-ha` runs the targeted regression smoke tests followed
by the full `pytest -q --cov` suite. Adjust `PYTEST_ARGS`/`PYTEST_COV_FLAGS` to
narrow the test selection.

#### Wheelhouse cache management

The `script/bootstrap_ssot_cached.sh` helper stages heavy wheels (e.g.
`homeassistant`, `pytest-homeassistant-custom-component`) in `.wheelhouse/ssot`
for offline or cached installs. Delete the directory whenever you need to rebuild
the cache for a clean-room test of updated dependencies, or pass
`SKIP_WHEELHOUSE_REFRESH=1` to reuse the existing cache.

##### Sharing cached wheels between environments

The bootstrap script pulls down heavy wheels into `.wheelhouse/`. Package the
cache once and reuse it on future containers or machines instead of redownloading
hundreds of megabytes every regression run:

```bash
tar -czf wheelhouse-ha-cache.tgz -C .wheelhouse .
```

Copy `wheelhouse-ha-cache.tgz` to the new environment, extract it at the project
root, and the next `script/bootstrap_ssot_cached.sh` invocation will reuse the
cached wheels immediately:

```bash
tar -xzf wheelhouse-ha-cache.tgz -C .
```

When a dependency pin changes, delete the archive (and `.wheelhouse/`) or rerun
`script/bootstrap_ssot_cached.sh` to regenerate the cache before producing a fresh snapshot.

#### Running Home Assistant integration tests locally

1. Install Poetry if not already available: `pip install poetry`
2. Install the full development toolchain (linting, typing, tests): `make install-dev` (or `poetry install --with dev,test`)
   - Minimal options-flow test stack (`homeassistant`, pytest helpers, and `bcrypt` only): `./script/install_options_flow_test_deps.sh`
3. Execute the regression suite, for example: `poetry run pytest tests/test_entity_recovery_manager.py tests/test_homeassistant_callback_stub_helper.py` or simply `make test-ha` (override pytest flags with `make test-ha PYTEST_ARGS="--maxfail=1 -k callback"` as needed)

### Available Make targets

- `make install`: Install Poetry dependencies.
- `make install-dev`: Install Poetry dependencies with dev and test groups.
- `make lint`: Run `ruff check . --fix` across the entire repository (auto-fixes safe issues).
- `make clean`: Remove Python bytecode caches via `script/clean_pycache.py` to keep local environments tidy during development.
- `make clean-node-modules`: Remove the `node_modules/` directory via `script/clean_node_modules.py`.
- `make test-ha`: Run targeted Home Assistant regression smoke tests followed by the full `pytest -q --cov` suite (output teed to `pytest_output.log`).
- `make test-unload`: Execute the targeted unload regression suite (`tests/test_unload_subentry_cleanup.py`) to verify the parent-unload rollback path.
- `make test-cov`: Run `pytest -q --cov` with coverage reporting (output teed to `pytest_output.log`).
- `make test-single TEST=<path>`: Run a single test file with optional `PYTEST_ARGS`.
- `make translation-check`: Check for missing translation keys across all locale files.
- `make check-ha-compat`: Check dependency compatibility with Home Assistant via `script/check_ha_compatibility.py`.
- `make doctoc`: Regenerate the AGENTS.md table of contents (requires Node.js; installs DocToc via `make bootstrap-doctoc`).
- `make bootstrap-doctoc`: Install the DocToc npm dev dependency into the local cache.

---
## Features

- 🗺️ **Real-time Device Tracking**: Track Google FindMy devices with location data, sourced from the FindMy network
- ⏱️ **Configurable Polling**: Flexible polling intervals with rate limit protection
- 🔔 **Sound Button Entity**: Devices include button entity that plays a sound on supported devices
- ✅ **Attribute grading system**: Best location data is selected automatically based on recency, accuracy, and source of data
- 📍 **Historical Map-View**: Each tracker has a filterable Map-View that shows tracker movement with location data, localized into every shipped UI language
- 📋 **Statistic Entity**: Detailed statistics for monitoring integration performance
- #️⃣ **Multi-Account Support**: Add multiple Find Hub Google accounts that show up separately
- ❣️ **More to come!**

The manifest classifies Google Find My Device as a **hub** integration. Home Assistant treats the integration as a central coordinator that manages multiple connected devices, aligning documentation and compliance checks with the restored 1.7.0-3 metadata.

>[!NOTE]
>**This is a true integration! No docker containers, external systems, or scripts required (other than for initial authentication)!**
>
## Installation

### HACS (Recommended)
1. Click the button below to add this custom repository to HACS\
[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?category=integration&repository=GoogleFindMy-HA&owner=BSkando)
2. Install "Google Find My Device" from HACS
3. Restart Home Assistant
4. Add the integration through the UI

### Manual Installation
1. Download this repository
2. Copy the `googlefindmy` folder to `custom_components/`
3. Restart Home Assistant
4. Add the integration through the UI

## First-Time Setup

>[!IMPORTANT]
>**Authentication is a 2-part process.  One part requires use of a python script to obtain a secrets.json file, which will contain all necessary keys for authentication!  This is currently the *ONLY* way to authenticate to the FindMy network.**

### <ins>Authentication Part 1 (generate `secrets.json`)</ins>

Generate the `secrets.json` bundle with **this repository's own login tooling**. It runs the integration's up-to-date fork code, so the bundle is exactly what Home Assistant consumes, and it retrieves **both** required keys (including the `shared_key`) automatically in a single run — no manual device selection, and none of the `keys_missing` pitfalls of the external script below.

> [!IMPORTANT]
> Whichever option you pick, run the login from the **same public IP address / network** that your Home Assistant instance uses, and sign in with the **same Google account** that owns the trackers. Google ties the end-to-end encryption keys in `secrets.json` to the account and may revoke them when requests arrive from a different IP or region. A mismatch produces a bundle that lists your devices and can ring them, but cannot decrypt any location reports — see [Devices appear but no location updates](#authentication-expires-repeatedly).

**Option A — Docker login helper (recommended).** Run the bundled one-command wrapper on your Docker host and complete the Google login through a browser tab: [`custom_components/googlefindmy/docker-login/`](custom_components/googlefindmy/docker-login/README.md). It runs Chrome **inside the container** at a controlled version, so you are not at the mercy of whatever Chrome your desktop auto-updates to — the exact failure that currently breaks the external browser flow ([BSkando#207](https://github.com/BSkando/GoogleFindMy-HA/issues/207)). No local Python or Chrome is required, and it works on ARM Linux too.

**Option B — Bundled CLI (`main.py`).** Copy the *contents* of `custom_components/googlefindmy/` into a fresh, empty directory (so that `main.py`, `Auth/`, `NovaApi/`, etc. sit directly at its top level) and run `python main.py` from there. The flat layout is the supported way to run it: the script resolves `Auth/secrets.json` relative to its own directory, so a run in place at `custom_components/googlefindmy/main.py` writes into your Home Assistant configuration directory instead of a scratch folder. It does **not** behave differently otherwise: `main.py` runs the token bootstrap in either layout, so an in-place run without stored credentials opens the Chrome login just as a flat run does. (An earlier version of this paragraph promised the opposite; the directory-dependent branch it described no longer exists.) Note that "without credentials" means the file has no username or no token at all: a *stale* token counts as present, and the run then stops later with an instruction to repeat it as `python main.py --reauth`. The browser packages this needs are `selenium` and `undetected-chromedriver`; install them where you run the CLI (`pip install selenium undetected-chromedriver`) — the scripts name the command themselves if they are missing. Run it from an **interactive terminal**: the desktop login opens Chrome on your own screen and asks you to confirm first, so it refuses to start when nobody can answer (see [Standalone login refuses to start](#standalone-login-refuses-to-start-attended-terminal-required)). If Chrome startup aborts with `only supports Chrome version …`, pin the version as described under [Chrome/ChromeDriver version mismatch](#chromechromedriver-version-mismatch-standalone-auth-scripts).

When either option finishes, copy the entire contents of the generated `secrets.json` (open it in a text editor, select all, copy) for Part 2.

<details>
<summary><b>Fallback: the external GoogleFindMyTools script</b></summary>

If you prefer the external tool, navigate to [GoogleFindMyTools](https://github.com/leonboe1/GoogleFindMyTools?tab=readme-ov-file#how-to-use) and follow the "How to use" directions for the `main.py` script. Two caveats make the options above the more robust choice:

> [!WARNING]
> You **MUST** go through **2 login processes**. After the first login your available devices are listed; you must then **select a device to locate**, which triggers a **second** login and retrieves the `shared_key`. If you stop after the device list appears, the resulting `secrets.json` has no `shared_key` and Home Assistant rejects the import with a `keys_missing` error, because locations cannot be decrypted without it.

> [!NOTE]
> The external browser login can abort with a Chrome/ChromeDriver version mismatch ([BSkando#207](https://github.com/BSkando/GoogleFindMy-HA/issues/207)). If you follow all of Leon's steps and still cannot get through the `main.py` sequence, try [BSkando/GoogleFindMyTools](https://github.com/BSkando/GoogleFindMyTools), or use Option A above, which sidesteps the desktop-Chrome dependency entirely.

</details>

### <ins>Authentication Part 2 (Home Assistant Steps)</ins>
1. Add the integration to your Home Assistant install.
2. In Home Assistant, paste the copied text from secrets.json when prompted.
3. After completing authentication and adding devices, RESTART Home Assistant!

### Automatic discovery & credential updates

- **Secrets watcher (automatic pickup):** Home Assistant watches for a fresh `secrets.json` and, when one appears, opens the config flow with the email and tokens pre-filled, so you can confirm the entry without pasting anything manually. Out of the box it watches both the integration's `Auth/secrets.json` and the login container's `docker-login/data/secrets.json`, so the container hand-off needs no configuration at all; the integration options only add further paths for layouts that differ from these defaults. Only **one** file is ever written: the watcher observes one or more paths, it never keeps a second copy. If several watched files happen to exist at once, the newest one wins (by modification time, with a content-hash tiebreak). After a successful import Home Assistant deletes the imported bundle and any watched copy that belongs to the **same** Google account — mirroring the existing `Auth/` cleanup — so no redundant secret lingers on disk. The cleanup is also content-aware: if the login container wrote **fresher** credentials of that same account while you were still confirming the flow, only the copies carrying the imported content are removed and the newer bundle is kept, so the watcher picks it up on its next scan instead of losing it. A watched file for a **different** account is likewise kept and logged rather than being silently discarded. An aborted or failed flow deletes nothing, so you can simply retry.
- **Update flows for existing entries:** When the watcher detects refreshed credentials for an account that is already configured, the integration pushes a `discovery_update` flow. Accepting it reauthenticates the existing entry and keeps all devices and options intact.
- **New trackers need no flow at all:** A tracker that shows up in your Google account later is added as an entity on its own, without a discovery card, a dialog or a click. The device list is refreshed on its own schedule, so a new tracker appears within a few minutes; reloading the integration makes it immediate. Discovery is reserved for what it is meant for: a new account, and refreshed credentials for an account you already have.
- **Duplicate suppression:** Whatever opens a discovery flow, duplicates are suppressed using Home Assistant's `DiscoveryKey` mechanism, so the same bundle never queues two cards.

### Multi-account behavior and duplicate protection

- Home Assistant supports connecting multiple Google accounts, but **only one config entry per email address stays active**. When duplicate entries share the same Google account, the integration automatically disables and unloads the non-authoritative entries to prevent device duplication and token conflicts.
- The disabled entries remain visible in **Settings → Devices & Services** with an integration-managed disabled state so you can review or remove them manually. Reactivating a disabled duplicate requires removing the authoritative entry first or supplying credentials for a different Google account.
- The login container publishes exactly one port: the noVNC viewer (7900), opened by **your** browser. Where it binds and what you are told to open are configured separately (`GFMY_NOVNC_BIND` / `GFMY_NOVNC_URL_HOST`, or simply `bash login.sh --ip <address>`). Home Assistant cannot guess which address your browser can reach, so the launcher prints the URL to open rather than the integration. Details and defaults: [`custom_components/googlefindmy/docker-login/README.md`](custom_components/googlefindmy/docker-login/README.md#the-two-address-roles).
- If two `secrets.json` files for **different** accounts are ever present in the watched paths at the same time, only the newer file is imported; the older account's file is **kept** (not deleted) and logged, so it is discovered and offered on the next scan rather than being silently discarded. Write one bundle at a time; the login container always writes a single complete bundle, so this only matters if you place files manually.

### Interoperability and third-party linking

Third-party consumers should anchor on the `google_device_id` state attribute when associating Find My trackers with external data sources (for example, Bermuda BLE Trilogy listeners). MAC addresses rotate for privacy and are intentionally omitted from state; `google_device_id` is the stable, registry-aligned identifier that will not change across reboots. See [docs/Ephemeral_Identifier_Resolver_API.md](docs/Ephemeral_Identifier_Resolver_API.md#stable-device-identifier-state-api) for usage guidance and templating examples.

### Local BLE presence via Bermuda

The integration ships a bidirectional bridge to the [jleinenbach/bermuda](https://github.com/jleinenbach/bermuda) Bermuda BLE Trilateration fork. Two capabilities are available:

- **EID Resolver API (always on).** Bermuda detects FMDN advertisements from your trackers locally and asks GoogleFindMy to map the ephemeral identifier to your `google_device_id`. The two integrations then share one Home Assistant device while keeping their own `device_tracker` entities, so live coordinates from your own BLE scanners and cloud coordinates from the Find Hub network coexist on the same tag.
- **FMDN Finder uploads (experimental, currently blocked).** When enabled via the `FEATURE_FMDN_FINDER_ENABLED` feature flag in `custom_components/googlefindmy/const.py`, the integration wires up a Bermuda listener that prepares an end-to-end encrypted Finder report for every stable area change. The actual upload to Google's Find Hub network is hard-disabled in `custom_components/googlefindmy/fmdn_finder/google_uploader.py` (`FMDN_UPLOAD_ENABLED = False`) because the endpoint requires DroidGuard attestation that Home Assistant cannot produce. The flag is therefore a developer-facing opt-in for the listener pipeline only. See [docs/BERMUDA_INTEGRATION.md](docs/BERMUDA_INTEGRATION.md) and [docs/FMDN_UPLOAD_LIMITATION.md](docs/FMDN_UPLOAD_LIMITATION.md) for details.

Setup steps, troubleshooting, the device-matching contract (congealment via HA `device_id`, never via MAC or entity name), and the FMDN throttling rules are documented in [docs/BERMUDA_INTEGRATION.md](docs/BERMUDA_INTEGRATION.md).

## Configuration Options

Accessible via the ⚙️ cogwheel button on the main Google Find My Device Integration page.

> [!TIP]
> The options table is a mirror of `OPTION_KEYS` and `DEFAULT_*` in `custom_components/googlefindmy/const.py`, which is the single source of truth for option order and defaults.

| **Option** | **Default** | **Units** | **Description** |
| :---: | :---: | :---: | --- |
| `ignored_devices` | none | - | Devices removed from tracking. Ignoring a device deletes it and its entities from the registries, so restoring it through **Manage ignored devices** reloads the integration to rebuild them. |
| `location_poll_interval` | 300 | seconds | How often the integration runs a poll cycle for all devices. |
| `device_poll_delay` | 5 | seconds | How much time to wait between polling devices during a poll cycle. |
| `min_poll_interval` | 60 | seconds | Hard lower bound between poll cycles and the manual locate cooldown. |
| `allow_history_fallback` | false | toggle | Falls back to Recorder history when no live device tracker state is available. |
| `enable_stats_entities` | true | toggle | Exposes the "Google Find My Integration" statistics entity (polling status, counters, etc.). |
| `google_home_filter_enabled` | true | toggle | Enables or disables Google Home device location filtering. |
| `google_home_filter_keywords` | nest,google,home,mini,hub,display,chromecast,speaker | text input | Comma-separated keywords used to filter out location data from Google Home devices. |
| `map_view_token_expiration` | false | toggle | Enables expiration of generated API tokens used in Map View history queries. |
| `semantic_locations` | none | - | User-defined semantic location zones (managed via a dedicated options flow step). |
| `delete_caches_on_remove` | true | toggle | Removes stored authentication caches when the integration is deleted. |
| `contributor_mode` | in_all_areas | selection | Chooses whether Google shares aggregated network-only data (`high_traffic`) or participates in full crowdsourced reporting (`in_all_areas`). |
| `stale_threshold` | 3900 | seconds | After this many seconds (default: 65 minutes) without a location update, the tracker state becomes `unknown`. Use the "Last Location" entity to always see the last known position. |
| `show_location_age` | true | toggle | Adds a `location_age` attribute (in seconds, rounded to 60s) to each tracker entity. Excluded from Recorder history to keep DB size predictable. |

### Map View link expiry (`map_view_token_expiration`)

With this option **off** (the default), the Map View link on a device page is
stable and keeps working indefinitely.

With it **on**, the token rotates weekly. The map accepts the current and the
previous bucket, but the link stored on the device page is only rebuilt when the
integration starts up. The stored link therefore dies the moment the **second**
weekly boundary is crossed — if the instance started shortly before a boundary,
that can be little more than a week later — and the device page's own Map View
link then returns "Unauthorized".

You do not have to restart to fix that. Call the service
**`googlefindmy.refresh_device_urls`** (Developer tools → Actions → *Refresh
Device URLs*); it rewrites the configuration URL of every device with a current
token, and the link works again immediately.

One prerequisite: Home Assistant must have a reachable base URL. If none is
configured, the service logs a warning and updates nothing, so the stale link
stays. If the action appears to do nothing, set an internal or external URL
under Settings → System → Network and check the log.

### Google Home filter behavior

The Google Home filter helps prevent noisy location updates from speakers and displays that frequently report "Home":

* **Defaults**: The filter starts enabled with keywords `nest`, `google`, `home`, `mini`, `hub`, `display`, `chromecast`, and `speaker`.
* **Detection**: Any detection whose semantic name contains one of these keywords is treated as a Google/Nest/Chromecast device.
* **Substitution**: When a Google Home detection is away from Home, the integration substitutes the `zone.home` latitude/longitude (and radius when available) so Home Assistant resolves the tracker to `home` instead of the semantic label.
* **Debounce window**: Consecutive "home" or Google Home detections for the same device within 15 minutes are suppressed to reduce spam.
* **Tuning**: Adjust `google_home_filter_enabled` and `google_home_filter_keywords` from the integration's options flow to refine matching or disable substitution. The keywords field accepts comma-separated values or a list; changes update both the detection logic and the config flow copy.

## Subentries and feature groups

Home Assistant's config-entry **subentries** let the integration organize devices and helper entities into feature groups. The coordinator deterministically provisions two subentries—`SERVICE_SUBENTRY_KEY` and `TRACKER_SUBENTRY_KEY`—and recreates them after reloads or restarts so entity grouping stays stable across updates. Both subentries persist alongside the config entry, storing options, `visible_device_ids`, and diagnostics based on their constant identifiers.

Home Assistant 2025.11+ handles subentry platform scheduling automatically. The parent `async_setup_entry` forwards the platform list **once** (no `config_subentry_id` allowed). Each platform then iterates the subentry coordinators on `entry.runtime_data` and calls `async_add_entities(..., config_subentry_id=<subentry_id>)` so devices and entities attach to the correct child entry. This pattern prevents orphaned tracker devices and avoids the silent failure caused by manual per-subentry forwarding.

- **Parent–child enforcement:** Each child is a `ConfigEntry` whose `parent_entry_id` links it to the owning parent. Device Registry entries attach to the parent or a specific child—never both—and subentry `unique_id` values only need to be unique within the parent scope.
- **Lifecycle guardrail:** Leave `async_setup(hass, config)` for domain-level helpers only. Instance work lives in `async_setup_entry`, which receives the populated entry and iterates `entry.subentries` so the parent and every child load without triggering `homeassistant.config_entries.UnknownEntry` during startup or reloads.

### Service hub subentry

The service hub subentry, identified by `SERVICE_SUBENTRY_KEY`, represents the account-level hub device for the integration.

- Home Assistant localizes the hub device name in the UI using `SERVICE_DEVICE_TRANSLATION_KEY` instead of a hard-coded string, so translations stay synchronized with the codebase.
- The hub publishes only integration-scope diagnostics (polling status, authentication health, statistics counters) and intentionally surfaces **zero tracker devices** via `visible_device_ids`. It is the logical parent for trackers, not a list of them.
- All diagnostic entities exposed here point to a shared service device in Home Assistant's device registry. Each entity still exports a stable unique ID and provides `DeviceInfo`, which Home Assistant uses to group the diagnostics under the service hub in the UI.[1]
- This shared hub device is what users see as the central integration device in the UI, reflecting Home Assistant's hub-style integration guidance.[1]

### Tracker subentry

The tracker subentry, keyed by `TRACKER_SUBENTRY_KEY`, represents the phones, tablets, and tags imported from Google Find My Device.

- Each tracker entry backs per-device entities such as `device_tracker`, “last seen” timestamp sensors, and control buttons for actions like ring / play sound / locate.
- Trackers register as individual device entries in the Home Assistant device registry with their own unique IDs and `DeviceInfo`. They remain standalone devices—Home Assistant automatically associates them with the correct config-entry subentry without manual `via_device` or `via_device_id` pointers.[1]
- Trackers never appear in the service hub’s `visible_device_ids` list and are never assigned to the service hub subentry; they stay within the tracker subentry so repairs and options target the correct devices.

### Subentry flow abort reasons

Config flows communicate state transitions through **abort reasons**, which power the toast notifications and translation strings surfaced in Home Assistant dialogs. Subentry-related flows use the following reason keys:

| Reason key | Where it appears | Meaning |
| --- | --- | --- |
| `invalid_subentry` | Reconfigure handlers, options steps, and repairs forms | The requested feature group could not be resolved or was removed during the flow. |
| `repairs_no_subentries` | Repairs entry point and move action | No feature groups exist, so the repairs workflow cannot continue. |
| `repair_no_devices` | Repairs → Move devices | A move operation was attempted without selecting any devices. |
| `subentry_move_success` | Repairs → Move devices | The selected devices were re-assigned successfully; the flow exits with a success toast. |
| `subentry_delete_invalid` | Repairs → Delete subentry | There are too few removable feature groups to continue. |
| `subentry_remove_failed` | Repairs → Delete subentry | Removing the requested feature group failed unexpectedly. |
| `subentry_delete_success` | Repairs → Delete subentry | A feature group was deleted (after optional device reassignment). |
| `reconfigure_successful` | Credentials refresh flow | The integration applied new credentials and refreshed the chosen feature group. |
| `credentials_saved_not_reloaded` | Credentials refresh flow | The new credentials were stored, but the entry could not be reloaded (it is disabled, ignored, or in a state a reload cannot come back from). They take effect the next time it is set up successfully. |

The `strings.json` and translation files under `custom_components/googlefindmy/translations/` provide localized messages for each key so UI notifications remain consistent.

## Services (Actions)

The integration provides a couple of Home Assistant Actions for use with automations.  Note that Device ID is different than Entity ID.  Device ID is a long, alpha-numeric value that can be obtained from the Device info pages.

| Action | Attribute | Description |
| :---: | :---: | --- |
| googlefindmy.locate_device | Device ID (required) | Request fresh location data for a specific device. |
| googlefindmy.play_sound | Device ID (required) | Play a sound on a specific device for location assistance.  Devices must be capable of playing a sound.  Most devices should be compatible. |
| googlefindmy.stop_sound | Device ID (required), Request UUID (optional) | Stop the active sound on the selected device. Google matches a stop against the cancel key of the play request it belongs to. If this Home Assistant instance does not hold that key, because the ring was started from a phone or another instance, the stop is still submitted but the action reports that it could not be correlated and the device may keep ringing. The same report appears when the key is older than 30 minutes: it is still sent, because Google queues the command until the tracker is reachable and it may well still fit, but nothing proves it does. A key in that state is kept rather than discarded, so every further stop for that device keeps reporting "not correlated" until a new Play Sound or a restart replaces it. That report is an action error, so a script or automation calling it stops at this step unless you wrap it in `continue_on_error: true`. |
| googlefindmy.locate_external | Device ID (required), Device Name (optional) | Trigger the locate flow via the external helper while optionally labeling logs with a human-readable device name. |
| googlefindmy.refresh_device_urls | - | Refreshes all device Map View URLs.  Useful if you are having problems with accessing Map View pages. |
| googlefindmy.rebuild_device_registry | - | Maintenance: rebuilds device registry links for Google Find My hubs and removes tracker devices incorrectly tied to the parent entry. |
| googlefindmy.rebuild_registry | Config Entry ID(s) (optional) | Reload integration config entries; without a payload the first configured entry reloads, or target specific IDs by passing one or many `entry_id` values. |

## Supported devices and functions

- **Device coverage:** Phones, tablets, Wear OS devices, earbuds, and compatible Bluetooth trackers surfaced in the Google Find My Device network.  Any device that appears in the official Google Find My interface is eligible to be imported.
- **Entities created:** Each tracked device exposes a `device_tracker` entity for live location, a binary sensor for connection state, a "last seen" timestamp sensor, and a Plus Code (Open Location Code) sensor. Optional helper entities (statistics, sound trigger button) are added depending on options and device capabilities, and a BLE battery sensor is added when the device is matched to a local Bermuda BLE tracker that reports battery.
- **Action support:** Sound playback is available on hardware that exposes the native "Play sound" action within Google's ecosystem.  The integration hides the button on devices that do not advertise support, aligning with [Home Assistant action documentation](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/docs-supported-functions/).

## Data updates and background behavior

- **Snapshot merge semantics:** Push updates from the FCM listener are merged into the coordinator snapshot rather than replacing it. Devices that were not part of a push event keep their previous metadata, which prevents transient gaps in the device tracker registry between full poll cycles. The subentry-device index is rebuilt from the merged snapshot.
- **Coordinator-driven updates:** Location and metadata are refreshed through Home Assistant's [`DataUpdateCoordinator`](https://developers.home-assistant.io/docs/integration_fetching_data/) with a default 300-second polling interval.  Staggered per-device delays keep API calls within Google's rate limits.
- **Manual refresh:** Call the `googlefindmy.locate_device` action to request fresh data outside the scheduled polling cycle.  The integration debounces requests to avoid repeated queries that would exceed the appropriate polling guidance.
- **Repair flows:** When authentication expires or Google invalidates API tokens, the integration raises a [Home Assistant repair issue](https://developers.home-assistant.io/docs/core/platform/repairs/) that guides you through reauthentication without removing the config entry.

### Optional: faster legacy-tracker EID computation (advanced)

Only **older** FMDN trackers exercise a pure-Python elliptic-curve path
(SECP160r1) when computing rotating EIDs. Modern trackers use P-256, which is
already C-backed (`cryptography`), so they are unaffected. For the legacy path,
`python-ecdsa` automatically uses `gmpy2` (preferred) or `gmpy` for its modular
arithmetic **if either is importable**, with no configuration. If neither is
present, it falls back to pure Python.

This is purely optional and the effect is small. After the polling/refresh
optimizations in #1140 the legacy path runs rarely, and the measured speedup is
modest: roughly **~1.15x on x86_64** for this small curve (not the "~3x" the
library advertises for large operands). On weaker ARM CPUs it may be somewhat
higher, but this is not quantified.

You can verify which backend would be used (and which accelerator versions are
installed) from the integration's diagnostics download (`crypto.ecdsa_acceleration`
and `crypto.gmpy2_version`) or from a one-time DEBUG log line at startup. Note
that this reports *import availability and installed version*, not guaranteed
runtime acceleration: a broken `gmpy2` install would still appear installed
while `python-ecdsa` silently falls back to pure Python.

| Platform | gmpy2 wheel? | Effect on legacy path |
|---|---|---|
| x86_64 / aarch64 (glibc) | yes | small speedup (~1.15x x86_64), used automatically |
| aarch64 (musl) | yes (>= 2.3.1) | small speedup, used automatically |
| armv6 / armv7 (32-bit) | no | not installable, no effect |

On **32-bit ARM (armv6/armv7)** there is no `gmpy2` wheel and a source build
fails, so it cannot be used there. Because the effect is already small after the
#1140 optimizations and only touches legacy trackers, installing `gmpy2` is a
minor, optional tweak rather than a recommended step.

## Map View localization

The Map View is a self-rendered HTML page served by the integration, so it does
**not** receive Home Assistant's frontend translations (those cover only entity,
config and service strings). Its labels live in a dedicated catalog,
[`custom_components/googlefindmy/map_i18n.py`](custom_components/googlefindmy/map_i18n.py),
and are resolved server-side from `hass.config.language` with an English
fallback. `Plus Code` stays untranslated on purpose (it is Google's brand name).

To add or adjust a language, edit the `MAP_LABELS` dict in that module: add a
locale entry with the **same key set** as `en` (a unit test enforces that every
locale carries every key with a non-empty value). This catalog is intentionally
separate from `strings.json` / `translations/`, so `make translation-check`,
`translation_key_check.py` and `translation_placeholder_check.py` are unaffected
by map-label changes and there is no hassfest schema risk.

## Known limitations

- **Historical data availability:** Map View history is generated locally and depends on the Recorder integration retaining statistics; pruning recorder data will remove historical traces.
- **Offline devices:** Google only reports the last known location for powered-off or offline hardware.  Devices may appear as `unavailable` until they reconnect to the Find My network.
- **Authentication tooling:** Generating `Auth/secrets.json` requires a one-time browser login, produced either by this repository's own tooling (the Docker login helper or the bundled `main.py`, see [Authentication Part 1](#authentication-part-1-generate-secretsjson)) or by the external GoogleFindMyTools script.  Future changes to Google's login flow may require updated tooling before the integration can connect again.
- **Multiple households:** Home Assistant imports all trackers from the authenticated Google account.  Fine-grained sharing to limit visibility per household member is not yet available and should be handled via entity permissions.

## Uninstallation / Removal

1. Disable or delete related automations, dashboards, and notification flows that reference `googlefindmy` entities to prevent "entity not found" errors after removal.
2. Open **Settings → Devices & Services → Integrations → Google Find My Device**.
3. Use the **⋮ menu → Delete** action to remove the config entry.  Home Assistant will unload entities and purge the stored token cache.
4. If you installed through HACS, remove the integration from HACS to stop future updates.  For manual installs, delete `custom_components/googlefindmy/` from your Home Assistant configuration directory.
5. Restart Home Assistant to clear any cached services.  If you encounter lingering repairs, resolve them through the [Home Assistant Repairs dashboard](https://www.home-assistant.io/integrations/repairs/).

## Concrete use cases

- Trigger a sound alert on misplaced earbuds via the `googlefindmy.play_sound` action when a BLE beacon indicates they are nearby.
- Build an automation that notifies you when a tracker enters or leaves a geofenced zone based on the `device_tracker` entity state.
- Monitor integration health by surfacing the statistics entity in dashboards to verify polling intervals and API latency.
- Combine the Map View history with [companion dashboards](https://github.com/BSkando/GoogleFindMy-Card) to visualize multi-day movement patterns for shared family devices.

## Troubleshooting

### No Location Data
- Check if devices have moved recently (Find My devices may not update GPS when stationary)
- Check battery levels (low battery may disable GPS reporting)

### Chrome/ChromeDriver version mismatch (standalone auth scripts)
When you run the standalone helper scripts (`get_oauth_token.py`,
`Auth/auth_flow.py`, `KeyBackup/shared_key_flow.py`) from the command line,
`undetected_chromedriver` downloads a driver for your installed Chrome version.
If the Chrome-for-Testing **stable** channel has moved ahead of the Chrome build
offered to your desktop (for example the driver targets Chrome 150 while only
149 is installed), startup can abort with `only supports Chrome version 150`.

The integration auto-detects the installed version and passes it through, so
this usually resolves itself. If detection fails, or you need to pin a specific
version, use the layered override (priority: **CLI flag > environment variable >
auto-detection**):

| Override | CLI flag | Environment variable |
| --- | --- | --- |
| Chrome binary path | `--chrome-path /path/to/chrome` | `GOOGLEFINDMY_CHROME_PATH` |
| Chrome major version | `--chrome-version 149` | `GOOGLEFINDMY_CHROME_VERSION` |

```bash
# Pin the major version on the command line
python custom_components/googlefindmy/get_oauth_token.py --chrome-version 149

# Or via environment variables. These also cover the Home Assistant runtime
# path, which has no command line of its own.
export GOOGLEFINDMY_CHROME_VERSION=149
export GOOGLEFINDMY_CHROME_PATH=/usr/bin/google-chrome
```

Run any of the scripts with `--help` to list the available options.

### The standalone login closes your other Chrome windows

**Before it starts its own browser, the standalone login terminates the Chrome
processes it finds running.** This is deliberate, not a bug: the login has to
drive a browser session it controls end to end, and an already running Chrome
would otherwise capture the sign-in and keep the credentials out of reach.

What that means in practice:

- **Close your Chrome windows before you run any of the helper scripts**
  (`get_oauth_token.py`, `Auth/auth_flow.py`, `KeyBackup/shared_key_flow.py`,
  or `main.py`). Unsaved tabs are lost as with any forced quit.
- **Do not start a login while other automation is using Chrome** on the same
  machine and user account (scraping jobs, kiosk displays, printing services).
- **The match is on the whole command line, not on the program name.** The
  cleanup uses `pgrep -f chrome`, so anything whose command line contains
  `chrome` is terminated too — a monitoring script called
  `chrome_metrics.py`, for example. If you run such a process, stop it or rename
  it before a login run.
- The scripts protect their own process and its parents, so running them from a
  terminal or a test runner does not terminate that terminal.
- Inside the provided login container the cleanup is skipped, because there it
  would tear down the container's own browser stack.
- **In Home Assistant this does not happen — with one exception worth knowing.**
  No module Home Assistant loads reaches `create_driver`. The exception is the
  interactive key-backup fallback: it is loaded dynamically and, in a Home
  Assistant process started in the foreground of a terminal whose bundle carries
  no shared key, it could reach the same code. That guard is being tightened to
  require the command-line tool itself.

### Standalone login refuses to start (attended terminal required)
The desktop login opens Chrome **on your own screen** and prints a "Press Enter
to continue" prompt first, so you decide when a browser window takes over. When
standard input is not a terminal there is nobody to decide, and the flow aborts
before Chrome starts:

```
RuntimeError: [AuthFlow] The interactive Chrome login needs an attended terminal
(stdin is not a terminal).
```

This is deliberate. An unattended run would open a browser nobody is watching,
and reading the prompt from a pipe would swallow the account e-mail that the CLI
asks for on the same standard input a moment later. Pick the option that matches
your situation:

| Situation | What to do |
| --- | --- |
| Normal shell / SSH session | Nothing — this is the supported path. |
| IDE run window (PyCharm, VS Code) that proxies stdin | `export GOOGLEFINDMY_ASSUME_INTERACTIVE=1` for that run. |
| No graphical desktop, or you prefer a browser tab | Use the [Docker login helper](custom_components/googlefindmy/docker-login/README.md); its entrypoint sets `GOOGLEFINDMY_CONTAINER_LOGIN=1` itself, and the prompt does not apply there because Chrome runs inside the container. |
| Automated caller (no browser window wanted) | Call the flow with `headless=True`. |

> [!WARNING]
> `GOOGLEFINDMY_ASSUME_INTERACTIVE=1` only claims "a human is sitting here"; it
> does not make an unattended run work. Set it per invocation, not permanently
> in a container, service unit or shell profile — that would restore exactly the
> unattended browser start this check prevents.

### Location updates stopped after an upgrade
Location data is fetched **outbound** from Home Assistant to Google (FCM push plus Nova/SPOT polling); it does **not** depend on your Home Assistant internal or external URL configuration. If updates stop after upgrading the integration:
1. **Reload the integration** (Settings → Devices & Services → Google Find My Device → ⋮ → Reload) or restart Home Assistant. This re-establishes the FCM connection and refreshes tokens, which resolves most post-upgrade stalls.
2. If updates are still missing, review the logs for authentication errors and re-authenticate if prompted (see [Authentication Expires Repeatedly](#authentication-expires-repeatedly)).

> **Note on the internal/external URL:** Your Home Assistant internal/external URL only affects the clickable **Map View links** on each device page, not location reception. Changing it can appear to "fix" updates because it triggers a reload or restart, but it is the restart that restores tracking, not the URL value. A configuration such as internal `http://ip-address:8123` plus external `https://your-domain:8123` is perfectly valid.

### FCM Connection Problems
- Extended timeout allows up to 60 seconds for device response
- Check firewall settings for Firebase Cloud Messaging
- Review FCM debug logs for connection details
- Ensure port 5228 is forwarded if you run this behind reverse-proxy, inside KVM or any other virtual environment not directly exposed.

### Authentication Expires Repeatedly
- Google may revoke tokens when API requests originate from a different IP address or geographic region than where the token was originally created.
- **Common scenario:** `secrets.json` generated on a laptop at home, but Home Assistant runs on a cloud VPS or a server in another country.
- **Fix:** Run the authentication script on the same network (same public IP) where your Home Assistant instance is located, then re-import the credentials.

### Rate Limiting
The integration respects Google's rate limits by:
- Sequential device polling (one device at a time)
- Configurable delays between requests
- Minimum poll interval enforcement

### "Invalid handler specified" when adding the integration
- Home Assistant shows this error when the config flow fails to register. Double-check that `custom_components/googlefindmy/manifest.json` sets `"domain": "googlefindmy"` and `"config_flow": true`.
- Inspect `custom_components/googlefindmy/config_flow.py` to ensure the `ConfigFlow` class inherits from `config_entries.ConfigFlow` and declares the domain via `class ConfigFlow(..., domain=DOMAIN)` (or `domain = DOMAIN`).
- Enable targeted debug logging while reproducing the issue to confirm the handler lifecycle:
  ```yaml
  logger:
    default: info
    logs:
      homeassistant.config_entries: debug
      homeassistant.data_entry_flow: debug
      homeassistant.loader: debug
      homeassistant.setup: debug
      custom_components.googlefindmy: debug
  ```
  You can apply the same levels temporarily via **Settings → System → Logs → Configure** or by calling the `logger.set_level` service.
- Review the Home Assistant logs for the integration's import-time entry (`ConfigFlow import OK; class=ConfigFlow, class.domain=googlefindmy, const.DOMAIN=googlefindmy, class_id=...`) followed by the registry verification messages to ensure the handler is present in `HANDLERS`.
- Run `pytest tests/test_config_flow_basic.py -q` to exercise the smoke tests that validate the handler registration and user-step initialization before retrying the flow.
- Automatic retry with exponential backoff

### Running pip-audit behind TLS inspection

Corporate proxies that intercept HTTPS often replace the default certificate
authority chain, which breaks tools such as `pip-audit`. Use
`python script/bootstrap_truststore.py` to merge your organization's CA bundle
with the upstream [``certifi``](https://pypi.org/project/certifi/) trust store
and (optionally) generate a `pip.conf` that points at an internal PyPI mirror.

1. Collect your proxy or internal PKI certificate in PEM format and save it as
   `company-ca.pem` in the repository root.
2. Run
   `python script/bootstrap_truststore.py --ca-file company-ca.pem --emit-exports`.
   The helper creates `.truststore/ca-bundle.pem` and prints the environment
   overrides required by both `pip` and `pip-audit`.
3. Export the recommended variables in the shell that will run security checks:
   ```bash
   export REQUESTS_CA_BUNDLE="$(pwd)/.truststore/ca-bundle.pem"
   export PIP_CERT="$(pwd)/.truststore/ca-bundle.pem"
   ```
4. (Optional) Provide an internal package index while generating the trust
   store, for example:
   ```bash
   python script/bootstrap_truststore.py \
       --ca-file company-ca.pem \
       --pip-config .truststore/pip.conf \
       --index-url https://pypi.internal.example/simple \
       --emit-exports
   export PIP_CONFIG_FILE="$(pwd)/.truststore/pip.conf"
   ```
5. Invoke `pip-audit` using the normal repository instructions. The tool now
   trusts the injected certificates and can reach either the public index or
   your internal mirror without disabling TLS verification.

The generated artifacts remain in `.truststore/` so developers can refresh
them whenever certificates rotate without committing secrets to version
control. The helper always creates this directory in the repository root, and
the `.gitignore` entry ensures the resulting bundle, optional `pip.conf`, and
any exported environment snippets never land in commits. It is safe to delete
the folder between runs; a subsequent invocation of
`script/bootstrap_truststore.py` recreates it with the latest certificates and
configuration.

### 401 Unauthorized responses
- When Google's Nova endpoint returns 401, the integration now clears both the
  entry-scoped and global ADM token cache entries before refreshing. This
  ensures a brand-new token is minted and stored automatically, without
  requiring you to restart Home Assistant or re-run the configuration flow.
- The regeneration also refreshes the associated metadata so subsequent
  requests resume with the updated token immediately.

## Privacy and Security

- All location data uses Google's end-to-end encryption
- Authentication tokens are securely cached
- All GPS coordinates are processed locally. The integration itself sends no
  location data anywhere except to Google, which is where it comes from.
- **The exceptions, and they are yours to trigger.** Opening a Map View page
  makes your browser talk to two third parties:
  - **Map tiles** from OpenStreetMap (`https://{s}.tile.openstreetmap.org/...`).
    The page fits its view to *all* locations it shows, so with the default
    history window the requested area is the area your device moved through
    during that window, not just its current position. The requests carry no
    device name, no account and no coordinates as such, but the requested tiles
    do describe that area.
  - **The Leaflet library** from `unpkg.com`, which the page currently loads to
    draw the map. That request carries no location data at all, only the fact
    that the page was opened. It is being removed in favour of a copy shipped
    with the integration.

  Nothing is requested while no Map View page is open, and no other page of this
  integration loads either.

## Security considerations

### What is stored, and where

| What | Where | Notes |
| --- | --- | --- |
| The credential bundle you paste during setup | Home Assistant's storage, one file per config entry: `.storage/googlefindmy_secrets_<entry_id>` | Written by the integration's token cache, not by you |
| Google account e-mail | The config entry itself (`.storage/core.config_entries`) | Needed to restart without asking you again |
| The location history of every tracker | Home Assistant's recorder database (`home-assistant_v2.db` by default) | Not written by this integration but by Home Assistant, recording the entities it creates, including the recorder-only `last_latitude`/`last_longitude` attributes the Map View reads back (`map_view.py`, `get_significant_states`). It is kept for as long as your `recorder` `purge_keep_days` says, and it travels with any backup that includes the database (Home Assistant's backup manager offers that as a choice; a recorder pointed at an external database is not in the backup at all). Exclude the entities under `recorder:` if you do not want that history |
| The pasted bundle and the OAuth token | Also the config entry (`.storage/core.config_entries`) | On **initial setup** they are moved into the token cache on the first successful start and removed from the entry. Two cases keep them there indefinitely: a setup that fails before that point, and any later credential replacement (reauth or the options flow), because `config_flow.py` → `_persist_secrets_bundle` writes them back and the reload then finds a primed cache and skips the removal (`__init__.py`, the `legacy_cache_primed` branch). The copy lives beside the token cache in the same `.storage` directory, so it widens no trust boundary, and the diagnostics download redacts it |
| Derived tokens (AAS, ADM, SPOT), FCM push identity, the shared key and the owner key | Same per-entry storage file | Refreshed automatically; the long-lived ones are what make the integration work after a restart |
| The Map View access token | Derived on demand from the instance UUID and the entry id, and carried inside each device's `configuration_url` in `.storage/core.device_registry` | Treat that URL as long-lived bearer material: the map view is not behind Home Assistant's login, so whoever holds the link sees the device's location. The token authenticates the **config entry**, not one device (`map_view.py` → `_resolve_entry_by_token`), so a recipient who knows another device id of the same account can substitute it in the path. With the default `map_view_token_expiration` (off) the token never expires |

`secrets.json` is **not** part of the running integration. It is produced by the
manual command-line login, and if you paste its contents, no file by that name
ever reaches the Home Assistant machine. Its *contents* do: `async_setup_entry`
hands the normalised bundle to `_async_save_secrets_data`, which stores it in
the per-entry file listed in the table above. What pasting avoids is a second,
loose copy on disk, not storage as such.

There is a second, optional hand-off that does put the file there, so it belongs
in this list. The integration watches two paths for a dropped bundle
(`discovery.py` → `_default_watch_paths`): the bundled `Auth/secrets.json` and
the login container's `docker-login/data/secrets.json`. The advanced option
`secrets_extra_watch_paths` adds any further paths you configure
(`discovery.py` → `_collect_extra_watch_paths`), and those are watched the same
way. A bundle found on any of them starts a discovery flow, and the copy is
deleted once Home Assistant is observed to hold the imported credentials
(`config_flow.py` → `_async_delete_watched_secrets`, armed by
`async_setup_entry`). Until then — and indefinitely if you never confirm the
flow, or if the import fails — the file stays on the Home Assistant machine in
clear. Deletion is also best-effort: a path Home Assistant cannot write to
keeps its copy. That one case does announce itself, in the Home Assistant log:
`Failed to remove watched secrets file after import: <path>`
(`config_flow.py` → `_remove_if_digest_matches`); search for it if you used a
watched path, and remove the named file yourself. The other case is silent by
construction: a flow you never confirmed never reaches the deletion at all, so
no message will ever appear for it. If you use that route, remove every such
copy yourself when you abandon an import, including the ones behind
`secrets_extra_watch_paths`. A legacy `Auth/secrets.json` found by
the token cache is imported once and then deleted (`Auth/token_cache.py`,
`os.remove(legacy_path)`), best-effort in the same way: on a read-only mount
the file stays, and the log says so
(`Failed to remove legacy cache file after migration: <path>`). Search for that
line too, and remove the file yourself if it appears.

### Who can read it

Anyone with **administrator access to Home Assistant** or **read access to its
configuration directory**. That is not a property of this integration: the
`.storage` directory holds the credentials of every integration you have
installed, and the recorder database holds their history. Protect the Home
Assistant instance and you protect these credentials; do not protect it and no
choice this integration could make would help.

Diagnostics downloads are redacted before they leave Home Assistant
(`diagnostics.py`, `TO_REDACT` and `TO_REDACT_PREFIXES`), including the pasted
bundle and the key names the token cache builds at run time, so an attached
diagnostics file does not contain your tokens. It does contain the entry id in
clear.

### What is *not* part of the Home Assistant runtime

Chrome and Selenium. The browser-based credential extraction is a manual step
you run yourself, from a terminal, on your own machine. No module Home Assistant
loads imports Selenium or starts a browser: an import-graph walk from
`__init__.py`, `config_flow.py` and `eid_resolver.py` reaches no browser package,
while the same walk from `chrome_driver.py` does — so the check can fire.

One qualification, because it is real: the interactive key-backup fallback is
guarded by a terminal check (`KeyBackup/shared_key_retrieval.py` →
`_retrieve_shared_key_hex`, `is_tty = sys.stdin and sys.stdin.isatty()`), not by
a check for "am I the CLI". A Home Assistant process running in the foreground
on a terminal, whose bundle carries no shared key, can therefore reach it. The
guard is being replaced by a signal the command-line process sets for itself.

### Reporting a security issue

Use GitHub's private vulnerability reporting (the *Security* tab of this
repository, *Report a vulnerability*) for anything with an attacker in it, and a
normal issue — one per item — for hardening suggestions.

## Contributing

Contributions are welcome and encouraged!

To contribute, please:
1. Fork the repository
2. Create a feature branch
3. Install the development dependencies with `make install-dev` (or `poetry install --with dev,test`)
4. Install the development hooks with `pre-commit install` and ensure `pre-commit run --all-files` passes before submitting changes. If the CLI entry points are unavailable, use the `python -m` fallbacks from the [module invocation primer](AGENTS.md#module-invocation-primer) to run the same commands reliably.
5. Run `python script/local_verify.py` to execute the required `ruff format --check` and `pytest -q` commands together (or invoke `python script/precommit_hooks/ruff_format.py --check ...` and `pytest -q` manually if you need custom arguments).
6. When running pytest (either through the helper script or directly) fix any failures and address every `DeprecationWarning` you encounter—rerun with `PYTHONWARNINGS=error::DeprecationWarning pytest -q` if you need help spotting new warnings.
7. Test thoroughly with your Find My devices
8. Submit a pull request with detailed description

For quick sanity checks during development, run the lint and type checks after installing dev dependencies:

```bash
make install-dev
poetry run ruff check .
poetry run mypy --strict
```

### Release process

- Update the version in both `custom_components/googlefindmy/manifest.json` and `custom_components/googlefindmy/const.py` (`INTEGRATION_VERSION`) at the same time so the manifest metadata and runtime constants remain in sync.
- Run the full verification suite (`ruff format --check`, targeted pytest modules, and `pytest -q`) before tagging a release to confirm the version bump did not introduce regressions.

### Development Scripts

Manifest validation (`hassfest`) now runs exclusively through the
[`hassfest-auto-fix`](.github/workflows/hassfest-auto-fix.yml) workflow. Every
push to `main` and every pull request automatically executes the
[`home-assistant/actions/hassfest`](https://github.com/home-assistant/actions/tree/master/hassfest#readme)
GitHub Action, which rewrites manifests when needed and re-runs the validator to
confirm the fixes.

When you need to inspect or download the results locally:

1. Open the relevant workflow run from the PR or commit.
2. Expand the **Run hassfest (may rewrite manifest)** step to review the console
   output, or download the generated artifact directly from the workflow UI.
3. If you need a fresh validation pass, trigger the workflow manually from the
   **Run workflow** button in the Actions tab or by re-running the job on the PR.

## Legacy CLI helpers & token cache selection

Several modules still expose lightweight CLI entry points (for example the device
listing helper and the standalone "Play/Stop Sound" examples). These scripts now
require you to target a specific Home Assistant config entry whenever more than
one token cache is available. Set the environment variable
`GOOGLEFINDMY_ENTRY_ID` to the desired config entry ID before running the CLI, or
pass a `cache=` override when instantiating the legacy `FcmReceiver` shim. If you
omit the entry ID while multiple caches are active the CLI will abort with a
message listing the available IDs so you can pick the right account.

## Credits

- Böttger, L. (2024). GoogleFindMyTools [Computer software]. https://github.com/leonboe1/GoogleFindMyTools
- Firebase Cloud Messaging integration. https://github.com/home-assistant/mobile-apps-fcm-push
- @txitxo0 for his amazing work on the MQTT based tool that I used to help kickstart this project!
- Open Location Code (Plus Code) encoder, (c) Google (Apache-2.0), vendored from
  [google/open-location-code](https://github.com/google/open-location-code),
  commit `dcff1534f70a0d7244d0d1c357c20f0aa28ab355`; modified: encode-only path.
  See [`custom_components/googlefindmy/vendor/openlocationcode/LICENSE`](custom_components/googlefindmy/vendor/openlocationcode/LICENSE).

[1]: https://developers.home-assistant.io/blog/2019/10/05/simple-mode/?utm_source=chatgpt.com "Simple Mode in Home Assistant 1.0"

## Special thanks to some amazing contributors!

- @DominicWindisch
- @suka97
- @jleinenbach

## Disclaimer

This integration is not affiliated with Google. Use at your own risk and in compliance with Google's Terms of Service. The developers are not responsible for any misuse or issues arising from the use of this integration.
