"""Session 6: forecast-accuracy commit ledger (patterns.commit_ledger).

Outcomes are mutually exclusive (won / lost / pushed / open) so shares sum
to 100%, and the committed-for quarter anchors to the close date at the
FIRST commit snapshot — a later push can never move a commitment between
quarters. Plant = explicit field histories over hand-built snapshots;
detect = the ledger walk over stored history.
"""
import random
from datetime import date, timedelta

from src import brief
from src.patterns import CommitLedgerEntry, commit_ledger, ledger_rollups
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


def test_outcome_classification(tmp_path, config):
    d1, d2 = AS_OF - timedelta(days=7), AS_OF
    store = SnapshotStore(":memory:", config)
    _ingest(store, tmp_path, d1, [
        _row("OPP-WON", "Avery Farrow", d1, forecast_category="commit"),
        _row("OPP-LOST", "Avery Farrow", d1, forecast_category="commit"),
        _row("OPP-PUSH", "Avery Farrow", d1, forecast_category="commit"),
        _row("OPP-OPEN", "Avery Farrow", d1, forecast_category="commit"),
        _row("OPP-NEVER", "Avery Farrow", d1),
    ])
    _ingest(store, tmp_path, d2, [
        _row("OPP-WON", "Avery Farrow", d2, stage="closed_won",
             forecast_category="commit"),
        _row("OPP-LOST", "Avery Farrow", d2, stage="closed_lost",
             forecast_category="commit"),
        _row("OPP-PUSH", "Avery Farrow", d2, forecast_category="commit",
             close_date=date(2026, 10, 15), close_date_changes=1),
        _row("OPP-OPEN", "Avery Farrow", d2, forecast_category="commit"),
        _row("OPP-NEVER", "Avery Farrow", d2),
    ])
    entries = {e.opp_id: e for e in commit_ledger(store, AS_OF, config)}
    assert set(entries) == {"OPP-WON", "OPP-LOST", "OPP-PUSH", "OPP-OPEN"}
    assert entries["OPP-WON"].outcome == "won"
    assert entries["OPP-LOST"].outcome == "lost"
    assert entries["OPP-PUSH"].outcome == "pushed"
    assert entries["OPP-OPEN"].outcome == "open"
    assert all(e.first_commit_snapshot == d1 for e in entries.values())


def test_push_before_first_commit_does_not_count(tmp_path, config):
    # close date moves between d1 and d2, but commit only appears AT d2:
    # no post-commit pair has moved, so the opp is open, not pushed.
    d1, d2 = AS_OF - timedelta(days=7), AS_OF
    store = SnapshotStore(":memory:", config)
    _ingest(store, tmp_path, d1, [_row("OPP-A", "Blair Farrow", d1)])
    _ingest(store, tmp_path, d2, [
        _row("OPP-A", "Blair Farrow", d2, forecast_category="commit",
             close_date=date(2026, 10, 15), close_date_changes=1)])
    entry = commit_ledger(store, AS_OF, config)[0]
    assert entry.outcome == "open"
    assert entry.first_commit_snapshot == d2


def test_committed_quarter_anchors_to_first_commit(tmp_path, config):
    # committed for 2026-09-15 (FY2027-Q1), then pushed into Q2: the ledger
    # keeps the commitment in the quarter it was promised for.
    d1, d2 = AS_OF - timedelta(days=7), AS_OF
    store = SnapshotStore(":memory:", config)
    _ingest(store, tmp_path, d1, [
        _row("OPP-A", "Casey Farrow", d1, forecast_category="commit",
             close_date=date(2026, 9, 15))])
    _ingest(store, tmp_path, d2, [
        _row("OPP-A", "Casey Farrow", d2, forecast_category="commit",
             close_date=date(2026, 12, 15), close_date_changes=1)])
    entry = commit_ledger(store, AS_OF, config)[0]
    assert entry.committed_quarter == "FY2027-Q1"
    assert entry.outcome == "pushed"


def test_ledger_anchors_to_evaluated_snapshot_not_later_history(tmp_path,
                                                                config):
    # evaluated at d1, the d2 loss must not leak into the ledger
    d1, d2 = AS_OF - timedelta(days=7), AS_OF
    store = SnapshotStore(":memory:", config)
    _ingest(store, tmp_path, d1, [
        _row("OPP-A", "Drew Farrow", d1, forecast_category="commit")])
    _ingest(store, tmp_path, d2, [
        _row("OPP-A", "Drew Farrow", d2, stage="closed_lost",
             forecast_category="commit")])
    assert commit_ledger(store, d2, config,
                         snapshot_date=d1)[0].outcome == "open"
    assert commit_ledger(store, d2, config)[0].outcome == "lost"


def test_ledger_rollups_counts_and_none_key():
    def entry(opp_id, owner, quarter, outcome, pushed_first=False):
        return CommitLedgerEntry(opp_id=opp_id, owner=owner,
                                 first_commit_snapshot=AS_OF,
                                 committed_quarter=quarter, outcome=outcome,
                                 pushed_before_close=pushed_first)
    entries = [
        entry("O1", "A", "FY2027-Q1", "won"),
        entry("O2", "A", "FY2027-Q1", "lost", pushed_first=True),
        entry("O3", "A", "FY2027-Q2", "pushed", pushed_first=True),
        entry("O4", "B", None, "open"),
    ]
    by_owner = ledger_rollups(entries, lambda e: e.owner)
    # O3 is still open: its push counts under the pushed outcome, never
    # under resolved_pushed_first
    assert by_owner["A"] == {"n": 3, "won": 1, "lost": 1, "pushed": 1,
                            "open": 0, "resolved_pushed_first": 1}
    assert by_owner["B"] == {"n": 1, "won": 0, "lost": 0, "pushed": 0,
                            "open": 1, "resolved_pushed_first": 0}
    by_quarter = ledger_rollups(entries, lambda e: e.committed_quarter)
    assert by_quarter[None]["open"] == 1
    assert by_quarter["FY2027-Q1"]["n"] == 2


