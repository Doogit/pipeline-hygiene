"""Session 6: --owner/--team brief filter.

A filtered brief recomputes every rollup and dollar figure over the
selection, but the required coverage multiple stays desk-wide (one coverage
basis everywhere: a team's filtered coverage must equal its row in the
unfiltered Teams table). Filtered runs are never recorded — a partial
open-opp map would corrupt flag streaks and since-last-run for every later
full brief — and write to a suffixed filename so the canonical brief
survives.
"""
from datetime import date, timedelta

import pytest

from src import brief
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


QUOTA_PAYLOAD = {
    "quotas": {"Alex Rivera": 200_000, "Blake Osei": 200_000,
               "Casey Nguyen": 200_000},
    "owners": {"Alex Rivera": {"team": "Team East", "region": "East"},
               "Blake Osei": {"team": "Team East", "region": "East"},
               "Casey Nguyen": {"team": "Team West", "region": "West"}},
}


def _seeded_store(tmp_path, config):
    """3 owners, 2 snapshots. Casey carries 10 closed outcomes (5 won,
    5 lost) so the desk-wide trailing win rate is 50% -> multiple 2.0 —
    while Alex alone has ZERO closed history, which makes any accidental
    per-selection recomputation observable (it would fall back to
    coverage_ratio_min)."""
    cfg = brief.merge_quota_payload(config, QUOTA_PAYLOAD)
    store = SnapshotStore(":memory:", cfg)
    closed = [_row(f"OPP-C{i}", "Casey Nguyen", PREV) for i in range(10)]
    open_rows = [
        _row("OPP-A1", "Alex Rivera", PREV),
        _row("OPP-A2", "Alex Rivera", PREV),
        _row("OPP-B1", "Blake Osei", PREV),
    ]
    path = tmp_path / f"opps_{PREV.isoformat()}.csv"
    write_csv(path, closed + open_rows)
    assert store.ingest_csv(path, PREV).rejected == 0

    closed2 = [_row(f"OPP-C{i}", "Casey Nguyen", AS_OF,
                    stage="closed_won" if i < 5 else "closed_lost")
               for i in range(10)]
    open_rows2 = [
        _row("OPP-A1", "Alex Rivera", AS_OF),
        _row("OPP-A2", "Alex Rivera", AS_OF),
        _row("OPP-B1", "Blake Osei", AS_OF),
    ]
    path = tmp_path / f"opps_{AS_OF.isoformat()}.csv"
    write_csv(path, closed2 + open_rows2)
    assert store.ingest_csv(path, AS_OF).rejected == 0
    return store, cfg


def test_owner_filter_recomputes_rollups(tmp_path, config):
    store, cfg = _seeded_store(tmp_path, config)
    data = brief.build(store, AS_OF, AS_OF, cfg,
                       owner_filter={"Alex Rivera"},
                       filter_label="owner Alex Rivera")
    assert set(data["owners"]) == {"Alex Rivera"}
    assert data["desk"].n_open == 2
    assert set(data["teams"]) == {"Team East"}
    # roster quota restricted to the selection (not Blake's share)
    assert data["teams"]["Team East"].quota == 200_000
    assert all(e.owner == "Alex Rivera" for e in data["ledger"])
    assert set(data["patterns"]) == {"Alex Rivera"}
    markdown = brief.render(data, cfg)
    assert "FILTERED BRIEF — owner Alex Rivera" in markdown
    assert "Blake Osei" not in markdown
    assert "Casey Nguyen" not in markdown


def test_filter_keeps_desk_wide_coverage_multiple(tmp_path, config):
    store, cfg = _seeded_store(tmp_path, config)
    full = brief.build(store, AS_OF, AS_OF, cfg)
    assert full["coverage_multiple"] == pytest.approx(2.0)
    filtered = brief.build(store, AS_OF, AS_OF, cfg,
                           owner_filter={"Alex Rivera"},
                           filter_label="owner Alex Rivera")
    # Alex has zero closed outcomes; a per-selection recomputation would
    # fall back to coverage_ratio_min. The basis must stay desk-wide.
    assert filtered["coverage_multiple"] == full["coverage_multiple"]
    assert filtered["coverage_basis"] == full["coverage_basis"]
    assert filtered["trajectory"]["coverage"]["required_multiple"] == \
        pytest.approx(2.0)
    # ...while quota restricts to the selection
    assert filtered["trajectory"]["coverage"]["total_quota"] == 200_000


