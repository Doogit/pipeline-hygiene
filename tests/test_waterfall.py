"""snapshots.waterfall: synthetic bridge fixtures (removed and
close+amount-change never occur in seeded data), a hypothesis property test
for the reconciliation identity, and the seeded-series cross-check against
the delta manifest.
"""
import random
from datetime import date

import pytest
from hypothesis import given, settings
from hypothesis import strategies as hst

from src import brief
from src.seed import write_csv
from src.seed.org import build_org
from src.seed.series import generate_series
from src.snapshots import SnapshotStore

AS_OF = date(2026, 8, 10)
D_PREV, D_CUR = date(2026, 8, 3), date(2026, 8, 10)

OPEN_STAGES = ("prospect", "qualify", "develop", "propose", "commit")


def _store_with(config, snapshots):
    """snapshots: {date: [(opp_id, owner, stage, amount, close_date)]}
    inserted directly — waterfall only reads owner/stage/amount/close_date."""
    store = SnapshotStore(":memory:", config)
    for snap_date, rows in snapshots.items():
        store.conn.executemany(
            "INSERT INTO opportunities "
            "(snapshot_date, opp_id, owner, stage, amount, close_date) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(snap_date.isoformat(), o, owner, stage, amount,
              close.isoformat() if close else None)
             for o, owner, stage, amount, close in rows])
        store.conn.execute(
            "INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?, NULL)",
            (snap_date.isoformat(), "synthetic", len(rows)))
    store.conn.commit()
    return store


def _reconciles(wf):
    lhs = (wf["beginning"]["dollars"] + wf["created"]["dollars"]
           + wf["increased"]["dollars"] - wf["decreased"]["dollars"]
           - wf["won"]["dollars"] - wf["lost"]["dollars"]
           - wf["removed"]["dollars"])
    assert lhs == pytest.approx(wf["ending"]["dollars"], abs=1e-6)


def test_waterfall_synthetic_bridge(config):
    sep30, sep15, sep29, oct1, oct15 = (date(2026, 9, 30), date(2026, 9, 15),
                                        date(2026, 9, 29), date(2026, 10, 1),
                                        date(2026, 10, 15))
    store = _store_with(config, {
        D_PREV: [
            ("A", "Alice", "develop", 100.0, sep30),
            ("B", "Alice", "propose", 200.0, sep30),
            ("C", "Bob", "qualify", 300.0, sep30),
            ("D", "Bob", "commit", 400.0, sep15),
            ("E", "Bob", "closed_won", 500.0, sep15),   # closed pre-pair
            ("F", "Bob", "prospect", 150.0, oct1),
        ],
        D_CUR: [
            ("A", "Alice", "closed_won", 120.0, sep30),  # close+amount-change
            ("B", "Alice", "closed_lost", 200.0, oct15), # close+push same pair
            # C absent -> removed
            ("D", "Bob", "commit", 380.0, sep29),        # decreased + pushed
            ("E", "Bob", "closed_won", 500.0, sep15),    # persists: no recount
            ("F", "Bob", "prospect", 150.0, oct1),
            ("G", "Bob", "develop", 250.0, oct1),        # created
            ("H", "Bob", "closed_lost", 90.0, oct1),     # arrives closed: nothing
        ],
    })
    wf = store.waterfall(D_PREV, D_CUR)
    assert wf["beginning"] == {"n": 5, "dollars": 1150.0}
    assert wf["created"] == {"n": 1, "dollars": 250.0}
    # A's pre-close appreciation is carried as drift so the bridge
    # reconciles while won reports the closing amount
    assert wf["increased"] == {"n": 1, "dollars": 20.0}
    assert wf["decreased"] == {"n": 1, "dollars": 20.0}
    assert wf["won"] == {"n": 1, "dollars": 120.0}       # closing amount
    assert wf["lost"] == {"n": 1, "dollars": 200.0}
    assert wf["removed"] == {"n": 1, "dollars": 300.0}
    assert wf["ending"] == {"n": 3, "dollars": 780.0}
    # pushes move no dollars; B's same-pair push folds into lost
    assert wf["pushed"] == {"n": 1, "dollars": 380.0}
    _reconciles(wf)


