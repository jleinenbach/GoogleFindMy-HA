# tests/test_diagnostics_secret_bundle.py
"""The credential bundle must never reach a diagnostics download.

`entry.data` carries the whole `secrets.json` bundle between the config flow and
the migration in `async_setup_entry`. If setup fails in between — which is
exactly when a user downloads diagnostics and pastes them into a public issue —
the bundle is still there, and `async_get_config_entry_diagnostics` copies
`dict(entry.data)` into its payload.
"""

from __future__ import annotations

import ast
import re
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.googlefindmy import diagnostics
from custom_components.googlefindmy.const import DATA_SECRET_BUNDLE, DOMAIN
from custom_components.googlefindmy.diagnostics import (
    TO_REDACT,
    TO_REDACT_PREFIXES,
    async_redact_data,
)
from custom_components.googlefindmy.redaction import REDACTED, RUNTIME_KEY_STEMS
from tests.helpers.config_entries_stub import make_config_entry

_SECRET = "0123456789abcdef" * 4


def _redact(payload: dict[str, object]) -> dict[str, object]:
    return async_redact_data(payload, TO_REDACT, TO_REDACT_PREFIXES)


def test_the_bundle_is_redacted_as_a_mapping() -> None:
    payload = {DATA_SECRET_BUNDLE: {"aas_token": _SECRET, "shared_key": _SECRET}}

    assert _redact(payload)[DATA_SECRET_BUNDLE] == REDACTED


def test_the_bundle_is_redacted_as_a_json_string() -> None:
    """A legacy bundle arrives as a string, where key redaction cannot reach in."""

    payload = {DATA_SECRET_BUNDLE: '{"aas_token": "' + _SECRET + '"}'}

    redacted = _redact(payload)

    assert redacted[DATA_SECRET_BUNDLE] == REDACTED
    assert _SECRET not in str(redacted)


def test_the_two_keys_that_decrypt_locations_are_redacted() -> None:
    payload = {"shared_key": _SECRET, "owner_key": _SECRET, "scanned_data": _SECRET}

    redacted = _redact(payload)

    assert all(value == REDACTED for value in redacted.values())


def test_the_account_address_does_not_survive_in_the_key_name() -> None:
    """Redacting the value leaves the address standing in the property name.

    These names are built as `<what>_<e-mail>`, and the diagnostics file is the
    one people attach to public issues, so the address has to go from the name
    as well. The numbering is shared across nesting levels, so a reader can
    still tell which entries belong to the same account.
    """

    payload = {
        # The address is in the payload, as it is in a real diagnostics file
        # (`entry.data` carries the account e-mail), so the key name can be
        # cleaned exactly instead of by guessing.
        "username": "user@example.com",
        "adm_token_user@example.com": _SECRET,
        "aas_token_issued_at_user@example.com": 1,
        "nested": {"spot_token_other@example.org": _SECRET},
    }

    redacted = _redact(payload)

    assert "example.com" not in str(redacted)
    assert "example.org" not in str(redacted)
    assert "adm_token_<account-1>" in redacted
    # The same account keeps the same number, and `issued_at` survives.
    assert "aas_token_issued_at_<account-1>" in redacted
    # `other@example.org` appears nowhere as a value; the stem `spot_token_`
    # still tells where it begins, so the readable part survives.
    assert "spot_token_<account-2>" in redacted["nested"]


def test_an_underscored_local_part_leaves_no_fragment() -> None:
    """`john_doe@example.com` must not become `adm_token_john_<account-1>`.

    Guessing where the name ends and the address begins is what produced that
    fragment. The address is taken from the payload's own values instead, and
    anything still address-shaped afterwards is removed greedily, even at the
    cost of an unreadable key.
    """

    payload = {
        "username": "john_doe@example.com",
        "adm_token_john_doe@example.com": _SECRET,
        "aas_token_issued_at_john_doe@example.com": 1,
        # No matching value anywhere: the greedy fallback has to take it.
        "spot_token_jane_roe@example.org": _SECRET,
    }

    redacted = _redact(payload)

    text = str(redacted)
    for fragment in ("john", "doe", "jane", "roe", "example.com", "example.org"):
        assert fragment not in text, fragment
    # The readable part survives where the address was known.
    assert "aas_token_issued_at_<account-1>" in redacted


