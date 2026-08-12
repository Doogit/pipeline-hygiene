"""Task 8: push derivations, rule H11 boundaries, series/seed H11 consistency.

Push derivations are history-only (no CSV column): pull-ins and no-change
pairs never count, frequent small pushes never trip H11 (Gong: movement per
se is not the risk signal), and a single-snapshot store provably yields no
H11 anywhere.
"""
import random
from datetime import date

from src import brief
from src.rules import evaluate_snapshot, h11_lost_deal_control
from src.seed import write_csv
from src.seed.org import build_org
from src.seed.pathologies import generate_snapshot
from src.seed.series import generate_series
from src.snapshots import SnapshotStore

AS_OF = date(2026, 8, 10)


def _mk_row(opp_id, close_date, **over):
    row = {
        "opp_id": opp_id, "account": "Bluequartz Labs", "opp_name": "Deal",
        "owner": "Avery Calloway", "stage": "develop", "amount": 50_000.0,
        "currency": "USD", "created_date": date(2026, 1, 5),
        "close_date": close_date, "last_activity_date": AS_OF,
        "next_step": "Send updated proposal to procurement",
        "next_step_date": date(2026, 12, 1), "forecast_category": "pipeline",
        "contact_count": 3, "product_line": "CorePlatform",
        "stage_entered_date": date(2026, 8, 1), "close_date_changes": 0,
    }
    row.update(over)
    return row


def _store_with_close_dates(tmp_path, config, closes_by_snapshot):
    """closes_by_snapshot: list of (snapshot_date, {opp_id: close_date})."""
    store = SnapshotStore(":memory:", config)
    for snap_date, closes in closes_by_snapshot:
        rows = [_mk_row(o, c, last_activity_date=snap_date,
                        next_step_date=date(2027, 6, 1))
                for o, c in sorted(closes.items())]
        csv_path = tmp_path / f"opps_{snap_date.isoformat()}.csv"
        write_csv(csv_path, rows)
        assert store.ingest_csv(csv_path, snap_date).rejected == 0
    return store


def test_push_stats_counts_later_moves_only(tmp_path, config):
    dates = [date(2026, 7, 20), date(2026, 7, 27), date(2026, 8, 3), AS_OF]
    closes = [
        # OPP-A: push 21, pull-in (excluded), push 5 -> count 2, cum 26, max 21
        {"OPP-A": date(2026, 9, 1), "OPP-B": date(2026, 10, 1)},
        {"OPP-A": date(2026, 9, 22), "OPP-B": date(2026, 10, 1)},
        {"OPP-A": date(2026, 9, 15), "OPP-B": date(2026, 10, 1)},
        {"OPP-A": date(2026, 9, 20), "OPP-B": date(2026, 10, 1)},
    ]
    store = _store_with_close_dates(tmp_path, config,
                                    list(zip(dates, closes)))
    stats = store.push_stats(AS_OF)
    assert stats["OPP-A"] == {"push_count": 2, "cumulative_extension_days": 26,
                              "max_push_days": 21}
    # OPP-B never moved: zeros, and pull-ins alone can never create pushes
    assert stats["OPP-B"] == {"push_count": 0, "cumulative_extension_days": 0,
                              "max_push_days": 0}
    # evaluated at an earlier snapshot, only history <= that date counts
    assert store.push_stats(dates[1])["OPP-A"]["push_count"] == 1


def test_h11_boundaries(config):
    def h11(push_count, cum, max_push):
        return h11_lost_deal_control(
            {"push_count": push_count, "cumulative_extension_days": cum,
             "max_push_days": max_push}, config, AS_OF)

    assert h11(1, 21, 21).severity == "high"          # exactly at single alarm
    assert h11(1, 20, 20) is None                     # one day under
    assert h11(2, 45, 23) is not None                 # exactly at cumulative
    assert h11(2, 44, 20) is None                     # both just under
    assert h11(5, 35, 7) is None                      # many small pushes
    assert h11(6, 45, 8) is not None                  # small pushes, big drift
    assert h11(0, 0, 0) is None
    # rows without push derivations (outside the store) stay silent
    assert h11_lost_deal_control({}, config, AS_OF) is None


def test_single_seed_has_no_h11(tmp_path, config):
    """History-only rule: a single snapshot provably yields no H11 —
    generator side (manifest) and engine side (store evaluation)."""
    rng = random.Random(42)
    org = build_org(rng)
    rows, expected = generate_snapshot(org, 400, AS_OF, config, rng)
    assert all("H11" not in rules for rules in expected.values())

    csv_path = tmp_path / f"opps_{AS_OF.isoformat()}.csv"
    write_csv(csv_path, rows)
    store = SnapshotStore(":memory:", config)
    assert store.ingest_csv(csv_path, AS_OF).rejected == 0
    stored = store.rows_with_history(AS_OF)
    assert all(r["push_count"] == 0 for r in stored)
    results = evaluate_snapshot(stored, config, AS_OF)
    assert all("H11" not in r.rule_ids() for r in results.values())


def test_series_scripts_h11_and_brief_shows_it(tmp_path, config):
    rng = random.Random(42)
    org = build_org(rng)
    dates, snapshots, manifest = generate_series(org, 150, 4, AS_OF, config, rng)

    introduced_h11 = {o for delta in manifest["deltas"]
                      for o, rules in delta["introduced"].items()
                      if "H11" in rules}
    assert introduced_h11, "series never scripted a big push"
    # a big push is recorded as a push and its size clears the alarm
    for delta in manifest["deltas"]:
        for opp_id, rules in delta["introduced"].items():
            if "H11" in rules:
                rec = delta["close_dates_pushed"][opp_id]
                pushed = (date.fromisoformat(rec["new"])
                          - date.fromisoformat(rec["old"])).days
                assert pushed >= config["push_alarm_days"]

    cfg = dict(config)
    cfg["quotas"] = {s.name: s.quota for s in org.sellers}
    store = SnapshotStore(":memory:", cfg)
    for d, rows in snapshots:
        csv_path = tmp_path / f"opps_{d.isoformat()}.csv"
        write_csv(csv_path, rows)
        assert store.ingest_csv(csv_path, d).rejected == 0

    data = brief.build(store, AS_OF, AS_OF, cfg)
    markdown = brief.render(data, cfg)
    still_open = {o for o in introduced_h11
                  if o in data["results"]}  # some may have closed later
    assert still_open, "every big-pushed opp closed before the last snapshot"
    for opp_id in still_open:
        assert "H11" in data["results"][opp_id].rule_ids()
        assert opp_id in markdown.split("## Slipping pipeline")[1] \
                                 .split("## ")[0]


def test_slipping_section_disqualify_marker(tmp_path, config):
    """push_count >= disqualify_review_pushes marks the row even when the
    pushes are individually and cumulatively below the H11 alarms."""
    d = [date(2026, 7, 20), date(2026, 7, 27), date(2026, 8, 3), AS_OF]
    base = date(2026, 9, 1)
    closes = [{"OPP-A": base},
              {"OPP-A": date(2026, 9, 8)},
              {"OPP-A": date(2026, 9, 15)},
              {"OPP-A": date(2026, 9, 22)}]
    store = _store_with_close_dates(tmp_path, config, list(zip(d, closes)))
    data = brief.build(store, AS_OF, AS_OF, config)
    assert data["results"]["OPP-A"].rule_ids() == []   # 3x7d: no H11, no H3(<2? source col 0)
    markdown = brief.render(data, config)
    section = markdown.split("## Slipping pipeline")[1].split("## ")[0]
    assert "| OPP-A |" in section
    assert "review close plan" in section
