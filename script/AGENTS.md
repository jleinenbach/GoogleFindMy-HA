# Script utilities guidelines

These conventions apply to every file within this directory tree.

## CLI expectations

* Provide a descriptive module-level docstring outlining the command's role
  and any noteworthy flags. Mention how to preview output when inputs are
  missing so reviewers can understand the UX quickly.
* Implement a `main()` function that returns an exit status integer and wrap
  execution in the `if __name__ == "__main__":` guard via `raise SystemExit`.
* Prefer `argparse.ArgumentParser` for option handling. Document default
  values in the `help` strings so the command remains self-documenting.
* When generating or applying large patches, prefer the `quiet_apply_patch.py`
  wrapper in this directory to truncate diff echoes and keep interactive
  sessions responsive.
* Print human-facing summaries with `print()` using UTF-8–safe f-strings.
  Avoid manual string concatenation when formatting values from multiple
  sources.

## Formatting conventions

* Keep line length within 99 characters unless an argparse description requires
  more space for clarity.
* Normalize filesystem interactions to use `pathlib.Path` objects and explicit
  UTF-8 encoding whenever reading text files.
* When iterating collections for display, sort deterministic output so diffs
  remain stable between runs.


## The declared minimum core version

`script/check_ha_compatibility.py` answers one question: is the minimum Home
Assistant version declared by this repository actually satisfiable by the pinned
requirements? Run `--check-declared-minimum` to verify the declared floor and
`--find-minimum` to discover the oldest satisfying release. The declared floor is
**`2025.9.1`**, recorded in `hacs.json` and in `pyproject.toml` (the comment there
explains why it is not `2025.8`).

That floor is the reason the integration carries a signature switch in its
device-registry code rather than a single modern call path. The replacement
device-registry API (`new_config_entry_id`, `new_config_subentry_id`,
`async_get_device_by_identifier`, `async_get_devices`) exists only from Core
`2026.8.0`; every release up to and including `2026.7.0` has none of it. So the code
probes the installed signature at runtime and keeps a **legacy ownership branch**,
and that branch is live code, not dead code, as long as this script reports
`2025.9.1`. (A second, differently named legacy branch in the same area serves only
test doubles; the two are told apart in `docs/AI_DEPRECATIONS_GUIDE.md`, section VI.) If you raise
the floor, the switch may be removed in the same change and not before. Background:
`docs/AI_DEPRECATIONS_GUIDE.md`, section VI.

## Gate scripts that can turn CI red

A script that fails a build and is documented nowhere is a riddle at the moment
it fires. Every such script is listed here with its purpose, its invocation, and
the condition under which it goes red.

* `vendor_leaflet.py` — keeps the Leaflet copy embedded by the map view in step
  with the version pinned in `package.json`.
  * CI runs `python script/vendor_leaflet.py --check` in the `vendored_assets`
    job. No network and no Node are needed for that mode.
  * It goes **red** when the pinned version differs from
    `custom_components/googlefindmy/vendor/leaflet/VERSION`, when a vendored file
    no longer matches its recorded SHA-256, or when the `LICENSE` file is gone
    (BSD-2-Clause attribution).
  * Fix: `npm install && python script/vendor_leaflet.py`, then commit the
    refreshed files and `VERSION`.
  * Why it exists: Dependabot reads manifests, never checked-in files. Without
    this gate a bump of the `leaflet` devDependency would leave the embedded copy
    behind silently, which is precisely how a vendored library ages into a
    vulnerability.
