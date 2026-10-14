"""Local SQLite state the operator never needs to see: topic mapping, pause
state, chat history for the LLM, escalations, and the authoritative copy of
assignment state (the Sheet's Assignments tab is a write-through view)."""

import json
import os
import sqlite3
import time
from dataclasses import dataclass

SCHEMA = """
CREATE TABLE IF NOT EXISTS topics (
    creator_id TEXT PRIMARY KEY,
    thread_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS pauses (
    creator_id TEXT PRIMARY KEY,
    until REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    creator_id TEXT NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS history_creator ON history (creator_id, id);
CREATE TABLE IF NOT EXISTS assignments (
    campaign_id TEXT NOT NULL,
    creator_id TEXT NOT NULL,
    status TEXT NOT NULL,
    sent_at REAL NOT NULL,
    brief_message_id INTEGER,
    post_link TEXT,
    PRIMARY KEY (campaign_id, creator_id)
);
CREATE TABLE IF NOT EXISTS escalations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    creator_id TEXT NOT NULL,
    campaign_id TEXT NOT NULL,
    question TEXT NOT NULL,
    answer TEXT,
    status TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS confirmations (
    campaign_id TEXT PRIMARY KEY,
    recipients TEXT NOT NULL,
    confirmed_at REAL NOT NULL
);
"""

# Pause "until" value for a manual /pause that only /resume clears.
MANUAL_PAUSE = 1e18


@dataclass
class Assignment:
    campaign_id: str
    creator_id: str
    status: str
    sent_at: float
    brief_message_id: int | None
    post_link: str | None


@dataclass
class Escalation:
    id: int
    creator_id: str
    campaign_id: str
    question: str
    answer: str | None
    status: str


