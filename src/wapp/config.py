"""Settings for the WhatsApp inbox, read once from ``config/config.json``.

Secrets are not in that file.  The access token and the app secret each live in
a file of their own, named by ``token_file`` and ``app_secret_file`` - so the
token can stay in the one ``Token.txt`` the greeting script already reads, and
nobody pastes it into a second place to keep in step.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Tuple

#: The project folder: ``wapp-integrations``.
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "config" / "config.json"


class ConfigError(Exception):
    """The settings could not be read, or are incomplete."""


DEFAULT_FAREWELL = ("It was a pleasure serving you, looking forward to a continued partnership "
                    "with IRAVI AGRO LIFE LLP. 🙏")
#: What starts the menu.  Matched without regard to case, punctuation or a
#: letter typed twice over ("Hiii!!" is "hi"), and with "sir", "anna" and the
#: like allowed around it ("Hello sir").
DEFAULT_GREETINGS = (
    "hi", "hello", "hey", "hai", "hlo", "hola", "namaste", "namaskar", "namaskaram",
    "good morning", "good afternoon", "good evening", "gm", "start", "get started", "menu",
)


@dataclass(frozen=True)
class BotConfig:
    """The customer menu: Ledger / Balance, answered without anybody at the desk."""

    enabled: bool = False
    #: The reports service, which finds a customer by mobile and makes the ledger PDF.
    reports_url: str = "http://127.0.0.1:8787"
    greetings: Tuple[str, ...] = DEFAULT_GREETINGS
    farewell: str = DEFAULT_FAREWELL
    #: A conversation left this long starts again from the menu.
    idle_minutes: int = 30
    #: Once someone replies from the inbox, the bot stays out of that chat for this long.
    pause_after_human_minutes: int = 60


@dataclass(frozen=True)
class Config:
    #: The ID of the sending number - not the number itself.
    phone_number_id: str
    #: The WhatsApp Business Account the number and its templates belong to.
    waba_id: str
    #: Meta's word for the shared secret checked on the webhook handshake.
    verify_token: str
    token: str
    #: Signs every webhook Meta sends.  Without it nothing is accepted.
    app_secret: str
    business_name: str = "Iravi Agro Life LLP"
    api_version: str = "v23.0"
    host: str = "127.0.0.1"
    port: int = 8790
    data_dir: Path = ROOT / "data"
    #: Where the inbox is reached from outside - used in the alert links.
    public_url: str = ""
    #: Numbers told on WhatsApp about each new message, e.g. the owner's mobile.
    alert_numbers: Tuple[str, ...] = ()
    #: A utility template used for an alert when the free 24-hour window to
    #: the alert number has closed.  Two body slots: who wrote, and what.
    alert_template: str = ""
    alert_template_language: str = "en"
    #: Opening a chat sends the blue ticks back to the customer.
    send_read_receipts: bool = True
    #: The greeting script's log, read so its sends show in the chats.
    send_logs: Tuple[Path, ...] = ()
    #: Cloudflare Access - when both are set, every request from outside this
    #: machine must carry a valid Access token for this application.
    access_team_domain: str = ""
    access_aud: str = ""
    #: When set, only these people (as Access reports them) may use the inbox.
    allowed_emails: Tuple[str, ...] = field(default=())
    #: Who to write to about one's data, printed on the public privacy page.
    privacy_contact: str = ""
    bot: BotConfig = field(default_factory=BotConfig)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "inbox.db"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"


def read_secret(path: Path) -> str:
    """The first non-blank line of ``path``, without quotes or spaces pasted around it."""
    if not path.is_file():
        return ""
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        value = line.strip().strip("'\"").strip()
        if value:
            return value
    return ""


def _path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def load_config(path: Optional[Path] = None) -> Config:
    """Read the settings and the two secrets; fail with a sentence if either is missing."""
    path = Path(path or os.environ.get("WAPP_CONFIG") or DEFAULT_CONFIG)
    if not path.is_file():
        raise ConfigError(
            f"{path} not found. Copy config/config.example.json to config/config.json and fill it in."
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from None
    raw = {k: v for k, v in raw.items() if not k.startswith("//")}

    missing = [k for k in ("phone_number_id", "waba_id", "verify_token") if not raw.get(k)]
    if missing:
        raise ConfigError(f"{path} is missing: {', '.join(missing)}")

    token_file = _path(raw.get("token_file", "config/token.txt"))
    secret_file = _path(raw.get("app_secret_file", "config/app_secret.txt"))
    token = os.environ.get("WA_TOKEN", "").strip() or read_secret(token_file)
    if not token:
        raise ConfigError(f"No access token: put it on the first line of {token_file}.")

    return Config(
        phone_number_id=str(raw["phone_number_id"]),
        waba_id=str(raw["waba_id"]),
        verify_token=str(raw["verify_token"]),
        token=token,
        # A missing app secret is not fatal at start-up: the inbox still opens
        # and sends, and the webhook says plainly why it is refusing.
        app_secret=read_secret(secret_file),
        business_name=raw.get("business_name", Config.business_name),
        api_version=raw.get("api_version", Config.api_version),
        host=raw.get("host", Config.host),
        port=int(raw.get("port", Config.port)),
        data_dir=_path(raw.get("data_dir", "data")),
        public_url=raw.get("public_url", "").rstrip("/"),
        alert_numbers=tuple(str(n) for n in raw.get("alert_numbers", [])),
        alert_template=raw.get("alert_template", ""),
        alert_template_language=raw.get("alert_template_language", "en"),
        send_read_receipts=bool(raw.get("send_read_receipts", True)),
        send_logs=tuple(_path(p) for p in raw.get("send_logs", [])),
        access_team_domain=raw.get("access_team_domain", "").strip(),
        access_aud=raw.get("access_aud", "").strip(),
        allowed_emails=tuple(e.lower() for e in raw.get("allowed_emails", [])),
        privacy_contact=raw.get("privacy_contact", ""),
        bot=_bot(raw.get("bot") or {}),
    )


def _bot(raw: dict) -> BotConfig:
    raw = {k: v for k, v in raw.items() if not k.startswith("//")}
    defaults = BotConfig()
    return BotConfig(
        enabled=bool(raw.get("enabled", False)),
        reports_url=raw.get("reports_url", defaults.reports_url).rstrip("/"),
        greetings=tuple(str(g) for g in raw.get("greetings", defaults.greetings)),
        farewell=raw.get("farewell", defaults.farewell),
        idle_minutes=int(raw.get("idle_minutes", defaults.idle_minutes)),
        pause_after_human_minutes=int(raw.get("pause_after_human_minutes", defaults.pause_after_human_minutes)),
    )
