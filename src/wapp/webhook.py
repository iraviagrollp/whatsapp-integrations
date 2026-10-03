"""What Meta posts to the webhook, turned into rows in the store.

Two things arrive: **messages** a customer sent, and **statuses** - receipts for
what the business sent (sent, delivered, read, failed).  Both come wrapped the
same way::

    entry[] -> changes[] -> value -> {metadata, contacts[], messages[], statuses[]}

Only deliveries for this inbox's own number are kept.  The Meta app also owns
the test number, and its traffic must not turn up as chats here.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .store import Store

logger = logging.getLogger(__name__)

MEDIA_TYPES = ("image", "video", "audio", "document", "sticker")


def signature_ok(app_secret: str, raw_body: bytes, header: Optional[str]) -> bool:
    """Whether Meta signed this body with the app secret (``X-Hub-Signature-256``)."""
    if not app_secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len("sha256="):])


@dataclass
class Received:
    """What one webhook delivery added, for the work done after replying to Meta."""

    #: Row IDs of new incoming messages carrying a file to fetch.
    media: List[int] = field(default_factory=list)
    #: New incoming messages, for the alerts: (row id, wa_id, preview).
    messages: List[Dict[str, Any]] = field(default_factory=list)


def describe(message: Dict[str, Any]) -> Dict[str, Any]:
    """The columns for one incoming message, whatever its type."""
    kind = message.get("type", "unknown")
    payload = message.get(kind) or {}
    row: Dict[str, Any] = {"type": kind, "body": None}

    if kind == "text":
        row["body"] = payload.get("body")
    elif kind in MEDIA_TYPES:
        row.update(
            media_id=payload.get("id"),
            mime=payload.get("mime_type"),
            body=payload.get("caption"),
            filename=payload.get("filename"),
        )
        if kind == "audio" and payload.get("voice"):
            row["extra"] = {"voice": True}
    elif kind == "location":
        place = " - ".join(p for p in (payload.get("name"), payload.get("address")) if p)
        row["body"] = place or "Location"
        row["extra"] = {"latitude": payload.get("latitude"), "longitude": payload.get("longitude")}
    elif kind == "contacts":
        names = [((c.get("name") or {}).get("formatted_name") or "a contact") for c in message.get("contacts", [])]
        phones = [p.get("phone") for c in message.get("contacts", []) for p in c.get("phones", []) if p.get("phone")]
        row["body"] = "Contact card: " + ", ".join(names) + (f" ({', '.join(phones)})" if phones else "")
    elif kind == "button":
        # A quick-reply button on a template.
        row["body"] = payload.get("text")
        row["extra"] = {"reply_id": payload.get("payload")}
    elif kind == "interactive":
        # A button or list row the bot offered: the title is what they saw,
        # the id is what the bot asked for.
        reply = payload.get("button_reply") or payload.get("list_reply") or {}
        row["body"] = reply.get("title")
        row["extra"] = {"reply_id": reply.get("id")}
    elif kind == "reaction":
        row["body"] = payload.get("emoji") or ""
        row["extra"] = {"message_id": payload.get("message_id")}
    else:
        # "unsupported", "order", "system" and anything WhatsApp adds later.
        errors = message.get("errors") or []
        reason = errors[0].get("title") if errors else ""
        row["body"] = f"[A {kind} message - not shown by the WhatsApp API{': ' + reason if reason else ''}]"
    return row


def process(payload: Dict[str, Any], store: Store, phone_number_id: str) -> Received:
    """Store everything in one webhook delivery; report what needs following up."""
    received = Received()
    if payload.get("object") != "whatsapp_business_account":
        return received

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            if change.get("field") != "messages":
                continue
            value = change.get("value") or {}
            if (value.get("metadata") or {}).get("phone_number_id") != phone_number_id:
                logger.info("Ignored a delivery for another number: %s", value.get("metadata"))
                continue

            for contact in value.get("contacts", []):
                store.save_contact(contact.get("wa_id"), (contact.get("profile") or {}).get("name"))

            for message in value.get("messages", []):
                row = describe(message)
                row_id = store.add_message(
                    wamid=message.get("id"),
                    wa_id=message.get("from"),
                    direction="in",
                    reply_to=(message.get("context") or {}).get("id"),
                    source="webhook",
                    ts=int(message.get("timestamp") or 0),
                    **row,
                )
                if row_id is None:
                    continue  # delivered before
                if row.get("media_id"):
                    received.media.append(row_id)
                if row["type"] != "reaction":
                    received.messages.append({
                        "id": row_id,
                        "wa_id": message.get("from"),
                        "type": row["type"],
                        "body": row.get("body"),
                        "reply_id": (row.get("extra") or {}).get("reply_id"),
                    })

            for status in value.get("statuses", []):
                errors = status.get("errors") or []
                error = None
                if errors:
                    first = errors[0]
                    detail = (first.get("error_data") or {}).get("details") or first.get("message") or ""
                    error = f"{first.get('code')}: {first.get('title', '')}" + (f" - {detail}" if detail
                                                                                and detail != first.get("title") else "")
                store.set_status(
                    status.get("id"),
                    status.get("recipient_id"),
                    status.get("status", ""),
                    int(status.get("timestamp") or 0),
                    error,
                )
    return received
