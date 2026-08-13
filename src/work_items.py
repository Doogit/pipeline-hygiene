"""Work-items ledger: draft artifacts + append-only audit trail (Monday Packet).

Feature-flagged via PIPELINE_HYGIENE_PACKETS (default off). When the flag is
off nothing here is constructed and these tables are never created, so the app
and the parity gate are byte-identical to the packets-disabled build.

The ledger is the one writable surface the packet subsystem adds; it never
writes to `opportunities` or any source system (read-only-against-source
invariant). All time is threaded explicitly (created_at / status_at / as_of) —
date.today() is banned outside CLI entry points, exactly as in the rules engine.
"""
import hashlib
import json
import os
import sqlite3
from datetime import date
from pathlib import Path

from .rules import RULES, evaluate_row

ITEM_TYPES = ("field_update", "next_step", "email_draft", "clinic_doc",
              "agenda_item")
STATUSES = ("proposed", "accepted", "edited", "dismissed", "expired")
# Items still "live" for idempotency and expiry (dismissed/expired are closed).
OPEN_STATUSES = ("proposed", "accepted", "edited")

FLAG_ENV = "PIPELINE_HYGIENE_PACKETS"


def packets_enabled(env=None):
    """True when the Monday-Packet subsystem is enabled. Default off."""
    val = (env if env is not None else os.environ).get(FLAG_ENV, "")
    return val.strip().lower() in ("1", "true", "yes", "on")


def normalize_owner(owner):
    """v2 row-scoping key (R6.2). Casefold matches the rest of the app's
    owner comparison; brief.canonical() is a nested closure, not reusable."""
    return (owner or "").casefold()


