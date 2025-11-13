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

