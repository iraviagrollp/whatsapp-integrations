"""The customer menu, driven through the webhook as Meta would drive it."""

import time
from datetime import date

import pytest
from fastapi.testclient import TestClient

from conftest import CUSTOMER, delivery, make_config, signed, text_message
from wapp import bot as botmodule
from wapp.app import create_app
from wapp.bot import ReportsError, rupees
from wapp.config import BotConfig
from wapp.store import Store

OWNER = "919701491148"
SUPPLIER = "919848012345"
VISITOR = "919000000001"


class FakeReports:
    def __init__(self):
        self.customers = {CUSTOMER: [{"id": "11", "name": "Vijayawada Agri", "city": "Vijayawada", "code": "C1"}]}
        self.suppliers = {SUPPLIER: [{"id": "21", "name": "Coromandel Fertilisers", "city": "Hyderabad"}]}
        self.down = False
        self.statements = []
        self.balances = {"11": "1234567.5", "12": "-2500"}

    def customers_by_mobile(self, wa_id):
        if self.down:
            raise ReportsError("the reports service is not reachable")
        return self.customers.get(wa_id, [])

    def suppliers_by_mobile(self, wa_id):
        if self.down:
            raise ReportsError("the reports service is not reachable")
        return self.suppliers.get(wa_id, [])

    def balance(self, account_id, as_on):
        if self.down:
            raise ReportsError("the reports service is not reachable")
        name = {"11": "Vijayawada Agri", "12": "Vijayawada Agri Seeds"}[account_id]
        return {"customer": name, "as_on": as_on.isoformat(), "balance": self.balances[account_id],
                "is_receivable": not self.balances[account_id].startswith("-")}

    def statement(self, account_id, end):
        if self.down:
            raise ReportsError("the reports service is not reachable")
        self.statements.append((account_id, end))
        return ({"customer": "Vijayawada Agri", "start": "2024-04-01", "end": end.isoformat(),
                 "closing_balance": "1234567.5", "is_receivable": True,
                 "file_name": f"Statement_{account_id}.pdf"}, b"%PDF-ledger")


@pytest.fixture
def reports():
    return FakeReports()


@pytest.fixture
def chat(tmp_path, graph, reports):
    config = make_config(tmp_path, alert_numbers=(OWNER,), bot=BotConfig(enabled=True))
    store = Store(config.db_path)
    client = TestClient(create_app(config, store, graph, reports), base_url="http://127.0.0.1:8790")
    counter = iter(range(1, 10_000))

    def say(text=None, reply_id=None, title=None, sender=CUSTOMER, list_reply=False):
        n = next(counter)
        if reply_id:
            kind = "list_reply" if list_reply else "button_reply"
            message = {"from": sender, "id": f"wamid.C{n}", "timestamp": str(int(time.time())),
                       "type": "interactive",
                       "interactive": {"type": kind, kind: {"id": reply_id, "title": title or reply_id}}}
        else:
            message = text_message(text, wamid=f"wamid.C{n}", sender=sender)
        raw, headers = signed(delivery([message]))
        before = len(graph.sent)
        assert client.post("/webhook", content=raw, headers=headers).status_code == 200
        return graph.sent[before:]

    chat.store = store
    say.store = store
    say.client = client
    return say


def buttons(sent):
    return [b["reply"]["title"] for b in sent["interactive"]["action"]["buttons"]]


def test_a_customer_saying_hi_gets_the_menu(chat):
    [menu] = chat("Hi")
    assert menu["to"] == CUSTOMER and menu["type"] == "interactive"
    assert "Vijayawada Agri" in menu["interactive"]["body"]["text"]
    assert buttons(menu) == ["Ledger", "Balance"]


def test_someone_who_is_not_a_customer_is_left_to_a_person(chat):
    sent = chat("Hi, what is the rate of urea?", sender="919000000001")
    # No menu for them - only the alert to the owner that somebody wrote.
    assert [s["to"] for s in sent] == [OWNER]
    assert "rate of urea" in sent[0]["text"]["body"]