def test_a_slash_in_the_local_part_does_not_survive() -> None:
    """`first/last@example.com` is a valid address, and hosted domains use it.

    The pattern that searches *inside* a key name stops at a slash on purpose,
    so that a path-shaped name cannot be swallowed whole. That made it reject
    such an address outright and leave `adm_token_first/` standing. The
    whole-string pattern that reads the payload's own values has no such
    constraint and does the exact replacement.
    """

    payload = {
        "username": "first/last@example.com",
        "adm_token_first/last@example.com": _SECRET,
        "aas_token_issued_at_first/last@example.com": 1,
    }

    redacted = _redact(payload)

    text = str(redacted)
    for fragment in ("first", "last", "example.com"):
        assert fragment not in text, fragment
    assert "aas_token_issued_at_<account-1>" in redacted


def test_an_anonymised_name_does_not_evict_a_later_literal_key() -> None:
    """Which field survives must not depend on the order they were inserted.

    `user@example.com` as a key anonymises to `<account-1>`; a literal key of
    that name arriving afterwards was not checked for a collision, because the
    check ran only for keys that had been rewritten. The later one then
    replaced the earlier one silently.
    """

    payload = {
        "username": "user@example.com",
        "user@example.com": "first",
        "<account-1>": "second",
    }

    redacted = _redact(payload)

    # The two keys must really have collided, or this test proves nothing.
    assert "<account-1>-2" in redacted
    assert len(redacted) == 3
    assert set(redacted.values()) >= {"first", "second"}


def test_run_time_key_names_are_redacted_by_prefix() -> None:
    """These names are built from the account e-mail and cannot be listed."""

    payload = {
        "adm_token_user@example.com": _SECRET,
        "spot_token_user@example.com": _SECRET,
        "android_id_user@example.com": _SECRET,
        "aas_token_issued_at_user@example.com": 1,
        "owner_key_user@example.com": _SECRET,
        "shared_key_user@example.com": _SECRET,
    }

    redacted = _redact(payload)

    assert _SECRET not in str(redacted)
    assert all(value == REDACTED for value in redacted.values())


def test_namespaced_run_time_key_names_are_redacted() -> None:
    """The token cache prefixes its keys with the entry id and a colon."""

    payload = {
        "entry-1:adm_token_user@example.com": _SECRET,
        "entry-1:android_id_user@example.com": _SECRET,
        "entry-1:aas_token": _SECRET,
        "entry-1:oauth_token": _SECRET,
        # Matched only by the exact rule on the bare name, no prefix covers it.
        "entry-1:username": _SECRET,
    }

    redacted = _redact(payload)

    assert _SECRET not in str(redacted)
    assert "user@example.com" not in str(redacted)
    assert all(value == REDACTED for value in redacted.values())


def test_a_colon_in_the_local_part_does_not_defeat_either_rule() -> None:
    """A quoted local part may contain ``:``; that is no namespace separator."""

    payload = {
        'adm_token_"first:tag"@example.com': _SECRET,
        'entry-1:adm_token_"first:tag"@example.com': _SECRET,
    }

    redacted = _redact(payload)

    assert _SECRET not in str(redacted)
    assert all(value == REDACTED for value in redacted.values())


