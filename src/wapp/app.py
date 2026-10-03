"""The inbox service: Meta's webhook in, the inbox page and its API out.

    /webhook            Meta's deliveries.  Public, but only what carries Meta's
                        signature (made with the app secret) is accepted.
    /privacy            The privacy notice Meta asks an app to publish.  Public.
    /api/health         Whether the service is up.  Public.
    everything else     The inbox page and its API.  From outside this machine,
                        only through Cloudflare Access - checked again here when
                        the Access settings are filled in.

It listens on 127.0.0.1 only: nothing reaches it but the Cloudflare tunnel and
a browser on this machine.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import phone, templates
from .bot import Bot, ReportsClient
from .config import ROOT, Config, load_config
from .graph import OUTSIDE_WINDOW, ApiError, Graph, media_message, split_mime
from .store import Store
from .webhook import process, signature_ok

logger = logging.getLogger("wapp")

WEB_DIR = ROOT / "web"
PUBLIC_PATHS = ("/webhook", "/privacy", "/api/health")
#: WhatsApp's own limits on what may be sent.
MAX_TEXT = 4096
MAX_UPLOAD = 100 * 1024 * 1024
TEMPLATE_CACHE_SECONDS = 300
SEND_LOG_IMPORT_SECONDS = 60


class AccessGuard:
    """Checks the Cloudflare Access token on every request from outside this machine.

    Cloudflare Access is what stands between the internet and the inbox; this
    is the second line, for the day the Access application is deleted or its
    policy loosened by mistake.  A request with no ``Cf-Connecting-Ip`` header
    addressed to 127.0.0.1 or localhost came from a browser on this machine and
    needs no token.
    """

    def __init__(self, config: Config):
        self.config = config
        self.enabled = bool(config.access_team_domain and config.access_aud)
        self._jwks = None
        if self.enabled:
            import jwt

            team = config.access_team_domain.replace("https://", "").split(".")[0]
            self._jwks = jwt.PyJWKClient(f"https://{team}.cloudflareaccess.com/cdn-cgi/access/certs")

    @staticmethod
    def is_local(request: Request) -> bool:
        host = (request.headers.get("host") or "").split(":")[0]
        return "cf-connecting-ip" not in request.headers and host in ("127.0.0.1", "localhost")

    def user(self, request: Request) -> Optional[str]:
        """Who is asking, or raise 403."""
        if self.is_local(request):
            return "this computer"
        email = request.headers.get("cf-access-authenticated-user-email", "")
        if self.enabled:
            import jwt

            token = request.headers.get("cf-access-jwt-assertion") or request.cookies.get("CF_Authorization")
            if not token:
                raise HTTPException(403, "Sign in through Cloudflare Access.")
            try:
                key = self._jwks.get_signing_key_from_jwt(token).key
                claims = jwt.decode(token, key, algorithms=["RS256"], audience=self.config.access_aud)
            except Exception as exc:  # any failure to prove the token is a refusal
                logger.warning("Refused a request with a bad Access token: %s", exc)
                raise HTTPException(403, "Your sign-in could not be verified. Sign in again.") from None
            email = claims.get("email", "")
        if self.config.allowed_emails and email.lower() not in self.config.allowed_emails:
            raise HTTPException(403, f"{email or 'This account'} may not use the inbox.")
        return email or None


class Inbox:
    """The work behind the routes: sending, fetching media, alerts."""

    def __init__(self, config: Config, store: Store, graph: Graph):
        self.config = config
        self.store = store
        self.graph = graph
        self._templates: List[Dict[str, Any]] = []
        self._templates_at = 0.0
        self._imported_at = 0.0
        self._import_lock = threading.Lock()
        self.bot: Optional[Bot] = None

    # ------------------------------------------------------------- media

    def _media_path(self, message_id: int, mime: Optional[str], filename: Optional[str]) -> Path:
        ext = Path(filename).suffix if filename and Path(filename).suffix else (
            mimetypes.guess_extension((mime or "").split(";")[0].strip()) or ".bin")
        folder = self.config.media_dir / datetime.now().strftime("%Y-%m")
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{message_id}{ext}"

    def fetch_media(self, message_id: int) -> Optional[Path]:
        """Download a received file from Meta and keep it.  Meta's copy does not last."""
        message = self.store.message(message_id)
        if not message or not message.get("media_id"):
            return None
        if message.get("media_path") and Path(message["media_path"]).is_file():
            return Path(message["media_path"])
        try:
            info = self.graph.media_info(message["media_id"])
            content = self.graph.download(info["url"])
        except ApiError as exc:
            logger.warning("Could not fetch media for message %s: %s", message_id, exc)
            return None
        mime = info.get("mime_type") or message.get("mime")
        path = self._media_path(message_id, mime, message.get("filename"))
        path.write_bytes(content)
        self.store.set_media_path(message_id, str(path), mime)
        return path

    def keep_copy(self, message_id: int, content: bytes, mime: str, filename: str) -> str:
        path = self._media_path(message_id, mime, filename)
        path.write_bytes(content)
        return str(path)

    # ------------------------------------------------------------ sending

    def record_sent(self, wa_id: str, wamid: str, *, kind: str = "text", body: Optional[str], who: Optional[str],
                    source: str = "inbox", content: Optional[bytes] = None, mime: Optional[str] = None,
                    filename: Optional[str] = None) -> Optional[int]:
        row_id = self.store.add_message(
            wamid=wamid, wa_id=wa_id, direction="out", type=kind, body=body, mime=mime,
            filename=filename, status="accepted", source=source,
            extra={"by": who} if who else None,
        )
        if row_id and content is not None:
            self.store.set_media_path(row_id, self.keep_copy(row_id, content, mime or "", filename or ""))
        return row_id

    def send_text(self, wa_id: str, text: str, who: Optional[str], source: str = "inbox") -> str:
        wamid = self.graph.send(wa_id, {"type": "text", "text": {"body": text, "preview_url": True}})
        self.record_sent(wa_id, wamid, kind="text", body=text, who=who, source=source)
        return wamid

    def send_file(self, wa_id: str, content: bytes, filename: str, mime_given: Optional[str],
                  caption: str, who: Optional[str]) -> List[str]:
        mime, kind = split_mime(filename, mime_given)
        media_id = self.graph.upload_media(content, filename, mime)
        wamid = self.graph.send(wa_id, media_message(kind, media_id, caption, filename))
        self.record_sent(wa_id, wamid, kind=kind, body=caption if kind != "audio" else None, who=who,
                         content=content, mime=mime, filename=filename)
        sent = [wamid]
        if caption and kind == "audio":
            # A voice note cannot carry a caption; the words go as their own message.
            sent.append(self.send_text(wa_id, caption, who))
        return sent

    # ---------------------------------------------------------- templates

    def templates(self, refresh: bool = False) -> List[Dict[str, Any]]:
        if refresh or time.time() - self._templates_at > TEMPLATE_CACHE_SECONDS:
            self._templates = self.graph.templates()
            self._templates_at = time.time()
        return self._templates

    def template(self, name: str, language: str) -> Dict[str, Any]:
        for template in self.templates():
            if template["name"] == name and template["language"] == language:
                return template
        for template in self.templates(refresh=True):
            if template["name"] == name and template["language"] == language:
                return template
        raise HTTPException(404, f"No approved template '{name}' ({language}).")

    # ------------------------------------------------------------- arrivals

    def arrived(self, new: List[Dict[str, Any]]) -> None:
        """New messages: the bot answers what is its to answer; staff hear of the rest."""
        left = [m for m in new if not (self.bot and self.bot.handle(m))]
        if left and self.config.alert_numbers:
            self.alert(left)

    def tell_staff(self, text: str) -> None:
        """A note to the alert numbers - something the bot could not do."""
        for target in self.config.alert_numbers:
            to = phone.normalise(target)
            if not to:
                continue
            try:
                self.send_text(to, text, None, source="alert")
            except ApiError as exc:
                logger.warning("Could not tell %s: %s", to, exc)

    # --------------------------------------------------------------- alerts

    def alert(self, new: List[Dict[str, Any]]) -> None:
        """Tell the alert numbers about each new message, on WhatsApp."""
        for message in new:
            sender = message["wa_id"]
            who = self.store.name(sender) or phone.display(sender)
            label = f"{who} ({phone.display(sender)})" if self.store.name(sender) else who
            preview = (message.get("body") or f"[{message['type']}]").strip()[:500]
            link = f"\n{self.config.public_url}/#{sender}" if self.config.public_url else ""
            for target in self.config.alert_numbers:
                to = phone.normalise(target)
                if not to or to == sender:
                    continue
                try:
                    self.send_text(to, f"New WhatsApp from {label}:\n{preview}{link}", None, source="alert")
                except ApiError as exc:
                    if exc.code == OUTSIDE_WINDOW and self.config.alert_template:
                        self._alert_by_template(to, label, preview)
                    else:
                        logger.warning("Could not alert %s: %s", to, exc)

    def _alert_by_template(self, to: str, label: str, preview: str) -> None:
        try:
            template = self.template(self.config.alert_template, self.config.alert_template_language)
            values = [label, preview.replace("\n", " ")]
            wamid = self.graph.send(to, templates.build(template, header_values=[], body_values=values))
            self.record_sent(to, wamid, kind="template", body=templates.preview(template, [], values),
                             who=None, source="alert")
        except (ApiError, HTTPException, templates.TemplateError) as exc:
            logger.warning("Could not alert %s by template: %s", to, exc)

    # ------------------------------------------------- the bulk script's log

    def import_send_logs(self, force: bool = False) -> None:
        if not force and time.time() - self._imported_at < SEND_LOG_IMPORT_SECONDS:
            return
        with self._import_lock:
            self._imported_at = time.time()
            for path in self.config.send_logs:
                try:
                    self.store.import_send_log(path)
                except Exception as exc:  # a half-written CSV is read next time
                    logger.warning("Could not read %s: %s", path, exc)


