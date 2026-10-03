"""Every message to and from the business number, kept on this machine.

Meta keeps no inbox for a Cloud API number: a message is handed to the webhook
once, and a received photo can be fetched for a short while only.  So this
SQLite file is the record - back up ``data/`` with everything else.

One table of messages, keyed by WhatsApp's own message ID (``wamid``) so a
webhook Meta delivers twice is stored once.  A delivery receipt can arrive for
a message this inbox never sent - a greeting from the bulk script - and then
becomes a row of its own, filled in later from the script's log.
"""

from __future__ import annotations

import csv
import json
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

#: How long after a customer's last message a normal reply is still allowed.
WINDOW_SECONDS = 24 * 60 * 60

#: A receipt only moves a message forward: read is never undone by a late "delivered".
STATUS_RANK = {"accepted": 0, "sent": 1, "delivered": 2, "read": 3, "played": 3}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    wa_id       TEXT PRIMARY KEY,
    name        TEXT,
    updated_at  INTEGER
);
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    wamid       TEXT UNIQUE,
    wa_id       TEXT NOT NULL,
    direction   TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    type        TEXT NOT NULL,
    body        TEXT,
    media_id    TEXT,
    media_path  TEXT,
    mime        TEXT,
    filename    TEXT,
    reply_to    TEXT,
    extra       TEXT,
    status      TEXT,
    error       TEXT,
    source      TEXT,
    ts          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_chat ON messages (wa_id, ts);
