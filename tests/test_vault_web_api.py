# tests/test_vault_web_api.py
"""Shape contract of the owner-key vault parser ``KeyBackup.vault_web_api``.

The parser reads the answer of the undocumented batchexecute RPC ``FAvFKc``. Its
contract: it never raises, and every rejected answer maps to exactly one
``VaultCause``. These tests feed it a reconstructed success answer and form
mutants of it and check the cause of each.

Sources of the material:

* The outer frame (``)]}'`` prefix, ``<length>\\n<json>\\n`` chunks for ``rt=c``,
  ``["wrb.fr", <rpc id>, <payload json string>, ...]`` items, trailing ``di`` and
  ``af.httprm`` items) follows public descriptions of batchexecute, for example
  the ``pybatchexecute`` project; it is foreign material.
* The inner payload form (entry list at ``inner[0][5]``, entry tuple
  ``key_b64, version, domain, epoch, ...``) has no public source. It is
  reconstructed from the parser of leonboe1/GoogleFindMyTools#114 and marked
  ``# reconstructed`` where it is built.
* Every case marked ``# own mutant`` is a deviation designed here.

Blobs come from ``tests.helpers.owner_key_vault_fixtures`` at run time; there is
no Base64 or hex key literal in this file.
"""

from __future__ import annotations

import ast
import base64
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from custom_components.googlefindmy.KeyBackup import vault_web_api
from custom_components.googlefindmy.KeyBackup.vault_web_api import (
    ENCRYPTED_OWNER_KEY_LEN,
    FETCH_SCRIPT,
    FIND_MY_DEVICE_URL,
    FINDER_HW_DOMAIN,
    OWNER_KEY_VERSIONS_FIELD,
    RPC_ID,
    VaultCause,
    VaultEntry,
    VaultFetchResult,
    from_bundle_field,
    parse_batchexecute,
    select_finder_hw,
    to_bundle_field,
)
from tests.helpers import owner_key_vault_fixtures as fixtures
from tests.helpers.owner_key_vault_fixtures import VaultWorld, make_vault_world

_HYGIENE_WINDOW = 16


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #


def _b64(blob: bytes) -> str:
    return base64.b64encode(blob).decode("ascii")


def _entry_rows(world: VaultWorld) -> list[list[Any]]:
    # reconstructed: tuple order key_b64, version, domain, epoch, plus a trailing
    # field the parser must ignore (``*_rest`` in the pull request).
    return [
        [_b64(blob), version, FINDER_HW_DOMAIN, 1_700_000_000 + version, None]
        for version, blob in world.wrapped_owner_keys.items()
    ]


def _payload(rows: object, *, index: int = 5) -> str:
    # reconstructed: the entry list sits at inner[0][5].
    head: list[object] = [None] * 6
    head[index] = rows
    return json.dumps([head])


def _chunk(envelope: object, *, length_delta: int = 0) -> str:
    body = json.dumps(envelope)
    return f"{len(body) + length_delta}\n{body}\n"


def _frame(
    payload: object,
    *,
    rpc_id: str = RPC_ID,
    length_delta: int = 0,
    prefix: str = ")]}'\n\n",
) -> str:
    item = ["wrb.fr", rpc_id, payload, None, None, None, "generic"]
    return (
        prefix
        + _chunk([item], length_delta=length_delta)
        + _chunk([["di", 42], ["af.httprm", 41, "-1", 7]])
    )


def _success_text(world: VaultWorld) -> str:
    return _frame(_payload(_entry_rows(world)))


def _leaked_windows(text: str, forms: tuple[str, ...]) -> list[str]:
    hits: list[str] = []
    for form in forms:
        for start in range(len(form) - _HYGIENE_WINDOW + 1):
            window = form[start : start + _HYGIENE_WINDOW]
            if window in text:
                hits.append(window)
                break
    return hits


@pytest.fixture
def world() -> VaultWorld:
    return make_vault_world(versions=(1, 2, 3), spot_version=1)


# --------------------------------------------------------------------------- #
# Success path
# --------------------------------------------------------------------------- #


def test_reconstructed_answer_yields_every_entry(world: VaultWorld) -> None:
    result = parse_batchexecute(_success_text(world))

    assert result.cause is None
    assert [(e.version, e.domain, e.key) for e in result.entries] == [
        (version, FINDER_HW_DOMAIN, blob)
        for version, blob in world.wrapped_owner_keys.items()
    ]
    assert [e.epoch for e in result.entries] == [
        1_700_000_001,
        1_700_000_002,
        1_700_000_003,
    ]


