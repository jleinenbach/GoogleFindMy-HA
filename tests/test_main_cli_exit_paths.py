# tests/test_main_cli_exit_paths.py
"""Contract tests for the standalone CLI exit and stale-token-purge paths.

These pin the ``sys.exit(1)`` fail-closed exits and the stale-key purges in
``main.py`` that the happy-path tests in ``test_main.py`` do not exercise:

* ``_ensure_authenticated`` -- corrupt-secrets recovery, already-authenticated
  short circuit, empty-email abort, stale derived-token purge.
* ``_ensure_aas_token``    -- cached/absent short circuits and the
  single-use-cookie exit.
* ``_clear_stale_adm_token``        -- username guard + ADM key clearing.
* ``_clear_stale_tokens_for_reauth`` -- file/dict/parse guards, key removal,
  and the restore it hands back for a login that ends without a token.

The standalone FCM lead (``_setup_fcm_receiver``) is intentionally excluded
(W0/E5 ``# pragma: no cover`` standalone lead).
"""

from __future__ import annotations

import builtins
import json
from pathlib import Path
from typing import Any

import pytest


class _AsyncDictCache:
    """Minimal async cache double: ``get``/``set`` over a plain dict.

    ``set(name, None)`` pops the key, mirroring the real cache contract that
    ``_clear_stale_adm_token`` relies on.
    """

    def __init__(self, initial: dict[str, Any] | None = None) -> None:
        self.data: dict[str, Any] = dict(initial or {})

    async def get(self, name: str) -> Any:
        return self.data.get(name)

    async def set(self, name: str, value: Any) -> None:
        if value is None:
            self.data.pop(name, None)
        else:
            self.data[name] = value


# ---------------------------------------------------------------------------
# _ensure_authenticated
# ---------------------------------------------------------------------------


