# AGENTS.md — Cryptography helpers typing contract

## Scope

This guidance applies to every file under `custom_components/googlefindmy/FMDNCrypto/`.

## Expectations

- Keep cryptographic helpers fully typed for mypy strict runs. Materialize intermediate values used in modular arithmetic into `int` variables so downstream callers receive concrete `int` results.
- Cache curve constants (`p`, `a`, `b`, `order`) as `int` locals before any arithmetic so repeated `int()` conversions do not appear inside expressions.
- When reducing coordinates or scalars modulo the curve prime/order, store the normalized value in a named variable (for example, `Rx_mod: int`) and reuse it for all subsequent calculations.
- Preserve deterministic "even Y" selection for point decompression: if a modular square root is odd, flip it by `p - y` before returning and store the final value in an `int` variable named `Ry` or `y_even`.

## Testing

- Run `mypy --strict custom_components/googlefindmy/FMDNCrypto` whenever these modules change.
