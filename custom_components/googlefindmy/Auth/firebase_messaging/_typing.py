# custom_components/googlefindmy/Auth/firebase_messaging/_typing.py
"""Shared typing helpers for the Firebase messaging transport layer."""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from typing import Any, Protocol, TypeVar, TypeAlias

JSONDict: TypeAlias = dict[str, Any]
MutableJSONMapping: TypeAlias = MutableMapping[str, Any]

NotificationPayloadT = TypeVar(
    "NotificationPayloadT",
    bound=Mapping[str, Any],
    contravariant=True,
)
NotificationContextT = TypeVar(
    "NotificationContextT",
    contravariant=True,
)
CredentialsMappingT = TypeVar(
    "CredentialsMappingT",
    bound=MutableMapping[str, Any],
    contravariant=True,
)


class OnNotificationCallable(
    Protocol[NotificationPayloadT, NotificationContextT]
):
    """Callable protocol for push notification handlers."""

    def __call__(
        self,
        payload: NotificationPayloadT,
        persistent_id: str,
        context: NotificationContextT | None,
    ) -> None:
        """Invoke the handler with the decoded payload."""


class CredentialsUpdatedCallable(Protocol[CredentialsMappingT]):
    """Callable protocol for credential update notifications."""

    def __call__(self, credentials: CredentialsMappingT) -> None:
        """Persist or react to refreshed credential payloads."""


__all__ = [
    "CredentialsMappingT",
    "CredentialsUpdatedCallable",
    "JSONDict",
    "MutableJSONMapping",
    "NotificationContextT",
    "NotificationPayloadT",
    "OnNotificationCallable",
]