def test_resolve_owner_filter_names_and_errors(tmp_path, config):
    store, cfg = _seeded_store(tmp_path, config)
    owners = store.owners(AS_OF)
    selected, label = brief.resolve_owner_filter(cfg, owners, None,
                                                 ["Team East"])
    assert selected == {"Alex Rivera", "Blake Osei"}
    assert label == "team Team East"
    selected, label = brief.resolve_owner_filter(
        cfg, owners, ["Casey Nguyen"], ["Team East"])
    assert selected == {"Alex Rivera", "Blake Osei", "Casey Nguyen"}
    with pytest.raises(ValueError, match="unknown team"):
        brief.resolve_owner_filter(cfg, owners, None, ["Team Nowhere"])
    with pytest.raises(ValueError, match="unknown owner"):
        brief.resolve_owner_filter(cfg, owners, ["Nobody"], None)
    with pytest.raises(ValueError, match="team metadata"):
        brief.resolve_owner_filter(config, owners, None, ["Team East"])


def test_filtered_run_not_recorded_and_file_suffixed(tmp_path, config):
    store, cfg = _seeded_store(tmp_path, config)
    out = tmp_path / "out"
    full_path, _ = brief.run(store, AS_OF, AS_OF, cfg, out)
    assert store.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    full_before = full_path.read_text(encoding="utf-8")

    path, _ = brief.run(store, AS_OF, AS_OF, cfg, out,
                        owner_filter={"Alex Rivera", "Blake Osei"},
                        filter_label="team Team East")
    assert path.name == f"desk_brief_{AS_OF.isoformat()}_team-team-east.md"
    assert store.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    assert full_path.read_text(encoding="utf-8") == full_before


def test_team_filtered_coverage_equals_unfiltered_team_row(tmp_path, config):
    store, cfg = _seeded_store(tmp_path, config)
    full = brief.build(store, AS_OF, AS_OF, cfg)
    filtered = brief.build(store, AS_OF, AS_OF, cfg,
                           owner_filter={"Alex Rivera", "Blake Osei"},
                           filter_label="team Team East")
    assert filtered["teams"]["Team East"].coverage_ratio == \
        full["teams"]["Team East"].coverage_ratio
    assert filtered["teams"]["Team East"].coverage_flagged == \
        full["teams"]["Team East"].coverage_flagged


def test_cli_filter(tmp_path, config):
    import json
    cfg = brief.merge_quota_payload(config, QUOTA_PAYLOAD)
    db = tmp_path / "pipeline.db"
    store = SnapshotStore(db, cfg)
    path = tmp_path / f"opps_{AS_OF.isoformat()}.csv"
    write_csv(path, [_row("OPP-A1", "Alex Rivera", AS_OF),
                     _row("OPP-B1", "Blake Osei", AS_OF)])
    assert store.ingest_csv(path, AS_OF).rejected == 0
    store.close()
    quotas = tmp_path / "quotas.json"
    quotas.write_text(json.dumps(QUOTA_PAYLOAD), encoding="utf-8")
    base = ["--as-of", AS_OF.isoformat(), "--db", str(db),
            "--config", str(TEST_CONFIG_PATH), "--out-dir",
            str(tmp_path / "out"), "--quotas", str(quotas)]

    assert brief.main(base + ["--owner", "Nobody"]) == 2
    assert brief.main(base + ["--team", "Team Nowhere"]) == 2
    assert brief.main(base + ["--team", "Team East"]) == 0
    out = tmp_path / "out" / \
        f"desk_brief_{AS_OF.isoformat()}_team-team-east.md"
    generated = out.read_text(encoding="utf-8")
    assert "FILTERED BRIEF — team Team East" in generated
    assert "Alex Rivera" in generated and "Blake Osei" in generated
