# AGENTS.md — Cryptography helpers typing contract

## Scope

This guidance applies to every file under `custom_components/googlefindmy/FMDNCrypto/`.

## Expectations

- Keep cryptographic helpers fully typed for mypy strict runs. Materialize intermediate values used in modular arithmetic into `int` variables so downstream callers receive concrete `int` results.
- Group orders and the reduction of the PRF output `r'` to the scalar `r` live only in `curve_profile.py` (`FmdnCurve`, `reduce_scalar`, `ScalarRule`, `ScalarDerivation`); other modules import them instead of restating an order literal or a reduction formula for `r`. New code takes coordinate lengths from `FmdnCurve.coord_len`; the older literals `_COORD_LEN` in `foreign_tracker_cryptor.py` and `LEGACY_EID_LENGTH`/`MODERN_EID_LENGTH` in `eid_generator.py` predate it.
- Cache curve constants (`p`, `a`, `b`, `order`) as `int` locals before any arithmetic so repeated `int()` conversions do not appear inside expressions.
- When reducing coordinates or scalars modulo the curve prime/order, store the normalized value in a named variable (for example, `Rx_mod: int`) and reuse it for all subsequent calculations.
- Preserve deterministic "even Y" selection for point decompression: if a modular square root is odd, flip it by `p - y` before returning and store the final value in an `int` variable named `Ry` or `y_even`.
- Docstring style: when citing FHN behavior, explicitly reference the relevant section (for example, "FHN Accessory Specification v1.3 — Authentication section" or "Table 10: Construction of a pseudorandom number") and summarize how the implementation satisfies that clause. Keep the citation concise and scoped to the helper being described.

## Testing

- Run `mypy --strict custom_components/googlefindmy/FMDNCrypto` whenever these modules change.