@pytest.mark.asyncio
async def test_the_diagnostics_dump_applies_both_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end: the dump itself must pass the prefix rule, not only the helper.

    A setup that failed before the migration leaves the bundle and run-time
    keys in ``entry.data``; that is exactly when diagnostics get downloaded.
    """

    async def _fake_get_integration(_hass: Any, _domain: str) -> SimpleNamespace:
        return SimpleNamespace(name="Test Integration", version="1.2.3")

    monkeypatch.setattr(diagnostics, "async_get_integration", _fake_get_integration)
    monkeypatch.setattr(
        diagnostics.dr, "async_get", lambda _hass: SimpleNamespace(devices={})
    )
    monkeypatch.setattr(
        diagnostics.er, "async_get", lambda _hass: SimpleNamespace(entities={})
    )
    entry = make_config_entry(
        domain=DOMAIN,
        data={
            DATA_SECRET_BUNDLE: {"aas_token": _SECRET, "shared_key": _SECRET},
            "adm_token_user@example.com": _SECRET,
            "entry-1:spot_token_user@example.com": _SECRET,
        },
        runtime_data=SimpleNamespace(coordinator=None),
    )
    hass = SimpleNamespace(data={DOMAIN: {}})

    payload = await diagnostics.async_get_config_entry_diagnostics(hass, entry)

    assert _SECRET not in str(payload)
    assert "user@example.com" not in str(payload)


def test_a_slash_in_the_local_part_is_taken_whole_without_a_value() -> None:
    """The stem tells where the address begins, a matching value is not needed.

    The pattern that searches inside a name stops at `/`, so without the
    address as a value it left `adm_token_first/` standing.
    """

    redacted = _redact({"adm_token_first/last@example.com": _SECRET})

    assert list(redacted) == ["adm_token_<account-1>"]


def test_a_longer_address_is_not_cut_by_a_registered_suffix() -> None:
    """`user@example.com` is the tail of `xuser@example.com`, not the same account."""

    payload = {
        "username": "user@example.com",
        # No stem: rule 2 must not take `user@` out of `xuser@`. First in the
        # payload, because the stem key below registers `xuser@` on its way
        # and would hide a missing boundary from this assertion.
        "xuser@example.com": 1,
        "adm_token_xuser@example.com": _SECRET,
    }

    redacted = _redact(payload)

    assert "xuser" not in str(redacted)
    # Its own account, not the registered one (`<account-1>`).
    assert "adm_token_<account-1>" not in redacted
    assert any(str(key).startswith("adm_token_<account-") for key in redacted)
    assert "x<account-" not in str(redacted)


def test_a_registered_address_does_not_match_inside_a_longer_domain() -> None:
    payload = {
        "username": "user@example.co",
        # No stem: rule 2 must not take `example.co` out of `example.com`.
        # First for the same reason as in the test above.
        "user@example.com": 1,
        "adm_token_user@example.com": _SECRET,
    }

    redacted = _redact(payload)

    # Its own account, not the registered one (`<account-1>`).
    assert "adm_token_<account-1>" not in redacted
    assert any(str(key).startswith("adm_token_<account-") for key in redacted)
    assert "<account-1>m" not in str(redacted)


def test_a_known_address_after_a_namespace_keeps_the_namespace() -> None:
    """Rule 2 replaces a known address exactly where it stands alone."""

    payload = {
        "username": "first/last@example.com",
        "ns:first/last@example.com": 1,
    }

    redacted = _redact(payload)

    assert "ns:<account-1>" in redacted
    assert "first" not in str(redacted)


def test_a_known_address_inside_a_longer_local_part_leaves_no_piece() -> None:
    """`_` and `/` may belong to a local part, so they are no boundary.

    `seen_a_b@example.com` may be the address `a_b@example.com`; taking out
    the registered `b@example.com` would leave `seen_a_` and name the wrong
    account. Rule 3 replaces such a name whole.
    """

    payload = {
        "username": "b@example.com",
        "seen_a_b@example.com": 1,
        "seen_a/b@example.com": 2,
    }

    redacted = _redact(payload)

    assert "seen_a" not in str(redacted)
    assert "example.com" not in str(redacted)


def test_an_address_before_a_namespaced_stem_is_replaced_too() -> None:
    """Rule 1 used to return at the stem and leave the part before `:` alone."""

    payload = {
        "username": "a@example.com",
        "adm_token_a@example.com:adm_token_b@example.com": _SECRET,
        "c@example.com:android_id_d@example.com": _SECRET,
    }

    redacted = _redact(payload)

    assert "example.com" not in str(redacted)
    assert "adm_token_<account-1>:adm_token_<account-2>" in redacted
    assert "<account-3>:android_id_<account-4>" in redacted


def test_a_quoted_local_part_with_a_space_is_taken_whole() -> None:
    redacted = _redact({'adm_token_"first last"@example.com': _SECRET})

    assert list(redacted) == ["adm_token_<account-1>"]


def test_no_word_with_an_at_sign_survives_in_a_name() -> None:
    """Two addresses in one name, or one without a dot in the domain."""

    payload = {
        "adm_token_a@example.com_b@example.org": _SECRET,
        "seen_by_user@localhost": 1,
    }

    redacted = _redact(payload)

    text = str(redacted)
    for fragment in ("@", "example", "localhost"):
        assert fragment not in text, fragment


def test_case_does_not_split_one_account() -> None:
    """The entry keeps the address as typed, some names are built lower-cased."""

    payload = {
        "username": "User@Example.com",
        "adm_token_user@example.com": _SECRET,
        "spot_token_USER@EXAMPLE.COM": _SECRET,
    }

    redacted = _redact(payload)

    assert "adm_token_<account-1>" in redacted
    assert "spot_token_<account-1>" in redacted
    assert "<account-2>" not in str(redacted)


def test_tuples_are_walked() -> None:
    """A tuple used to be passed through whole, secret values included."""

    payload = {
        "pair": (
            {"adm_token_user@example.com": _SECRET, "username": "user@example.com"},
            "plain",
        )
    }

    redacted = _redact(payload)

    assert redacted["pair"] == (
        {"adm_token_<account-1>": REDACTED, "username": REDACTED},
        "plain",
    )


def test_an_address_inside_a_tuple_is_numbered_like_any_other() -> None:
    """Registration walks tuples too, so the numbering follows the payload order."""

    payload = {
        "accounts": ("a@example.com",),
        "adm_token_b@example.com": _SECRET,
        "adm_token_a@example.com": _SECRET,
    }

    redacted = _redact(payload)

    assert list(redacted)[1:] == ["adm_token_<account-2>", "adm_token_<account-1>"]


def test_a_lone_quote_in_the_local_part_leaves_no_piece() -> None:
    payload = {
        'adm_token_x"y@example.com': _SECRET,
        'seen_john"doe@example.com': 1,
    }

    redacted = _redact(payload)

    assert "adm_token_<account-1>" in redacted
    for fragment in ('x"', "john", "doe", "example"):
        assert fragment not in str(redacted), fragment


def test_an_escaped_quote_inside_a_quoted_local_part_ends_nothing() -> None:
    """`"first\\" last"@example.com`: the `\\"` is a quoted pair, not the end."""

    payload = {
        'adm_token_"first\\" last"@example.com': _SECRET,
        'seen_"first\\" last"@example.com': 1,
    }

    redacted = _redact(payload)

    assert "adm_token_<account-1>" in redacted
    for fragment in ("first", "last", "example"):
        assert fragment not in str(redacted), fragment


def test_a_backslash_does_not_stop_the_replacement() -> None:
    """A quoted pair in the local part, and a backslash in the domain."""

    payload = {
        'adm_token_"a\\"b"@example.com': _SECRET,
        "adm_token_user@exa\\mple.com": _SECRET,
    }

    redacted = _redact(payload)

    assert "adm_token_<account-1>" in redacted
    for fragment in ("@", "a\\", "mple", "example"):
        assert fragment not in str(redacted), fragment


def test_a_very_long_name_skips_rule_1_and_still_loses_every_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rule 1 grows with the square of the length; past the bound it is skipped.

    Counted, not timed: the stem search must not run for such a name.
    """

    from custom_components.googlefindmy import redaction

    calls: list[str] = []
    real = redaction._stem_cut

    def counting(key: str) -> tuple[int, str] | None:
        calls.append(key)
        return real(key)

    monkeypatch.setattr(redaction, "_stem_cut", counting)
    long_key = "adm_token_a@example.com:" * 3000 + "adm_token_b@example.com"
    short_key = "adm_token_a@example.com:adm_token_b@example.com"

    redacted = _redact({short_key: _SECRET, long_key: _SECRET})

    assert "@" not in str(list(redacted))
    assert long_key not in calls
    assert short_key in calls
    assert "adm_token_<account-1>:adm_token_<account-2>" in redacted