CREATE TABLE IF NOT EXISTS chat_reads (
    wa_id       TEXT PRIMARY KEY,
    last_read   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS bot_sessions (
    wa_id       TEXT PRIMARY KEY,
    state       TEXT NOT NULL,
    data        TEXT,
    updated_at  INTEGER NOT NULL
);
"""


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # SQLite serialises writers itself; the lock only keeps this process's
        # threads from tripping over each other's "database is locked".
        self._lock = threading.Lock()
        with self._connect() as db:
            db.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def _write(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock, self._connect() as db:
            return db.execute(sql, tuple(params))

    # ------------------------------------------------------------- contacts

    def save_contact(self, wa_id: str, name: Optional[str]) -> None:
        if not name:
            return
        self._write(
            "INSERT INTO contacts (wa_id, name, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (wa_id) DO UPDATE SET name = excluded.name, updated_at = excluded.updated_at",
            (wa_id, name, int(time.time())),
        )

    # ------------------------------------------------------------- messages

    def add_message(self, **fields: Any) -> Optional[int]:
        """Store a message; returns its row ID, or None if that ``wamid`` is already here."""
        if isinstance(fields.get("extra"), (dict, list)):
            fields["extra"] = json.dumps(fields["extra"])
        fields.setdefault("ts", int(time.time()))
        columns = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        cursor = self._write(f"INSERT OR IGNORE INTO messages ({columns}) VALUES ({marks})", fields.values())
        return cursor.lastrowid if cursor.rowcount else None

    def set_status(self, wamid: str, wa_id: str, status: str, ts: int, error: Optional[str] = None) -> None:
        """Record a delivery receipt, never moving a message backwards.

        A receipt for an unknown message - one sent by the bulk script, or from
        before the inbox existed - becomes a placeholder row the script's log
        can later describe.
        """
        with self._lock, self._connect() as db:
            row = db.execute("SELECT status FROM messages WHERE wamid = ?", (wamid,)).fetchone()
            if row is None:
                db.execute(
                    "INSERT INTO messages (wamid, wa_id, direction, type, status, error, source, ts) "
                    "VALUES (?, ?, 'out', 'unknown', ?, ?, 'external', ?)",
                    (wamid, wa_id, status, error, ts),
                )
                return
            current = row["status"] or "accepted"
            if current == "failed":
                return
            if status == "failed" or STATUS_RANK.get(status, 0) > STATUS_RANK.get(current, 0):
                db.execute("UPDATE messages SET status = ?, error = ? WHERE wamid = ?", (status, error, wamid))

    def set_media_path(self, message_id: int, path: str, mime: Optional[str] = None) -> None:
        self._write("UPDATE messages SET media_path = ?, mime = COALESCE(?, mime) WHERE id = ?",
                    (path, mime, message_id))

    def message(self, message_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        return dict(row) if row else None

    def messages(self, wa_id: str, limit: int = 500) -> List[Dict[str, Any]]:
        """The chat's latest messages, oldest first."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM (SELECT * FROM messages WHERE wa_id = ? ORDER BY ts DESC, id DESC LIMIT ?) "
                "ORDER BY ts, id",
                (wa_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- chats

    def chats(self) -> List[Dict[str, Any]]:
        """One row per person, newest conversation first, with the unread count."""
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT m.wa_id,
                       c.name,
                       MAX(m.ts)                                         AS last_ts,
                       MAX(CASE WHEN m.direction = 'in' THEN m.ts END)    AS last_in,
                       SUM(CASE WHEN m.direction = 'in'
                                 AND m.ts > COALESCE(r.last_read, 0) THEN 1 ELSE 0 END) AS unread
                FROM messages m
                LEFT JOIN contacts c   ON c.wa_id = m.wa_id
                LEFT JOIN chat_reads r ON r.wa_id = m.wa_id
                WHERE m.type != 'reaction'
                GROUP BY m.wa_id
                ORDER BY last_ts DESC
                """
            ).fetchall()
            chats = []
            for row in rows:
                last = db.execute(
                    "SELECT direction, type, body, filename, status FROM messages "
                    "WHERE wa_id = ? AND type != 'reaction' ORDER BY ts DESC, id DESC LIMIT 1",
                    (row["wa_id"],),
                ).fetchone()
                chats.append({**dict(row), "last": dict(last) if last else None})
        return chats

    def name(self, wa_id: str) -> Optional[str]:
        with self._connect() as db:
            row = db.execute("SELECT name FROM contacts WHERE wa_id = ?", (wa_id,)).fetchone()
        return row["name"] if row else None

    def last_inbound(self, wa_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM messages WHERE wa_id = ? AND direction = 'in' ORDER BY ts DESC, id DESC LIMIT 1",
                (wa_id,),
            ).fetchone()
        return dict(row) if row else None

    def window_closes(self, wa_id: str) -> Optional[int]:
        """When the free 24-hour reply window to this person shuts, or None if it is shut."""
        last = self.last_inbound(wa_id)
        if not last:
            return None
        closes = last["ts"] + WINDOW_SECONDS
        return closes if closes > time.time() else None

    def mark_read(self, wa_id: str) -> None:
        self._write(
            "INSERT INTO chat_reads (wa_id, last_read) VALUES (?, ?) "
            "ON CONFLICT (wa_id) DO UPDATE SET last_read = excluded.last_read",
            (wa_id, int(time.time())),
        )

    # --------------------------------------------------------- the bot

    def session(self, wa_id: str) -> Optional[Dict[str, Any]]:
        """Where the bot's conversation with this person stands, if anywhere."""
        with self._connect() as db:
            row = db.execute("SELECT * FROM bot_sessions WHERE wa_id = ?", (wa_id,)).fetchone()
        if not row:
            return None
        return {"state": row["state"], "data": json.loads(row["data"] or "{}"), "updated_at": row["updated_at"]}

    def save_session(self, wa_id: str, state: str, data: Optional[Dict[str, Any]] = None) -> None:
        self._write(
            "INSERT INTO bot_sessions (wa_id, state, data, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (wa_id) DO UPDATE SET state = excluded.state, data = excluded.data, "
            "updated_at = excluded.updated_at",
            (wa_id, state, json.dumps(data or {}), int(time.time())),
        )

    def last_human_reply(self, wa_id: str) -> Optional[int]:
        """When somebody last answered this chat from the inbox page."""
        with self._connect() as db:
            row = db.execute(
                "SELECT MAX(ts) AS ts FROM messages WHERE wa_id = ? AND direction = 'out' AND source = 'inbox'",
                (wa_id,),
            ).fetchone()
        return row["ts"] if row and row["ts"] else None

    # ------------------------------------------------- the bulk script's log

    def import_send_log(self, path: Path) -> int:
        """Describe the greetings the bulk script sent, so they read sensibly in the chats.

        ``sent_log.csv`` holds the time, number, template and message ID of
        every send.  A row already known from a receipt is filled in; one not
        yet known is added, so the chat shows it even before any receipt.
        """
        if not path.is_file():
            return 0
        added = 0
        with path.open(newline="", encoding="utf-8") as handle:
            rows = [r for r in csv.DictReader(handle)
                    if r.get("result") == "sent" and (r.get("detail") or "").startswith("wamid.")]
        with self._lock, self._connect() as db:
            for row in rows:
                template = row["template"]
                direct = template.endswith(" (direct)")
                body = (f"{template[:-9]} - sent as a normal message by the bulk script" if direct
                        else f"Template: {template}")
                try:
                    ts = int(datetime.fromisoformat(row["time"]).timestamp())
                except ValueError:
                    ts = int(time.time())
                updated = db.execute(
                    "UPDATE messages SET type = 'template', body = ?, source = 'script' "
                    "WHERE wamid = ? AND type = 'unknown'",
                    (body, row["detail"]),
                ).rowcount
                if not updated:
                    added += db.execute(
                        "INSERT OR IGNORE INTO messages (wamid, wa_id, direction, type, body, status, source, ts) "
                        "VALUES (?, ?, 'out', 'template', ?, 'sent', 'script', ?)",
                        (row["detail"], row["number"], body, ts),
                    ).rowcount
        return added
