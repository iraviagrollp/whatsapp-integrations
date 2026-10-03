"""The inbox: chats, replies inside the window, templates outside it, access."""

import io
import time

from conftest import CUSTOMER, delivery, signed, text_message
from wapp.graph import ApiError


def _customer_writes(client, minutes_ago=5, body="Need 20 bags of urea"):
    raw, headers = signed(delivery([text_message(body, wamid=f"wamid.IN{minutes_ago}",
                                                 ts=time.time() - minutes_ago * 60)]))
    client.post("/webhook", content=raw, headers=headers)


def test_a_reply_inside_the_window_is_sent_and_shown(client, graph, store):
    _customer_writes(client)
    reply = client.post(f"/api/chats/{CUSTOMER}/send", data={"text": "Will send today, sir"})
    assert reply.status_code == 200
    assert graph.sent[0]["to"] == CUSTOMER and graph.sent[0]["text"]["body"] == "Will send today, sir"
    chat = client.get(f"/api/chats/{CUSTOMER}").json()
    assert chat["window_closes"]
    assert [(m["direction"], m["status"]) for m in chat["messages"]] == [("in", None), ("out", "accepted")]


def test_a_number_typed_any_way_reaches_the_same_chat(client, graph):
    _customer_writes(client)
    client.post("/api/chats/09618344433/send", data={"text": "Hello"})
    assert graph.sent[0]["to"] == CUSTOMER
    assert client.get("/api/number", params={"raw": "+91 96183 44433"}).json()["wa_id"] == CUSTOMER
    assert client.get("/api/number", params={"raw": "12345"}).status_code == 422


def test_after_24_hours_only_a_template_may_be_sent(client, graph):
    _customer_writes(client, minutes_ago=25 * 60)
    refused = client.post(f"/api/chats/{CUSTOMER}/send", data={"text": "Hello"})
    assert refused.status_code == 409 and graph.sent == []
    sent = client.post(f"/api/chats/{CUSTOMER}/template",
                       data={"name": "payment_due", "language": "en", "body_values": '["Ravi", "12,500"]'})
    assert sent.status_code == 200, sent.text
    template = graph.sent[0]["template"]
    assert template["components"][0]["parameters"][1]["text"] == "12,500"
    last = client.get(f"/api/chats/{CUSTOMER}").json()["messages"][-1]
    assert last["type"] == "template" and last["body"] == "Dear Ravi, Rs 12,500 is due."


def test_a_template_with_blanks_missing_is_refused_before_sending(client, graph):
    response = client.post(f"/api/chats/{CUSTOMER}/template",
                           data={"name": "payment_due", "language": "en", "body_values": '["Ravi"]'})
    assert response.status_code == 422 and graph.sent == []
    response = client.post(f"/api/chats/{CUSTOMER}/template", data={"name": "gandhi_", "language": "en"})
    assert response.status_code == 422 and "attach" in response.json()["detail"]


def test_an_image_template_uploads_the_picture(client, graph):
    response = client.post(f"/api/chats/{CUSTOMER}/template", data={"name": "gandhi_", "language": "en"},
                           files={"file": ("card.png", io.BytesIO(b"PNG"), "image/png")})
    assert response.status_code == 200, response.text
    assert graph.uploads == [{"filename": "card.png", "mime": "image/png", "size": 3}]
    assert graph.sent[0]["template"]["components"][0]["parameters"][0]["image"] == {"id": "UP1"}


def test_a_pdf_goes_as_a_document_with_its_caption(client, graph, store):
    _customer_writes(client)
    response = client.post(f"/api/chats/{CUSTOMER}/send", data={"text": "Statement attached"},
                           files={"file": ("Statement.pdf", io.BytesIO(b"%PDF"), "application/pdf")})
    assert response.status_code == 200, response.text
    sent = graph.sent[0]
    assert sent["type"] == "document"
    assert sent["document"] == {"id": "UP1", "caption": "Statement attached", "filename": "Statement.pdf"}
    mine = client.get(f"/api/chats/{CUSTOMER}").json()["messages"][-1]
    assert client.get(mine["media_url"].replace("api/", "/api/")).content == b"%PDF"


