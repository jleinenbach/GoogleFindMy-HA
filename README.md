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
>**Home Assistant Core 2025.10 or newer is recommended.** The functional minimum is **2025.8.0** (enforced in `hacs.json` and `pyproject.toml`): that release provides the config subentry flow maturity and the `async_added_to_hass` behavior the tracker/service subentries depend on. The Core-managed config subentry model itself has been available since the 2025.3 cycle. Running 2025.10 or newer is recommended for the bug fixes and stability improvements made since 2025.8, not because of a hard API requirement.

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
- 📍 **Historical Map-View**: Each tracker has a filterable Map-View that shows tracker movement with location data
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

### <ins>Authentication Part 1 (External Steps)</ins>
1. Navigate to [GoogleFindMyTools](https://github.com/leonboe1/GoogleFindMyTools?tab=readme-ov-file#how-to-use) repository and follow the directions on "How to use" the main.py script.
   > [!IMPORTANT]
   > Run the authentication script from the **same public IP address / network** that your Home Assistant instance uses, and sign in with the **same Google account** that owns the trackers. Google ties the end-to-end encryption keys in `secrets.json` to the account and may revoke them when requests arrive from a different IP or region. A mismatch produces a bundle that lists your devices and can ring them, but cannot decrypt any location reports — see [Devices appear but no location updates](#authentication-expires-repeatedly).
2. **CRITICAL STEP!**  Complete the **ENTIRE** authentication process to generate `Auth/secrets.json`
> [!WARNING]
>While going through the process in main.py to authenticate, you **MUST** go through **2 login processes!**  After the first login is successful, your available devices will be listed.  You must complete the next step to display location data for one of your devices.  You will then login again.  After you complete this step, you should see valid location data for your device, followed by several errors that are not important.  ONLY at this point are you ready to move on to the next step!
3. Copy the entire contents of the secrets.json file.
    - Specifically, open the file in a text editor, select all, and copy.
> [!IMPORTANT]
> The encryption key (`shared_key`) is only retrieved during the **second** login, which happens when you actively **select a device to locate**. If you stop after the device list appears (skipping that step), the resulting `secrets.json` has no `shared_key` and Home Assistant will reject the import with a `keys_missing` error, because locations cannot be decrypted without it.
> As an alternative to the external GoogleFindMyTools, the bundled CLI (`custom_components/googlefindmy/main.py`) fetches **both** keys automatically in a single run, without requiring you to select a device manually, so it is the more robust way to generate a complete `secrets.json`.

### <ins>Authentication Part 2 (Home Assistant Steps)</ins>
4. Add the integration to your Home Assistant install.
5. In Home Assistant, paste the copied text from secrets.json when prompted.
6. After completing authentication and adding devices, RESTART Home Assistant!

### Problems with Authentication?
>[!NOTE]
>Recently, some have had issues with the script from the repository above.  If you follow all the steps in Leon's repository and are unable to get through the main.py sequence due to errors, please try using my modification of the script [BACKUP:GoogleFindMyTools](https://github.com/BSkando/GoogleFindMyTools)

### Automatic discovery & credential updates

- **Auth/secrets.json watcher:** Home Assistant now monitors the integration's `Auth/secrets.json` file. Dropping a new bundle into `custom_components/googlefindmy/Auth/` immediately opens the config flow with the email and tokens pre-filled, so you can confirm the entry without pasting anything manually.
- **Update flows for existing entries:** When the watcher detects refreshed credentials for an account that is already configured, the integration pushes a `discovery_update` flow. Accepting it reauthenticates the existing entry and keeps all devices and options intact.
- **Cloud discovery channel:** Cloud-triggered discovery continues to operate in parallel, using the same deduplication logic as the secrets watcher. Regardless of source, duplicate flows are suppressed using Home Assistant's `DiscoveryKey` mechanism.

### Multi-account behavior and duplicate protection

- Home Assistant supports connecting multiple Google accounts, but **only one config entry per email address stays active**. When duplicate entries share the same Google account, the integration automatically disables and unloads the non-authoritative entries to prevent device duplication and token conflicts.
- The disabled entries remain visible in **Settings → Devices & Services** with an integration-managed disabled state so you can review or remove them manually. Reactivating a disabled duplicate requires removing the authoritative entry first or supplying credentials for a different Google account.

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
| `ignored_devices` | none | - | Devices hidden from tracking. Use **Manage ignored devices** to restore them. |
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
| `stale_threshold` | 1800 | seconds | After this many seconds (default: 30 minutes) without a location update, the tracker state becomes `unknown`. Use the "Last Location" entity to always see the last known position. |
| `show_location_age` | true | toggle | Adds a `location_age` attribute (in seconds, rounded to 60s) to each tracker entity. Excluded from Recorder history to keep DB size predictable. |

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

The `strings.json` and translation files under `custom_components/googlefindmy/translations/` provide localized messages for each key so UI notifications remain consistent.

## Services (Actions)

The integration provides a couple of Home Assistant Actions for use with automations.  Note that Device ID is different than Entity ID.  Device ID is a long, alpha-numeric value that can be obtained from the Device info pages.

| Action | Attribute | Description |
| :---: | :---: | --- |
| googlefindmy.locate_device | Device ID (required) | Request fresh location data for a specific device. |
| googlefindmy.play_sound | Device ID (required) | Play a sound on a specific device for location assistance.  Devices must be capable of playing a sound.  Most devices should be compatible. |
| googlefindmy.stop_sound | Device ID (required) | Stop the active sound on the selected device. |
| googlefindmy.locate_external | Device ID (required), Device Name (optional) | Trigger the locate flow via the external helper while optionally labeling logs with a human-readable device name. |
| googlefindmy.refresh_device_urls | - | Refreshes all device Map View URLs.  Useful if you are having problems with accessing Map View pages. |
| googlefindmy.rebuild_device_registry | - | Maintenance: rebuilds device registry links for Google Find My hubs and removes tracker devices incorrectly tied to the parent entry. |
| googlefindmy.rebuild_registry | Config Entry ID(s) (optional) | Reload integration config entries; without a payload the first configured entry reloads, or target specific IDs by passing one or many `entry_id` values. |

## Supported devices and functions

- **Device coverage:** Phones, tablets, Wear OS devices, earbuds, and compatible Bluetooth trackers surfaced in the Google Find My Device network.  Any device that appears in the official Google Find My interface is eligible to be imported.
- **Entities created:** Each tracked device exposes a `device_tracker` entity for live location, a binary sensor for connection state, and optional helper entities (statistics, sound trigger button) depending on device capabilities.
- **Action support:** Sound playback is available on hardware that exposes the native "Play sound" action within Google's ecosystem.  The integration hides the button on devices that do not advertise support, aligning with [Home Assistant action documentation](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/docs-supported-functions/).

## Data updates and background behavior

- **Snapshot merge semantics:** Push updates from the FCM listener are merged into the coordinator snapshot rather than replacing it. Devices that were not part of a push event keep their previous metadata, which prevents transient gaps in the device tracker registry between full poll cycles. The subentry-device index is rebuilt from the merged snapshot.
- **Coordinator-driven updates:** Location and metadata are refreshed through Home Assistant's [`DataUpdateCoordinator`](https://developers.home-assistant.io/docs/integration_fetching_data/) with a default 300-second polling interval.  Staggered per-device delays keep API calls within Google's rate limits.
- **Manual refresh:** Call the `googlefindmy.locate_device` action to request fresh data outside the scheduled polling cycle.  The integration debounces requests to avoid repeated queries that would exceed the appropriate polling guidance.
- **Repair flows:** When authentication expires or Google invalidates API tokens, the integration raises a [Home Assistant repair issue](https://developers.home-assistant.io/docs/core/platform/repairs/) that guides you through reauthentication without removing the config entry.

## Known limitations

- **Historical data availability:** Map View history is generated locally and depends on the Recorder integration retaining statistics; pruning recorder data will remove historical traces.
- **Offline devices:** Google only reports the last known location for powered-off or offline hardware.  Devices may appear as `unavailable` until they reconnect to the Find My network.
- **Authentication tooling:** Generating `Auth/secrets.json` currently relies on the external GoogleFindMyTools scripts.  Future upstream changes to Google's login flow may require updated tooling before the integration can connect again.
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
- No location data is transmitted to third parties
- Local processing of all GPS coordinates

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

[1]: https://developers.home-assistant.io/blog/2019/10/05/simple-mode/?utm_source=chatgpt.com "Simple Mode in Home Assistant 1.0"

## Special thanks to some amazing contributors!

- @DominicWindisch
- @suka97
- @jleinenbach

## Disclaimer

This integration is not affiliated with Google. Use at your own risk and in compliance with Google's Terms of Service. The developers are not responsible for any misuse or issues arising from the use of this integration.
