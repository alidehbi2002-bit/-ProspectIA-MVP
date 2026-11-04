"""Base de données SQLite : prospects, messages, historique, liste « ne plus contacter »."""
from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import datetime, timezone
from urllib.parse import unquote

SCHEMA = """
CREATE TABLE IF NOT EXISTS prospects (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name         TEXT NOT NULL,
    title             TEXT,
    company           TEXT,
    project           TEXT CHECK (project IN ('A', 'B')) NOT NULL,  -- A = mécénat, B = corners/partenariats
    score             INTEGER DEFAULT 0,
    role_verified     INTEGER DEFAULT 0,
    linkedin_url      TEXT,
    public_identifier TEXT UNIQUE,          -- le « slug » de l'URL linkedin.com/in/<slug>
    provider_id       TEXT,                 -- identifiant interne LinkedIn, récupéré via Unipile
    email             TEXT,
    source_label      TEXT,                 -- ex : « site de l'entreprise, rapport RSE 2025 »
    channel           TEXT DEFAULT 'invite' CHECK (channel IN ('invite', 'inmail')),
    status            TEXT DEFAULT 'new',
    step              INTEGER DEFAULT 0,    -- 0 rien, 1 premier message, 2 relance 1, 3 relance 2
    chat_id           TEXT,
    invited_at        TEXT,
    connected_at      TEXT,
    last_message_at   TEXT,
    replied_at        TEXT,
    error             TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    prospect_id INTEGER NOT NULL REFERENCES prospects(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('note', 'first', 'followup1', 'followup2')),
    body        TEXT NOT NULL,
    PRIMARY KEY (prospect_id, kind)
);
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    prospect_id INTEGER,
    kind        TEXT NOT NULL,
    detail      TEXT,
    dry_run     INTEGER DEFAULT 0,
    at          TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS suppression (
    key_hash TEXT PRIMARY KEY,
    kind     TEXT NOT NULL,      -- 'linkedin' ou 'email'
    reason   TEXT,
    at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE INDEX IF NOT EXISTS idx_prospects_status ON prospects(status);
CREATE INDEX IF NOT EXISTS idx_prospects_provider ON prospects(provider_id);
CREATE INDEX IF NOT EXISTS idx_events_kind_at ON events(kind, at);
"""

# Statuts possibles d'un prospect
NEW, TO_REVIEW, APPROVED, INVITED, CONNECTED, MESSAGED = "new", "to_review", "approved", "invited", "connected", "messaged"
REPLIED, NO_RESPONSE, OPTED_OUT, DONE, ERROR, SKIPPED = "replied", "no_response", "opted_out", "done", "error", "skipped"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def public_identifier_from_url(url: str | None) -> str | None:
    """https://www.linkedin.com/in/jean-dupont-123/ -> 'jean-dupont-123'."""
    if not url:
        return None
    m = re.search(r"linkedin\.com/in/([^/?#]+)", url.strip(), flags=re.I)
    if not m:
        return None
    slug = unquote(m.group(1))
    # Les identifiants internes LinkedIn (ACo...) sont sensibles à la casse : on ne les modifie pas.
    return slug if slug.startswith("ACo") else slug.lower()


def _hash(value: str) -> str:
    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


# ---------- Prospects ----------

