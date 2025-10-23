# Google FindMy Device (Find Hub) - Home Assistant Integration <img src="https://github.com/BSkando/GoogleFindMy-HA/blob/main/icon.png" width="30">

A comprehensive Home Assistant custom integration for Google's FindMy Device network, enabling real-time(ish) tracking and control of FindMy devices directly within Home Assistant!

>[!TIP]
>**Check out my companion Lovelace card, designed to work perfectly with this integration!**
>
>**[Google FindMy Card!](https://github.com/BSkando/GoogleFindMy-Card)**

## Come join our Discord for real time help and chat!

[Google FindMy Discord Server](https://discord.gg/RHvBYZ58P)

---
<img src="https://github.com/BSkando/GoogleFindMy-HA/blob/main/icon.png" width="30"> [![GitHub Repo stars](https://img.shields.io/github/stars/BSkando/GoogleFindMy-HA?style=for-the-badge&logo=github)](https://github.com/BSkando/GoogleFindMy-HA) [![Home Assistant Community Forum](https://img.shields.io/badge/Home%20Assistant-Community%20Forum-blue?style=for-the-badge&logo=home-assistant)](https://community.home-assistant.io/t/google-findmy-find-hub-integration/931136) [![Buy me a coffee](https://img.shields.io/badge/Coffee-Addiction!-yellow?style=for-the-badge&logo=buy-me-a-coffee)](https://www.buymeacoffee.com/bskando) <img src="https://github.com/BSkando/GoogleFindMy-HA/blob/main/icon.png" width="30">

---
## Features

- 🗺️ **Real-time Device Tracking**: Track Google FindMy devices with location data, sourced from the FindMy network
- ⏱️ **Configurable Polling**: Flexible polling intervals with rate limit protection
- 🔔 **Sound Button Entity**: Devices include button entity that plays a sound on supported devices
- ✅ **Attribute grading system**: Best location data is selected automatically based on recency, accuracy, and source of data
- 📍 **Historical Map-View**: Each tracker has a filterable Map-View that shows tracker movement with location data
- 📋 **Statistic Entity**: Detailed statistics for monitoring integration performance
- ❣️ **More to come!**

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
2. **CRITICAL STEP!**  Complete the **ENTIRE** authentication process to generate `Auth/secrets.json`
> [!WARNING]
>While going through the process in main.py to authenticate, you **MUST** go through **2 login processes!**  After the first login is successful, your available devices will be listed.  You must complete the next step to display location data for one of your devices.  You will then login again.  After you complete this step, you should see valid location data for your device, followed by several errors that are not important.  ONLY at this point are you ready to move on to the next step!
3. Copy the entire contents of the secrets.json file.
    - Specifically, open the file in a text editor, select all, and copy.

### <ins>Authentication Part 2 (Home Assistant Steps)</ins>
4. Add the integration to your Home Assistant install.
5. In Home Assistant, paste the copied text from secrets.json when prompted.
6. After completing authentication and adding devices, RESTART Home Assistant!

### Problems with Authentication?
>[!NOTE]
>Recently, some have had issues with the script from the repository above.  If you follow all the steps in Leon's repository and are unable to get through the main.py sequence due to errors, please try using my modification of the script [BACKUP:GoogleFindMyTools](https://github.com/BSkando/GoogleFindMyTools)

## Configuration Options

Accessible via the ⚙️ cogwheel button on the main Google Find My Device Integration page.

| **Option** | **Default** | **Units** | **Description** |
| :---: | :---: | :---: | --- |
| tracked_devices | - | - | Select which devices from your account are tracked with the integration. |
| location_poll_interval | 300 | seconds | How often the integration runs a poll cycle for all devices |
| device_poll_delay | 5 | seconds | How much time to wait between polling devices during a poll cycle |
| min_accuract_threshold | 100 | meters | Distance beyond which location data will be rejected from writing to logbook/recorder |
| movement_threshold | 50 | meters | Distance a device must travel to show an update in device location |
| google_home_filter_enabled | true | toggle | Enables/disables Google Home device location update filtering |
| google_home_filter_keywords | various | text input | Keywords, separated by commas, that are used in filtering out location data from Google Home devices |
| enable_stats_entities | true | toggle | Enables/disables "Google Find My Integration" statistics entity, which displays various useful statistics, including when polling is active |
| map_vew_token_expiration | false | toggle | Enables/disables expiration of generated API token for accessing recorder history, used in Map View location data queries |

## Services (Actions)

The integration provides a couple of Home Assistant Actions for use with automations.  Note that Device ID is different than Entity ID.  Device ID is a long, alpha-numeric value that can be obtained from the Device info pages.

| Action | Attribute | Description |
| :---: | :---: | --- |
| googlefindmy.locate_device | Device ID | Request fresh location data for a specific device. |
| googlefindmy.play_sound | Device ID | Play a sound on a specific device for location assistance.  Devices must be capable of playing a sound.  Most devices should be compatible. |
| googlefindmy.refresh_device_urls | - | Refreshes all device Map View URLs.  Useful if you are having problems with accessing Map View pages. |

## Supported devices and functions

- **Device coverage:** Phones, tablets, Wear OS devices, earbuds, and compatible Bluetooth trackers surfaced in the Google Find My Device network.  Any device that appears in the official Google Find My interface is eligible to be imported.
- **Entities created:** Each tracked device exposes a `device_tracker` entity for live location, a binary sensor for connection state, and optional helper entities (statistics, sound trigger button) depending on device capabilities.
- **Action support:** Sound playback is available on hardware that exposes the native "Play sound" action within Google's ecosystem.  The integration hides the button on devices that do not advertise support, aligning with [Home Assistant action documentation](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/docs-supported-functions/).

## Data updates and background behavior

- **Coordinator-driven updates:** Location and metadata are refreshed through Home Assistant's [`DataUpdateCoordinator`](https://developers.home-assistant.io/docs/integration_fetching_data/) with a default 300-second polling interval.  Staggered per-device delays keep API calls within Google's rate limits.
- **Manual refresh:** Call the `googlefindmy.locate_device` action to request fresh data outside the scheduled polling cycle.  The integration debounces requests to avoid repeated queries that would exceed the appropriate polling guidance.
- **Repair flows:** When authentication expires or Google invalidates API tokens, the integration raises a [Home Assistant repair issue](https://developers.home-assistant.io/docs/core/platform/repairs/) that guides you through reauthentication without removing the config entry.

## Known limitations

- **Historical data availability:** Map View history is generated locally and depends on the Recorder integration retaining statistics; pruning recorder data will remove historical traces.
- **Offline devices:** Google only reports the last known location for powered-off or offline hardware.  Devices may appear as `unavailable` until they reconnect to the Find My network.
- **Authentication tooling:** Generating `Auth/secrets.json` currently relies on the external GoogleFindMyTools scripts.  Future upstream changes to Google's login flow may require updated tooling before the integration can connect again.
- **Multiple households:** Home Assistant imports all trackers from the authenticated Google account.  Fine-grained sharing to limit visibility per household member is not yet available and should be handled via entity permissions.

## Deinstallation / Removal

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

### FCM Connection Problems
- Extended timeout allows up to 60 seconds for device response
- Check firewall settings for Firebase Cloud Messaging
- Review FCM debug logs for connection details

### Rate Limiting
The integration respects Google's rate limits by:
- Sequential device polling (one device at a time)
- Configurable delays between requests
- Minimum poll interval enforcement
- Automatic retry with exponential backoff

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
3. Install the development dependencies with `python -m pip install -r requirements-dev.txt`
4. Install the development hooks with `pre-commit install` and ensure `pre-commit run --all-files` passes before submitting changes
5. Run `python script/local_verify.py` to execute the required `ruff format --check` and `pytest -q` commands together (or invoke `python script/precommit_hooks/ruff_format.py --check ...` and `pytest -q` manually if you need custom arguments).
6. When running pytest (either through the helper script or directly) fix any failures and address every `DeprecationWarning` you encounter—rerun with `PYTHONWARNINGS=error::DeprecationWarning pytest -q` if you need help spotting new warnings.
7. Test thoroughly with your Find My devices
8. Submit a pull request with detailed description

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

## Special thanks to some amazing contributors!

- @DominicWindisch
- @suka97
- @jleinenbach

## Disclaimer

This integration is not affiliated with Google. Use at your own risk and in compliance with Google's Terms of Service. The developers are not responsible for any misuse or issues arising from the use of this integration.