def test_a_named_tuple_keeps_its_type() -> None:
    pair = namedtuple("pair", "first second")

    redacted = _redact({"pair": pair({"adm_token": _SECRET}, "plain")})

    assert type(redacted["pair"]) is pair
    assert redacted["pair"].first == {"adm_token": REDACTED}


def test_two_passes_share_the_numbering_when_asked() -> None:
    accounts: dict[str, str] = {}
    first = async_redact_data(
        {"username": "b@example.com"}, TO_REDACT, TO_REDACT_PREFIXES, accounts
    )
    second = async_redact_data(
        {"adm_token_a@example.com": _SECRET, "adm_token_b@example.com": _SECRET},
        TO_REDACT,
        TO_REDACT_PREFIXES,
        accounts,
    )

    assert first == {"username": REDACTED}
    assert list(second) == ["adm_token_<account-2>", "adm_token_<account-1>"]


@pytest.mark.asyncio
async def test_the_final_pass_alone_redacts_what_only_it_sees(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The prefix list of the last call is bound by this test and no other.

    The FCM receiver state never passes through the `effective_config` call,
    so only `return async_redact_data(payload, ...)` can redact it. Its key
    order also pins the shared numbering: `b@` is numbered in the first pass.
    """

    async def _fake_get_integration(_hass: Any, _domain: str) -> SimpleNamespace:
        return SimpleNamespace(name="Test Integration", version="1.2.3")

    monkeypatch.setattr(diagnostics, "async_get_integration", _fake_get_integration)
    monkeypatch.setattr(
        diagnostics.dr, "async_get", lambda _hass: SimpleNamespace(devices={})
    )
    monkeypatch.setattr(
        diagnostics.er, "async_get", lambda _hass: SimpleNamespace(entities={})
    )
    monkeypatch.setattr(
        diagnostics,
        "_fcm_receiver_state",
        lambda _hass: {
            "adm_token_a@example.com": _SECRET,
            "adm_token_b@example.com": _SECRET,
        },
    )
    entry = make_config_entry(
        domain=DOMAIN,
        data={"username": "b@example.com"},
        runtime_data=SimpleNamespace(coordinator=None),
    )
    hass = SimpleNamespace(data={DOMAIN: {}})

    payload = await diagnostics.async_get_config_entry_diagnostics(hass, entry)

    assert _SECRET not in str(payload)
    assert "example.com" not in str(payload)
    assert list(payload["fcm_receiver_state"]) == [
        "adm_token_<account-2>",
        "adm_token_<account-1>",
    ]


_ADDRESS_WORDS = ("user", "mail", "account")
_STEM_TAIL = re.compile(r"[a-z][a-z0-9_]*_$")


def _module_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = "text"`` bindings, used as stems in f-strings."""

    constants: dict[str, str] = {}
    for node in tree.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            for target in targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = value.value
    return constants


def _pieces(node: ast.expr, constants: dict[str, str]) -> list[str | ast.expr]:
    """Flatten an f-string or a ``+`` chain into text and expressions.

    A name bound to a module-level string is resolved to its text, and
    neighbouring text is joined, so `f"{_BASE}_{user}"` yields one stem.
    """

    raw: list[str | ast.expr] = []
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        raw = [node.value]
    elif isinstance(node, ast.Name) and node.id in constants:
        raw = [constants[node.id]]
    elif isinstance(node, ast.JoinedStr):
        for part in node.values:
            if isinstance(part, ast.FormattedValue):
                raw.extend(_pieces(part.value, constants))
            else:
                raw.extend(_pieces(part, constants))
    elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        raw = _pieces(node.left, constants) + _pieces(node.right, constants)
    else:
        raw = [node]
    joined: list[str | ast.expr] = []
    for piece in raw:
        if isinstance(piece, str) and joined and isinstance(joined[-1], str):
            joined[-1] += piece
        else:
            joined.append(piece)
    return joined


def _package_sources() -> list[tuple[str, str]]:
    root = Path(diagnostics.__file__).parent
    return [
        (path.name, path.read_text(encoding="utf-8"))
        for path in sorted(root.rglob("*.py"))
    ]


def _key_builders(
    sources: list[tuple[str, str]],
) -> tuple[set[str], set[str], list[str]]:
    """Return stems, address variables and unresolved sites in ``sources``."""

    stems: set[str] = set()
    variables: set[str] = set()
    unresolved: list[str] = []
    for name, source in sources:
        tree = ast.parse(source)
        constants = _module_constants(tree)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.JoinedStr, ast.BinOp)):
                continue
            pieces = _pieces(node, constants)
            for before, after in zip(pieces, pieces[1:], strict=False):
                if isinstance(after, str):
                    continue
                variable = ast.unparse(after).lower()
                if not any(word in variable for word in _ADDRESS_WORDS):
                    continue
                if not isinstance(before, str) or not before.endswith("_"):
                    continue
                match = _STEM_TAIL.search(before)
                if match is None:
                    unresolved.append(f"{name}:{node.lineno}")
                    continue
                stems.add(match.group(0))
                variables.add(variable)
    return stems, variables, unresolved