def test_declared_length_off_by_one_still_parses(world: VaultWorld) -> None:
    # The pull request reports the declared chunk length off by one character
    # against a live capture; the parser must rely on the JSON end instead.
    for delta in (-1, 1):
        text = _frame(_payload(_entry_rows(world)), length_delta=delta)
        assert parse_batchexecute(text).cause is None


def test_answer_without_xssi_prefix_parses(world: VaultWorld) -> None:
    text = _frame(_payload(_entry_rows(world)), prefix="")
    assert parse_batchexecute(text).cause is None


def test_other_domain_with_other_length_is_kept_for_the_filter(
    world: VaultWorld,
) -> None:
    # own mutant: the derived length only holds for wrapped owner keys; another
    # domain with another blob length must not reject the whole answer.
    rows = _entry_rows(world) + [[_b64(b"\x01" * 20), 9, "other_domain", 5]]
    result = parse_batchexecute(_frame(_payload(rows)))

    assert result.cause is None
    assert [e.domain for e in result.entries].count("other_domain") == 1
    assert all(e.domain == FINDER_HW_DOMAIN for e in select_finder_hw(result.entries))


# --------------------------------------------------------------------------- #
# Form mutants: one expected cause each, never an exception
# --------------------------------------------------------------------------- #


def _rows_with_blob(world: VaultWorld, blob: bytes) -> list[list[Any]]:
    rows = _entry_rows(world)
    rows[0][0] = _b64(blob)
    return rows


def _mutants(world: VaultWorld) -> dict[str, tuple[str, VaultCause]]:
    blob = world.wrapped_owner_keys[1]
    rows = _entry_rows(world)
    return {
        # own mutant
        "length_plus_one": (
            _frame(_payload(_rows_with_blob(world, blob + b"\x00"))),
            VaultCause.WRONG_LENGTH,
        ),
        # own mutant
        "length_minus_one": (
            _frame(_payload(_rows_with_blob(world, blob[:-1]))),
            VaultCause.WRONG_LENGTH,
        ),
        # own mutant
        "entry_list_at_index_4": (
            _frame(_payload(rows, index=4)),
            VaultCause.FOREIGN_SHAPE,
        ),
        # own mutant
        "foreign_rpc_name": (
            _frame(_payload(rows), rpc_id="XyZ123"),
            VaultCause.NO_FRAME,
        ),
        # own mutant
        "null_payload": (_frame(None), VaultCause.EMPTY_PAYLOAD),
        # own mutant
        "empty_string_payload": (_frame(""), VaultCause.EMPTY_PAYLOAD),
        # own mutant
        "empty_entry_list": (_frame(_payload([])), VaultCause.EMPTY_PAYLOAD),
        # own mutant
        "broken_length_line": (
            _frame(_payload(rows)).replace(")]}'\n\n", ")]}'\n\n-", 1),
            VaultCause.NO_FRAME,
        ),
        # own mutant
        "non_numeric_length_line": (
            ")]}'\n\nabc\n" + json.dumps([["wrb.fr", RPC_ID, _payload(rows)]]) + "\n",
            VaultCause.NO_FRAME,
        ),
        # own mutant
        "empty_body": ("", VaultCause.NO_FRAME),
        # own mutant
        "html_error_page": ("<html><body>Error 400</body></html>", VaultCause.NO_FRAME),
        # own mutant
        "broken_chunk_json": (')]}\'\n\n12\n[["wrb.fr",\n', VaultCause.NO_FRAME),
        # own mutant
        "payload_not_json": (_frame("not json"), VaultCause.FOREIGN_SHAPE),
        # own mutant
        "payload_not_a_string": (_frame([1, 2]), VaultCause.FOREIGN_SHAPE),
        # own mutant
        "payload_json_object": (_frame(json.dumps({"a": 1})), VaultCause.FOREIGN_SHAPE),
        # own mutant
        "payload_deeply_nested": (_frame("[" * 100_000), VaultCause.FOREIGN_SHAPE),
        # own mutant
        "entry_list_not_a_list": (_frame(_payload({"k": 1})), VaultCause.FOREIGN_SHAPE),
        # own mutant
        "entry_too_short": (
            _frame(_payload([[_b64(blob), 1, FINDER_HW_DOMAIN]])),
            VaultCause.FOREIGN_SHAPE,
        ),
        # own mutant
        "version_as_string": (
            _frame(_payload([[_b64(blob), "1", FINDER_HW_DOMAIN, 5]])),
            VaultCause.FOREIGN_SHAPE,
        ),
        # own mutant
        "version_as_bool": (
            _frame(_payload([[_b64(blob), True, FINDER_HW_DOMAIN, 5]])),
            VaultCause.FOREIGN_SHAPE,
        ),
        # own mutant
        "epoch_as_float": (
            _frame(_payload([[_b64(blob), 1, FINDER_HW_DOMAIN, 5.0]])),
            VaultCause.FOREIGN_SHAPE,
        ),
        # own mutant
        "tuple_order_swapped": (
            _frame(_payload([[1, _b64(blob), FINDER_HW_DOMAIN, 5]])),
            VaultCause.FOREIGN_SHAPE,
        ),
        # own mutant
        "urlsafe_base64_alphabet": (
            _frame(
                _payload(
                    [
                        [
                            _b64(blob).replace("+", "-").replace("/", "_") + "-_",
                            1,
                            FINDER_HW_DOMAIN,
                            5,
                        ]
                    ]
                )
            ),
            VaultCause.FOREIGN_SHAPE,
        ),
        # own mutant
        "base64_with_whitespace": (
            _frame(
                _payload(
                    [[_b64(blob)[:8] + " " + _b64(blob)[8:], 1, FINDER_HW_DOMAIN, 5]]
                )
            ),
            VaultCause.FOREIGN_SHAPE,
        ),
        # own mutant
        "base64_bad_padding": (
            _frame(_payload([[_b64(blob).rstrip("=") + "=", 1, FINDER_HW_DOMAIN, 5]])),
            VaultCause.FOREIGN_SHAPE,
        ),
        # own mutant
        "envelope_not_a_list": (
            ")]}'\n\n" + _chunk({"wrb.fr": RPC_ID}),
            VaultCause.NO_FRAME,
        ),
    }