class Store:
    def __init__(self, path: str):
        if os.path.dirname(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    # Key/value settings the bot manages itself (e.g. the inbox group chosen with /setup)

    def get_value(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_value(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.db.commit()

    # Topics

    def get_thread(self, creator_id: str) -> int | None:
        row = self.db.execute("SELECT thread_id FROM topics WHERE creator_id = ?", (creator_id,)).fetchone()
        return row["thread_id"] if row else None

    def set_thread(self, creator_id: str, thread_id: int) -> None:
        self.db.execute(
            "INSERT INTO topics (creator_id, thread_id) VALUES (?, ?) "
            "ON CONFLICT(creator_id) DO UPDATE SET thread_id = excluded.thread_id",
            (creator_id, thread_id),
        )
        self.db.commit()

    def forget_thread(self, creator_id: str) -> None:
        self.db.execute("DELETE FROM topics WHERE creator_id = ?", (creator_id,))
        self.db.commit()

    def creator_for_thread(self, thread_id: int) -> str | None:
        row = self.db.execute("SELECT creator_id FROM topics WHERE thread_id = ?", (thread_id,)).fetchone()
        return row["creator_id"] if row else None

    # Pauses

    def pause(self, creator_id: str, seconds: float | None = None) -> None:
        """Pause auto-replies. seconds=None means until /resume."""
        until = MANUAL_PAUSE if seconds is None else time.time() + seconds
        self.db.execute(
            "INSERT INTO pauses (creator_id, until) VALUES (?, ?) "
            "ON CONFLICT(creator_id) DO UPDATE SET until = excluded.until",
            (creator_id, until),
        )
        self.db.commit()

    def resume(self, creator_id: str) -> None:
        self.db.execute("DELETE FROM pauses WHERE creator_id = ?", (creator_id,))
        self.db.commit()

    def is_paused(self, creator_id: str) -> bool:
        row = self.db.execute("SELECT until FROM pauses WHERE creator_id = ?", (creator_id,)).fetchone()
        return bool(row) and row["until"] > time.time()

    def is_paused_manually(self, creator_id: str) -> bool:
        row = self.db.execute("SELECT until FROM pauses WHERE creator_id = ?", (creator_id,)).fetchone()
        return bool(row) and row["until"] >= MANUAL_PAUSE

    # History

    def add_history(self, creator_id: str, role: str, text: str) -> None:
        self.db.execute(
            "INSERT INTO history (creator_id, role, text, ts) VALUES (?, ?, ?, ?)",
            (creator_id, role, text, time.time()),
        )
        self.db.commit()

    def recent_history(self, creator_id: str, limit: int = 20) -> list[tuple[str, str]]:
        rows = self.db.execute(
            "SELECT role, text FROM history WHERE creator_id = ? ORDER BY id DESC LIMIT ?",
            (creator_id, limit),
        ).fetchall()
        return [(r["role"], r["text"]) for r in reversed(rows)]

    # Assignments

    def add_assignment(self, campaign_id: str, creator_id: str, brief_message_id: int | None, status: str = "sent") -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO assignments (campaign_id, creator_id, status, sent_at, brief_message_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (campaign_id, creator_id, status, time.time(), brief_message_id),
        )
        self.db.commit()

    def get_assignment(self, campaign_id: str, creator_id: str) -> Assignment | None:
        row = self.db.execute(
            "SELECT * FROM assignments WHERE campaign_id = ? AND creator_id = ?", (campaign_id, creator_id)
        ).fetchone()
        return Assignment(**dict(row)) if row else None

    def update_assignment(self, campaign_id: str, creator_id: str, status: str, post_link: str | None = None) -> None:
        self.db.execute(
            "UPDATE assignments SET status = ?, post_link = COALESCE(?, post_link) "
            "WHERE campaign_id = ? AND creator_id = ?",
            (status, post_link, campaign_id, creator_id),
        )
        self.db.commit()

    def campaign_assignments(self, campaign_id: str) -> list[Assignment]:
        rows = self.db.execute("SELECT * FROM assignments WHERE campaign_id = ?", (campaign_id,)).fetchall()
        return [Assignment(**dict(r)) for r in rows]

    def active_assignments(self, creator_id: str, max_age_days: int = 30, limit: int = 5) -> list[Assignment]:
        cutoff = time.time() - max_age_days * 86400
        rows = self.db.execute(
            "SELECT * FROM assignments WHERE creator_id = ? AND sent_at >= ? "
            "AND status NOT IN ('declined', 'blocked') ORDER BY sent_at DESC LIMIT ?",
            (creator_id, cutoff, limit),
        ).fetchall()
        return [Assignment(**dict(r)) for r in rows]

    def campaign_for_brief_message(self, creator_id: str, message_id: int) -> str | None:
        row = self.db.execute(
            "SELECT campaign_id FROM assignments WHERE creator_id = ? AND brief_message_id = ?",
            (creator_id, message_id),
        ).fetchone()
        return row["campaign_id"] if row else None

    # Escalations

    def add_escalation(self, creator_id: str, campaign_id: str, question: str) -> int:
        cur = self.db.execute(
            "INSERT INTO escalations (creator_id, campaign_id, question, status, created_at) VALUES (?, ?, ?, 'open', ?)",
            (creator_id, campaign_id, question, time.time()),
        )
        self.db.commit()
        return cur.lastrowid

    def open_escalation(self, creator_id: str) -> Escalation | None:
        row = self.db.execute(
            "SELECT id, creator_id, campaign_id, question, answer, status FROM escalations "
            "WHERE creator_id = ? AND status = 'open' ORDER BY id DESC LIMIT 1",
            (creator_id,),
        ).fetchone()
        return Escalation(**dict(row)) if row else None

    def answer_escalation(self, escalation_id: int, answer: str) -> None:
        self.db.execute(
            "UPDATE escalations SET answer = ?, status = 'answered' WHERE id = ?", (answer, escalation_id)
        )
        self.db.commit()

    def get_escalation(self, escalation_id: int) -> Escalation | None:
        row = self.db.execute(
            "SELECT id, creator_id, campaign_id, question, answer, status FROM escalations WHERE id = ?",
            (escalation_id,),
        ).fetchone()
        return Escalation(**dict(row)) if row else None

    def close_escalation(self, escalation_id: int, status: str) -> None:
        self.db.execute("UPDATE escalations SET status = ? WHERE id = ?", (status, escalation_id))
        self.db.commit()

    # Campaign confirmations (recipient snapshot taken when the operator taps Confirm)

    def confirm_campaign(self, campaign_id: str, recipient_ids: list[str]) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO confirmations (campaign_id, recipients, confirmed_at) VALUES (?, ?, ?)",
            (campaign_id, json.dumps(recipient_ids), time.time()),
        )
        self.db.commit()

    def confirmed_recipients(self, campaign_id: str) -> list[str] | None:
        row = self.db.execute("SELECT recipients FROM confirmations WHERE campaign_id = ?", (campaign_id,)).fetchone()
        return json.loads(row["recipients"]) if row else None
