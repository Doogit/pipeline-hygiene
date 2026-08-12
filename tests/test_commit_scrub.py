"""WS5: --commit-scrub pre-forecast-call sheet.

Every OPEN commit/best_case opp appears (flagged or not); a scrub never
records a run and never touches the brief filename; --owner/--team/--region
compose via build(owner_filter=...).
"""
import sqlite3
from datetime import date, timedelta

from src import brief
from src.scoring import days_left_in_quarter
from src.seed import write_csv
from src.snapshots import SnapshotStore

from .conftest import TEST_CONFIG_PATH

AS_OF = date(2026, 8, 10)
PREV = AS_OF - timedelta(days=7)


def _row(opp_id, owner, snap_date, **over):
    row = {
        "opp_id": opp_id, "account": "Granitefreight LLC", "opp_name": "Deal",
        "owner": owner, "stage": "propose", "amount": 50_000.0,
        "currency": "USD", "created_date": date(2026, 1, 5),
        "close_date": date(2026, 10, 1), "last_activity_date": snap_date,
        "next_step": "Send updated proposal to procurement",
        "next_step_date": snap_date + timedelta(days=10),
        "forecast_category": "pipeline", "contact_count": 3,
        "product_line": "CorePlatform",
        "stage_entered_date": snap_date - timedelta(days=3),
        "close_date_changes": 0,
    }
    row.update(over)
    return row


def _seeded_store(tmp_path, config, db=":memory:"):
    """OPP-X: committed at PREV for 2026-09-15 (FY2027-Q1), pushed to
    2026-10-20 by AS_OF, still commit. OPP-Y: best_case, never commit (no
    ledger entry). OPP-Z: pipeline (excluded). OPP-W: commit then won
    (closed -> excluded)."""
    store = SnapshotStore(db, config)
    rows_prev = [
        _row("OPP-X", "Alex Rivera", PREV, forecast_category="commit",
             amount=100_000.0, close_date=date(2026, 9, 15)),
        _row("OPP-Y", "Blake Osei", PREV),
        _row("OPP-Z", "Alex Rivera", PREV),
        _row("OPP-W", "Alex Rivera", PREV, forecast_category="commit",
             close_date=date(2026, 8, 20)),
    ]
    path = tmp_path / f"opps_{PREV.isoformat()}.csv"
    write_csv(path, rows_prev)
    assert store.ingest_csv(path, PREV).rejected == 0
    rows_cur = [
        _row("OPP-X", "Alex Rivera", AS_OF, forecast_category="commit",
             amount=100_000.0, close_date=date(2026, 10, 20),
             close_date_changes=1),
        _row("OPP-Y", "Blake Osei", AS_OF, forecast_category="best_case"),
        _row("OPP-Z", "Alex Rivera", AS_OF),
        _row("OPP-W", "Alex Rivera", AS_OF, stage="closed_won",
             forecast_category="commit", close_date=date(2026, 8, 20)),
    ]
    path = tmp_path / f"opps_{AS_OF.isoformat()}.csv"
    write_csv(path, rows_cur)
    assert store.ingest_csv(path, AS_OF).rejected == 0
    return store


def test_days_left_in_quarter_boundaries():
    assert days_left_in_quarter(date(2026, 8, 10), 7) == 51
    assert days_left_in_quarter(date(2026, 9, 30), 7) == 0
    assert days_left_in_quarter(date(2026, 10, 1), 7) == 91


def test_scrub_content(tmp_path, config):
    store = _seeded_store(tmp_path, config)
    path, data = brief.run_commit_scrub(store, AS_OF, AS_OF, config,
                                        tmp_path / "out")
    assert path.name == f"commit_scrub_{AS_OF.isoformat()}.md"
    markdown = path.read_text(encoding="utf-8")
    assert "# Commit scrub — 2026-08-10" in markdown
    assert "51 day(s) left in FY2027-Q1." in markdown
    assert "2 open commit/best_case opp(s), $150,000." in markdown
    # every open commit/best_case opp, flagged or not; dollar-ranked
    x = next(l for l in markdown.splitlines() if l.startswith("| OPP-X"))
    y = next(l for l in markdown.splitlines() if l.startswith("| OPP-Y"))
    assert markdown.index("| OPP-X") < markdown.index("| OPP-Y")
    assert "OPP-Z" not in markdown and "OPP-W" not in markdown
    # ledger context: committed-for anchors to close_date at first commit
    assert "| FY2027-Q1 | 1 |" in x        # committed-for, one push
    assert "| - | 0 |" in y                # never commit -> no ledger entry
    # three literal blank checklist cells on every row
    assert x.endswith("| [ ] | [ ] | [ ] |")
    assert y.endswith("| [ ] | [ ] | [ ] |")


def test_scrub_records_no_run_and_writes_no_brief(tmp_path, config):
    store = _seeded_store(tmp_path, config)
    out = tmp_path / "out"
    brief.run_commit_scrub(store, AS_OF, AS_OF, config, out)
    assert store.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    assert list(out.glob("desk_brief_*")) == []


def test_scrub_composes_with_owner_filter(tmp_path, config):
    store = _seeded_store(tmp_path, config)
    path, _ = brief.run_commit_scrub(store, AS_OF, AS_OF, config,
                                     tmp_path / "out",
                                     owner_filter={"Blake Osei"},
                                     filter_label="owner Blake Osei",
                                     filter_slug="blake-osei")
    assert path.name == f"commit_scrub_{AS_OF.isoformat()}_blake-osei.md"
    markdown = path.read_text(encoding="utf-8")
    assert "FILTERED — owner Blake Osei." in markdown
    assert "1 open commit/best_case opp(s), $50,000." in markdown
    assert "OPP-X" not in markdown
    assert store.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_cli_commit_scrub_writes_only_the_scrub(tmp_path, config, capsys):
    db = tmp_path / "pipeline.db"
    _seeded_store(tmp_path, config, db=str(db))
    out = tmp_path / "out"
    rc = brief.main(["--as-of", AS_OF.isoformat(), "--db", str(db),
                     "--config", str(TEST_CONFIG_PATH),
                     "--out-dir", str(out), "--commit-scrub"])
    assert rc == 0
    assert (out / f"commit_scrub_{AS_OF.isoformat()}.md").exists()
    assert list(out.glob("desk_brief_*")) == []
    assert sqlite3.connect(db).execute(
        "SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    assert "2 open commit/best_case opps" in capsys.readouterr().out


def test_cli_commit_scrub_honors_env_paths(tmp_path, config, monkeypatch,
                                           capsys):
    db = tmp_path / "env_pipeline.db"
    _seeded_store(tmp_path, config, db=str(db))
    out = tmp_path / "env_out"
    monkeypatch.setenv("PIPELINE_HYGIENE_DB", str(db))
    monkeypatch.setenv("PIPELINE_HYGIENE_CONFIG", str(TEST_CONFIG_PATH))
    monkeypatch.setenv("PIPELINE_HYGIENE_OUT", str(out))

    rc = brief.main(["--as-of", AS_OF.isoformat(), "--commit-scrub"])

    assert rc == 0
    assert (out / f"commit_scrub_{AS_OF.isoformat()}.md").exists()
    captured = capsys.readouterr()
    assert f"reading snapshot store {db}" in captured.err
    assert "2 open commit/best_case opps" in captured.out