def test_waterfall_owner_filter(config):
    store = _store_with(config, {
        D_PREV: [("A", "Alice", "develop", 100.0, date(2026, 9, 30)),
                 ("X", "Bob", "develop", 900.0, date(2026, 9, 30))],
        D_CUR: [("A", "Alice", "closed_won", 100.0, date(2026, 9, 30)),
                ("X", "Bob", "develop", 900.0, date(2026, 9, 30))],
    })
    wf = store.waterfall(D_PREV, D_CUR, owners={"Alice"})
    assert wf["beginning"] == {"n": 1, "dollars": 100.0}
    assert wf["won"] == {"n": 1, "dollars": 100.0}
    assert wf["ending"] == {"n": 0, "dollars": 0.0}
    _reconciles(wf)


_state = hst.one_of(
    hst.none(),
    hst.tuples(
        hst.sampled_from(OPEN_STAGES + ("closed_won", "closed_lost")),
        hst.one_of(hst.none(),
                   hst.floats(min_value=0, max_value=1e6,
                              allow_nan=False, allow_infinity=False))))


@settings(max_examples=60, deadline=None)
@given(hst.lists(hst.tuples(_state, _state), max_size=25))
def test_waterfall_reconciliation_property(config, opps):
    """beginning + created + increased - decreased - won - lost - removed
    == ending, for arbitrary presence/stage/amount transitions (including
    reopened rows, None amounts, and arrive-already-closed rows)."""
    snapshots = {D_PREV: [], D_CUR: []}
    for i, (prev_state, cur_state) in enumerate(opps):
        opp_id = f"OPP-{i:04d}"
        if prev_state is not None:
            snapshots[D_PREV].append(
                (opp_id, "Owner", prev_state[0], prev_state[1], None))
        if cur_state is not None:
            snapshots[D_CUR].append(
                (opp_id, "Owner", cur_state[0], cur_state[1], None))
    store = _store_with(config, snapshots)
    _reconciles(store.waterfall(D_PREV, D_CUR))
    store.close()


def test_waterfall_matches_series_manifest(tmp_path, config):
    """Seeded-series cross-check: waterfall created/won/lost line up with
    the scripted delta manifest; removed never occurs in seeded data."""
    rng = random.Random(42)
    org = build_org(rng)
    dates, snapshots, manifest = generate_series(org, 150, 4, AS_OF, config,
                                                 rng)
    store = SnapshotStore(":memory:", config)
    for d, rows in snapshots:
        p = tmp_path / f"opps_{d.isoformat()}.csv"
        write_csv(p, rows)
        assert store.ingest_csv(p, d).rejected == 0
    for i, (prev_d, cur_d) in enumerate(zip(dates, dates[1:])):
        wf = store.waterfall(prev_d, cur_d)
        scripted = manifest["deltas"][i]
        assert wf["created"]["n"] == len(scripted["added"])
        assert wf["won"]["n"] + wf["lost"]["n"] == len(scripted["closed"])
        assert wf["removed"] == {"n": 0, "dollars": 0.0}
        _reconciles(wf)


def test_pacing_data_target_optional(config):
    waterfalls = [
        {"cur_date": date(2026, 8, 3), "created": {"n": 2, "dollars": 100.0}},
        {"cur_date": date(2026, 8, 10), "created": {"n": 1, "dollars": 50.0}},
    ]
    pacing = brief.pacing_data(waterfalls, config)
    assert pacing["target"] is None
    assert pacing["mean_dollars"] == 75.0
    assert [w["dollars"] for w in pacing["weekly"]] == [100.0, 50.0]
    with_target = brief.pacing_data(
        waterfalls, dict(config, pipeline_gen_weekly_target=200))
    assert with_target["target"] == 200
    assert brief.pacing_data([], config) is None
