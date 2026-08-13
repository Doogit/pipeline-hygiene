"""Org backtest: replay rules over stored history, join each flagged opp to
its final outcome. Plant = hand-built snapshots that trip exactly one rule
(H1 staleness) per opp; detect = the backtest walk and outcome join.
"""
from datetime import date, timedelta

import pytest

from src import backtest as bt
from src.seed import write_csv
from src.snapshots import SnapshotStore

AS_OF = date(2026, 8, 10)


def _row(opp_id, snap_date, **over):
    """A clean propose-stage row that trips NO rule; perturb one field to
    trip exactly one."""
    row = {
        "opp_id": opp_id, "account": "Granitefreight LLC", "opp_name": "Deal",
        "owner": "Avery Farrow", "stage": "propose", "amount": 50_000.0,
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


def _stale(opp_id, snap_date, **over):
    # last activity 40d ago >> propose staleness threshold (14d) -> H1 only.
    return _row(opp_id, snap_date,
                last_activity_date=snap_date - timedelta(days=40), **over)


def _ingest(store, tmp_path, snap_date, rows):
    path = tmp_path / f"opps_{snap_date.isoformat()}.csv"
    write_csv(path, rows)
    assert store.ingest_csv(path, snap_date).rejected == 0


def _store(tmp_path, config):
    d1, d2 = AS_OF - timedelta(days=7), AS_OF
    store = SnapshotStore(":memory:", config)
    _ingest(store, tmp_path, d1, [
        _stale("OPP-A", d1), _stale("OPP-B", d1),
        _stale("OPP-C", d1), _stale("OPP-D", d1),
        _row("OPP-CLEAN", d1),
    ])
    _ingest(store, tmp_path, d2, [
        _stale("OPP-A", d2, stage="closed_lost"),
        _stale("OPP-B", d2, stage="closed_lost"),
        _stale("OPP-C", d2, stage="closed_won"),
        _stale("OPP-D", d2),          # still open + still stale -> re-flagged
        _row("OPP-CLEAN", d2),
    ])
    return store


def _rec(records, rule):
    return next(r for r in records if r["rule"] == rule)


def test_flag_outcome_join_and_dedup(tmp_path, config):
    cfg = dict(config)
    cfg["min_closed_for_win_rate"] = 2      # let the share compute on small n
    store = _store(tmp_path, cfg)
    records, meta = bt.backtest(store, cfg, AS_OF)

    h1 = _rec(records, "H1")
    # A,B,C,D distinct — D flagged in both snapshots counts once (dedup)
    assert h1["flagged"] == 4
    assert (h1["won"], h1["lost"], h1["still_open"]) == (1, 2, 1)
    assert h1["resolved"] == 3
    assert h1["lost_share"] == pytest.approx(2 / 3)
    # the clean opp tripped nothing -> every other rule is empty
    assert all(_rec(records, r)["flagged"] == 0
               for r in ("H2", "H3", "H4", "H5", "H6", "H7", "H8", "H9",
                         "H10", "H11"))
    assert meta["dates"] == [AS_OF - timedelta(days=7), AS_OF]


def test_small_n_guard_hides_share(tmp_path, config):
    # default basis (>=10 resolved) -> 3 resolved is too few, share suppressed
    store = _store(tmp_path, config)
    records, _ = bt.backtest(store, config, AS_OF)
    assert _rec(records, "H1")["lost_share"] is None


def test_render_and_run_write_report(tmp_path, config):
    cfg = dict(config)
    cfg["min_closed_for_win_rate"] = 2
    store = _store(tmp_path, cfg)
    path, records = bt.run(store, AS_OF, cfg, tmp_path)
    assert path.exists() and path.name == f"backtest_{AS_OF.isoformat()}.md"
    text = path.read_text(encoding="utf-8")
    assert "| rule | flag | flagged |" in text
    assert "| H1 | stale by stage | 4 | 1 | 2 | 1 | 67% |" in text
    # a rule with no resolved flagged opps renders n/a, not a bogus 0%
    assert "n/a" in text


def test_empty_store_reports_nothing(tmp_path, config):
    store = SnapshotStore(":memory:", config)
    records, meta = bt.backtest(store, config, AS_OF)
    assert meta["dates"] == []
    assert all(r["flagged"] == 0 for r in records)
    text = bt.render_backtest(records, meta, config, AS_OF)
    assert "nothing to backtest" in text