# ------------------------------------------------------------------- shaping

def _message_json(row: Dict[str, Any], reactions: Dict[str, List[str]]) -> Dict[str, Any]:
    extra = json.loads(row["extra"]) if row.get("extra") else {}
    has_media = bool(row.get("media_id") or row.get("media_path"))
    return {
        "id": row["id"],
        "wamid": row["wamid"],
        "direction": row["direction"],
        "type": row["type"],
        "body": row["body"],
        "filename": row["filename"],
        "mime": row["mime"],
        "media_url": f"api/media/{row['id']}" if has_media else None,
        "reply_to": row["reply_to"],
        "status": row["status"],
        "error": row["error"],
        "source": row["source"],
        "by": extra.get("by"),
        "voice": bool(extra.get("voice")),
        "location": {k: extra[k] for k in ("latitude", "longitude")} if "latitude" in extra else None,
        "reactions": reactions.get(row["wamid"] or "", []),
        "ts": row["ts"],
    }


def _chat_json(chat: Dict[str, Any], store: Store) -> Dict[str, Any]:
    last = chat.get("last") or {}
    preview = last.get("body") or (last.get("filename") or (f"[{last.get('type')}]" if last.get("type") else ""))
    return {
        "wa_id": chat["wa_id"],
        "name": chat.get("name"),
        "number": phone.display(chat["wa_id"]),
        "last_ts": chat["last_ts"],
        "unread": chat["unread"] or 0,
        "preview": preview,
        "last_direction": last.get("direction"),
        "last_status": last.get("status"),
        "window_closes": store.window_closes(chat["wa_id"]),
    }


