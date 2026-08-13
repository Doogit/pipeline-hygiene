"""Captured meeting notes + provenance, plus the capture-rejection log (R3.6/3.7).

Feature-flagged via PIPELINE_HYGIENE_PACKETS (default off) exactly like
work_items.py: when the flag is off nothing here is constructed and these tables
are never created, so the app and the parity gate stay byte-identical to the
packets-disabled build.

Raw notes are stored so a later, improved extractor can re-process them without
re-capturing (R3.6). All time is threaded explicitly (captured_at / as_of) —
date.today() is banned outside CLI entry points, matching the rules engine.

PRIVACY / RETENTION (P1 open item, plan Deferred 2026-08-13): raw notes may
carry PII, deal terms, and competitively sensitive text, and today they persist
indefinitely in the same plaintext SQLite file with no purge path. This module
deliberately does NOT wire an indefinite-retention background path; a lifecycle
policy (purge N days post-processing, or tie deletion to work-item resolution)
plus file-level access control matching note sensitivity remain OPEN and must be
decided before this is exposed via any export/API surface. `delete_note` /
`delete_notes_for_opp` are provided so a future retention job has a deletion path
to call — nothing calls them automatically yet.
"""
import re
import sqlite3
from datetime import date
from pathlib import Path

# Privacy guard (R3.7). A local size threshold for a single captured notes blob;
# oversize input is refused before storage. Intentionally NOT sourced from
# app/server.py's MAX_UPLOAD_BYTES — app wiring is PR E's job and this module
# must not depend on the web layer. 256 KiB is generous for meeting notes while
# refusing an accidental multi-megabyte dump.
MAX_NOTES_BYTES = 256 * 1024

# Common credential shapes stripped BEFORE storage so secrets never land in the
# notes table (and never egress to an extractor). Best-effort minimization, not
# a DLP guarantee. Each pattern replaces the secret VALUE with a redaction
# marker while keeping surrounding note text intact and re-processable.
_REDACTED = "[REDACTED]"
_CREDENTIAL_PATTERNS = (
    # AWS access key id
    re.compile(r"AKIA[0-9A-Z]{16}"),
    # Anthropic / OpenAI style API keys (sk-... / sk-ant-...)
    re.compile(r"sk-(?:ant-)?[A-Za-z0-9_\-]{16,}"),
    # GitHub personal-access / fine-grained tokens
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    # Slack tokens
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"),
    # generic "password: value" / "secret = value" / "api_key: value"
    re.compile(
        r"(?i)\b(?:password|passwd|secret|api[_-]?key|token)\b\s*[:=]\s*\S+"),
)


class NoteTooLargeError(ValueError):
    """Raised when a captured notes blob exceeds MAX_NOTES_BYTES (R3.7)."""


def scrub_credentials(text):
    """Strip common credential patterns from note text before storage (R3.7).

    Returns the text with secret values replaced by a redaction marker. The
    'key: value' family redacts the whole assignment so the value never
    survives; the bare-token families redact just the matched token."""
    if not text:
        return text
    scrubbed = text
    for pat in _CREDENTIAL_PATTERNS:
        if pat.pattern.startswith("(?i)"):
            # 'password: hunter2' -> 'password: [REDACTED]'
            scrubbed = pat.sub(
                lambda m: re.split(r"[:=]", m.group(0), maxsplit=1)[0]
                + ": " + _REDACTED, scrubbed)
        else:
            scrubbed = pat.sub(_REDACTED, scrubbed)
    return scrubbed


def _iso(value):
    return value.isoformat() if isinstance(value, date) else value


_SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    opp_id          TEXT NOT NULL,
    author          TEXT,
    captured_at     TEXT NOT NULL,
    text            TEXT NOT NULL,
    source_filename TEXT
);
CREATE TABLE IF NOT EXISTS capture_rejections (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    at       TEXT NOT NULL,
    opp_id   TEXT,
    note_id  INTEGER,
    reason   TEXT NOT NULL,
    detail   TEXT
);
"""

_NOTE_COLS = ("id", "opp_id", "author", "captured_at", "text", "source_filename")
_REJECTION_COLS = ("id", "at", "opp_id", "note_id", "reason", "detail")


class NotesStore:
    """SQLite-backed captured notes + capture-rejection log. Constructed only
    when packets are enabled; instantiating it creates its tables (CREATE TABLE
    IF NOT EXISTS) in the same database file the snapshot store uses.

    The rejection log reuses work_item_events' append-only shape (an insert-only
    audit table) rather than a bespoke design: it is never mutated, only
    appended and read. Rejected proposals are logged here and NEVER shown to the
    user (R3.3)."""

    def __init__(self, db_path, config=None):
        self.db_path = str(db_path)
        self.config = config
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    # --- capture ---

    def capture(self, *, opp_id, text, captured_at, author=None,
                source_filename=None):
        """Store one captured notes blob for an opp (R3.6), returning its id.

        Enforces the privacy guard (R3.7) BEFORE anything is written: oversize
        input is refused (NoteTooLargeError) and credential patterns are
        stripped from the text. `captured_at` is threaded explicitly (no wall
        clock). The stored text is re-processable by a future extractor."""
        raw = text or ""
        if len(raw.encode("utf-8")) > MAX_NOTES_BYTES:
            raise NoteTooLargeError(
                f"notes blob for {opp_id} exceeds {MAX_NOTES_BYTES} bytes")
        scrubbed = scrub_credentials(raw)
        at = _iso(captured_at)
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO notes (opp_id, author, captured_at, text, "
                "source_filename) VALUES (?,?,?,?,?)",
                (opp_id, author, at, scrubbed, source_filename))
        return cur.lastrowid

    def note(self, note_id):
        row = self.conn.execute(
            f"SELECT {', '.join(_NOTE_COLS)} FROM notes WHERE id=?",
            (note_id,)).fetchone()
        return dict(zip(_NOTE_COLS, row)) if row else None

    def notes_for_opp(self, opp_id):
        cur = self.conn.execute(
            f"SELECT {', '.join(_NOTE_COLS)} FROM notes WHERE opp_id=? "
            f"ORDER BY id", (opp_id,))
        return [dict(zip(_NOTE_COLS, row)) for row in cur.fetchall()]

    # --- rejection log (append-only; never shown to the user, R3.3) ---

    def log_rejection(self, *, at, reason, opp_id=None, note_id=None,
                      detail=None):
        """Append one capture-rejection audit row. Reused for both invalid
        proposals from the extractor and refused captures. Returns its id."""
        at_s = _iso(at)
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO capture_rejections (at, opp_id, note_id, reason, "
                "detail) VALUES (?,?,?,?,?)",
                (at_s, opp_id, note_id, reason, detail))
        return cur.lastrowid

    def rejections(self, *, opp_id=None):
        clause, params = "", ()
        if opp_id is not None:
            clause, params = " WHERE opp_id=?", (opp_id,)
        cur = self.conn.execute(
            f"SELECT {', '.join(_REJECTION_COLS)} FROM capture_rejections"
            f"{clause} ORDER BY id", params)
        return [dict(zip(_REJECTION_COLS, row)) for row in cur.fetchall()]

    # --- deletion (retention hook; nothing calls these automatically, P1) ---

    def delete_note(self, note_id):
        """Delete one captured note. Provided as the deletion path a future
        retention policy would call; NOT invoked automatically (P1 open item)."""
        with self.conn:
            self.conn.execute("DELETE FROM notes WHERE id=?", (note_id,))

    def delete_notes_for_opp(self, opp_id):
        with self.conn:
            self.conn.execute("DELETE FROM notes WHERE opp_id=?", (opp_id,))