class TestEnsureAuthenticated:
    """Auth-flow guards, abort and stale-purge on first-run login."""

    def test_already_authenticated_short_circuits(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:  # type: ignore[no-untyped-def]
        """When username *and* a token are present, the Chrome flow is never
        triggered (early return, no browser)."""
        from custom_components.googlefindmy import main as cli_main

        monkeypatch.setattr(cli_main, "_this_dir", tmp_path, raising=True)
        secrets = tmp_path / "Auth" / "secrets.json"
        secrets.parent.mkdir(parents=True, exist_ok=True)
        secrets.write_text(
            json.dumps({"username": "u@example.com", "oauth_token": "tok"}),
            encoding="utf-8",
        )

        from custom_components.googlefindmy.Auth import auth_flow

        def _boom() -> tuple[str, str]:
            raise AssertionError("Chrome flow must not run when authenticated")

        monkeypatch.setattr(
            auth_flow, "request_oauth_account_token_flow", _boom, raising=True
        )

        cli_main._ensure_authenticated()  # returns without touching auth_flow

    def test_corrupt_secrets_treated_as_missing_and_continues(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:  # type: ignore[no-untyped-def]
        """A secrets.json that fails to parse is logged and treated as missing;
        the login flow then runs and persists fresh credentials."""
        from custom_components.googlefindmy import main as cli_main

        monkeypatch.setattr(cli_main, "_this_dir", tmp_path, raising=True)
        secrets = tmp_path / "Auth" / "secrets.json"
        secrets.parent.mkdir(parents=True, exist_ok=True)
        secrets.write_text("{ not valid json", encoding="utf-8")

        from custom_components.googlefindmy.Auth import auth_flow

        monkeypatch.setattr(
            auth_flow,
            "request_oauth_account_token_flow",
            lambda: ("fresh-oauth", "detected@example.com"),
            raising=True,
        )

        with caplog.at_level("WARNING"):
            cli_main._ensure_authenticated()

        assert "treating as missing credentials" in caplog.text
        written = json.loads(secrets.read_text(encoding="utf-8"))
        assert written["oauth_token"] == "fresh-oauth"
        assert written["username"] == "detected@example.com"

    def test_missing_browser_packages_explain_themselves(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:  # type: ignore[no-untyped-def]
        """Selenium is not installed by Home Assistant, so the CLI must say so.

        `manifest.json` deliberately no longer lists the browser packages, which
        makes a bare copy of the integration directory the normal case rather
        than the exception. The first-run login is where that shows up, and it
        has to name the install command instead of showing a traceback.
        """
        import sys

        from custom_components.googlefindmy import main as cli_main
        from custom_components.googlefindmy.browser_deps import INSTALL_COMMAND

        monkeypatch.setattr(cli_main, "_this_dir", tmp_path, raising=True)

        real_import = builtins.__import__

        def _no_auth_flow(name: str, *args: Any, **kwargs: Any) -> Any:
            if name.endswith("Auth.auth_flow"):
                raise ImportError("No module named 'selenium'")
            return real_import(name, *args, **kwargs)

        monkeypatch.delitem(
            sys.modules, "custom_components.googlefindmy.Auth.auth_flow", raising=False
        )
        monkeypatch.setattr(builtins, "__import__", _no_auth_flow)

        with pytest.raises(SystemExit) as excinfo:
            cli_main._ensure_authenticated()

        assert excinfo.value.code == 1
        out = capsys.readouterr().out
        assert INSTALL_COMMAND in out
        assert "No module named 'selenium'" in out

    def test_empty_email_aborts_with_exit_one(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:  # type: ignore[no-untyped-def]
        """When the Chrome session yields no e-mail and the interactive prompt
        is left blank, the process exits 1 (fail closed, no partial write)."""
        from custom_components.googlefindmy import main as cli_main

        monkeypatch.setattr(cli_main, "_this_dir", tmp_path, raising=True)

        from custom_components.googlefindmy.Auth import auth_flow

        monkeypatch.setattr(
            auth_flow,
            "request_oauth_account_token_flow",
            lambda: ("oauth-without-email", ""),
            raising=True,
        )
        monkeypatch.setattr("builtins.input", lambda _prompt="": "  ")

        with pytest.raises(SystemExit) as excinfo:
            cli_main._ensure_authenticated()

        assert excinfo.value.code == 1
        # No secrets file was written on the abort path.
        assert not (tmp_path / "Auth" / "secrets.json").is_file()

    def test_stale_derived_tokens_purged_before_write(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:  # type: ignore[no-untyped-def]
        """Existing derived ADM keys and a stale ``aas_token`` are dropped when
        a fresh OAuth token is written, so they get regenerated from it."""
        from custom_components.googlefindmy import main as cli_main

        monkeypatch.setattr(cli_main, "_this_dir", tmp_path, raising=True)
        secrets = tmp_path / "Auth" / "secrets.json"
        secrets.parent.mkdir(parents=True, exist_ok=True)
        # has_user True but has_token False (no oauth/aas token present) -> the
        # login flow runs and the purge loop is reached. A present ``aas_token``
        # would flip has_token True and short-circuit before the purge.
        secrets.write_text(
            json.dumps(
                {
                    "username": "u@example.com",
                    "adm_token_u@example.com": "old",
                    "adm_token_issued_at_u@example.com": "123",
                    "adm_probe_x": "1",
                }
            ),
            encoding="utf-8",
        )

        from custom_components.googlefindmy.Auth import auth_flow

        monkeypatch.setattr(
            auth_flow,
            "request_oauth_account_token_flow",
            lambda: ("new-oauth", "u@example.com"),
            raising=True,
        )

        cli_main._ensure_authenticated()

        written = json.loads(secrets.read_text(encoding="utf-8"))
        assert written["oauth_token"] == "new-oauth"
        assert "aas_token" not in written
        assert not any(k.startswith(("adm_token_", "adm_probe_")) for k in written)


# ---------------------------------------------------------------------------
# _ensure_aas_token
# ---------------------------------------------------------------------------


class TestEnsureAasToken:
    """Eager OAuth->AAS exchange short circuits and fail-closed exit."""

    @pytest.mark.asyncio
    async def test_returns_when_aas_token_cached(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cached AAS token means nothing to do -- the retrieval module is
        never imported/called."""
        from custom_components.googlefindmy import main as cli_main
        from custom_components.googlefindmy.Auth import aas_token_retrieval

        def _boom(*_a: Any, **_k: Any) -> None:
            raise AssertionError("exchange must not run when AAS is cached")

        monkeypatch.setattr(
            aas_token_retrieval, "async_get_aas_token", _boom, raising=True
        )
        cache = _AsyncDictCache({"aas_token": "already-here", "oauth_token": "o"})

        await cli_main._ensure_aas_token(cache)

    @pytest.mark.asyncio
    async def test_returns_when_no_oauth_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without an OAuth token there is nothing to exchange (auth not
        needed) -- the exchange is skipped."""
        from custom_components.googlefindmy import main as cli_main
        from custom_components.googlefindmy.Auth import aas_token_retrieval

        def _boom(*_a: Any, **_k: Any) -> None:
            raise AssertionError("exchange must not run without an OAuth token")

        monkeypatch.setattr(
            aas_token_retrieval, "async_get_aas_token", _boom, raising=True
        )
        cache = _AsyncDictCache({"oauth_token": "   "})  # blank -> treated absent

        await cli_main._ensure_aas_token(cache)

    @pytest.mark.asyncio
    async def test_exchanges_when_oauth_present_and_aas_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A present OAuth token and absent AAS token trigger exactly one
        exchange call and no exit."""
        from custom_components.googlefindmy import main as cli_main
        from custom_components.googlefindmy.Auth import aas_token_retrieval

        calls: list[Any] = []

        async def _fake_exchange(*, cache: Any) -> None:
            calls.append(cache)

        monkeypatch.setattr(
            aas_token_retrieval, "async_get_aas_token", _fake_exchange, raising=True
        )
        cache = _AsyncDictCache({"oauth_token": "fresh"})

        await cli_main._ensure_aas_token(cache)

        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_exchange_failure_exits_one_with_reauth_hint(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A failed exchange (stale single-use cookie) exits 1 with an
        actionable re-authentication message on stderr."""
        from custom_components.googlefindmy import main as cli_main
        from custom_components.googlefindmy.Auth import aas_token_retrieval

        async def _raise(*, cache: Any) -> None:
            raise RuntimeError("BadAuthentication")

        monkeypatch.setattr(
            aas_token_retrieval, "async_get_aas_token", _raise, raising=True
        )
        cache = _AsyncDictCache({"oauth_token": "stale-cookie"})

        with pytest.raises(SystemExit) as excinfo:
            await cli_main._ensure_aas_token(cache)

        assert excinfo.value.code == 1
        assert "--reauth" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# _clear_stale_adm_token
# ---------------------------------------------------------------------------


class TestClearStaleAdmToken:
    """Per-session ADM token invalidation before the first CLI request."""

    @pytest.mark.asyncio
    async def test_no_username_is_a_noop(self) -> None:
        """Without a username there is no namespaced ADM key to clear."""
        from custom_components.googlefindmy import main as cli_main

        cache = _AsyncDictCache({"username": "   "})  # blank
        await cli_main._clear_stale_adm_token(cache)

        assert cache.data == {"username": "   "}

    @pytest.mark.asyncio
    async def test_clears_namespaced_adm_keys_case_folded(self) -> None:
        """The ADM token and its issued-at stamp are cleared under the
        lower-cased username namespace."""
        from custom_components.googlefindmy import main as cli_main

        cache = _AsyncDictCache(
            {
                "username": "  User@Example.COM  ",
                "adm_token_user@example.com": "tok",
                "adm_token_issued_at_user@example.com": "999",
                "keep_me": "yes",
            }
        )

        await cli_main._clear_stale_adm_token(cache)

        assert "adm_token_user@example.com" not in cache.data
        assert "adm_token_issued_at_user@example.com" not in cache.data
        assert cache.data["keep_me"] == "yes"


# ---------------------------------------------------------------------------
# _clear_stale_tokens_for_reauth
# ---------------------------------------------------------------------------


class TestClearStaleTokensForReauth:
    """`--reauth` purge of all cached/derived tokens from secrets.json."""

    def test_missing_file_is_a_noop(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:  # type: ignore[no-untyped-def]
        from custom_components.googlefindmy import main as cli_main

        monkeypatch.setattr(cli_main, "_this_dir", tmp_path, raising=True)
        cli_main._clear_stale_tokens_for_reauth()  # no Auth/secrets.json -> return

    def test_non_dict_json_is_a_noop(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:  # type: ignore[no-untyped-def]
        from custom_components.googlefindmy import main as cli_main

        monkeypatch.setattr(cli_main, "_this_dir", tmp_path, raising=True)
        secrets = tmp_path / "Auth" / "secrets.json"
        secrets.parent.mkdir(parents=True, exist_ok=True)
        secrets.write_text(json.dumps(["a", "list"]), encoding="utf-8")

        cli_main._clear_stale_tokens_for_reauth()

        # File left untouched (still a list).
        assert json.loads(secrets.read_text(encoding="utf-8")) == ["a", "list"]

    def test_corrupt_json_is_a_noop(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:  # type: ignore[no-untyped-def]
        from custom_components.googlefindmy import main as cli_main

        monkeypatch.setattr(cli_main, "_this_dir", tmp_path, raising=True)
        secrets = tmp_path / "Auth" / "secrets.json"
        secrets.parent.mkdir(parents=True, exist_ok=True)
        secrets.write_text("{ broken", encoding="utf-8")

        cli_main._clear_stale_tokens_for_reauth()

        assert secrets.read_text(encoding="utf-8") == "{ broken"

    def test_no_matching_keys_is_a_noop(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:  # type: ignore[no-untyped-def]
        from custom_components.googlefindmy import main as cli_main

        monkeypatch.setattr(cli_main, "_this_dir", tmp_path, raising=True)
        secrets = tmp_path / "Auth" / "secrets.json"
        secrets.parent.mkdir(parents=True, exist_ok=True)
        secrets.write_text(json.dumps({"username": "u@x"}), encoding="utf-8")

        cli_main._clear_stale_tokens_for_reauth()

        assert json.loads(secrets.read_text(encoding="utf-8")) == {"username": "u@x"}

    def test_removes_all_token_families_and_keeps_username(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:  # type: ignore[no-untyped-def]
        """Every ``oauth_token``/``aas_token``/derived key is removed while
        non-token keys (username) survive; a count is reported."""
        from custom_components.googlefindmy import main as cli_main

        monkeypatch.setattr(cli_main, "_this_dir", tmp_path, raising=True)
        secrets = tmp_path / "Auth" / "secrets.json"
        secrets.parent.mkdir(parents=True, exist_ok=True)
        secrets.write_text(
            json.dumps(
                {
                    "username": "u@x",
                    "oauth_token": "o",
                    "aas_token": "a",
                    "adm_token_u@x": "d",
                    "adm_token_issued_at_u@x": "1",
                    "aas_token_issued_at_u@x": "2",
                    "adm_probe_u@x": "p",
                    "owner_key": "ok",
                    "shared_key": "sk",
                }
            ),
            encoding="utf-8",
        )

        cli_main._clear_stale_tokens_for_reauth()

        remaining = json.loads(secrets.read_text(encoding="utf-8"))
        assert remaining == {"username": "u@x"}


# ---------------------------------------------------------------------------
# --reauth restore path
# ---------------------------------------------------------------------------
#
# `--reauth` has to empty the cache *before* the login, because an empty cache
# is the only thing that makes `_ensure_authenticated` open Chrome at all. That
# ordering used to mean a cancelled re-authentication signed the user out: the
# tokens were gone, the new ones never arrived, and the run exited 130 saying
# "nothing was saved". These tests pin the repair -- the clear now hands back a
# restore, and every path that ends without a fresh token uses it.


class TestReauthRestore:
    """The cleared tokens come back when the login produces nothing."""

    _SECRETS = {
        "username": "u@x",
        "oauth_token": "o",
        "aas_token": "a",
        "owner_key": "ok",
    }

    def _prepare(self, tmp_path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
        from custom_components.googlefindmy import main as cli_main

        monkeypatch.setattr(cli_main, "_this_dir", tmp_path, raising=True)
        secrets = tmp_path / "Auth" / "secrets.json"
        secrets.parent.mkdir(parents=True, exist_ok=True)
        secrets.write_text(json.dumps(self._SECRETS), encoding="utf-8")
        return cli_main, secrets

    def test_cancelled_login_restores_every_cleared_key(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:  # type: ignore[no-untyped-def]
        cli_main, secrets = self._prepare(tmp_path, monkeypatch)

        restore = cli_main._clear_stale_tokens_for_reauth()
        assert restore is not None
        assert json.loads(secrets.read_text(encoding="utf-8")) == {"username": "u@x"}

        restore()

        assert json.loads(secrets.read_text(encoding="utf-8")) == self._SECRETS

    @pytest.mark.parametrize("anchor", ["oauth_token", "aas_token"])
    def test_a_fresh_anchor_stops_the_whole_restore(
        self,
        anchor: str,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:  # type: ignore[no-untyped-def]
        """A started chain is never mixed with the one it replaces.

        The predecessor of this test asserted the opposite -- that the stale
        ``aas_token`` came back beside a fresh ``oauth_token`` -- and so froze
        the defect Codex reported. It is the derived values that make the
        difference: ``_ensure_aas_token`` returns early on any cached
        ``aas_token``, so a restored one means the next run never exchanges the
        fresh anchor and silently keeps the credentials ``--reauth`` was asked
        to replace, vault keys included.
        """
        cli_main, secrets = self._prepare(tmp_path, monkeypatch)
        # The parameters are literals on purpose -- reading them from the
        # constant would make the test agree with whatever the code says. This
        # asserts the set instead, so a third anchor cannot slip in unexercised.
        assert set(cli_main._REAUTH_CHAIN_ANCHORS) == {"oauth_token", "aas_token"}

        restore = cli_main._clear_stale_tokens_for_reauth()
        assert restore is not None
        secrets.write_text(
            json.dumps({"username": "u@x", anchor: "fresh"}), encoding="utf-8"
        )

        restore()

        current = json.loads(secrets.read_text(encoding="utf-8"))
        assert current[anchor] == "fresh"
        # Absent, not merely different: a restored value would be read as a
        # usable credential, an absent one forces the exchange.
        assert "owner_key" not in current
        for other in ("oauth_token", "aas_token"):
            if other != anchor:
                assert other not in current
        assert "was stored before the run ended" in capsys.readouterr().out

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_a_blank_anchor_is_not_a_fresh_sign_in(
        self, blank: str, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:  # type: ignore[no-untyped-def]
        """An empty value must not cost the user their tokens.

        ``key in current`` would count a blank leftover as a completed sign-in
        and suppress a restore the cancelling user is entitled to.
        """
        cli_main, secrets = self._prepare(tmp_path, monkeypatch)

        restore = cli_main._clear_stale_tokens_for_reauth()
        assert restore is not None
        secrets.write_text(
            json.dumps({"username": "u@x", "oauth_token": blank}), encoding="utf-8"
        )

        restore()

        current = json.loads(secrets.read_text(encoding="utf-8"))
        assert current["aas_token"] == "a"
        assert current["owner_key"] == "ok"
        # The blank stays: the restore fills gaps, it does not overwrite.
        assert current["oauth_token"] == blank

    def test_a_derived_key_alone_does_not_stop_the_restore(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:  # type: ignore[no-untyped-def]
        """Only an anchor starts a chain; a derived value on its own does not.

        Blocking on any reappearing key would be the cheaper rule and the wrong
        one: it would cost a cancelling user their whole bundle over a value
        that cannot stand in for a sign-in.
        """
        cli_main, secrets = self._prepare(tmp_path, monkeypatch)

        restore = cli_main._clear_stale_tokens_for_reauth()
        assert restore is not None
        secrets.write_text(
            json.dumps({"username": "u@x", "owner_key": "new"}), encoding="utf-8"
        )

        restore()

        current = json.loads(secrets.read_text(encoding="utf-8"))
        assert current["oauth_token"] == "o"
        assert current["aas_token"] == "a"
        assert current["owner_key"] == "new"

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("token", True),
            ("", False),
            ("   ", False),
            (None, False),
            (0, False),
            (False, False),
            ({}, False),
        ],
    )
    def test_credential_presence_predicate(self, value: object, expected: bool) -> None:
        """The predicate answers "would a consumer read this as a credential".

        Not "is this truthy" and not "is this set": ``_ensure_aas_token`` and
        ``_ensure_authenticated`` both gate on ``isinstance(value, str)``, so a
        non-string left by a half-written file is a value those two ignore.
        Counting it as a fresh sign-in would cost a cancelling user the whole
        bundle over something that never started a chain.
        """
        from custom_components.googlefindmy import main as cli_main

        assert cli_main._credential_is_present(value) is expected

    def test_restore_recreates_a_deleted_file(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:  # type: ignore[no-untyped-def]
        cli_main, secrets = self._prepare(tmp_path, monkeypatch)

        restore = cli_main._clear_stale_tokens_for_reauth()
        assert restore is not None
        secrets.unlink()

        restore()

        current = json.loads(secrets.read_text(encoding="utf-8"))
        assert current["oauth_token"] == "o"

    def test_restore_refuses_to_overwrite_an_unreadable_file(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:  # type: ignore[no-untyped-def]
        """Corruption is not a licence to throw the rest of the file away.

        Writing the snapshot over a file that can no longer be parsed would drop
        every key the clear did not touch (the username, anything a future
        version stores). Refusing and saying so is the recoverable outcome.
        """
        cli_main, secrets = self._prepare(tmp_path, monkeypatch)

        restore = cli_main._clear_stale_tokens_for_reauth()
        assert restore is not None
        secrets.write_text("{ broken", encoding="utf-8")

        restore()

        assert secrets.read_text(encoding="utf-8") == "{ broken"
        assert "Could not read secrets.json" in capsys.readouterr().out

    def test_nothing_to_clear_yields_no_restore(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:  # type: ignore[no-untyped-def]
        from custom_components.googlefindmy import main as cli_main

        monkeypatch.setattr(cli_main, "_this_dir", tmp_path, raising=True)
        secrets = tmp_path / "Auth" / "secrets.json"
        secrets.parent.mkdir(parents=True, exist_ok=True)
        secrets.write_text(json.dumps({"username": "u@x"}), encoding="utf-8")

        assert cli_main._clear_stale_tokens_for_reauth() is None

    def test_the_cleared_file_keeps_owner_only_permissions(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:  # type: ignore[no-untyped-def]
        """The clear writes a token file, so it goes through the atomic writer.

        The old implementation used a plain ``open(..., "w")``: not atomic, and
        it left the mode to the umask on a file that still holds the username
        and, after a restore, the tokens again.
        """
        cli_main, secrets = self._prepare(tmp_path, monkeypatch)

        cli_main._clear_stale_tokens_for_reauth()

        assert secrets.stat().st_mode & 0o777 == 0o600

    def test_a_failed_restore_write_says_so_instead_of_raising(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:  # type: ignore[no-untyped-def]
        """The restore runs while an exception is already on its way out.

        It is called from the ``except`` arm in ``_main``, so raising here would
        replace the reason the login ended (the cancellation, the Ctrl+C) with a
        disk error and lose the exit status that goes with it. A write that
        fails therefore reports and returns.
        """
        cli_main, secrets = self._prepare(tmp_path, monkeypatch)

        restore = cli_main._clear_stale_tokens_for_reauth()
        assert restore is not None

        def _fail(path: object, data: object) -> None:
            raise OSError("read-only file system")

        monkeypatch.setattr(cli_main, "_atomic_write_json", _fail)

        restore()  # must not raise

        assert "Could not restore the previous tokens" in capsys.readouterr().out
        # The cleared state is what is left on disk; the message is what tells
        # the user their next move.
        assert json.loads(secrets.read_text(encoding="utf-8")) == {"username": "u@x"}


# ---------------------------------------------------------------------------
# docker-login/README.md: the two restore messages, rendered not retyped
# ---------------------------------------------------------------------------
#
# The root AGENTS.md rule on quoted program output ends with "Extend the guard
# when you quote a new message there". The `--reauth` paragraph of the guide
# quotes two of the four messages `main._restore_cleared_tokens` can print (the
# successful and the declined restore), so those two are guarded here; the other
# two -- unreadable file, failed write -- the guide paraphrases. The rule's
# stated extent is what makes this necessary: the two guards in
# `tests/test_auth_flow.py` cover the `LoginAborted` sentences and explicitly
# say nothing about the `--reauth` paragraph that sits in the same section.
#
# Deliberately a near-copy of `_documented_quotes` in `tests/test_auth_flow.py`
# rather than a shared helper: the two read the same section of the same file
# but answer different questions, and the semantics have already diverged (this
# one pairs a trigger phrase with the lead-in). Merging them across two test
# modules is its own change, with its own risk of making both weaker.


def _reauth_documented_quotes() -> list[tuple[str, str]]:
    """Return (lead-in prose, fenced block) pairs from "Cancelling a login".

    Both halves are whitespace-collapsed so the guide may wrap however it likes;
    the messages themselves are single lines that the guide has to break.

    Pairing prose with the block that *directly follows it* is the guard. The
    section holds four fenced blocks, two of them this module's; "the message
    appears somewhere in the section" would pass on a guide that files the
    declined-restore text under the successful-restore lead-in, which is the
    conflation the rule exists to catch.
    """
    from custom_components.googlefindmy import main as cli_main

    readme = Path(cli_main.__file__).resolve().parent / "docker-login" / "README.md"
    assert readme.is_file(), f"missing guide: {readme}"
    text = readme.read_text(encoding="utf-8")
    marker = "\n## Cancelling a login\n"
    assert marker in text, (
        f"{readme} no longer has a '## Cancelling a login' section; this guard "
        "and the one in tests/test_auth_flow.py both anchor on that heading."
    )
    body = text[text.index(marker) + len(marker) :]
    end = body.find("\n## ")
    section = body if end == -1 else body[:end]

    parts = section.split("```")
    assert len(parts) % 2 == 1, (
        "unbalanced ``` fences in the 'Cancelling a login' section: "
        f"{len(parts) - 1} delimiters found"
    )
    return [
        (" ".join(parts[i - 1].split()), _fenced_body(parts[i]))
        for i in range(1, len(parts), 2)
    ]


def _fenced_body(raw: str) -> str:
    """Collapse a fenced block, dropping an optional info string.

    ``\u0060\u0060\u0060text`` and ``\u0060\u0060\u0060console`` are ordinary Markdown and say nothing
    about the output; folding the tag into the body would make an exact match
    fail on a guide that is right. The opening fence of an untagged block leaves
    an empty first line, so nothing is dropped there.
    """
    lines = raw.split("\n")
    head = lines[0].strip() if lines else ""
    if head and " " not in head:
        lines = lines[1:]
    return " ".join(" ".join(lines).split())


def _rendered_restore_message(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    *,
    fresh_anchor: bool,
) -> str:
    """Drive the real restore and return the line the user actually sees."""
    from custom_components.googlefindmy import main as cli_main

    monkeypatch.setattr(cli_main, "_this_dir", tmp_path, raising=True)
    secrets = tmp_path / "Auth" / "secrets.json"
    secrets.parent.mkdir(parents=True, exist_ok=True)
    secrets.write_text(
        json.dumps(
            {
                "username": "u@x",
                "oauth_token": "o",
                "aas_token": "a",
                "owner_key": "ok",
            }
        ),
        encoding="utf-8",
    )

    restore = cli_main._clear_stale_tokens_for_reauth()
    assert restore is not None
    if fresh_anchor:
        secrets.write_text(
            json.dumps({"username": "u@x", "oauth_token": "fresh"}), encoding="utf-8"
        )
    capsys.readouterr()  # drop the "Cleared N cached token(s)" line
    restore()
    return " ".join(capsys.readouterr().out.split())


# Per outcome: the sentence the block must directly follow, and the phrase that
# names *when* this outcome happens. The lead-in alone is not enough -- swap the
# trigger sentence for another path's and the guide sends a user looking for
# words that path cannot emit, while a lead-in-only guard stays green (measured).
_RESTORE_OUTCOMES = {
    "restored": (
        "the cleared tokens are put back and the run says so:",
        "If that login then ends without a token",
        False,
    ),
    "declined": (
        "a mix of the two would send the next run back to the old account:",
        "The login already stored a new token",
        True,
    ),
}


@pytest.mark.parametrize("kind", sorted(_RESTORE_OUTCOMES))
def test_the_guide_quotes_each_reauth_restore_message(
    kind: str,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Each restore outcome is documented with the text it can actually emit.

    The counts and the anchor name are interpolated, so retyping the sentence in
    the guide is how it drifts. Rendering it through the real function and then
    locating it under its own lead-in is what fails when the wording, the key
    names, or the introducing sentence change.

    Measured extent, stated so it is not mistaken for coverage: two of the four
    messages ``_restore_cleared_tokens`` can print, inside one named section of
    one file. The unreadable-file and failed-write messages stay unguarded --
    the guide paraphrases them rather than quoting them. The token count in the
    restored message is the one this fixture produces (three keys); a real cache
    holds more, so the guide's number documents the shape of the line, not a
    figure a user should expect to match.
    """
    lead_in, trigger, fresh_anchor = _RESTORE_OUTCOMES[kind]
    message = _rendered_restore_message(
        tmp_path, monkeypatch, capsys, fresh_anchor=fresh_anchor
    )
    assert message, "the restore printed nothing to render"

    quotes = _reauth_documented_quotes()
    introduced = [(prose, block) for prose, block in quotes if prose.endswith(lead_in)]
    assert introduced, (
        f"no fenced block follows the {kind} lead-in in the guide. Without the "
        "lead-in both messages could sit under one sentence and the guide would "
        f"be as wrong as before. Lead-ins found: {[p[-70:] for p, _ in quotes]}"
    )
    for prose, _ in introduced:
        assert trigger in prose, (
            f"the block for the {kind} outcome no longer says when it happens. "
            f"Expected the prose before it to contain {trigger!r}. A guard that "
            "only matched the closing clause let the trigger be swapped for "
            f"another path's. Prose found: {prose[-200:]!r}"
        )
        for other, (_, other_trigger, _) in _RESTORE_OUTCOMES.items():
            if other != kind:
                assert other_trigger not in prose, (
                    f"the prose introducing the {kind} block also announces the "
                    f"{other} outcome ({other_trigger!r}), so a reader cannot "
                    "tell which output belongs to which situation."
                )
    # Exactly one block, and it is the message. Two weaker shapes were measured
    # and rejected: ``in`` passes on a block that also promises a backup file the
    # program never writes, and ``any(... == ...)`` passes when a second block
    # under a duplicated lead-in carries that promise instead.
    assert [block for _, block in introduced] == [message], (
        f"the {kind} outcome is not documented by exactly one fenced block "
        f"holding its message and nothing else. Quote it verbatim, alone, in a "
        f"fenced block directly after {lead_in!r}. Expected: [{message!r}]. "
        f"Found: {[b for _, b in introduced]!r}"
    )

    # Presence alone permits the conflation inverted: a block carrying BOTH
    # sentences documents this outcome as producing the other one too.
    stray = _rendered_restore_message(
        tmp_path / "other", monkeypatch, capsys, fresh_anchor=not fresh_anchor
    )
    assert all(stray not in block for _, block in introduced), (
        f"the block introduced by {lead_in!r} also quotes the other outcome's "
        f"message. Each outcome gets its own block, or a reader is told to "
        f"expect output this one cannot produce: {stray!r}"
    )
