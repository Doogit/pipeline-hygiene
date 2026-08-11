"""Task 11: sandbagging / happy-ears detection + persona-recovery ground truth.

The seed manifest maps owners -> personas; the detector only ever sees field
history (plant = persona-driven field patterns in the simulator, detect =
statistics over snapshots — non-circular). The recovery test asserts
flagged-overcall owners are majority happy_ears, flagged-undercall majority
sandbagger, and clean operators entirely unflagged.
"""
import random
from datetime import date, timedelta

from src import brief
from src.patterns import owner_patterns
from src.seed import write_csv
from src.seed.org import build_org
from src.seed.series import generate_series
from src.snapshots import SnapshotStore

AS_OF = date(2026, 8, 10)


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


def _ingest(store, tmp_path, snap_date, rows):
    path = tmp_path / f"opps_{snap_date.isoformat()}.csv"
    write_csv(path, rows)
    assert store.ingest_csv(path, snap_date).rejected == 0


def test_overcall_mechanics_and_suppression(tmp_path, config):
    d1, d2 = AS_OF - timedelta(days=7), AS_OF
    store = SnapshotStore(":memory:", config)
    # OPP-A: commit, close date pushed after first commit week -> overcalled
    # OPP-B: commit, untouched -> not overcalled
    _ingest(store, tmp_path, d1, [
        _row("OPP-A", "Avery Farrow", d1, forecast_category="commit"),
        _row("OPP-B", "Avery Farrow", d1, forecast_category="commit")])
    _ingest(store, tmp_path, d2, [
        _row("OPP-A", "Avery Farrow", d2, forecast_category="commit",
             close_date=date(2026, 10, 15), close_date_changes=1),
        _row("OPP-B", "Avery Farrow", d2, forecast_category="commit")])
    p = owner_patterns(store, AS_OF, config)["Avery Farrow"]
    assert p.n_ever_commit == 2
    assert p.overcall_share == 0.5
    # 2 ever-commit opps < min_opps_for_owner_score -> suppressed, unflagged
    assert p.overcall_small_n and not p.overcall_flagged


def test_undercall_won_share_counts_only_observed_wins(tmp_path, config):
    d1, d2 = AS_OF - timedelta(days=7), AS_OF
    store = SnapshotStore(":memory:", config)
    _ingest(store, tmp_path, d1, [
        # open then won without ever commit/best_case -> never-called win
        _row("OPP-A", "Blair Farrow", d1),
        # open then won, was best_case before -> called win
        _row("OPP-B", "Blair Farrow", d1, forecast_category="best_case"),
        # already closed in the first stored snapshot -> excluded (no
        # pre-win history)
        _row("OPP-C", "Blair Farrow", d1, stage="closed_won",
             forecast_category="omitted"),
    ])
    _ingest(store, tmp_path, d2, [
        _row("OPP-A", "Blair Farrow", d2, stage="closed_won"),
        _row("OPP-B", "Blair Farrow", d2, stage="closed_won",
             forecast_category="best_case"),
        _row("OPP-C", "Blair Farrow", d2, stage="closed_won",
             forecast_category="omitted"),
    ])
    p = owner_patterns(store, AS_OF, config)["Blair Farrow"]
    assert p.n_won == 2
    assert p.undercall_won_share == 0.5


def test_persona_recovery(tmp_path, config):
    """The point of this task: detector output at the last snapshot recovers
    the planted personas from field history alone."""
    rng = random.Random(42)
    org = build_org(rng)
    dates, snapshots, manifest = generate_series(org, 800, 8, AS_OF, config,
                                                 rng)
    store = SnapshotStore(":memory:", config)
    for d, rows in snapshots:
        _ingest(store, tmp_path, d, rows)

    persona = {s.name: s.persona for s in org.sellers}
    patterns = owner_patterns(store, AS_OF, config)
    over = [o for o, p in patterns.items() if p.overcall_flagged]
    under = [o for o, p in patterns.items() if p.undercall_flagged]

    assert over, "no overcall owners flagged"
    assert under, "no undercall owners flagged"
    assert sum(persona[o] == "happy_ears" for o in over) > len(over) / 2
    assert sum(persona[o] == "sandbagger" for o in under) > len(under) / 2
    clean = {o for o, p in persona.items() if p == "clean_operator"}
    assert not (set(over) | set(under)) & clean


def test_brief_renders_patterns_with_disclaimer(tmp_path, config):
    rng = random.Random(42)
    org = build_org(rng)
    dates, snapshots, _ = generate_series(org, 150, 4, AS_OF, config, rng)
    store = SnapshotStore(":memory:", config)
    for d, rows in snapshots:
        _ingest(store, tmp_path, d, rows)
    markdown = brief.render(brief.build(store, AS_OF, AS_OF, config), config)
    section = markdown.split("#### Forecast integrity patterns")[1]
    assert "Coaching signal, not a comp input." in section
    assert "Suppressed as small_n" in section
    # subsection sits under the appendix H5 detail
    assert markdown.index("### Forecast integrity (H5)") \
        < markdown.index("#### Forecast integrity patterns") \
        < markdown.index("### Since last run (detail)")