def test_every_run_time_stem_of_the_token_cache_is_listed() -> None:
    """A new `<stem>_<user>` key in the package must reach `RUNTIME_KEY_STEMS`.

    Without the stem, rule 1 does not apply and the name falls back to the
    pattern rules, which replace a name without a stem whole: nothing leaks,
    the name only becomes unreadable. That is why this guard keeps a list of
    what it sees, not a proof that nothing else exists.

    Seen: f-strings with any number of parts and ``+`` chains, where text
    ending in ``_`` directly precedes an expression whose source contains
    ``user``, ``mail`` or ``account``; a stem held in a module-level string
    constant is resolved.

    Not seen: a variable named otherwise (``acct``, ``login``, ``owner``), a
    stem held in an attribute or returned by a call, a separator other than
    ``_``, ``+=``, ``str.join``, and ``%`` or ``str.format`` spread over more
    than one line. Single-line ``%`` and ``str.format`` forms are caught by
    the next test. A broader rule was measured and dropped: flagging every
    address variable that does not follow ``_`` hits nine places in the
    package that build log lines or ids, not cache keys.
    """

    stems, variables, unresolved = _key_builders(_package_sources())

    # Positive controls, one per form the walk claims to see. No `+` chain
    # builds a key in the package today, so that form is checked on a sample.
    assert _key_builders([("sample.py", 'k = "probe_" + username')])[0] == {"probe_"}
    assert "adm_token_" in stems  # two-part f-string
    assert "adm_best_ttl_sec_" in stems  # three parts, `{self._ns}` first
    assert "shared_key_" in stems  # `f"{_CACHE_KEY_BASE}_{username}"`
    assert "email_key" in variables  # the word list reaches `mail`
    assert unresolved == [], unresolved
    missing = sorted(stems - set(RUNTIME_KEY_STEMS))
    assert not missing, f"add {missing} to RUNTIME_KEY_STEMS in redaction.py"


