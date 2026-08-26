# tests/test_sound_dispatch_contract.py
"""Pin the API -> coordinator sound contract as a type, not as a bool."""

from __future__ import annotations

import dataclasses

from custom_components.googlefindmy.const import (
    PlaySoundResult,
    SoundDispatchOutcome,
)

# NOTE: "only TRANSPORT_FAILED may arm the push cooldown" is a property of
# coordinator/locate.py, not of const.py. Asserting it here would only restate
# the enum against itself, so it is pinned where it is observable, in
# tests/test_coordinator_locate_basics.py (see AP-4 of
# PLAN_GFMY_SOUND_FAILURE_CLASSIFICATION), parametrised over every member of
# SoundDispatchOutcome so that future members are covered automatically.


def test_result_is_frozen_and_defaults_to_no_cancel_key() -> None:
    """A result must not be mutated after the fact, and must not invent a key."""

    result = PlaySoundResult(SoundDispatchOutcome.REJECTED_AUTH)

    assert result.cancel_key is None
    assert result.accepted is False
    assert dataclasses.is_dataclass(result)
    try:
        result.outcome = SoundDispatchOutcome.ACCEPTED  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        pass
    else:  # pragma: no cover - only reached if the dataclass loses frozen=True
        raise AssertionError("PlaySoundResult must be frozen")


def test_accepted_is_the_only_true_predicate() -> None:
    """``accepted`` must not creep into meaning "no error"."""

    for member in SoundDispatchOutcome:
        assert PlaySoundResult(member).accepted is (
            member is SoundDispatchOutcome.ACCEPTED
        )
