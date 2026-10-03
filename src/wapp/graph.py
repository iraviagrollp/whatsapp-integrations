"""Meta's Graph API, as far as the inbox needs it.

Standard library only: the calls are few and simple, and nothing here should
pull in an HTTP client the rest of the project does not need.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: A normal message to somebody whose 24-hour window has closed.
OUTSIDE_WINDOW = 131047
#: Throughput limits: wait and try once more.
RATE_LIMIT_CODES = {4, 80007, 130429, 131056}


class ApiError(Exception):
    """Meta refused a call.  ``code`` is Meta's error code, ``detail`` its explanation."""

    def __init__(self, code: int, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class Graph:
    """One business number's view of the Graph API."""

    def __init__(self, token: str, phone_number_id: str, waba_id: str, version: str = "v23.0"):
        self.token = token
        self.phone_number_id = phone_number_id
        self.waba_id = waba_id
        self.base = f"https://graph.facebook.com/{version}"

    # ------------------------------------------------------------ transport

    def _open(self, request: urllib.request.Request, timeout: int = 30) -> bytes:
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            try:
                error = json.loads(exc.read())["error"]
            except Exception:
                raise ApiError(exc.code, str(exc.reason)) from None
            detail = (error.get("error_data") or {}).get("details") or error.get("message", "")
            raise ApiError(int(error.get("code", exc.code)), detail) from None
        except urllib.error.URLError as exc:
            raise ApiError(0, f"Meta could not be reached: {exc.reason}") from None

    def call(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        *,
        raw: Optional[bytes] = None,
        content_type: str = "application/json",
    ) -> Dict[str, Any]:
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        request = urllib.request.Request(
            f"{self.base}/{path}",
            data=data,
            method=method,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": content_type},
        )
        return json.loads(self._open(request) or b"{}")

    # --------------------------------------------------------------- media

    def media_info(self, media_id: str) -> Dict[str, Any]:
        """Where Meta keeps a received file for now, and its type."""
        return self.call("GET", media_id)

    def download(self, url: str) -> bytes:
        """The file itself.  The link needs the token too, and expires in minutes."""
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.token}"})
        return self._open(request, timeout=120)

    def upload_media(self, content: bytes, filename: str, mime: Optional[str] = None) -> str:
        """Upload a file to send; returns the media ID to send it by."""
        mime = mime or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        boundary = uuid.uuid4().hex
        safe_name = filename.replace('"', "'")
        body = b"".join([
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"messaging_product\"\r\n\r\nwhatsapp\r\n".encode(),
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"type\"\r\n\r\n{mime}\r\n".encode(),
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{safe_name}\"\r\n"
            f"Content-Type: {mime}\r\n\r\n".encode(),
            content,
            f"\r\n--{boundary}--\r\n".encode(),
        ])
        reply = self.call("POST", f"{self.phone_number_id}/media", raw=body,
                          content_type=f"multipart/form-data; boundary={boundary}")
        return reply["id"]

    # ------------------------------------------------------------ messages

    def send(self, to: str, content: Dict[str, Any]) -> str:
        """Send one message; ``content`` is its type and payload.  Returns its ID."""
        body = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to, **content}
        for attempt in (1, 2):
            try:
                reply = self.call("POST", f"{self.phone_number_id}/messages", body)
                return reply["messages"][0]["id"]
            except ApiError as exc:
                if exc.code in RATE_LIMIT_CODES and attempt == 1:
                    time.sleep(5)
                    continue
                raise
        raise AssertionError("unreachable")

    def mark_read(self, wamid: str) -> None:
        """Blue ticks on the customer's phone for this message and those before it."""
        self.call("POST", f"{self.phone_number_id}/messages",
                  {"messaging_product": "whatsapp", "status": "read", "message_id": wamid})

    # ----------------------------------------------------------- templates

    def templates(self) -> List[Dict[str, Any]]:
        """Every approved template in the account, following Meta's pages."""
        found: List[Dict[str, Any]] = []
        query = "limit=100&fields=name,language,status,category,parameter_format,components"
        while True:
            page = self.call("GET", f"{self.waba_id}/message_templates?{query}")
            found += [t for t in page.get("data", []) if t.get("status") == "APPROVED"]
            paging = page.get("paging") or {}
            after = (paging.get("cursors") or {}).get("after")
            if not (after and paging.get("next")):
                return found
            query = query.split("&after=")[0] + "&after=" + urllib.parse.quote(after)


def media_message(kind: str, media_id: str, caption: str = "", filename: str = "") -> Dict[str, Any]:
    """The content of a media message - image, video, audio or document."""
    media: Dict[str, Any] = {"id": media_id}
    if caption and kind in ("image", "video", "document"):
        media["caption"] = caption
    if kind == "document" and filename:
        media["filename"] = filename
    return {"type": kind, kind: media}


def media_kind(mime: str) -> str:
    """Which WhatsApp message type carries a file of this type."""
    if mime in ("image/jpeg", "image/png"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    return "document"


def split_mime(filename: str, given: Optional[str]) -> Tuple[str, str]:
    """The file's type, preferring the browser's word for it, and the message kind."""
    mime = (given or "").split(";")[0].strip() or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return mime, media_kind(mime)