def payload_hash(payload):
    """Stable hash over canonical JSON so key ordering / whitespace can't
    silently break dedup (mirrors record_run's sort_keys convention)."""
    blob = _payload_json(payload, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _iso(value):
    return value.isoformat() if isinstance(value, date) else value


def _json_default(value):
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _payload_json(payload, **kwargs):
    return json.dumps(payload, sort_keys=True, default=_json_default, **kwargs)


def _coerce(value):
    """Compare a stored snapshot field against a proposed value irrespective of
    date-vs-string typing (capture field_update expiry)."""
    if isinstance(value, date):
        return value.isoformat()
    return "" if value is None else str(value)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS work_items (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at          TEXT NOT NULL,
    snapshot_id         TEXT,
    opp_id              TEXT NOT NULL,
    owner_normalized    TEXT NOT NULL,
    source              TEXT NOT NULL,
    item_type           TEXT NOT NULL,
    target_field        TEXT,
    payload_json        TEXT NOT NULL,
    proposed_value_hash TEXT NOT NULL,
    status              TEXT NOT NULL,
    status_at           TEXT NOT NULL,
    status_by           TEXT,
    dismiss_reason      TEXT,
    dismiss_note        TEXT
);
CREATE TABLE IF NOT EXISTS work_item_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    work_item_id INTEGER NOT NULL,
    at           TEXT NOT NULL,
    from_status  TEXT,
    to_status    TEXT NOT NULL,
    by           TEXT,
    reason       TEXT
);
"""

_ITEM_COLS = ("id", "created_at", "snapshot_id", "opp_id", "owner_normalized",
              "source", "item_type", "target_field", "payload_json",
              "proposed_value_hash", "status", "status_at", "status_by",
              "dismiss_reason", "dismiss_note")


class WorkItemStore:
    """SQLite-backed work-items ledger. Constructed only when packets are
    enabled; instantiating it creates the two tables (CREATE TABLE IF NOT
    EXISTS) in the same database file the snapshot store uses."""

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

    # --- write: idempotent proposal ---

    def upsert_item(self, *, opp_id, owner, source, item_type, payload, as_of,
                    snapshot_id=None, by=None):
        """Idempotently record a proposed work item (R1.2). Re-running the
        packet build for the same underlying problem refreshes the matching
        OPEN item instead of duplicating it:

        - field_update keys on (opp_id, target_field) — a re-proposal for the
          same field supersedes even when the (LLM-worded) text differs, so the
          non-deterministic capture path cannot spawn duplicates.
        - every other item_type keys on (opp_id, item_type, proposed_value_hash)
          — identical deterministic-template inputs yield identical text.

        The whole check-then-insert/refresh runs in one transaction so it is
        atomic against concurrent web routes. Returns the work_item id.
        """
        if item_type not in ITEM_TYPES:
            raise ValueError(f"unknown item_type {item_type!r}")
        if item_type == "field_update" and not payload.get("field"):
            raise ValueError("field_update payload requires a 'field'")
        target_field = payload.get("field") if item_type == "field_update" else None
        phash = payload_hash(payload)
        pj = _payload_json(payload)
        at = _iso(as_of)
        holes = ",".join("?" for _ in OPEN_STATUSES)
        with self.conn:
            if item_type == "field_update":
                match = self.conn.execute(
                    f"SELECT id FROM work_items WHERE opp_id=? AND item_type=? "
                    f"AND target_field=? AND status IN ({holes}) ORDER BY id LIMIT 1",
                    (opp_id, item_type, target_field, *OPEN_STATUSES)).fetchone()
            else:
                match = self.conn.execute(
                    f"SELECT id FROM work_items WHERE opp_id=? AND item_type=? "
                    f"AND proposed_value_hash=? AND status IN ({holes}) "
                    f"ORDER BY id LIMIT 1",
                    (opp_id, item_type, phash, *OPEN_STATUSES)).fetchone()
            if match:
                wid = match[0]
                self.conn.execute(
                    "UPDATE work_items SET payload_json=?, proposed_value_hash=?, "
                    "snapshot_id=?, created_at=? WHERE id=?",
                    (pj, phash, snapshot_id, at, wid))
                return wid
            cur = self.conn.execute(
                "INSERT INTO work_items (created_at, snapshot_id, opp_id, "
                "owner_normalized, source, item_type, target_field, payload_json, "
                "proposed_value_hash, status, status_at, status_by) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (at, snapshot_id, opp_id, normalize_owner(owner), source,
                 item_type, target_field, pj, phash, "proposed", at, by))
            wid = cur.lastrowid
            self._append_event(wid, None, "proposed", by, "created", at)
            return wid

    # --- write: status transitions (every change is audit-logged, R1.4) ---

    def transition(self, item_id, to_status, *, at, by=None, reason=None,
                   dismiss_reason=None, dismiss_note=None):
        """Move an item to a new status and append an audit event. Every status
        change goes through here so `work_item_events` is the complete trail."""
        if to_status not in STATUSES:
            raise ValueError(f"unknown status {to_status!r}")
        at_s = _iso(at)
        with self.conn:
            row = self.conn.execute(
                "SELECT status FROM work_items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise KeyError(f"no work item {item_id}")
            from_status = row[0]
            self.conn.execute(
                "UPDATE work_items SET status=?, status_at=?, status_by=?, "
                "dismiss_reason=?, dismiss_note=? WHERE id=?",
                (to_status, at_s, by, dismiss_reason, dismiss_note, item_id))
            self._append_event(item_id, from_status, to_status, by, reason, at_s)

    def accept(self, item_id, *, at, by=None):
        self.transition(item_id, "accepted", at=at, by=by, reason="accepted")

    def dismiss(self, item_id, *, reason, at, by=None, note=None):
        self.transition(item_id, "dismissed", at=at, by=by, reason=reason,
                        dismiss_reason=reason, dismiss_note=note)

    def edit(self, item_id, new_payload, *, at, by=None):
        """Replace an item's payload and mark it `edited` (audit-logged)."""
        pj = _payload_json(new_payload)
        at_s = _iso(at)
        target_field = new_payload.get("field")
        with self.conn:
            row = self.conn.execute(
                "SELECT status FROM work_items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise KeyError(f"no work item {item_id}")
            self.conn.execute(
                "UPDATE work_items SET payload_json=?, proposed_value_hash=?, "
                "target_field=?, status=?, status_at=?, status_by=? WHERE id=?",
                (pj, payload_hash(new_payload), target_field, "edited", at_s,
                 by, item_id))
            self._append_event(item_id, row[0], "edited", by, "edited", at_s)

    def _append_event(self, work_item_id, from_status, to_status, by, reason, at):
        # Runs inside a caller-held transaction; never commits on its own.
        self.conn.execute(
            "INSERT INTO work_item_events (work_item_id, at, from_status, "
            "to_status, by, reason) VALUES (?,?,?,?,?,?)",
            (work_item_id, at, from_status, to_status, by, reason))

    # --- expiry ---

    def expire_resolved(self, rows, config, as_of, *, by="system"):
        """Auto-expire open items whose underlying problem is resolved in the
        newest snapshot (R1.3), per source class — there is no 1:1 item->rule
        map, so capture items cannot re-evaluate a rule:

        - rule-sourced (source is a rule id): re-evaluate that rule on the opp's
          current row; gone, or the opp absent/closed -> expired.
        - capture field_update: expired when the newest row's target field
          already equals the proposed value (the seller made the change).
        - manual / other: never auto-expired (cleared only by manual dismiss).

        `rows` is the newest snapshot's rows (caller passes store.load_rows).
        Threads as_of; no wall clock. Returns the count expired.
        """
        by_opp = {r["opp_id"]: r for r in rows}
        expired = 0
        open_items = self.conn.execute(
            f"SELECT id, opp_id, source, item_type, payload_json FROM work_items "
            f"WHERE status IN ({','.join('?' for _ in OPEN_STATUSES)})",
            OPEN_STATUSES).fetchall()
        for wid, opp_id, source, item_type, pj in open_items:
            if self._resolved(source, item_type, pj, by_opp.get(opp_id),
                              config, as_of):
                self.transition(wid, "expired", at=as_of, by=by,
                                reason="resolved_in_source")
                expired += 1
        return expired

    @staticmethod
    def _resolved(source, item_type, payload_json, row, config, as_of):
        if source in RULES:
            if row is None:
                return True  # opp gone from source -> the rule can't fire
            result = evaluate_row(row, config, as_of)  # empty for closed opps
            return all(v.rule_id != source for v in result.violations)
        if source == "capture" and item_type == "field_update":
            if row is None:
                return False
            payload = json.loads(payload_json)
            field = payload.get("field")
            if field is None or field not in row:
                return False
            return _coerce(row.get(field)) == _coerce(payload.get("proposed_value"))
        return False

    # --- reads ---

    def items(self, *, status=None, owner=None, open_only=False):
        clauses, params = [], []
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        if open_only:
            clauses.append(f"status IN ({','.join('?' for _ in OPEN_STATUSES)})")
            params.extend(OPEN_STATUSES)
        if owner is not None:
            clauses.append("owner_normalized=?")
            params.append(normalize_owner(owner))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        cur = self.conn.execute(
            f"SELECT {', '.join(_ITEM_COLS)} FROM work_items{where} ORDER BY id",
            params)
        return [dict(zip(_ITEM_COLS, row)) for row in cur.fetchall()]

    def events(self, item_id):
        cur = self.conn.execute(
            "SELECT work_item_id, at, from_status, to_status, by, reason "
            "FROM work_item_events WHERE work_item_id=? ORDER BY id", (item_id,))
        cols = ("work_item_id", "at", "from_status", "to_status", "by", "reason")
        return [dict(zip(cols, row)) for row in cur.fetchall()]
