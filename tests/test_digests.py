"""Task 10: flag streaks (memory across runs) + private coaching digests.

Streaks: 3 sequential flagged runs -> streak 3; cleared then re-flagged ->
resets to 1. Digests: one file per owner, deterministic, and PRIVATE — no
other owner's name or opp ids, no rankings.
"""
import random
import re
from datetime import date, timedelta

from src import brief
from src.brief import flag_streaks, write_digests
from src.rules import OppResult, Violation
from src.seed import write_csv
from src.seed.org import build_org
from src.seed.pathologies import generate_snapshot
from src.snapshots import SnapshotStore

AS_OF = date(2026, 8, 10)


def _result(opp_id, *rules):
    return OppResult(opp_id, tuple(Violation(r, "high", "x") for r in rules),
                     ())


def test_flag_streaks_unit():
    results = {"OPP-A": _result("OPP-A", "H4")}
    # 3 sequential runs (2 recorded + current) -> streak 3
    assert flag_streaks([{"OPP-A": ["H4"]}, {"OPP-A": ["H4"]}],
                        results)[("OPP-A", "H4")] == 3
    # cleared in the immediately preceding run -> streak resets to 1
    assert flag_streaks([{"OPP-A": ["H4"]}, {"OPP-A": []}],
                        results)[("OPP-A", "H4")] == 1
    # never flagged before, no history
    assert flag_streaks([], results)[("OPP-A", "H4")] == 1


def _snapshot_csv(tmp_path, snap_date, next_step, next_step_date):
    row = {
        "opp_id": "OPP-A", "account": "Cedarrobotics Inc", "opp_name": "Deal",
        "owner": "Sage Farrow", "stage": "develop", "amount": 60_000.0,
        "currency": "USD", "created_date": date(2026, 1, 5),
        "close_date": date(2026, 12, 15), "last_activity_date": snap_date,
        "next_step": next_step, "next_step_date": next_step_date,
        "forecast_category": "pipeline", "contact_count": 3,
        "product_line": "CorePlatform",
        "stage_entered_date": snap_date - timedelta(days=3),
        "close_date_changes": 0,
    }
    path = tmp_path / f"opps_{snap_date.isoformat()}.csv"
    write_csv(path, [row])
    return path


def test_streaks_through_recorded_runs(tmp_path, config):
    """H4 flagged 3 runs, cleared on the 4th, re-flagged on the 5th."""
    store = SnapshotStore(":memory:", config)
    dates = [AS_OF + timedelta(days=7 * i) for i in range(5)]
    plan = [("", None), ("", None), ("", None),
            ("Send signed order form to procurement", None), ("", None)]
    # runs 1-3: H4 (empty next step); run 4: clean; run 5: H4 again
    plan[3] = ("Send signed order form to procurement",
               dates[3] + timedelta(days=10))
    datas = []
    for d, (step, step_date) in zip(dates, plan):
        csv_path = _snapshot_csv(tmp_path, d, step, step_date)
        assert store.ingest_csv(csv_path, d).rejected == 0
        _, data = brief.run(store, d, d, config, tmp_path / "out")
        datas.append(data)
    assert [d["streaks"].get(("OPP-A", "H4")) for d in datas] \
        == [1, 2, 3, None, 1]
    # annotation appears at streak 3, not on a fresh re-flag after a clear
    run3 = brief.render(datas[2], config).split("### Top 10 exceptions")[1] \
                                         .split("### ")[0]
    assert "flagged 3 runs" in run3
    run5 = brief.render(datas[4], config).split("### Top 10 exceptions")[1] \
                                         .split("### ")[0]
    assert "flagged" not in run5


def _seeded_data(tmp_path, config):
    rng = random.Random(42)
    org = build_org(rng)
    rows, _ = generate_snapshot(org, 400, AS_OF, config, rng)
    csv_path = tmp_path / f"opps_{AS_OF.isoformat()}.csv"
    write_csv(csv_path, rows)
    cfg = dict(config)
    cfg["quotas"] = {s.name: s.quota for s in org.sellers}
    store = SnapshotStore(":memory:", cfg)
    assert store.ingest_csv(csv_path, AS_OF).rejected == 0
    return brief.build(store, AS_OF, AS_OF, cfg), cfg


def test_digests_private_per_owner_and_deterministic(tmp_path, config):
    data, cfg = _seeded_data(tmp_path, config)
    paths = write_digests(data, cfg, tmp_path / "out")
    owners = list(data["owners"])
    assert len(paths) == len(owners)

    opp_owner = {r["opp_id"]: r["owner"] for r in data["rows"]}
    for owner, path in zip(owners, paths):
        content = path.read_text(encoding="utf-8")
        assert content.startswith(f"# Coaching digest — {owner}")
        assert "Private" in content
        assert "## Suggested coaching focus" in content
        # PRIVATE: no other owner's name, no other owner's opps
        for other in owners:
            if other != owner:
                assert other not in content
        for opp_id in set(re.findall(r"OPP-\d+", content)):
            assert opp_owner[opp_id] == owner
        if data["owners"][owner].small_n:
            assert "small_n" in content

    # at least one small_n owner exercises the carry-over
    assert any(data["owners"][o].small_n for o in owners)

    # deterministic: same data -> byte-identical files
    again = write_digests(data, cfg, tmp_path / "out2")
    for a, b in zip(paths, again):
        assert a.read_bytes() == b.read_bytes()


def test_digest_top_risks_capped_and_dollar_ranked(tmp_path, config):
    data, cfg = _seeded_data(tmp_path, config)
    results = data["results"]
    owner = max(
        data["owners"],
        key=lambda o: sum(1 for r in data["rows"]
                          if r["owner"] == o and r["opp_id"] in results
                          and results[r["opp_id"]].violations))
    content = brief.digest_markdown(data, owner, cfg)
    section = content.split("## Top risks (dollar-weighted)")[1] \
                     .split("## ")[0]
    # Primary list is capped at 5; "Also flagged" carries the rest so the
    # digest enumerates EVERY flagged deal (persona fix — a capped list hid
    # deals the desk brief still counted against the owner).
    primary, _, overflow = section.partition("Also flagged")
    primary_risks = [l for l in primary.splitlines() if l.startswith("- ")]
    assert 0 < len(primary_risks) <= 5
    all_risks = [l for l in section.splitlines()
                 if l.startswith("- OPP-")]
    n_flagged = sum(1 for r in data["rows"]
                    if r["owner"] == owner and r["opp_id"] in results
                    and results[r["opp_id"]].violations
                    and r["stage"] not in ("closed_won", "closed_lost"))
    assert len(all_risks) == n_flagged
    amounts = [float(re.search(r"\$([\d,]+)", l).group(1).replace(",", ""))
               for l in all_risks]
    assert amounts == sorted(amounts, reverse=True)