_MUTANT_NAMES = sorted(_mutants(make_vault_world()))


@pytest.mark.parametrize("name", _MUTANT_NAMES)
def test_form_mutant_maps_to_exactly_one_cause(world: VaultWorld, name: str) -> None:
    text, expected = _mutants(world)[name]

    result = parse_batchexecute(text)

    assert isinstance(result, VaultFetchResult)
    assert result.cause is expected
    assert result.entries == ()


def test_every_parser_cause_has_a_mutant(world: VaultWorld) -> None:
    covered = {cause for _text, cause in _mutants(world).values()}
    assert covered == {
        VaultCause.NO_FRAME,
        VaultCause.EMPTY_PAYLOAD,
        VaultCause.FOREIGN_SHAPE,
        VaultCause.WRONG_LENGTH,
    }


def test_one_bad_entry_rejects_the_whole_answer(world: VaultWorld) -> None:
    rows = _entry_rows(world)
    rows[-1][0] = _b64(world.wrapped_owner_keys[3] + b"\x00")  # own mutant

    result = parse_batchexecute(_frame(_payload(rows)))

    assert result.cause is VaultCause.WRONG_LENGTH
    assert result.entries == ()


@settings(max_examples=200, deadline=None, derandomize=True)
@given(st.text())
def test_parser_never_raises_on_arbitrary_text(text: str) -> None:
    assert isinstance(parse_batchexecute(text), VaultFetchResult)


@settings(max_examples=200, deadline=None, derandomize=True)
@given(
    st.recursive(
        st.none()
        | st.booleans()
        | st.integers()
        | st.floats(allow_nan=False)
        | st.text(max_size=8),
        lambda children: (
            st.lists(children, max_size=6)
            | st.dictionaries(st.text(max_size=4), children, max_size=4)
        ),
        max_leaves=30,
    )
)
def test_parser_never_raises_on_arbitrary_payload_json(inner: object) -> None:
    result = parse_batchexecute(_frame(json.dumps(inner)))
    assert isinstance(result, VaultFetchResult)
    assert (result.cause is None) == bool(result.entries)


# --------------------------------------------------------------------------- #
# finder_hw selection and epoch rule
# --------------------------------------------------------------------------- #


def _entry(
    version: int, epoch: int, domain: str = FINDER_HW_DOMAIN, fill: int = 0
) -> VaultEntry:
    return VaultEntry(
        version=version,
        epoch=epoch,
        domain=domain,
        key=bytes([fill]) * ENCRYPTED_OWNER_KEY_LEN,
    )