def _number_or_422(raw: str) -> str:
    wa_id = phone.normalise(raw)
    if not wa_id:
        raise HTTPException(422, f"'{raw}' is not a phone number WhatsApp can reach.")
    return wa_id


def _meta_error(exc: ApiError) -> HTTPException:
    hints = {
        OUTSIDE_WINDOW: "The 24-hour window has closed - only a template can be sent now.",
        131026: "WhatsApp could not deliver to this number - it may not be on WhatsApp.",
        131049: "Meta held this marketing message back to limit how many each person receives.",
        190: "The access token was refused. Put a fresh one in the token file and restart.",
        131031: "The WhatsApp account is locked - see Account Quality in Meta Business.",
        131042: "There is a problem with the payment method on the WhatsApp account.",
    }
    return HTTPException(502, f"WhatsApp refused it. {hints.get(exc.code, '')} ({exc})".replace("  ", " "))


# --------------------------------------------------------------------- the app

def create_app(config: Optional[Config] = None, store: Optional[Store] = None,
               graph: Optional[Graph] = None, reports: Optional[ReportsClient] = None) -> FastAPI:
    config = config or load_config()
    store = store or Store(config.db_path)
    graph = graph or Graph(config.token, config.phone_number_id, config.waba_id, config.api_version)
    inbox = Inbox(config, store, graph)
    if config.bot.enabled:
        inbox.bot = Bot(config.bot, store, graph, reports or ReportsClient(config.bot.reports_url),
                        inbox.record_sent, inbox.tell_staff)
        logger.info("The customer menu bot is on, using the reports service at %s.", config.bot.reports_url)
    guard = AccessGuard(config)

    if not guard.enabled:
        logger.warning("Cloudflare Access is not checked by the service itself "
                       "(access_team_domain / access_aud are not set). Keep the Access application in place.")
    if not config.app_secret:
        logger.error("No app secret: the webhook will refuse every delivery until it is set.")

    app = FastAPI(title="Iravi WhatsApp inbox", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.inbox = inbox

    @app.middleware("http")
    async def access(request: Request, call_next):
        if not request.url.path.startswith(PUBLIC_PATHS):
            try:
                request.state.user = guard.user(request)
            except HTTPException as exc:
                return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return await call_next(request)

    # ------------------------------------------------------------ webhook

    @app.get("/webhook")
    def verify(request: Request):
        """Meta's handshake when the callback URL is saved in the app dashboard."""
        params = request.query_params
        if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == config.verify_token:
            logger.info("Webhook verified by Meta.")
            return PlainTextResponse(params.get("hub.challenge", ""))
        logger.warning("Refused a webhook handshake with the wrong verify token.")
        raise HTTPException(403, "Verify token does not match.")

    @app.post("/webhook")
    async def deliver(request: Request, background: BackgroundTasks):
        raw = await request.body()
        if not config.app_secret:
            raise HTTPException(503, "The app secret is not configured.")
        if not signature_ok(config.app_secret, raw, request.headers.get("x-hub-signature-256")):
            logger.warning("Refused a webhook delivery without a valid signature.")
            raise HTTPException(401, "Signature does not match.")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            raise HTTPException(400, "Not JSON.") from None
        # An error here is a 500, and Meta retries the delivery; the message
        # IDs make a retried delivery harmless.
        received = process(payload, store, config.phone_number_id)
        for message_id in received.media:
            background.add_task(inbox.fetch_media, message_id)
        if received.messages:
            # After Meta has its 200: a ledger takes seconds to make, and Meta
            # retries a webhook that is slow to answer.
            background.add_task(inbox.arrived, received.messages)
        return {"ok": True}

    @app.get("/privacy", response_class=HTMLResponse)
    def privacy():
        contact = config.privacy_contact or "us on this WhatsApp number"
        return PRIVACY_HTML.format(name=config.business_name, contact=contact)

    @app.get("/api/health")
    def health():
        return {"ok": True, "webhook_ready": bool(config.app_secret)}

    # ----------------------------------------------------------- the inbox

    @app.get("/api/me")
    def me(request: Request):
        return {
            "business": config.business_name,
            "user": getattr(request.state, "user", None),
            "webhook_ready": bool(config.app_secret),
        }

    @app.get("/api/chats")
    def chats():
        inbox.import_send_logs()
        return [_chat_json(chat, store) for chat in store.chats()]

    @app.get("/api/number")
    def number(raw: str):
        wa_id = _number_or_422(raw)
        return {"wa_id": wa_id, "number": phone.display(wa_id), "name": store.name(wa_id)}

    @app.get("/api/chats/{wa_id}")
    def chat(wa_id: str):
        wa_id = _number_or_422(wa_id)
        rows = store.messages(wa_id)
        reactions: Dict[str, List[str]] = {}
        for row in rows:
            if row["type"] == "reaction" and row["body"]:
                target = json.loads(row["extra"] or "{}").get("message_id")
                if target:
                    reactions.setdefault(target, []).append(row["body"])
        return {
            "wa_id": wa_id,
            "name": store.name(wa_id),
            "number": phone.display(wa_id),
            "window_closes": store.window_closes(wa_id),
            "messages": [_message_json(r, reactions) for r in rows if r["type"] != "reaction"],
        }

    @app.post("/api/chats/{wa_id}/read")
    def read(wa_id: str, background: BackgroundTasks):
        wa_id = _number_or_422(wa_id)
        store.mark_read(wa_id)
        last = store.last_inbound(wa_id)
        if config.send_read_receipts and last and last.get("wamid") and time.time() - last["ts"] < 25 * 86400:
            background.add_task(_quietly, graph.mark_read, last["wamid"])
        return {"ok": True}

    @app.post("/api/chats/{wa_id}/send")
    async def send(request: Request, wa_id: str, text: str = Form(""), file: Optional[UploadFile] = File(None)):
        wa_id = _number_or_422(wa_id)
        text = text.strip()
        if not text and not file:
            raise HTTPException(422, "Type a message or attach a file.")
        if len(text) > MAX_TEXT:
            raise HTTPException(422, f"A WhatsApp message can be at most {MAX_TEXT} characters.")
        if not store.window_closes(wa_id):
            raise HTTPException(409, "This person has not written in the last 24 hours, "
                                     "so WhatsApp only allows a template. Choose one instead.")
        who = getattr(request.state, "user", None)
        try:
            if file:
                content = await file.read()
                if len(content) > MAX_UPLOAD:
                    raise HTTPException(422, "That file is larger than WhatsApp accepts (100 MB).")
                sent = inbox.send_file(wa_id, content, file.filename or "file", file.content_type, text, who)
            else:
                sent = [inbox.send_text(wa_id, text, who)]
        except ApiError as exc:
            raise _meta_error(exc) from None
        return {"ok": True, "sent": sent}

    @app.get("/api/templates")
    def template_list(refresh: bool = False):
        try:
            found = inbox.templates(refresh)
        except ApiError as exc:
            raise _meta_error(exc) from None
        return sorted((templates.summary(t) for t in found), key=lambda t: (t["name"], t["language"]))

    @app.post("/api/chats/{wa_id}/template")
    async def send_template(request: Request, wa_id: str, name: str = Form(...), language: str = Form(...),
                            header_values: str = Form("[]"), body_values: str = Form("[]"),
                            file: Optional[UploadFile] = File(None)):
        wa_id = _number_or_422(wa_id)
        try:
            header = [str(v) for v in json.loads(header_values)]
            body = [str(v) for v in json.loads(body_values)]
        except (json.JSONDecodeError, TypeError):
            raise HTTPException(422, "The blanks could not be read.") from None
        who = getattr(request.state, "user", None)
        try:
            template = inbox.template(name, language)
            media_id, content, mime, filename = None, None, None, ""
            if file:
                content = await file.read()
                filename = file.filename or "file"
                mime, _kind = split_mime(filename, file.content_type)
                media_id = graph.upload_media(content, filename, mime)
            message = templates.build(template, header_values=header, body_values=body,
                                      media_id=media_id, filename=filename)
            wamid = graph.send(wa_id, message)
        except templates.TemplateError as exc:
            raise HTTPException(422, str(exc)) from None
        except ApiError as exc:
            raise _meta_error(exc) from None
        inbox.record_sent(wa_id, wamid, kind="template", body=templates.preview(template, header, body),
                          who=who, content=content, mime=mime, filename=filename or None)
        return {"ok": True, "sent": [wamid]}

    @app.get("/api/media/{message_id}")
    def media(message_id: int):
        message = store.message(message_id)
        if not message:
            raise HTTPException(404, "No such message.")
        path = Path(message["media_path"]) if message.get("media_path") else None
        if not (path and path.is_file()):
            path = inbox.fetch_media(message_id)
        if not path:
            raise HTTPException(404, "The file is no longer available from WhatsApp.")
        return FileResponse(path, media_type=message.get("mime") or None,
                            filename=message.get("filename") or None,
                            content_disposition_type="inline")

    # ------------------------------------------------------------- the page

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    return app


def _quietly(function, *args) -> None:
    try:
        function(*args)
    except ApiError as exc:
        logger.info("Ignored: %s", exc)


PRIVACY_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Privacy notice - {name}</title>
<style>body{{font:16px/1.6 system-ui,sans-serif;max-width:46rem;margin:2rem auto;padding:0 1rem;color:#1d2a1f}}
h1{{font-size:1.5rem}}h2{{font-size:1.1rem;margin-top:1.6rem}}</style></head><body>
<h1>Privacy notice - {name} on WhatsApp</h1>
<p>{name} uses the WhatsApp Business Platform to talk with its customers, suppliers and staff:
to answer their questions, send account statements, reports and greetings.</p>
<h2>What we keep</h2>
<p>Your WhatsApp number and profile name, and the messages and files you send to our business number,
together with the messages we send you and whether they were delivered and read.</p>
<h2>Why</h2>
<p>To reply to you, to keep a record of our conversations with you, and to send you information about
your account with us. We do not sell or share your information with anyone for their own purposes.</p>
<h2>Where</h2>
<p>Messages pass through WhatsApp (Meta Platforms) under WhatsApp's own terms, and are kept on our own
systems in India. Only people at {name} who need them can see them.</p>
<h2>Your choices</h2>
<p>Reply STOP at any time and we will not send you greetings or offers. To see or delete what we hold
about you, write to {contact}.</p>
</body></html>"""