@pytest.fixture
def later(monkeypatch):
    """The delayed thank-you, held here instead of on a timer: ``later.run()`` sends it."""
    waiting = []
    monkeypatch.setattr(botmodule, "run_later", lambda seconds, action: waiting.append((seconds, action)))

    def run():
        for _seconds, action in waiting:
            action()
        waiting.clear()

    later.waiting = waiting
    later.run = run
    return later


def test_a_visitor_saying_hi_gets_the_visitor_menu(chat):
    [menu] = chat("Hii", sender=VISITOR)
    assert menu["to"] == VISITOR
    assert menu["interactive"]["body"]["text"] == "Welcome to IRAVI AGRO LIFE LLP.\n\nHow can we help you?"
    assert buttons(menu) == ["Reach us", "Talk to us"]


def test_reach_us_sends_the_office_location_then_thanks_half_a_minute_later(chat, graph, later):
    chat("Hello", sender=VISITOR)
    [pin] = chat(reply_id="reach_us", title="Reach us", sender=VISITOR)
    assert pin["type"] == "location"
    assert pin["location"] == {"latitude": 17.4855564, "longitude": 78.4145767, "name": "IRAVI AGRO LIFE LLP"}
    assert [seconds for seconds, _ in later.waiting] == [30]
    before = len(graph.sent)
    later.run()
    [thanks] = graph.sent[before:]
    assert thanks["to"] == VISITOR
    assert thanks["text"]["body"].startswith("Thanks a lot for reaching out to us!")
    assert "https://www.instagram.com/iraviagrolife/" in thanks["text"]["body"]


def test_talk_to_us_gives_the_number_and_email_and_thanks_only_once(chat, later):
    chat("Namaste", sender=VISITOR)
    [contact] = chat(reply_id="talk_to_us", title="Talk to us", sender=VISITOR)
    assert "8977417663" in contact["text"]["body"] and "info@iraviagrolife.com" in contact["text"]["body"]
    # The other button still answers, typed as well as tapped - but one thank-you is enough.
    [pin] = chat("1", sender=VISITOR)
    assert pin["type"] == "location"
    assert len(later.waiting) == 1


def test_a_visitor_added_to_a_customer_record_gets_the_ledger_menu_on_their_next_hi(chat, reports):
    chat("Hi", sender=VISITOR)
    reports.customers[VISITOR] = [{"id": "11", "name": "Vijayawada Agri", "city": "Vijayawada", "code": "C1"}]
    [menu] = chat("Hi", sender=VISITOR)
    assert buttons(menu) == ["Ledger", "Balance"]


def test_a_visitors_question_is_left_to_a_person(chat):
    chat("Hi", sender=VISITOR)
    sent = chat("Do you have dealership in Guntur?", sender=VISITOR)
    assert [s["to"] for s in sent] == [OWNER]


def test_no_thank_you_if_a_person_answers_first(chat, graph, later):
    chat("Hi", sender=VISITOR)
    chat(reply_id="talk_to_us", sender=VISITOR)
    chat.store.add_message(wamid="wamid.HUMAN", wa_id=VISITOR, direction="out", type="text", body="Hello!",
                           status="accepted", source="inbox", extra={"by": "staff"})
    before = len(graph.sent)
    later.run()
    assert graph.sent[before:] == []


def test_a_supplier_saying_hi_is_left_to_a_person(chat):
    sent = chat("Hi", sender=SUPPLIER)
    assert [s["to"] for s in sent] == [OWNER]


def test_no_visitor_menu_while_the_reports_service_is_down(chat, reports):
    reports.down = True
    sent = chat("Hi", sender=VISITOR)
    assert [s["to"] for s in sent] == [OWNER]


