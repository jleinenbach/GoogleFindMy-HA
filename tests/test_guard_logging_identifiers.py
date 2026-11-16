# tests/test_guard_logging_identifiers.py
"""Guard: no log call in the integration passes a class (c) identifier in clear text.

`AGENTS.md` section 5 grades device identifiers by one question: can somebody
who holds only the log file use the value without owning this Home Assistant
instance or this Google account? Class (c) (hardware addresses, stable
identifiers of third parties) is never logged in clear text; class (a)
(rotating identifiers) and class (b) (registry identifiers of this instance,
the operator's own Google canonical ids) are allowed.

This guard watches class (c) only. Class (b) is not watched: 253 DEBUG sites
carry `entry_id`/`device_id` values that are contract-conformant, and a guard
over them would have to whitelist every single one. Class (c) is recognised
at the identifier: a leaf named like an address (`ble_address`, `mac`,
`scanner_address`) or the bare Bermuda `scanner` name. A value counts as
handled only when a masking helper wraps it (same rule as the account-address
guard in `test_config_flow_basics.py`); any other wrapper still leaks.

Phone values are recognised the same way: a leaf named like one of the phone
secrets (`_PHONE_SECRET_NAME`), an `android_id`/`androidId` outside `Auth/`, and
every field of `decoder._SECRET_PROTO_FIELDS` in schema or snake_case spelling,
compared on the whole leaf. The set is derived, so a field the decoder hides is
watched here without an edit.

Blind spots, stated so that a green run is not read as more: a field named by a
string argument (`data.get("imei")`, `getattr(rec, "imei")`, `data[KEY_IMEI]`),
a longer name that contains a field name (`device_imei`), and a whole message
passed to the log (`str(phone_info)`, `MessageToDict(meta)`). Ordinary words in
the set (`encrypted`, `iv`, `email`) also match harmless uses such as
`len(iv)`; such a site is masked or listed in `_NOT_AN_ADDRESS` with its reason.

The scan is package-wide and covers all log levels, because a clear-text
address is equally wrong at DEBUG and at WARNING. Written as an AST walk
rather than a pin on the sites that were found, because the leak reappears
with every new log line, in a shape nobody predicted.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from custom_components import googlefindmy
from custom_components.googlefindmy.ProtoDecoders import decoder

_PACKAGE_ROOT = Path(googlefindmy.__file__).resolve().parent

# Leaf identifiers that carry a class (c) value. `(^|_)mac(_|$)` and
# `address` cover hardware addresses in every spelling the tree uses;
# `^scanner$` is the bare Bermuda scanner name (the scanner device's name,
# which falls back to a slug of its MAC, see `_mask_address_for_logs`).
# `scanner_device_id` is a Home Assistant registry id (class (b)) and is
# deliberately not matched.
_ADDRESS_NAME = re.compile(r"(^|_)mac(_|$)|address|^scanner$", re.IGNORECASE)

# Values of phones listed without a canonical ID: the
# phone's Wi-Fi name, its SIM name, the per-request locate key, the per-entry
# key behind the phone row ids, ciphertext and raw pushes. Only names that are
# unambiguous across the package: a bare `android_id` is the integration's own
# GSF ID in `Auth/` (logged there by design) and a bare `key` is mostly a dict
# key, so the phone modules carry their own, stricter guard in
# `tests/test_phone_log_hygiene.py`. `phone_target` names the table from row id
# to Android ID (`api.py`), whose values are class (c).
_PHONE_SECRET_NAME = re.compile(
    r"ssid|wifi|sim_display|locate_key|phone_id_key|ciphertext|push_hex"
    r"|phone_android_id|phone_status|phone_target",
    re.IGNORECASE,
)

# Outside `Auth/` an `android_id` (also `androidId`) is a phone's Android ID
# (class (c)); inside it is the integration's own GSF ID and stays allowed.
_ANDROID_ID_NAME = re.compile(r"android_?id", re.IGNORECASE)


def _spelling_key(name: str) -> str:
    """One key for the camelCase and the snake_case spelling of a name."""
    return name.replace("_", "").lower()


# Fields of the protobuf schema that the decoder never shows
# (`decoder._SECRET_PROTO_FIELDS`, kept complete by
# `tests/test_decoder_spot_schema.py`). A value read from such a field is a
# secret or personal data whatever the code calls it, so the field name itself
# is a leaf to watch, in the schema spelling (`simDisplayName`,
# `phoneInformationField1`) and in snake_case (`sim_display_name`). Derived, not
# listed: a field added to the decoder's set is watched here without an edit.
# Matched on the whole leaf, not as a substring: `iv` and `email` are fields of
# that set and must not match `active` or `email_hash`.
_SECRET_FIELD_KEYS: frozenset[str] = frozenset(
    _spelling_key(name) for name in decoder._SECRET_PROTO_FIELDS
)
_OWN_ANDROID_ID_DIR = "Auth/"

# Identifiers that match the pattern but are not addresses. Empty on purpose:
# an entry is added only when the red probe below reports a false positive,
# never as a stock of guesses.
_NOT_AN_ADDRESS: frozenset[str] = frozenset()

# Helpers whose return value is log-safe.
_MASKERS = frozenset(
    {"_mask_address_for_logs", "_mask_email_for_logs", "_redact_account_for_log"}
)

# Reviewed sites that log a class (a) value under an address-like name:
# (path relative to the package, leaf name). `ble_address` in the BLE scanner
# is `service_info.address`, the MAC of an FMDN advertisement. The resolved
# branch logs it in full: the advertisement belongs to one of the user's own
# trackers and the MAC rotates with the EID (`eid_resolver.py`,
# `EIDMatch.ble_address` docstring; `docs/FMDN.md` S3.5). The unresolved
# branch of the same module masks it (somebody else's tracker until proven
# otherwise, class (c)). The exception is keyed by (path, leaf, format-string
# prefix) so that it excuses exactly that one site and not every `ble_address`
# line of the module; `test_reviewed_rotating_sites_still_exist` fails when
# the site disappears.
# A line moved out of the resolved branch while keeping the prefix would
# still count as the one match; that case is caught by the behavioural test
# `test_callback_masks_unresolved_advertisement_mac` in
# `test_ble_scanner_contracts.py`, which drives the unresolved path and asserts
# the clear-text MAC is absent.
_REVIEWED_ROTATING: frozenset[tuple[str, str, str]] = frozenset(
    {("fmdn_finder/ble_scanner.py", "ble_address", "BLE scan: resolved")}
)

_LOG_LEVELS = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
)


def _leaks(node: ast.AST, relative: str = "") -> list[str]:
    """Names of address-carrying leaves reachable without a masker."""
    if isinstance(node, ast.Call):
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name in _MASKERS:
            return []
    found: list[str] = []
    for child in ast.iter_child_nodes(node):
        found.extend(_leaks(child, relative))
    leaf: str | None = None
    if isinstance(node, ast.Name):
        leaf = node.id
    elif isinstance(node, ast.Attribute):
        leaf = node.attr
    elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
        leaf = str(node.slice.value)
    if (
        leaf
        and (
            _ADDRESS_NAME.search(leaf)
            or _PHONE_SECRET_NAME.search(leaf)
            or _spelling_key(leaf) in _SECRET_FIELD_KEYS
            or (
                _ANDROID_ID_NAME.search(leaf)
                and not relative.startswith(_OWN_ANDROID_ID_DIR)
            )
        )
        and leaf not in _NOT_AN_ADDRESS
    ):
        found.append(leaf)
    return found


def _is_log_call(node: ast.Call) -> bool:
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr not in _LOG_LEVELS:
        return False
    receiver = ast.unparse(func.value)
    return "LOG" in receiver.upper()


def _is_reviewed(
    reviewed: frozenset[tuple[str, str, str]], relative: str, leaf: str, fmt: str
) -> bool:
    # An empty prefix would excuse every line of the module; the exception is
    # meant to name one site, so it must carry a prefix.
    assert all(prefix for _path, _name, prefix in reviewed), (
        "empty prefix in reviewed list"
    )
    return any(
        relative == path and leaf == name and fmt.startswith(prefix)
        for path, name, prefix in reviewed
    )


def scan(
    root: Path = _PACKAGE_ROOT,
    reviewed: frozenset[tuple[str, str, str]] = _REVIEWED_ROTATING,
) -> tuple[list[tuple[str, int, str, str]], int]:
    """Return ((path, line, leaf, format string) offenders, scanned calls)."""
    offenders: list[tuple[str, int, str, str]] = []
    scanned = 0
    for path in sorted(root.rglob("*.py")):
        if path.name.endswith("_pb2.py") or "vendor" in path.parts:
            continue
        relative = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not _is_log_call(node):
                continue
            scanned += 1
            # `.log(level, msg, ...)` carries the format string at index 1.
            msg_index = 1 if node.func.attr == "log" else 0
            first = node.args[msg_index] if len(node.args) > msg_index else None
            fmt = (
                first.value
                if isinstance(first, ast.Constant) and isinstance(first.value, str)
                else ""
            )
            # The format string is scanned too: an f-string or a `%`-formatted
            # first argument leaks exactly as much as a positional argument.
            arguments: list[ast.AST] = [*node.args, *(kw.value for kw in node.keywords)]
            for argument in arguments:
                for leaf in _leaks(argument, relative):
                    if _is_reviewed(reviewed, relative, leaf, fmt):
                        continue
                    offenders.append((relative, argument.lineno, leaf, fmt))
    return offenders, scanned


def test_no_log_call_passes_a_clear_text_address() -> None:
    offenders, scanned = scan()
    # Vacuum guard: the package carries well over 700 DEBUG calls alone; a
    # scan that sees fewer log calls did not walk the package (wrong root,
    # parse failure, renamed logger).
    assert scanned > 700, (
        f"only {scanned} log calls scanned; the guard would be vacuous"
    )
    assert offenders == [], (
        "Log calls pass a class (c) identifier in clear text "
        "(AGENTS.md section 5): mask it with `_mask_address_for_logs`, or, if the "
        "value rotates, add (path, leaf, format-string prefix) to `_REVIEWED_ROTATING` "
        "with the reason:\n"
        + "\n".join(f"{path}:{line}: {leaf}" for path, line, leaf, _fmt in offenders)
    )


def test_each_reviewed_site_matches_exactly_one_log_call() -> None:
    """The exception list excuses one vetted call per entry, no more, no less.

    Fewer than one: the entry outlived the site it excuses. More than one: a
    log call was copied or moved and kept the vetted prefix, so a foreign
    advertisement MAC could be logged in full while this guard stays green.
    """
    offenders, _ = scan(reviewed=frozenset())
    matches = {
        site: [
            (path, line)
            for path, line, leaf, fmt in offenders
            if _is_reviewed(frozenset({site}), path, leaf, fmt)
        ]
        for site in _REVIEWED_ROTATING
    }
    wrong = {site: hits for site, hits in matches.items() if len(hits) != 1}
    assert wrong == {}, (
        "each reviewed site must match exactly one clear-text log call "
        f"(0 = stale entry, >1 = copied site): {wrong}"
    )


def test_phone_secret_names_are_caught(tmp_path: Path) -> None:
    """Red probe for `_PHONE_SECRET_NAME`: each name in a log call is reported."""
    names = ["wifi_ssid", "locate_key", "phone_id_key", "ciphertext", "push_hex"]
    lines = ["import logging", "_LOGGER = logging.getLogger(__name__)"]
    lines += [f'_LOGGER.debug("x %s", {name})' for name in names]
    (tmp_path / "probe.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    offenders, scanned = scan(root=tmp_path, reviewed=frozenset())
    assert scanned == len(names)
    assert sorted(leaf for _p, _l, leaf, _f in offenders) == sorted(names)


def test_phone_target_and_android_id_are_caught(tmp_path: Path) -> None:
    """Red probe: the target table and an Android ID outside `Auth/` are reported."""
    lines = ["import logging", "_LOGGER = logging.getLogger(__name__)"]
    lines += [
        '_LOGGER.debug("x %s", self._phone_targets.get(device_id))',
        '_LOGGER.debug("x %s", phone_target[0])',
        '_LOGGER.debug("x %s", android_id)',
    ]
    (tmp_path / "probe.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    auth = tmp_path / "Auth"
    auth.mkdir()
    (auth / "own.py").write_text(
        'import logging\n_LOGGER = logging.getLogger(__name__)\n_LOGGER.debug("x %s", android_id)\n',
        encoding="utf-8",
    )
    offenders, scanned = scan(root=tmp_path, reviewed=frozenset())
    assert scanned == 4
    assert sorted((path, leaf) for path, _l, leaf, _f in offenders) == [
        ("probe.py", "_phone_targets"),
        ("probe.py", "android_id"),
        ("probe.py", "phone_target"),
    ]


def _snake(name: str) -> str:
    return re.sub(r"([A-Z])", r"_\1", name).lower()


def test_every_secret_proto_field_is_caught_in_both_spellings(tmp_path: Path) -> None:
    """Red probe: each field of the decoder's secret set, as attribute and snake name.

    Built from the set itself, so a field added there is probed here as well.
    """
    names = sorted(decoder._SECRET_PROTO_FIELDS)
    lines = ["import logging", "_LOGGER = logging.getLogger(__name__)"]
    for name in names:
        lines.append(f'_LOGGER.debug("x %s", record.{name})')
        lines.append(f'_LOGGER.debug("x %s", {_snake(name)})')
        lines.append(f'_LOGGER.debug("x %s", data["{name}"])')
    (tmp_path / "probe.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    offenders, scanned = scan(root=tmp_path, reviewed=frozenset())
    assert scanned == 3 * len(names)
    reported = {line for _p, line, _leaf, _f in offenders}
    assert reported == set(range(3, 3 + 3 * len(names))), sorted(
        set(range(3, 3 + 3 * len(names))) - reported
    )


def test_the_schema_spellings_found_in_review_are_caught(tmp_path: Path) -> None:
    """Red probe for the spellings a name list had missed, and `androidId`."""
    leaves = [
        "record.simDisplayName",
        "x.simInformation",
        "phone_info.phoneInformationField1",
        "meta.metadataField11",
        "androidId",
        "self.android_id",
    ]
    lines = ["import logging", "_LOGGER = logging.getLogger(__name__)"]
    lines += [f'_LOGGER.debug("x %s", {leaf})' for leaf in leaves]
    (tmp_path / "probe.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    offenders, _ = scan(root=tmp_path, reviewed=frozenset())
    assert sorted(leaf for _p, _l, leaf, _f in offenders) == sorted(
        [
            "simDisplayName",
            "simInformation",
            "phoneInformationField1",
            "metadataField11",
            "androidId",
            "android_id",
        ]
    )


def test_a_secret_field_name_matches_the_whole_leaf_only(tmp_path: Path) -> None:
    """Counter-case: `iv` and `email` are in the set; longer names that contain them are not."""
    assert "iv" in decoder._SECRET_PROTO_FIELDS
    assert "email" in decoder._SECRET_PROTO_FIELDS
    lines = ["import logging", "_LOGGER = logging.getLogger(__name__)"]
    lines += [
        '_LOGGER.debug("x %s", active)',
        '_LOGGER.debug("x %s", self.email_count)',
        '_LOGGER.debug("x %s", _mask_email_for_logs(email))',
    ]
    (tmp_path / "probe.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    offenders, scanned = scan(root=tmp_path, reviewed=frozenset())
    assert scanned == 3
    assert offenders == []
