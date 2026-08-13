"""Stage funnel: derive width / advancement / dwell from stored snapshots.
Plant = hand-built weekly snapshots moving opps through known stages; detect =
the funnel walk. Plus a smoke test over the regenerated progression-on demo
store.
"""
from datetime import date, timedelta
from pathlib import Path

from src import funnel as fn
from src.seed import write_csv
from src.snapshots import SnapshotStore

D1 = date(2026, 7, 27)
D2 = date(2026, 8, 3)
D3 = date(2026, 8, 10)
DEMO_SNAPSHOTS = Path(__file__).resolve().parent.parent / "data" / "snapshots"


def _row(opp_id, snap_date, stage, **over):
    """A schema-valid row in a given stage (rule violations are irrelevant to
    the funnel, which reads only opp_id/snapshot_date/stage)."""
    row = {
        "opp_id": opp_id, "account": "Granitefreight LLC", "opp_name": "Deal",
        "owner": "Avery Farrow", "stage": stage, "amount": 50_000.0,
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


def _ingest(store, tmp_path, snap_date, rows):
    path = tmp_path / f"opps_{snap_date.isoformat()}.csv"
    write_csv(path, rows)
    assert store.ingest_csv(path, snap_date).rejected == 0


def _store(tmp_path, config):
    """OPP-ADV: prospect->qualify->develop (advances). OPP-STALL: qualify x3
    (never moves). OPP-WON: propose x2 then closed_won. OPP-LOST: develop then
    closed_lost x2."""
    store = SnapshotStore(":memory:", config)
    _ingest(store, tmp_path, D1, [
        _row("OPP-ADV", D1, "prospect"), _row("OPP-STALL", D1, "qualify"),
        _row("OPP-WON", D1, "propose"), _row("OPP-LOST", D1, "develop"),
    ])
    _ingest(store, tmp_path, D2, [
        _row("OPP-ADV", D2, "qualify"), _row("OPP-STALL", D2, "qualify"),
        _row("OPP-WON", D2, "propose"), _row("OPP-LOST", D2, "closed_lost"),
    ])
    _ingest(store, tmp_path, D3, [
        _row("OPP-ADV", D3, "develop"), _row("OPP-STALL", D3, "qualify"),
        _row("OPP-WON", D3, "closed_won"), _row("OPP-LOST", D3, "closed_lost"),
    ])
    return store


def _rec(records, stage):
    return next(r for r in records if r["stage"] == stage)


def test_widths_advancement_and_dwell(tmp_path, config):
    store = _store(tmp_path, config)
    records, meta = fn.funnel(store, config, D3)
    assert meta["dates"] == [D1, D2, D3]

    prospect = _rec(records, "prospect")
    assert prospect["width"] == 1                      # only OPP-ADV
    assert prospect["advanced"] == 1
    assert prospect["advancement_rate"] == 1.0
    assert prospect["median_dwell_days"] == 7          # D1 -> D2 exit

    qualify = _rec(records, "qualify")
    assert qualify["width"] == 2                        # OPP-ADV, OPP-STALL
    assert (qualify["advanced"], qualify["stalled"]) == (1, 1)
    assert qualify["advancement_rate"] == 0.5
    assert qualify["median_dwell_days"] == 7           # ADV left after 7d

    develop = _rec(records, "develop")
    assert develop["width"] == 2                        # OPP-ADV (still there), OPP-LOST
    assert (develop["advanced"], develop["stalled"], develop["lost"]) == (0, 1, 1)
    assert develop["advancement_rate"] == 0.0
    assert develop["median_dwell_days"] == 7           # LOST left to closed after 7d

    propose = _rec(records, "propose")
    assert propose["width"] == 1                        # OPP-WON
    assert (propose["won"], propose["advanced"]) == (1, 0)
    assert propose["median_dwell_days"] == 14          # D1 -> D3 close

    commit = _rec(records, "commit")
    assert commit["width"] == 0
    assert commit["advancement_rate"] is None
    assert commit["median_dwell_days"] is None


def test_render_and_run_write_report(tmp_path, config):
    store = _store(tmp_path, config)
    path, records = fn.run(store, D3, config, tmp_path)
    assert path.exists() and path.name == f"funnel_{D3.isoformat()}.md"
    text = path.read_text(encoding="utf-8")
    assert "| stage | width | advanced | stalled | won | lost |" in text
    assert "| prospect | 1 | 1 | 0 | 0 | 0 | 100% | 7d |" in text
    # a stage with no opps renders n/a, not a bogus 0
    assert "| commit | 0 | 0 | 0 | 0 | 0 | n/a | n/a |" in text


def test_empty_store_reports_nothing(config):
    store = SnapshotStore(":memory:", config)
    records, meta = fn.funnel(store, config, D3)
    assert meta["dates"] == []
    assert all(r["width"] == 0 for r in records)
    text = fn.render_funnel(records, meta, config, D3)
    assert "nothing to analyze" in text


def test_smoke_on_progression_demo_store(tmp_path, config):
    """The regenerated demo series (progression on) yields real mid-funnel
    movement: some stage advances and reports a median dwell."""
    store = SnapshotStore(":memory:", config)
    for csv_path in sorted(DEMO_SNAPSHOTS.glob("opps_*.csv")):
        snap_date = date.fromisoformat(csv_path.stem.split("_")[1])
        assert store.ingest_csv(csv_path, snap_date).rejected == 0
    as_of = store.latest_snapshot_date()
    records, meta = fn.funnel(store, config, as_of)
    assert meta["transitions"] > 0
    assert any(r["advanced"] > 0 for r in records)
    assert any(r["median_dwell_days"] is not None for r in records)