def test_select_keeps_highest_epoch_per_version() -> None:
    chosen = select_finder_hw(
        [
            _entry(2, 10, fill=1),
            _entry(2, 30, fill=2),
            _entry(2, 20, fill=3),
            _entry(1, 5, fill=4),
        ]
    )

    assert [(e.version, e.epoch, e.key[0]) for e in chosen] == [(1, 5, 4), (2, 30, 2)]


def test_select_tie_goes_to_the_later_entry() -> None:
    chosen = select_finder_hw([_entry(1, 7, fill=1), _entry(1, 7, fill=2)])

    assert [e.key[0] for e in chosen] == [2]


def test_select_drops_foreign_domains() -> None:
    chosen = select_finder_hw(
        [
            _entry(1, 99, domain="other_domain"),
            _entry(1, 1, fill=5),
            _entry(4, 1, domain="x"),
        ]
    )

    assert [(e.version, e.domain, e.key[0]) for e in chosen] == [
        (1, FINDER_HW_DOMAIN, 5)
    ]


def test_select_of_nothing_is_empty() -> None:
    assert select_finder_hw([]) == ()


def test_select_drops_versions_below_one() -> None:
    """Versions 0 and -1 never reach the bundle; version 1 stays (boundary)."""
    chosen = select_finder_hw([_entry(0, 1), _entry(-1, 1), _entry(1, 1, fill=6)])

    assert [(e.version, e.key[0]) for e in chosen] == [(1, 6)]
    assert list(to_bundle_field([_entry(0, 1), _entry(2, 1)])) == ["2"]


# --------------------------------------------------------------------------- #
# Bundle field
# --------------------------------------------------------------------------- #


def test_field_name_matches_the_test_factory() -> None:
    assert OWNER_KEY_VERSIONS_FIELD == fixtures.OWNER_KEY_VERSIONS_FIELD
    assert FINDER_HW_DOMAIN == fixtures.VAULT_DOMAIN


def test_to_bundle_field_matches_the_factory_shape(world: VaultWorld) -> None:
    entries = parse_batchexecute(_success_text(world)).entries

    assert to_bundle_field(entries) == world.bundle_field


def test_to_bundle_field_applies_the_selection() -> None:
    field_value = to_bundle_field(
        [_entry(1, 1, fill=1), _entry(1, 2, fill=2), _entry(2, 1, domain="x")]
    )

    assert list(field_value) == ["1"]
    assert field_value["1"]["epoch"] == 2


def test_bundle_field_round_trip_keeps_bytes(world: VaultWorld) -> None:
    entries = parse_batchexecute(_success_text(world)).entries

    restored = from_bundle_field(to_bundle_field(entries))

    assert restored == select_finder_hw(entries)
    assert restored is not None
    assert {e.version: e.key for e in restored} == world.wrapped_owner_keys


def test_from_bundle_field_of_empty_dict_is_empty() -> None:
    assert from_bundle_field({}) == ()


def _field_mutants(world: VaultWorld) -> dict[str, object]:
    good = world.bundle_field
    item = dict(good["1"])

    def with_item(**changes: object) -> dict[str, object]:
        mutated = dict(item)
        mutated.update(changes)
        return {**good, "1": mutated}

    missing_key = dict(item)
    del missing_key["key"]
    return {
        "not_a_dict": [item],
        "string": "owner_key_versions",
        "none": None,
        "item_not_a_dict": {**good, "1": "x"},
        "name_mismatch": {**{k: v for k, v in good.items() if k != "1"}, "7": item},
        "non_string_name": {1: item},
        "uppercase_hex": with_item(key=str(item["key"]).upper()),
        "hex_too_short": with_item(key=str(item["key"])[:-2]),
        "hex_too_long": with_item(key=str(item["key"]) + "00"),
        "hex_with_space": with_item(key=" " + str(item["key"])[1:]),
        "foreign_domain": with_item(domain="other_domain"),
        "version_as_string": with_item(version="1"),
        "version_as_bool": with_item(version=True),
        "version_zero": {
            **{k: v for k, v in good.items() if k != "1"},
            "0": {**item, "version": 0},
        },
        "version_negative": {
            **{k: v for k, v in good.items() if k != "1"},
            "-1": {**item, "version": -1},
        },
        "epoch_missing": with_item(epoch=None),
        "key_missing": {**good, "1": missing_key},
    }


@pytest.mark.parametrize("name", sorted(_field_mutants(make_vault_world())))
def test_from_bundle_field_rejects_foreign_shape(world: VaultWorld, name: str) -> None:
    assert from_bundle_field(_field_mutants(world)[name]) is None


