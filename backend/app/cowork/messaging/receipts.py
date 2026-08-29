"""Durable at-most-once receipts for authenticated inbound messaging events.

The upstream event id is never stored.  A claimed receipt is intentionally terminal for
automatic delivery even if the process crashes before marking it completed: after dispatch we
cannot distinguish "nothing happened" from "the action happened but the response was lost".
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.cowork_store.routing import cowork_store

MESSAGING_EVENT_RECEIPT_RETENTION_DAYS = 30
_MAX_EVENT_ID_CHARS = 512
_MAX_EVENT_TYPE_CHARS = 256


@dataclass(frozen=True)
class MessagingEventIdentity:
    event_key: str
    event_type: str


def feishu_event_identity(payload: Mapping[str, Any]) -> MessagingEventIdentity:
    """Return a privacy-preserving v2/legacy identity or reject an actionable event."""

    raw_header = payload.get("header")
    header = raw_header if isinstance(raw_header, Mapping) else {}
    raw_event_id = header.get("event_id") or payload.get("uuid")
    raw_event_type = header.get("event_type") or payload.get("type")
    if (
        not isinstance(raw_event_id, str)
        or not raw_event_id.strip()
        or len(raw_event_id) > _MAX_EVENT_ID_CHARS
        or not isinstance(raw_event_type, str)
        or not raw_event_type.strip()
        or len(raw_event_type) > _MAX_EVENT_TYPE_CHARS
    ):
        raise ValueError("actionable Feishu event is missing a bounded identity")
    event_key = hashlib.sha256(f"feishu\0{raw_event_id}".encode()).hexdigest()
    return MessagingEventIdentity(event_key=event_key, event_type=raw_event_type)


async def claim_feishu_event(identity: MessagingEventIdentity) -> bool:
    return await cowork_store().claim_messaging_event(
        event_key=identity.event_key,
        platform="feishu",
        event_type=identity.event_type,
        retention_days=MESSAGING_EVENT_RECEIPT_RETENTION_DAYS,
    )


async def complete_feishu_event(identity: MessagingEventIdentity) -> None:
    completed = await cowork_store().complete_messaging_event(event_key=identity.event_key)
    if not completed:
        raise RuntimeError("飞书事件 receipt 结算失败")


__all__ = [
    "MESSAGING_EVENT_RECEIPT_RETENTION_DAYS",
    "MessagingEventIdentity",
    "claim_feishu_event",
    "complete_feishu_event",
    "feishu_event_identity",
]
