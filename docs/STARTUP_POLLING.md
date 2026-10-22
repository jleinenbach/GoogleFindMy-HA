# Location polling and Home Assistant startup

Home Assistant can display "Wrapping up startup" while Google Find My's config
entry already reports `loaded`. Entry setup time does not include every task
Home Assistant waits for before reaching `RUNNING`.

## Cause

`PollingOperations._async_update_data` in
[`coordinator/polling.py`](../custom_components/googlefindmy/coordinator/polling.py)
starts a separate sequential location-poll cycle. Scheduling it with
`hass.async_create_task` makes it an ordinary task tracked by Home Assistant's
startup barrier, even though the coordinator does not await it.

For 14 poll targets and a five-second inter-device delay, the pauses alone total
65 seconds (`13 * 5`), before request and response time. Slow or absent location
responses can extend that wait. This is a startup dependency, not evidence that
the asynchronous request blocks the event loop.

## Fix and lifecycle

Schedule the poll with `ConfigEntry.async_create_background_task` when the
coordinator has an entry, and `HomeAssistant.async_create_background_task` for
an unbound coordinator. These Home Assistant helpers exclude the poll from
startup waits and cancel it on shutdown; the entry helper also cancels it on
unload. Keep `_poll_cycle_task`, `_async_cancel_poll_cycle`, the stop listener,
and the teardown latch to settle cancellation and disarm retry/resolver timers.

The first device-list refresh still runs during entry setup. Poll ordering,
delays, timeouts, location updates, and background reauthentication remain the
same. Tracker entities continue to use the coordinator's existing dynamic
discovery listener. This change removes the long location cycle from startup;
it does not defer all discovery or authentication work.

## Regression coverage

[`test_poll_startup.py`](../tests/test_poll_startup.py) holds a location poll
pending while exercising Home Assistant's real task tracking and startup
method. It checks that HA reaches `RUNNING` before that poll finishes, that
entry-bound polls are registered with the entry, and that coordinator teardown
cancels the poll. Both entry-bound and unbound cases fail with the original
ordinary-task scheduler.

Existing predictive-polling, stop, unload, and teardown-latch tests continue to
cover cadence and cleanup.

## Observed pre-fix timing

A read-only MCP check on 2026-10-12, with Core 2026.10.1 and integration 1.7.15.19,
recorded the following local timestamps in Europe/Budapest:

| Event | Time |
| --- | --- |
| Custom integrations being loaded | 11:28:12 |
| FCM marked connected in system health | 11:28:28 |
| Poll completion, derived from coordinator diagnostics | 11:29:46.982 |
| An unrelated aggregate cover first available | 11:29:47.055 |

The entry's `config_entry_setup` duration was 5.27 seconds. The unrelated cover
became available approximately 73 milliseconds after poll completion and
95 seconds after integration loading began. Warning-level logs did not contain
an explicit pending-task trace, so the timeline is correlation. The code's
task-tracking behavior and the failing-before/passing-after regression establish
the startup-blocking mechanism. Deployment verification should separately
compare the next restart and confirm polling still completes after HA starts.