def test_ledger_sends_the_pdf_then_asks_for_more(chat, graph, reports):
    chat("Hi")
    preparing, document, more = chat(reply_id="ledger", title="Ledger")
    assert "Preparing" in preparing["text"]["body"]
    assert document["type"] == "document"
    assert document["document"]["filename"] == "Statement_11.pdf"
    caption = document["document"]["caption"]
    assert "Vijayawada Agri" in caption and "01-04-2024" in caption and "Balance due: ₹12,34,567.50" in caption
    assert graph.uploads == [{"filename": "Statement_11.pdf", "mime": "application/pdf", "size": 11}]
    assert reports.statements == [("11", date.today())]
    assert more["interactive"]["body"]["text"] == "Do you need any more support?"
    assert buttons(more) == ["Yes", "No"]


def test_balance_shows_what_they_owe_then_yes_goes_back_to_the_menu(chat):
    chat("Hi")
    balance, more = chat(reply_id="balance", title="Balance")
    text = balance["text"]["body"]
    assert text.startswith("Your outstanding balance with IRAVI AGRO LIFE LLP as on ")
    assert "*₹12,34,567.50*" in text
    assert buttons(more) == ["Yes", "No"]
    [menu] = chat(reply_id="yes", title="Yes")
    assert buttons(menu) == ["Ledger", "Balance"]


def test_no_says_goodbye_and_a_thank_you_after_it_is_left_alone(chat):
    chat("Hi")
    chat(reply_id="balance")
    [goodbye] = chat(reply_id="no", title="No")
    assert "It was a pleasure serving you" in goodbye["text"]["body"]
    assert "IRAVI AGRO LIFE LLP" in goodbye["text"]["body"]
    # "Thank you" straight after: the bot does not start over; the owner is told.
    sent = chat("Thank you")
    assert [s["to"] for s in sent] == [OWNER]


def test_typed_answers_work_as_well_as_buttons(chat):
    chat("hello")
    assert "outstanding balance" in chat("2")[0]["text"]["body"]
    assert buttons(chat("YES")[0]) == ["Ledger", "Balance"]
    assert chat("1")[1]["type"] == "document"


def test_an_answer_the_bot_does_not_understand_repeats_the_question(chat):
    chat("Hi")
    [again] = chat("what about my order?")
    assert again["interactive"]["body"]["text"] == "Please choose one of the options below."


def test_a_number_on_two_accounts_is_asked_which(chat, reports):
    reports.customers[CUSTOMER] = [{"id": "11", "name": "Vijayawada Agri", "city": "Vijayawada"},
                                   {"id": "12", "name": "Vijayawada Agri Seeds", "city": "Vijayawada"}]
    [menu] = chat("Hi")
    assert "Vijayawada Agri" not in menu["interactive"]["body"]["text"]  # no single name to greet
    [choose] = chat(reply_id="ledger")
    rows = choose["interactive"]["action"]["sections"][0]["rows"]
    assert [r["id"] for r in rows] == ["acct:11", "acct:12"]
    sent = chat(reply_id="acct:12", title="Vijayawada Agri Seeds", list_reply=True)
    assert reports.statements[-1][0] == "12"
    assert sent[-1]["interactive"]["body"]["text"] == "Do you need any more support?"


def test_when_the_reports_service_is_down_the_customer_and_staff_are_told(chat, reports):
    chat("Hi")
    reports.down = True
    sent = chat(reply_id="ledger")
    texts = [(s["to"], s.get("text", {}).get("body", "")) for s in sent]
    assert any(to == CUSTOMER and "could not prepare your ledger" in body for to, body in texts)
    assert any(to == OWNER and "Vijayawada Agri" in body for to, body in texts)


def test_the_bot_stays_out_of_a_chat_a_person_has_answered(chat, graph):
    chat("Hi")
    chat.client.post(f"/api/chats/{CUSTOMER}/send", data={"text": "Sir, I will call you"})
    sent = chat("ok")
    assert [s["to"] for s in sent] == [OWNER]  # alert only - no bot reply


def test_a_conversation_left_idle_starts_again(chat):
    chat("Hi")
    chat.store.save_session(CUSTOMER, botmodule.MORE, {"accounts": [{"id": "11", "name": "Vijayawada Agri"}]})
    with chat.store._connect() as db:
        db.execute("UPDATE bot_sessions SET updated_at = ?", (int(time.time()) - 31 * 60,))
    [menu] = chat("Hello")
    assert buttons(menu) == ["Ledger", "Balance"]


