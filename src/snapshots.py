"""SQLite snapshot store: snapshots, opportunities, runs.

Holds full rows per (snapshot_date, opp_id) so history-dependent fields can be
derived from plain CRM exports that carry no field history:

- close_date_changes: count of consecutive-snapshot pairs (among snapshots at
  or before the evaluated one, where the opp is present) whose close_date
  differs.
- stage_entered_date: earliest snapshot date of the contiguous run (walking
  back from the evaluated snapshot) in which the current stage was observed.

Source columns, when present, take precedence over derivations. If a source
column is absent and fewer than 2 snapshots exist at-or-before the evaluated
date, the value stays None and dependent rules (H3, H6) report
insufficient_history.
"""
import json
import sqlite3
from datetime import date
from pathlib import Path

from .ingest import validate_csv

_DATE_COLS = ("created_date", "close_date", "last_activity_date",
              "next_step_date", "stage_entered_date")

_OPP_COLS = [
    "opp_id", "account", "opp_name", "owner", "stage", "amount", "currency",
    "created_date", "close_date", "last_activity_date", "next_step",
    "next_step_date", "forecast_category", "contact_count", "product_line",
    "stage_entered_date", "close_date_changes",
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_date TEXT PRIMARY KEY,
    source_file   TEXT NOT NULL,
    row_count     INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS opportunities (
    snapshot_date TEXT NOT NULL,
    opp_id TEXT NOT NULL,
    account TEXT, opp_name TEXT, owner TEXT, stage TEXT,
    amount REAL, currency TEXT,
    created_date TEXT, close_date TEXT, last_activity_date TEXT,
    next_step TEXT, next_step_date TEXT,
    forecast_category TEXT, contact_count INTEGER, product_line TEXT,
    stage_entered_date TEXT, close_date_changes INTEGER,
    PRIMARY KEY (snapshot_date, opp_id)
);
CREATE TABLE IF NOT EXISTS runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    as_of TEXT NOT NULL,
    snapshot_date TEXT NOT NULL,
    summary_json TEXT,
    validation_json TEXT
);
"""


def _to_db(value):
    return value.isoformat() if isinstance(value, date) else value


def _from_db(col, value):
    if value is None:
        return None
    if col in _DATE_COLS:
        return date.fromisoformat(value)
    return value


class SnapshotStore:
    def __init__(self, db_path, config):
        self.db_path = str(db_path)
        self.config = config
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    # --- ingest ---

    def ingest_csv(self, csv_path, snapshot_date, stage_map_name="default"):
        """Validate then store a snapshot (replacing any same-date snapshot)."""
        rows, report = validate_csv(csv_path, self.config, stage_map_name)
        sd = snapshot_date.isoformat()
        with self.conn:
            self.conn.execute("DELETE FROM opportunities WHERE snapshot_date = ?", (sd,))
            self.conn.execute(
                "INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?)",
                (sd, str(csv_path), len(rows)))
            self.conn.executemany(
                f"INSERT INTO opportunities (snapshot_date, {', '.join(_OPP_COLS)}) "
                f"VALUES ({', '.join(['?'] * (len(_OPP_COLS) + 1))})",
                [[sd] + [_to_db(r[c]) for c in _OPP_COLS] for r in rows])
        return report

    # --- reads ---

    def snapshot_dates(self):
        cur = self.conn.execute("SELECT snapshot_date FROM snapshots ORDER BY snapshot_date")
        return [date.fromisoformat(r[0]) for r in cur.fetchall()]

    def latest_snapshot_date(self):
        dates = self.snapshot_dates()
        return dates[-1] if dates else None

    def load_rows(self, snapshot_date):
        cur = self.conn.execute(
            f"SELECT {', '.join(_OPP_COLS)} FROM opportunities "
            f"WHERE snapshot_date = ? ORDER BY opp_id",
            (snapshot_date.isoformat(),))
        return [{c: _from_db(c, v) for c, v in zip(_OPP_COLS, row)}
                for row in cur.fetchall()]

    # --- history derivations ---

    def _history(self, opp_id, up_to):
        """(snapshot_date, stage, close_date) for this opp, ascending, <= up_to."""
        cur = self.conn.execute(
            "SELECT snapshot_date, stage, close_date FROM opportunities "
            "WHERE opp_id = ? AND snapshot_date <= ? ORDER BY snapshot_date",
            (opp_id, up_to.isoformat()))
        return cur.fetchall()

    def rows_with_history(self, snapshot_date):
        """Rows for a snapshot with optional columns filled from history when
        absent and >= 2 snapshots exist at-or-before snapshot_date."""
        rows = self.load_rows(snapshot_date)
        n_snapshots = len([d for d in self.snapshot_dates() if d <= snapshot_date])
        if n_snapshots < 2:
            return rows
        for row in rows:
            if row["close_date_changes"] is not None and row["stage_entered_date"] is not None:
                continue
            history = self._history(row["opp_id"], snapshot_date)
            if row["close_date_changes"] is None:
                row["close_date_changes"] = sum(
                    1 for a, b in zip(history, history[1:]) if a[2] != b[2])
            if row["stage_entered_date"] is None:
                entered = snapshot_date
                for snap_date, stage, _ in reversed(history):
                    if stage != row["stage"]:
                        break
                    entered = date.fromisoformat(snap_date)
                row["stage_entered_date"] = entered
        return rows

    # --- runs ---

    def record_run(self, as_of, snapshot_date, summary, validation_report=None):
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO runs (as_of, snapshot_date, summary_json, validation_json) "
                "VALUES (?, ?, ?, ?)",
                (as_of.isoformat(), snapshot_date.isoformat(),
                 json.dumps(summary, sort_keys=True),
                 json.dumps(validation_report.to_dict(), sort_keys=True)
                 if validation_report else None))
        return cur.lastrowid

    def last_run(self, before_run_id=None):
        query = "SELECT run_id, as_of, snapshot_date, summary_json FROM runs"
        params = ()
        if before_run_id is not None:
            query += " WHERE run_id < ?"
            params = (before_run_id,)
        query += " ORDER BY run_id DESC LIMIT 1"
        row = self.conn.execute(query, params).fetchone()
        if row is None:
            return None
        return {"run_id": row[0], "as_of": date.fromisoformat(row[1]),
                "snapshot_date": date.fromisoformat(row[2]),
                "summary": json.loads(row[3]) if row[3] else None}
