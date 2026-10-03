"""A fake Meta, so the tests send nothing and need no token."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from wapp.app import create_app
from wapp.config import Config
from wapp.graph import ApiError
from wapp.store import Store

PHONE_ID = "1392473273945992"
SECRET = "test-app-secret"
CUSTOMER = "919618344433"


class FakeGraph:
    def __init__(self):
        self.sent: List[Dict[str, Any]] = []
        self.uploads: List[Dict[str, Any]] = []
        self.read: List[str] = []
        self.fail_with: Dict[str, ApiError] = {}   # number -> error to raise
        self.templates_list: List[Dict[str, Any]] = [
            {"name": "gandhi_", "language": "en", "status": "APPROVED", "category": "MARKETING",
             "components": [{"type": "HEADER", "format": "IMAGE"},
                            {"type": "BODY", "text": "Wishing you a happy Gandhi Jayanti"}]},
            {"name": "payment_due", "language": "en", "status": "APPROVED", "category": "UTILITY",
             "components": [{"type": "BODY", "text": "Dear {{1}}, Rs {{2}} is due."}]},
        ]
        self.media = {"MEDIA-IN": ({"url": "https://lookaside/x", "mime_type": "image/jpeg"}, b"JPEG")}

    def send(self, to, content):
        if to in self.fail_with:
            raise self.fail_with[to]
        wamid = f"wamid.OUT{len(self.sent) + 1}"
        self.sent.append({"to": to, "id": wamid, **content})
        return wamid

    def upload_media(self, content, filename, mime=None):
        self.uploads.append({"filename": filename, "mime": mime, "size": len(content)})
        return f"UP{len(self.uploads)}"

    def media_info(self, media_id):
        return self.media[media_id][0]

    def download(self, url):
        return next(c for info, c in self.media.values() if info["url"] == url)

    def mark_read(self, wamid):
        self.read.append(wamid)

    def templates(self):
        return self.templates_list


def make_config(tmp_path, **overrides) -> Config:
    values = dict(phone_number_id=PHONE_ID, waba_id="28701936172750228", verify_token="verify-me",
                  token="token", app_secret=SECRET, data_dir=tmp_path / "data")
    values.update(overrides)
    return Config(**values)


def signed(payload: Dict[str, Any], secret: str = SECRET):
    raw = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {"X-Hub-Signature-256": signature, "Content-Type": "application/json"}


def delivery(messages=(), statuses=(), contacts=(), phone_id=PHONE_ID) -> Dict[str, Any]:
    value: Dict[str, Any] = {"messaging_product": "whatsapp",
                             "metadata": {"display_phone_number": "917416189998", "phone_number_id": phone_id}}
    if contacts:
        value["contacts"] = list(contacts)
    if messages:
        value["messages"] = list(messages)
    if statuses:
        value["statuses"] = list(statuses)
    return {"object": "whatsapp_business_account",
            "entry": [{"id": "28701936172750228", "changes": [{"field": "messages", "value": value}]}]}


def text_message(body="Thank you, same to you", wamid="wamid.IN1", sender=CUSTOMER, ts=None, **extra):
    return {"from": sender, "id": wamid, "timestamp": str(int(ts or time.time())), "type": "text",
            "text": {"body": body}, **extra}


@pytest.fixture
def graph():
    return FakeGraph()


@pytest.fixture
def config(tmp_path):
    return make_config(tmp_path)


@pytest.fixture
def store(config):
    return Store(config.db_path)


@pytest.fixture
def client(config, store, graph):
    return TestClient(create_app(config, store, graph), base_url="http://127.0.0.1:8790")
