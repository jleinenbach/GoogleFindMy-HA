# AGENTS.md — Authentication helpers (gpsoauth integration)

> **Scope:** `custom_components/googlefindmy/Auth/**`
>
> Applies to synchronous and asynchronous token retrieval helpers, cache utilities, and any additional gpsoauth wrappers added under this folder.

## gpsoauth stub expectations

* `gpsoauth.perform_oauth` requires the third positional argument (`android_id`) to be an **integer**. Helpers must resolve or convert IDs into the numeric representation (for example, `int(hex_value, 16)` for cached values stored as hexadecimal strings) before invoking the API. Regression tests (for example, `tests/test_adm_token_retrieval.py::test_async_request_token_uses_cached_android_id`) assert this behavior. When editing `gpsoauth.pyi`, update the stub signature to `perform_oauth(email: str, aas_token: str, android_id: int, *, service: str, app: str, client_sig: str) -> dict[str, Any]` so type checkers reflect the runtime and test contract.
* `gpsoauth.perform_master_login` mirrors the same positional types (`email: str`, `password: str`, `android_id: int`). Provide the integer form of the identifier directly—do **not** stringify the value. Stored `"0x"`-prefixed IDs must be normalized into their integer value ahead of time so downstream helpers stay consistent, and mirror the signature change in the stub when updating it.
* Both functions return a `dict[str, Any]` containing response keys like `"Token"`, `"Auth"`, and `"Error"`. Persist this annotation so mypy strict remains satisfied when parsing the response payload.

When the upstream stubs change, update this file and adjust the affected call sites so that future type-checking runs remain stable.

## Linting reminder

Keep `TYPE_CHECKING` aliases only when the alias is referenced in the module. Remove stale aliases during cleanups so linting runs stay predictable and reviewers can confirm no runtime imports are hidden behind unused guards.

## Shared helper preference

When multiple modules need the same small utility (for example, `_mask_email_for_logs`), define it once in a shared helper module and import it at module scope rather than re-importing inside functions. Centralizing helpers avoids circular-import traps and prevents Ruff from flagging inline imports.

## Cookie handling

When reading cookies from external authentication flows (for example, Selenium-managed sessions), always validate both the presence and the expected type of each field before use. In particular, confirm that the `"value"` entry resolves to a `str` and raise a descriptive exception if validation fails so helpers consuming the data can rely on strict return contracts.
