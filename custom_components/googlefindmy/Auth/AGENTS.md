# AGENTS.md — Authentication helpers (gpsoauth integration)

> **Scope:** `custom_components/googlefindmy/Auth/**`
>
> Applies to synchronous and asynchronous token retrieval helpers, cache utilities, and any additional gpsoauth wrappers added under this folder.

## gpsoauth stub expectations

* `gpsoauth.perform_oauth` requires the third positional argument (`android_id`) to be a **string**. Convert integer IDs with `str(android_id)` before invoking the API. The stub exports the signature `perform_oauth(email: str, aas_token: str, android_id: str, *, service: str, app: str, client_sig: str) -> dict[str, Any]`.
* `gpsoauth.perform_master_login` mirrors the same positional types (`email: str`, `password: str`, `android_id: str`). Ensure any helper that resolves an integer device identifier also casts to `str` before calling the function.
* Both functions return a `dict[str, Any]` containing response keys like `"Token"`, `"Auth"`, and `"Error"`. Persist this annotation so mypy strict remains satisfied when parsing the response payload.

When the upstream stubs change, update this file and adjust the affected call sites so that future type-checking runs remain stable.

## Linting reminder

Keep `TYPE_CHECKING` aliases only when the alias is referenced in the module. Remove stale aliases during cleanups so linting runs stay predictable and reviewers can confirm no runtime imports are hidden behind unused guards.

## Cookie handling

When reading cookies from external authentication flows (for example, Selenium-managed sessions), always validate both the presence and the expected type of each field before use. In particular, confirm that the `"value"` entry resolves to a `str` and raise a descriptive exception if validation fails so helpers consuming the data can rely on strict return contracts.
