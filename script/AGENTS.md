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