def test_pushed_before_close(tmp_path, config):
    """A commit that pushes and then resolves is won/lost (outcomes stay
    mutually exclusive) with pushed_before_close carrying the depth; a
    clean resolve carries False."""
    d1, d2, d3 = (AS_OF - timedelta(days=14), AS_OF - timedelta(days=7),
                  AS_OF)
    store = SnapshotStore(":memory:", config)
    _ingest(store, tmp_path, d1, [
        _row("OPP-PL", "Ellis Farrow", d1, forecast_category="commit"),
        _row("OPP-CW", "Ellis Farrow", d1, forecast_category="commit"),
    ])
    _ingest(store, tmp_path, d2, [
        _row("OPP-PL", "Ellis Farrow", d2, forecast_category="commit",
             close_date=date(2026, 10, 15), close_date_changes=1),
        _row("OPP-CW", "Ellis Farrow", d2, forecast_category="commit"),
    ])
    _ingest(store, tmp_path, d3, [
        _row("OPP-PL", "Ellis Farrow", d3, stage="closed_lost",
             forecast_category="commit", close_date=date(2026, 10, 15),
             close_date_changes=1),
        _row("OPP-CW", "Ellis Farrow", d3, stage="closed_won",
             forecast_category="commit"),
    ])
    entries = {e.opp_id: e for e in commit_ledger(store, AS_OF, config)}
    assert entries["OPP-PL"].outcome == "lost"
    assert entries["OPP-PL"].pushed_before_close is True
    assert entries["OPP-CW"].outcome == "won"
    assert entries["OPP-CW"].pushed_before_close is False
    rollup = ledger_rollups(entries.values(), lambda e: e.owner)
    assert rollup["Ellis Farrow"]["resolved_pushed_first"] == 1


def test_ledger_rate_small_n_guard():
    # fraction always; percentage only at min_n resolved (a bare 100% on
    # n=1 is a leaderboard cell, not a coaching signal)
    assert brief.ledger_rate({"won": 0, "lost": 0}, 5) == "n/a"
    assert brief.ledger_rate({"won": 1, "lost": 1}, 5) == "1/2"
    assert brief.ledger_rate({"won": 1, "lost": 1}, 2) == "1/2 (50%)"
    assert brief.ledger_rate({"won": 3, "lost": 1}, 4) == "3/4 (75%)"


def test_digest_carries_own_ledger_row(tmp_path, config):
    d1, d2 = AS_OF - timedelta(days=7), AS_OF
    store = SnapshotStore(":memory:", config)
    _ingest(store, tmp_path, d1, [
        _row("OPP-W", "Avery Farrow", d1, forecast_category="commit"),
        _row("OPP-O", "Avery Farrow", d1, forecast_category="commit"),
        _row("OPP-N", "Blair Farrow", d1)])
    _ingest(store, tmp_path, d2, [
        _row("OPP-W", "Avery Farrow", d2, stage="closed_won",
             forecast_category="commit"),
        _row("OPP-O", "Avery Farrow", d2, forecast_category="commit"),
        _row("OPP-N", "Blair Farrow", d2)])
    data = brief.build(store, AS_OF, AS_OF, config)
    avery = brief.digest_markdown(data, "Avery Farrow", config)
    assert "## Your commit accuracy" in avery
    assert ("Of your 2 ever-commit deal(s): 1 won, 0 lost, 0 pushed, "
            "1 still open." in avery)
    blair = brief.digest_markdown(data, "Blair Farrow", config)
    assert ("None of your deals has been forecast commit in stored "
            "history." in blair)


def test_brief_renders_ledger_with_disclaimer(tmp_path, config):
    rng = random.Random(42)
    org = build_org(rng)
    dates, snapshots, _ = generate_series(org, 150, 4, AS_OF, config, rng)
    store = SnapshotStore(":memory:", config)
    for d, rows in snapshots:
        _ingest(store, tmp_path, d, rows)
    cfg = brief.merge_quota_payload(config, {
        "quotas": {s.name: s.quota for s in org.sellers},
        "owners": {s.name: {"team": s.team, "region": s.region}
                   for s in org.sellers}})
    markdown = brief.render(brief.build(store, AS_OF, AS_OF, cfg), cfg)
    section = markdown.split("### Commit accuracy (forecast ledger)")[1]
    assert "Coaching signal, not a comp input." in section
    for label in ("| Owner |", "| Team |", "| Region |",
                  "| Committed-for quarter |"):
        assert label in section
    # sits in the appendix between the patterns and the since-last-run detail
    assert markdown.index("#### Forecast integrity patterns") \
        < markdown.index("### Commit accuracy (forecast ledger)") \
        < markdown.index("### Since last run (detail)")


def test_ledger_unavailable_outside_store(config):
    rows = [_row("OPP-A", "Avery Farrow", AS_OF)]
    data = brief.build_from_rows(rows, AS_OF, AS_OF, config)
    markdown = brief.render(data, config)
    section = markdown.split("### Commit accuracy (forecast ledger)")[1]
    assert "Unavailable outside the snapshot store." in section
