"""The customer menu, answered on WhatsApp without anybody at the desk.

A customer greets the business - "Hi", "hii", "Hello sir", "Namaste" - or taps
the button on the services template, and, if their number is on a customer
record, gets::

    How can we help you today?      [ Ledger ]  [ Balance ]

    Ledger   -> their ledger statement as a PDF, from the reports service
    Balance  -> what they owe the firm today, as one line
    then     -> "Do you need any more support?"   [ Yes ]  [ No ]
                Yes: the menu again.  No: the goodbye.

Only a greeting starts the menu.  A customer who writes a real question - "when
will my order be dispatched?" - is left to a person, not answered with a menu
that does not answer it.  So is anybody whose number is on no customer record,
and any chat a person has just answered from the inbox: the bot never talks
over the staff.

The states, kept per number in the store::

    menu    the menu was sent; waiting for Ledger or Contact Details
    choose  their number is on several customer records; waiting for which one
    more    "any more support?" was asked; waiting for Yes or No
    done    goodbye said; the next greeting starts the menu again
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import phone
from .config import BotConfig
from .graph import ApiError, Graph
from .store import Store

logger = logging.getLogger(__name__)

MENU, CHOOSE, MORE, DONE = "menu", "choose", "more", "done"

LEDGER, BALANCE, YES, NO = "ledger", "balance", "yes", "no"
MENU_BUTTONS = [(LEDGER, "Ledger"), (BALANCE, "Balance")]
#: What a typed answer may say instead of tapping a button.
TYPED = {
    LEDGER: {"1", "ledger", "ledger statement", "statement"},
    BALANCE: {"2", "balance", "outstanding", "outstanding balance", "due", "dues"},
    YES: {"yes", "y", "yeah", "yes please", "ok", "okay"},
    NO: {"no", "n", "nope", "no thanks", "no thank you"},
}
#: Words that may come with a greeting without making it a question.
COURTESIES = {"sir", "madam", "mam", "maam", "anna", "garu", "ji", "team", "iravi", "agro", "life",
              "bro", "brother", "akka", "bhai", "there", "all", "dear", "everyone", "friend"}


def _squeeze(text: str) -> str:
    """Lower case, letters and single spaces only, no letter twice in a row: "Hiii!!" -> "hi"."""
    letters = re.sub(r"[^a-z\s]", " ", (text or "").lower())
    return re.sub(r"(.)\1+", r"\1", " ".join(letters.split()))


def is_greeting(text: Optional[str], greetings) -> bool:
    """Whether a message is just a greeting - "Hi", "hii", "Hello sir", "Good morning anna".

    A greeting with a question after it ("hi, what is the rate of urea") is not
    one: the question needs a person.
    """
    known = {_squeeze(g) for g in greetings}
    courtesies = {_squeeze(c) for c in COURTESIES}
    words = [w for w in _squeeze(text).split() if w not in courtesies]
    if not words:
        return False
    if " ".join(words) in known:
        return True
    # "hi hello", "hello good morning": every part a greeting of its own.
    phrase, parts = "", []
    for word in words:
        phrase = f"{phrase} {word}".strip()
        if phrase in known:
            parts.append(phrase)
            phrase = ""
    return not phrase and bool(parts)


class ReportsError(Exception):
    """The reports service could not answer."""


class ReportsClient:
    """The reports service, over HTTP: the only thing here that knows it exists."""

    def __init__(self, base_url: str, timeout: int = 180):
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    def _open(self, request: urllib.request.Request) -> bytes:
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read()).get("detail", exc.reason)
            except Exception:
                detail = exc.reason
            raise ReportsError(f"{exc.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ReportsError(f"the reports service is not reachable ({exc})") from None

    def customers_by_mobile(self, wa_id: str) -> List[Dict[str, Any]]:
        number = urllib.parse.quote(wa_id)
        return json.loads(self._open(urllib.request.Request(f"{self.base}/api/customers/by-mobile?number={number}")))

    def balance(self, account_id: str, as_on: date) -> Dict[str, Any]:
        """What a customer owes today - the figure their statement closes on, with no PDF."""
        url = f"{self.base}/api/receivables/balance/{urllib.parse.quote(str(account_id))}?as_on={as_on.isoformat()}"
        return json.loads(self._open(urllib.request.Request(url)))

    def statement(self, account_id: str, end: date) -> Tuple[Dict[str, Any], bytes]:
        """Produce a customer's ledger statement; returns its summary and the PDF."""
        request = urllib.request.Request(
            f"{self.base}/api/receivables/statement",
            data=json.dumps({"account_id": str(account_id), "end": end.isoformat()}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        summary = json.loads(self._open(request))
        name = urllib.parse.quote(summary["file_name"])
        pdf = self._open(urllib.request.Request(f"{self.base}/api/receivables/file/statement/{name}"))
        return summary, pdf


def rupees(amount: str) -> str:
    """₹12,34,567.89 - Indian grouping, as the sheets print it."""
    try:
        value = abs(Decimal(amount)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError):
        return f"₹{amount}"
    whole, paise = f"{value:.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        head = ",".join(re.findall(r"\d{1,2}", head[::-1]))[::-1]
        whole = f"{head},{tail}"
    return f"₹{whole}.{paise}"


def asked_for(message: Dict[str, Any]) -> Optional[str]:
    """Ledger or Balance, asked for by name - tapped on any menu, or typed as a word.

    Answered whatever the conversation was doing: a customer who taps Balance
    on the first menu after the ledger arrived wants their balance, not to be
    told to tap Yes or No.  A bare "1" or "2" is not counted here - outside the
    menu it means nothing.
    """
    reply = (message.get("reply_id") or "").lower()
    if reply in (LEDGER, BALANCE):
        return reply
    text = re.sub(r"[^\w\s]", "", (message.get("body") or "").lower()).strip()
    for choice in (LEDGER, BALANCE):
        if text in TYPED[choice] and not text.isdigit():
            return choice
    return None


def understood(message: Dict[str, Any], *choices: str) -> Optional[str]:
    """Which of ``choices`` the customer picked - by button, or by typing it."""
    reply = (message.get("reply_id") or "").lower()
    if reply in choices:
        return reply
    text = re.sub(r"[^\w\s]", "", (message.get("body") or "").lower()).strip()
    for choice in choices:
        if text in TYPED.get(choice, ()):
            return choice
    return None


class Bot:
    def __init__(
        self,
        config: BotConfig,
        store: Store,
        graph: Graph,
        reports: ReportsClient,
        record_sent: Callable[..., Optional[int]],
        tell_staff: Callable[[str], None],
        today: Callable[[], date] = date.today,
    ):
        self.config = config
        self.store = store
        self.graph = graph
        self.reports = reports
        self.record_sent = record_sent
        self.tell_staff = tell_staff
        self.today = today
        self._locks: Dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock(self, wa_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(wa_id, threading.Lock())

    # ------------------------------------------------------------- sending

    def _send(self, wa_id: str, message: Dict[str, Any], shown: str, **kept: Any) -> None:
        """Send ``message``; keep ``shown`` - what the customer sees - in the chat record."""
        wamid = self.graph.send(wa_id, message)
        self.record_sent(wa_id, wamid, body=shown, who=None, source="bot", **kept)

    def _text(self, wa_id: str, text: str) -> None:
        self._send(wa_id, {"type": "text", "text": {"body": text}}, text, kind="text")

    def _buttons(self, wa_id: str, text: str, buttons: List[Tuple[str, str]]) -> None:
        content = {
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": text},
                "action": {"buttons": [{"type": "reply", "reply": {"id": i, "title": t}} for i, t in buttons]},
            },
        }
        shown = text + "\n" + "  ".join(f"[{t}]" for _i, t in buttons)
        self._send(wa_id, content, shown, kind="interactive")

    def _menu(self, wa_id: str, accounts: List[Dict[str, Any]], again: bool = False) -> None:
        if again:
            text = "How else can we help you?"
        else:
            # "VAMSI ENTERPRISES! 🙏" - or, for a number on several accounts,
            # no one name to greet them by.
            name = accounts[0]["name"].strip() if len(accounts) == 1 else "Welcome"
            text = f"{name}! 🙏\n\nHow can we help you today?"
        self._buttons(wa_id, text, MENU_BUTTONS)

    def _ask_more(self, wa_id: str) -> None:
        self._buttons(wa_id, "Do you need any more support?", [(YES, "Yes"), (NO, "No")])

    def _choose_account(self, wa_id: str, accounts: List[Dict[str, Any]]) -> None:
        rows = [{"id": f"acct:{a['id']}", "title": a["name"][:24],
                 "description": (a.get("city") or "")[:72]} for a in accounts[:10]]
        content = {
            "type": "interactive",
            "interactive": {
                "type": "list",
                "body": {"text": "Your number is on more than one account with us. Which account is this for?"},
                "action": {"button": "Choose account", "sections": [{"title": "Accounts", "rows": rows}]},
            },
        }
        shown = "Which account is this for?\n" + "\n".join(f"{i}. {a['name']}" for i, a in enumerate(accounts, 1))
        self._send(wa_id, content, shown, kind="interactive")

    # ---------------------------------------------------------- the ledger

    def _ledger(self, wa_id: str, account: Dict[str, Any]) -> None:
        self._text(wa_id, "Preparing your ledger statement, please wait a moment… ⏳")
        try:
            summary, pdf = self.reports.statement(account["id"], self.today())
        except ReportsError as exc:
            logger.warning("Could not produce the ledger for %s (%s): %s", wa_id, account["name"], exc)
            self._text(wa_id, "Sorry, we could not prepare your ledger just now. "
                              "Our team has been told and will send it to you shortly.")
            self.tell_staff(f"The WhatsApp bot could not send the ledger of {account['name']} "
                            f"to {phone.display(wa_id)}: {exc}")
            return
        start = date.fromisoformat(summary["start"]).strftime("%d-%m-%Y")
        end = date.fromisoformat(summary["end"]).strftime("%d-%m-%Y")
        closing = summary.get("closing_balance", "0")
        try:
            owed = Decimal(closing)
        except InvalidOperation:
            owed = Decimal(0)
        balance = ("Balance due: " if owed > 0 else "Balance in your favour: " if owed < 0 else "Balance: ") \
            + rupees(closing)
        caption = f"Ledger statement - {summary.get('customer') or account['name']}\n{start} to {end}\n{balance}"
        media_id = self.graph.upload_media(pdf, summary["file_name"], "application/pdf")
        content = {"type": "document",
                   "document": {"id": media_id, "filename": summary["file_name"], "caption": caption}}
        self._send(wa_id, content, caption, kind="document", content=pdf,
                   mime="application/pdf", filename=summary["file_name"])

    # ---------------------------------------------------------- the balance

    def _balance(self, wa_id: str, account: Dict[str, Any], several: bool) -> None:
        try:
            found = self.reports.balance(account["id"], self.today())
        except ReportsError as exc:
            logger.warning("Could not read the balance for %s (%s): %s", wa_id, account["name"], exc)
            self._text(wa_id, "Sorry, we could not check your balance just now. "
                              "Our team has been told and will send it to you shortly.")
            self.tell_staff(f"The WhatsApp bot could not send the balance of {account['name']} "
                            f"to {phone.display(wa_id)}: {exc}")
            return
        on = date.fromisoformat(found["as_on"]).strftime("%d-%m-%Y")
        try:
            owed = Decimal(found["balance"])
        except (InvalidOperation, KeyError, TypeError):
            owed = Decimal(0)
        who = f"*{found.get('customer') or account['name']}*\n" if several else ""
        if owed > 0:
            text = (f"{who}Your outstanding balance with IRAVI AGRO LIFE LLP as on {on} is "
                    f"*{rupees(found['balance'])}*.")
        elif owed < 0:
            text = (f"{who}You have no outstanding balance with IRAVI AGRO LIFE LLP as on {on}. "
                    f"Your advance with us is *{rupees(found['balance'])}*.")
        else:
            text = f"{who}Your account with IRAVI AGRO LIFE LLP is fully settled as on {on}. Thank you!"
        self._text(wa_id, text)

    def _serve(self, wa_id: str, want: str, account: Dict[str, Any], several: bool) -> None:
        if want == BALANCE:
            self._balance(wa_id, account, several)
        else:
            self._ledger(wa_id, account)

    # ------------------------------------------------------- the conversation

    def handle(self, message: Dict[str, Any]) -> bool:
        """Answer one incoming message, if it is the bot's to answer.  True if it was."""
        if not self.config.enabled:
            return False
        wa_id = message["wa_id"]
        with self._lock(wa_id):
            try:
                return self._handle(wa_id, message)
            except ApiError as exc:
                logger.warning("The bot could not answer %s: %s", wa_id, exc)
                return False

    def _handle(self, wa_id: str, message: Dict[str, Any]) -> bool:
        now = time.time()
        human = self.store.last_human_reply(wa_id)
        if human and now - human < self.config.pause_after_human_minutes * 60:
            return False  # a person is handling this chat

        # After the goodbye, or after a long silence, the next greeting starts
        # afresh.  A "thank you" after the goodbye is no greeting, so it goes
        # to a person below without any special rule for it.
        session = self.store.session(wa_id)
        if session and (session["state"] == DONE or now - session["updated_at"] > self.config.idle_minutes * 60):
            session = None

        asked = asked_for(message)
        if session is None:
            # Only a greeting, the template's button, or Ledger / Balance -
            # tapped on an earlier menu or typed - starts a conversation;
            # anything else is a question for a person.
            tapped = asked is not None
            if not (message.get("type") == "button" or tapped
                    or is_greeting(message.get("body"), self.config.greetings)):
                return False
            try:
                accounts = self.reports.customers_by_mobile(wa_id)
            except ReportsError as exc:
                logger.warning("Could not look up %s: %s", wa_id, exc)
                return False
            if not accounts:
                return False  # not a customer: left to a person
            session = {"state": MENU, "data": {"accounts": accounts}}
            if not tapped:
                self._menu(wa_id, accounts)
                self.store.save_session(wa_id, MENU, session["data"])
                return True
            # A button from an earlier menu: do what it asks straight away.

        data = session["data"]
        accounts = data.get("accounts", [])
        state = session["state"]
        if asked and state == MORE:
            # "Any more support?" answered by tapping Balance on an earlier
            # menu: that is a yes, and what they want - so give it.
            state = MENU

        if state == MENU:
            choice = understood(message, LEDGER, BALANCE)
            if choice is None:
                self._buttons(wa_id, "Please choose one of the options below.", MENU_BUTTONS)
                self.store.save_session(wa_id, MENU, data)
                return True
            if len(accounts) > 1:
                self._choose_account(wa_id, accounts)
                self.store.save_session(wa_id, CHOOSE, {**data, "want": choice})
                return True
            self._serve(wa_id, choice, accounts[0], several=False)
            self._ask_more(wa_id)
            self.store.save_session(wa_id, MORE, data)
            return True

        if state == CHOOSE:
            if asked:
                data = {**data, "want": asked}  # changed their mind while choosing the account
            account = self._picked_account(message, accounts)
            if account is None:
                self._choose_account(wa_id, accounts)
                self.store.save_session(wa_id, CHOOSE, data)
                return True
            self._serve(wa_id, data.get("want", LEDGER), account, several=True)
            self._ask_more(wa_id)
            self.store.save_session(wa_id, MORE, data)
            return True

        if state == MORE:
            choice = understood(message, YES, NO)
            if choice is None and is_greeting(message.get("body"), self.config.greetings):
                choice = YES  # a fresh "hi" mid-way: the menu again
            if choice == YES:
                self._menu(wa_id, accounts, again=True)
                self.store.save_session(wa_id, MENU, data)
            elif choice == NO:
                self._text(wa_id, self.config.farewell)
                self.store.save_session(wa_id, DONE, data)
            else:
                self._buttons(wa_id, "Please tap Yes or No - do you need any more support?",
                              [(YES, "Yes"), (NO, "No")])
                self.store.save_session(wa_id, MORE, data)
            return True

        return False

    @staticmethod
    def _picked_account(message: Dict[str, Any], accounts: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        reply = message.get("reply_id") or ""
        if reply.startswith("acct:"):
            return next((a for a in accounts if str(a["id"]) == reply[5:]), None)
        typed = (message.get("body") or "").strip()
        if typed.isdigit() and 1 <= int(typed) <= len(accounts):
            return accounts[int(typed) - 1]
        return None