def add_prospect(conn: sqlite3.Connection, *, full_name: str, project: str, linkedin_url: str,
                 messages: dict[str, str], title: str = "", company: str = "", score: int = 0,
                 role_verified: bool = False, email: str = "", source_label: str = "",
                 channel: str = "invite", provider_id: str | None = None,
                 now: datetime | None = None) -> int | None:
    """Ajoute un prospect et ses messages. Renvoie None si doublon ou si la personne est en liste STOP."""
    now = now or utcnow()
    slug = public_identifier_from_url(linkedin_url)
    if not slug:
        raise ValueError(f"URL LinkedIn invalide : {linkedin_url!r}")
    if is_suppressed(conn, linkedin_slug=slug, email=email):
        return None
    if conn.execute("SELECT 1 FROM prospects WHERE public_identifier = ?", (slug,)).fetchone():
        return None  # déjà dans la base : jamais contacté deux fois
    cur = conn.execute(
        """INSERT INTO prospects (full_name, title, company, project, score, role_verified, linkedin_url,
               public_identifier, provider_id, email, source_label, channel, status, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (full_name, title, company, project, int(score), int(bool(role_verified)), linkedin_url, slug,
         provider_id or None, email or None, source_label, channel, NEW, iso(now), iso(now)),
    )
    pid = cur.lastrowid
    for kind, body in messages.items():
        if body:
            conn.execute("INSERT INTO messages (prospect_id, kind, body) VALUES (?,?,?)", (pid, kind, body.strip()))
    conn.commit()
    return pid


def get_message(conn: sqlite3.Connection, prospect_id: int, kind: str) -> str | None:
    row = conn.execute("SELECT body FROM messages WHERE prospect_id=? AND kind=?", (prospect_id, kind)).fetchone()
    return row["body"] if row else None


def update_prospect(conn: sqlite3.Connection, prospect_id: int, now: datetime | None = None, **fields) -> None:
    fields["updated_at"] = iso(now or utcnow())
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE prospects SET {cols} WHERE id = ?", (*fields.values(), prospect_id))
    conn.commit()


def find_prospect(conn: sqlite3.Connection, *, provider_id: str | None = None,
                  public_identifier: str | None = None, chat_id: str | None = None) -> sqlite3.Row | None:
    if provider_id:
        row = conn.execute("SELECT * FROM prospects WHERE provider_id = ?", (provider_id,)).fetchone()
        if row:
            return row
    if public_identifier:
        pub = public_identifier if public_identifier.startswith("ACo") else public_identifier.lower()
        row = conn.execute("SELECT * FROM prospects WHERE public_identifier = ?", (pub,)).fetchone()
        if row:
            return row
    if chat_id:
        return conn.execute("SELECT * FROM prospects WHERE chat_id = ?", (chat_id,)).fetchone()
    return None


# ---------- Historique ----------

def log_event(conn: sqlite3.Connection, kind: str, prospect_id: int | None = None, detail: str = "",
              dry_run: bool = False, now: datetime | None = None) -> None:
    conn.execute("INSERT INTO events (prospect_id, kind, detail, dry_run, at) VALUES (?,?,?,?,?)",
                 (prospect_id, kind, detail, int(dry_run), iso(now or utcnow())))
    conn.commit()


def count_events(conn: sqlite3.Connection, kinds: tuple[str, ...], since: datetime) -> int:
    marks = ",".join("?" * len(kinds))
    row = conn.execute(f"SELECT COUNT(*) AS n FROM events WHERE kind IN ({marks}) AND at >= ?",
                       (*kinds, iso(since))).fetchone()
    return row["n"]


# ---------- Liste « ne plus contacter » ----------

def suppress(conn: sqlite3.Connection, *, linkedin_slug: str | None = None, email: str | None = None,
             reason: str = "", now: datetime | None = None) -> None:
    for kind, value in (("linkedin", linkedin_slug), ("email", email)):
        if value:
            conn.execute("INSERT OR IGNORE INTO suppression (key_hash, kind, reason, at) VALUES (?,?,?,?)",
                         (_hash(value), kind, reason, iso(now or utcnow())))
    conn.commit()


def is_suppressed(conn: sqlite3.Connection, *, linkedin_slug: str | None = None, email: str | None = None) -> bool:
    for value in (linkedin_slug, email):
        if value and conn.execute("SELECT 1 FROM suppression WHERE key_hash = ?", (_hash(value),)).fetchone():
            return True
    return False


# ---------- Interrupteur d'arrêt d'urgence ----------

def kv_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def kv_set(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    conn.execute("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, value))
    conn.commit()


def pause(conn: sqlite3.Connection, reason: str) -> None:
    kv_set(conn, "paused", "1")
    kv_set(conn, "paused_reason", reason)
    log_event(conn, "paused", detail=reason)


def resume(conn: sqlite3.Connection) -> None:
    kv_set(conn, "paused", "0")
    kv_set(conn, "paused_reason", None)
    log_event(conn, "resumed")


def is_paused(conn: sqlite3.Connection) -> tuple[bool, str]:
    return kv_get(conn, "paused") == "1", kv_get(conn, "paused_reason") or ""