def test_the_bots_messages_show_in_the_inbox(chat):
    chat("Hi")
    chat(reply_id="ledger")
    messages = chat.client.get(f"/api/chats/{CUSTOMER}").json()["messages"]
    mine = [m for m in messages if m["direction"] == "out"]
    assert {m["source"] for m in mine} == {"bot"}
    document = next(m for m in mine if m["type"] == "document")
    assert chat.client.get("/" + document["media_url"]).content == b"%PDF-ledger"
    # The customer's tap is stored with the title they saw.
    assert [m["body"] for m in messages if m["direction"] == "in"] == ["Hi", "ledger"]


@pytest.mark.parametrize("amount, shown", [
    ("1234567.5", "₹12,34,567.50"), ("-500", "₹500.00"), ("999", "₹999.00"), ("100000", "₹1,00,000.00"),
])
def test_rupees_in_indian_grouping(amount, shown):
    assert rupees(amount) == shown


# ------------------------------------------------------------ greetings only

@pytest.mark.parametrize("text", [
    "Hi", "hii", "HIIII!!", "Hello", "helloo sir", "Hlo", "Hey", "Namaste 🙏", "namaskaram garu",
    "Good morning", "good morning anna", "Gm", "hi hello", "Hello, good evening sir",
])
def test_a_greeting_starts_the_menu(chat, text):
    [menu] = chat(text)
    assert buttons(menu) == ["Ledger", "Balance"]


@pytest.mark.parametrize("text", [
    "When will my order be dispatched?", "hi, what is the rate of urea", "Need 20 bags",
    "ok", "🙏", "thank you", "Hi I paid 50000 today",
])
def test_anything_else_is_left_to_a_person(chat, text):
    sent = chat(text)
    assert [s["to"] for s in sent] == [OWNER]  # the alert, and no menu


def test_the_templates_button_starts_the_menu(chat):
    message_from_template = {"from": CUSTOMER, "id": "wamid.TPL", "timestamp": str(int(time.time())),
                             "type": "button", "button": {"text": "Get started", "payload": "Get started"}}
    raw, headers = signed(delivery([message_from_template]))
    chat.client.post("/webhook", content=raw, headers=headers)
    last = chat.client.get(f"/api/chats/{CUSTOMER}").json()["messages"][-1]
    assert last["source"] == "bot" and "[Ledger]  [Balance]" in last["body"]


def test_a_tap_on_an_old_menu_still_works(chat, reports):
    """Yesterday's menu, tapped today: the balance comes straight back."""
    [balance, more] = chat(reply_id="balance", title="Balance")
    assert "*₹12,34,567.50*" in balance["text"]["body"]
    assert buttons(more) == ["Yes", "No"]


# ------------------------------------------------------------------ balance

def test_an_advance_is_shown_as_an_advance(chat, reports):
    reports.balances["11"] = "-2500"
    chat("Hi")
    text = chat(reply_id="balance")[0]["text"]["body"]
    assert "no outstanding balance" in text and "advance with us is *₹2,500.00*" in text


def test_a_settled_account_says_so(chat, reports):
    reports.balances["11"] = "0.00"
    chat("Hi")
    assert "fully settled" in chat(reply_id="balance")[0]["text"]["body"]


def test_a_number_on_two_accounts_is_asked_which_for_the_balance(chat, reports):
    reports.customers[CUSTOMER] = [{"id": "11", "name": "Vijayawada Agri", "city": "Vijayawada"},
                                   {"id": "12", "name": "Vijayawada Agri Seeds", "city": "Vijayawada"}]
    chat("Hi")
    [choose] = chat(reply_id="balance")
    assert choose["interactive"]["type"] == "list"
    balance, more = chat(reply_id="acct:12", list_reply=True)
    text = balance["text"]["body"]
    assert text.startswith("*Vijayawada Agri Seeds*") and "advance with us is *₹2,500.00*" in text
    assert reports.statements == []  # the balance never makes a PDF


