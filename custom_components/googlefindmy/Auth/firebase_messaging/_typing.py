# custom_components/googlefindmy/Auth/firebase_messaging/_typing.py
"""Shared typing helpers for the Firebase messaging transport layer."""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from typing import Any, Protocol, TypeVar

type JSONDict = dict[str, Any]
type MutableJSONMapping = MutableMapping[str, Any]

NotificationPayloadT_contra = TypeVar(
    "NotificationPayloadT_contra",
    bound=Mapping[str, Any],
    contravariant=True,
)
NotificationContextT_contra = TypeVar(
    "NotificationContextT_contra",
    contravariant=True,
)
NotificationContextT = TypeVar("NotificationContextT")
CredentialsMappingT_contra = TypeVar(
    "CredentialsMappingT_contra",
    bound=MutableMapping[str, Any],
    contravariant=True,
)


class OnNotificationCallable(
    Protocol[NotificationPayloadT_contra, NotificationContextT_contra]
):
    """Callable protocol for push notification handlers."""

    def __call__(
        self,
        payload: NotificationPayloadT_contra,
        persistent_id: str,
        context: NotificationContextT_contra | None,
    ) -> None:
        """Invoke the handler with the decoded payload."""


class CredentialsUpdatedCallable(Protocol[CredentialsMappingT_contra]):
    """Callable protocol for credential update notifications."""

    def __call__(self, credentials: CredentialsMappingT_contra) -> None:
        """Persist or react to refreshed credential payloads."""


__all__ = [
    "CredentialsMappingT_contra",
    "CredentialsUpdatedCallable",
    "JSONDict",
    "MutableJSONMapping",
    "NotificationContextT",
    "NotificationContextT_contra",
    "NotificationPayloadT_contra",
    "OnNotificationCallable",
]