def test_metas_refusal_comes_back_as_a_sentence(client, graph):
    _customer_writes(client)
    graph.fail_with[CUSTOMER] = ApiError(131026, "Message undeliverable")
    response = client.post(f"/api/chats/{CUSTOMER}/send", data={"text": "Hello"})
    assert response.status_code == 502 and "may not be on WhatsApp" in response.json()["detail"]


def test_opening_a_chat_clears_unread_and_sends_blue_ticks(client, graph, store):
    _customer_writes(client)
    assert client.get("/api/chats").json()[0]["unread"] == 1
    client.post(f"/api/chats/{CUSTOMER}/read")
    assert client.get("/api/chats").json()[0]["unread"] == 0
    assert graph.read == ["wamid.IN5"]


def test_the_bulk_scripts_sends_show_in_the_chats(tmp_path, graph):
    from fastapi.testclient import TestClient

    from conftest import make_config
    from wapp.app import create_app
    from wapp.store import Store

    log = tmp_path / "sent_log.csv"
    log.write_text("time,number,template,result,detail\n"
                   "2026-10-02T07:12:00,919618344433,gandhi_,sent,wamid.G1\n"
                   "2026-10-02T07:12:01,917019170176,gandhi_,failed,131026: x\n", encoding="utf-8")
    config = make_config(tmp_path, send_logs=(log,))
    store = Store(config.db_path)
    # The receipt arrives before the log is read: the row is filled in, not doubled.
    store.set_status("wamid.G1", CUSTOMER, "delivered", int(time.time()))
    client = TestClient(create_app(config, store, graph), base_url="http://127.0.0.1:8790")
    chats = client.get("/api/chats").json()
    assert [c["wa_id"] for c in chats] == [CUSTOMER]
    [message] = client.get(f"/api/chats/{CUSTOMER}").json()["messages"]
    assert message["body"] == "Template: gandhi_" and message["status"] == "delivered"
    assert message["source"] == "script"


def test_from_outside_the_inbox_needs_cloudflare_access_but_the_webhook_does_not(tmp_path, graph):
    from fastapi.testclient import TestClient

    from conftest import make_config
    from wapp.app import create_app
    from wapp.store import Store

    config = make_config(tmp_path, access_team_domain="iravi", access_aud="aud-tag")
    app = create_app(config, Store(config.db_path), graph)
    outside = TestClient(app, base_url="https://whatsapp.ialreports.com")
    assert outside.get("/api/chats", headers={"Cf-Connecting-Ip": "1.2.3.4"}).status_code == 403
    assert outside.get("/", headers={"Cf-Connecting-Ip": "1.2.3.4"}).status_code == 403
    assert outside.get("/privacy").status_code == 200
    raw, headers = signed(delivery([text_message()]))
    assert outside.post("/webhook", content=raw, headers=headers).status_code == 200
    # The same request on this machine needs no token.
    assert TestClient(app, base_url="http://127.0.0.1:8790").get("/api/chats").status_code == 200


def test_the_allowed_list_turns_away_other_accounts(tmp_path, graph):
    from fastapi.testclient import TestClient

    from conftest import make_config
    from wapp.app import create_app
    from wapp.store import Store

    config = make_config(tmp_path, allowed_emails=("owner@example.com",))
    outside = TestClient(create_app(config, Store(config.db_path), graph), base_url="https://whatsapp.ialreports.com")
    def as_(email):
        return {"Cf-Connecting-Ip": "1.2.3.4", "Cf-Access-Authenticated-User-Email": email}
    assert outside.get("/api/chats", headers=as_("someone@example.com")).status_code == 403
    assert outside.get("/api/chats", headers=as_("Owner@example.com")).status_code == 200


def test_the_page_and_its_files_are_served(client):
    assert "WhatsApp inbox" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200
    assert "Privacy notice" in client.get("/privacy").text