def test_a_fresh_greeting_after_more_support_shows_the_menu(chat):
    chat("Hi")
    chat(reply_id="balance")
    [menu] = chat("hello")
    assert buttons(menu) == ["Ledger", "Balance"]


def test_the_templates_button_works_right_after_a_goodbye(chat):
    """What happened on 2 Oct: No -> goodbye, then a tap on the template's Hi seconds later."""
    chat("Hi")
    chat(reply_id="balance")
    chat(reply_id="no")
    tap = {"from": CUSTOMER, "id": "wamid.TAP", "timestamp": str(int(time.time())),
           "type": "button", "button": {"text": "Hi", "payload": "Hi"}}
    raw, headers = signed(delivery([tap]))
    chat.client.post("/webhook", content=raw, headers=headers)
    last = chat.client.get(f"/api/chats/{CUSTOMER}").json()["messages"][-1]
    assert last["source"] == "bot" and "[Ledger]  [Balance]" in last["body"]


def test_a_typed_hi_right_after_a_goodbye_starts_again_too(chat):
    chat("Hi")
    chat(reply_id="balance")
    chat(reply_id="no")
    [menu] = chat("hi")
    assert buttons(menu) == ["Ledger", "Balance"]


# ------------------------------------- Ledger / Balance asked for at any point

def test_balance_tapped_on_the_first_menu_after_the_ledger_is_answered(chat, reports):
    """What happened on 3 Oct: Ledger arrived, then Balance tapped on the first menu."""
    chat("Hi")
    chat(reply_id="ledger")
    balance, more = chat(reply_id="balance", title="Balance")
    assert "outstanding balance" in balance["text"]["body"]
    assert buttons(more) == ["Yes", "No"]


@pytest.mark.parametrize("text", ["Balance", "balance", "BALANCE!", "Outstanding balance", "dues"])
def test_balance_typed_with_no_conversation_open_is_answered(chat, text):
    balance, more = chat(text)
    assert "*₹12,34,567.50*" in balance["text"]["body"]
    assert buttons(more) == ["Yes", "No"]


def test_ledger_typed_with_no_conversation_open_sends_the_pdf(chat, reports):
    preparing, document, more = chat("ledger")
    assert document["type"] == "document" and reports.statements


def test_ledger_typed_after_the_goodbye_is_answered(chat):
    chat("Hi")
    chat(reply_id="balance")
    chat(reply_id="no")
    assert chat("Ledger")[1]["type"] == "document"


def test_a_bare_number_outside_the_menu_is_left_to_a_person(chat):
    sent = chat("2")
    assert [s["to"] for s in sent] == [OWNER]


def test_typed_ledger_from_a_stranger_is_left_to_a_person(chat):
    sent = chat("ledger", sender="919000000001")
    assert [s["to"] for s in sent] == [OWNER]


def test_changing_from_ledger_to_balance_while_choosing_the_account(chat, reports):
    reports.customers[CUSTOMER] = [{"id": "11", "name": "Vijayawada Agri", "city": ""},
                                   {"id": "12", "name": "Vijayawada Agri Seeds", "city": ""}]
    chat("Hi")
    chat(reply_id="ledger")              # asked which account
    chat(reply_id="balance")             # changed their mind: asked again which account
    balance, _more = chat(reply_id="acct:11", list_reply=True)
    assert "outstanding balance" in balance["text"]["body"] and reports.statements == []


def test_the_menu_greets_the_customer_by_name(chat):
    [menu] = chat("Hi")
    assert menu["interactive"]["body"]["text"] == "Vijayawada Agri! 🙏\n\nHow can we help you today?"


def test_a_number_on_two_accounts_is_simply_welcomed(chat, reports):
    reports.customers[CUSTOMER] = [{"id": "11", "name": "Vijayawada Agri", "city": ""},
                                   {"id": "12", "name": "Vijayawada Agri Seeds", "city": ""}]
    [menu] = chat("Hi")
    assert menu["interactive"]["body"]["text"] == "Welcome! 🙏\n\nHow can we help you today?"