def test_key_names_are_not_built_with_percent_or_format() -> None:
    """The stem walk does not parse these two forms, so they must not occur.

    Checked line by line: a ``%`` or ``.format(`` on the next line is not seen.
    """

    pattern = re.compile(r"""_%(?:\([^)]*\))?[a-z]["']\s*%|_\{[^}]*\}["']\.format\(""")
    # Positive control: the pattern has to see every form it claims.
    for sample in (
        '"adm_token_%s" % user',
        '"adm_token_%(u)s" % values',
        '"adm_token_{}".format(user)',
        '"adm_token_{0}".format(user)',
        '"adm_token_{u}".format(u=user)',
    ):
        assert pattern.search(sample), sample
    hits = [
        f"{name}:{number}"
        for name, source in _package_sources()
        for number, line in enumerate(source.splitlines(), 1)
        if pattern.search(line)
    ]

    assert hits == []


def test_harmless_keys_survive() -> None:
    """A diagnostics file that redacts everything is useless."""

    payload = {"poll_interval": 300, "enable_stats_entities": True, "nested": {"a": 1}}

    assert _redact(payload) == payload


def test_fcm_credential_material_is_redacted_by_name() -> None:
    """These are fixed key names, so they are matched exactly, not by prefix."""

    payload = {
        "fcm_credentials": {"gcm": {"security_token": _SECRET}},
        "fcm_creds": {"gcm": {"security_token": _SECRET}},
        "fcm_installation": _SECRET,
        "fcm_registration": _SECRET,
    }

    redacted = _redact(payload)

    assert _SECRET not in str(redacted)
    assert all(value == REDACTED for value in redacted.values())


def test_fcm_health_fields_survive() -> None:
    """The push diagnostics are the reason people download this file.

    A blanket ``fcm_`` prefix rule would blank the receiver state, the status
    snapshot and the counters this module builds itself, which is precisely the
    information needed to explain a push failure.
    """

    payload = {
        "fcm_receiver_state": "connected",
        "fcm_status": {"connected": True, "last_error": None},
        "fcm_lock_contention_count": 3,
        "fcm_acquisition_duration_seconds": 1.25,
        "fcm_push_enabled": True,
    }

    assert _redact(payload) == payload
