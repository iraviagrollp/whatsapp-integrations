"""Meta's deliveries: checked, stored once, receipts never moving backwards."""

import time

from conftest import CUSTOMER, delivery, signed, text_message


def test_handshake_answers_the_challenge_only_with_the_right_token(client):
    ok = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me",
                                        "hub.challenge": "12345"})
    assert ok.status_code == 200 and ok.text == "12345"
    bad = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "nope",
                                         "hub.challenge": "12345"})
    assert bad.status_code == 403


def test_an_unsigned_or_wrongly_signed_delivery_is_refused(client, store):
    raw, headers = signed(delivery([text_message()]), secret="someone-else")
    assert client.post("/webhook", content=raw, headers=headers).status_code == 401
    raw, headers = signed(delivery([text_message()]))
    assert client.post("/webhook", content=raw, headers={"Content-Type": "application/json"}).status_code == 401
    assert store.chats() == []


def test_a_text_message_is_stored_with_the_senders_name(client, store):
    raw, headers = signed(delivery([text_message()],
                                   contacts=[{"wa_id": CUSTOMER, "profile": {"name": "Ravi Kumar"}}]))
    assert client.post("/webhook", content=raw, headers=headers).status_code == 200
    [chat] = store.chats()
    assert chat["wa_id"] == CUSTOMER and chat["name"] == "Ravi Kumar" and chat["unread"] == 1
    [message] = store.messages(CUSTOMER)
    assert message["direction"] == "in" and message["body"] == "Thank you, same to you"


def test_a_delivery_meta_sends_twice_is_stored_once(client, store):
    raw, headers = signed(delivery([text_message()]))
    client.post("/webhook", content=raw, headers=headers)
    client.post("/webhook", content=raw, headers=headers)
    assert len(store.messages(CUSTOMER)) == 1


def test_the_test_numbers_traffic_is_ignored(client, store):
    raw, headers = signed(delivery([text_message()], phone_id="1130906663444915"))
    assert client.post("/webhook", content=raw, headers=headers).status_code == 200
    assert store.chats() == []


def test_a_photo_is_fetched_from_meta_and_kept(client, store):
    photo = {"from": CUSTOMER, "id": "wamid.PIC", "timestamp": str(int(time.time())), "type": "image",
             "image": {"id": "MEDIA-IN", "mime_type": "image/jpeg", "caption": "Our stock"}}
    raw, headers = signed(delivery([photo]))
    client.post("/webhook", content=raw, headers=headers)
    [message] = store.messages(CUSTOMER)
    assert message["body"] == "Our stock"
    assert message["media_path"] and open(message["media_path"], "rb").read() == b"JPEG"
    served = client.get(f"/api/media/{message['id']}")
    assert served.status_code == 200 and served.content == b"JPEG"


def test_receipts_move_forward_only_and_failures_keep_their_reason(client, store):
    now = int(time.time())
    statuses = [
        {"id": "wamid.G1", "recipient_id": CUSTOMER, "status": "read", "timestamp": str(now)},
        {"id": "wamid.G1", "recipient_id": CUSTOMER, "status": "delivered", "timestamp": str(now)},
        {"id": "wamid.G2", "recipient_id": "917019170176", "status": "failed", "timestamp": str(now),
         "errors": [{"code": 131049, "title": "This message was not delivered to maintain healthy ecosystem "
                                               "engagement."}]},
    ]
    raw, headers = signed(delivery(statuses=statuses))
    client.post("/webhook", content=raw, headers=headers)
    [g1] = store.messages(CUSTOMER)
    assert g1["status"] == "read"
    [g2] = store.messages("917019170176")
    assert g2["status"] == "failed" and g2["error"].startswith("131049")


def test_a_reaction_is_shown_on_the_message_it_answers(client, store, graph):
    store.add_message(wamid="wamid.G1", wa_id=CUSTOMER, direction="out", type="template", body="Greeting")
    reaction = {"from": CUSTOMER, "id": "wamid.R", "timestamp": str(int(time.time())), "type": "reaction",
                "reaction": {"message_id": "wamid.G1", "emoji": "🙏"}}
    raw, headers = signed(delivery([reaction]))
    client.post("/webhook", content=raw, headers=headers)
    chat = client.get(f"/api/chats/{CUSTOMER}").json()
    assert [m["reactions"] for m in chat["messages"]] == [["🙏"]]


def test_the_owner_is_alerted_and_not_about_their_own_messages(tmp_path, graph):
    from fastapi.testclient import TestClient

    from conftest import make_config
    from wapp.app import create_app
    from wapp.store import Store

    config = make_config(tmp_path, alert_numbers=("9701491148",), public_url="https://whatsapp.ialreports.com")
    client = TestClient(create_app(config, Store(config.db_path), graph), base_url="http://127.0.0.1:8790")
    raw, headers = signed(delivery([text_message("Rate for urea?"),
                                    text_message("my own note", wamid="wamid.OWN", sender="919701491148")],
                                   contacts=[{"wa_id": CUSTOMER, "profile": {"name": "Ravi"}}]))
    client.post("/webhook", content=raw, headers=headers)
    [alert] = graph.sent
    assert alert["to"] == "919701491148"
    assert "Ravi (+91 96183 44433)" in alert["text"]["body"] and "Rate for urea?" in alert["text"]["body"]
    assert f"whatsapp.ialreports.com/#{CUSTOMER}" in alert["text"]["body"]