# --------------------------------------------------------------------------- #
# Constants, enum and module boundary
# --------------------------------------------------------------------------- #


def test_blob_length_is_derived_from_the_decryptor_sources() -> None:
    from custom_components.googlefindmy.KeyBackup import (  # noqa: PLC0415
        cloud_key_decryptor,
    )
    from custom_components.googlefindmy.SpotApi.GetEidInfoForE2eeDevices import (  # noqa: PLC0415
        get_owner_key,
    )

    gcm_tag_len = 16
    assert ENCRYPTED_OWNER_KEY_LEN == (
        cloud_key_decryptor.GCM_IV_LEN_DEFAULT
        + get_owner_key._OWNER_KEY_LENGTH
        + gcm_tag_len
    )
    assert len(make_vault_world().wrapped_owner_keys[1]) == ENCRYPTED_OWNER_KEY_LEN


def test_cause_names_are_the_closed_set() -> None:
    assert [cause.value for cause in VaultCause] == [
        "no_frame",
        "empty_payload",
        "foreign_shape",
        "wrong_length",
        "http_status",
        "self_test_failed",
        "alert_deadline",
        "window_closed",
    ]


def test_driver_constants() -> None:
    assert (
        FIND_MY_DEVICE_URL
        == "https://www.google.com/android/find/?login=&device=1&rs=1"
    )
    assert f'"rpcids": "{RPC_ID}"' in FETCH_SCRIPT
    assert "g.SNlM0e" in FETCH_SCRIPT
    assert (
        "callback({ok: resp.ok, status: resp.status, text: await resp.text()})"
        in FETCH_SCRIPT
    )


def test_module_imports_only_the_standard_library() -> None:
    tree = ast.parse(Path(vault_web_api.__file__).read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative import"
            assert node.module is not None
            roots.add(node.module.split(".")[0])
    assert roots
    assert roots <= set(sys.stdlib_module_names) | {"__future__"}


def test_module_has_no_print_and_no_logger() -> None:
    tree = ast.parse(Path(vault_web_api.__file__).read_text(encoding="utf-8"))
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert "print" not in names
    assert not {"logging", "_LOGGER", "LOGGER"} & names


# --------------------------------------------------------------------------- #
# repr / str hygiene
# --------------------------------------------------------------------------- #


def test_repr_and_str_never_show_blob_material(world: VaultWorld) -> None:
    result = parse_batchexecute(_success_text(world))
    forms = world.all_secret_strings()
    rendered = [repr(result), str(result), f"{result}", f"{result!r}"]
    for entry in result.entries:
        rendered.extend([repr(entry), str(entry), f"{entry!r}"])
    rendered.append(repr(result.entries))

    for text in rendered:
        assert _leaked_windows(text, forms) == [], text


def test_result_repr_names_only_count_and_cause(world: VaultWorld) -> None:
    assert (
        repr(parse_batchexecute(_success_text(world)))
        == "VaultFetchResult(entries=3, cause=None)"
    )
    assert str(parse_batchexecute("")) == "VaultFetchResult(entries=0, cause=no_frame)"


# --------------------------------------------------------------------------- #
# Chunk end and the defensive net
# --------------------------------------------------------------------------- #


def test_frame_chunk_without_trailing_newline_parses(world: VaultWorld) -> None:
    # A body that ends right after the JSON of the frame chunk leaves no newline
    # to skip; the frame must still be read.
    item = ["wrb.fr", RPC_ID, _payload(_entry_rows(world)), None, None, None, "generic"]
    body = json.dumps([item])

    result = parse_batchexecute(f")]}}'\n\n{len(body)}\n{body}")

    assert result.cause is None
    assert len(result.entries) == len(world.wrapped_owner_keys)


@pytest.mark.parametrize(
    "exc", [ValueError, TypeError, IndexError, KeyError, RecursionError]
)
def test_unexpected_error_inside_the_parser_is_a_foreign_shape(
    world: VaultWorld, monkeypatch: pytest.MonkeyPatch, exc: type[Exception]
) -> None:
    """The "never raises" contract holds for errors no shape check anticipated."""

    def _boom(_payload: object) -> tuple[object, ...]:
        raise exc("synthetic")

    monkeypatch.setattr(vault_web_api, "_parse_payload", _boom)

    result = parse_batchexecute(_success_text(world))

    assert result.entries == ()
    assert result.cause is VaultCause.FOREIGN_SHAPE
