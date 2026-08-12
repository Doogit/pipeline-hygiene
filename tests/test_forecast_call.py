"""Task 9: forecast-call prep page-1 structure, risky commits, trajectory.

Page 1 = headline, risky commits (<=10, coaching prompts), trajectory
(coverage basis always printed), since-last-run summary, slipping pipeline.
Detail sections live under the Appendix header. Coaching prompts are a fixed
deterministic string table — questions to ask the seller, never gotchas.
"""
import random
from datetime import date

from src import brief
from src.brief import COACHING_PROMPTS, RISKY_RULES, risky_commits
from src.rules import evaluate_snapshot
from src.seed import write_csv
from src.seed.org import build_org
from src.seed.pathologies import generate_snapshot
from src.seed.series import generate_series
from src.snapshots import SnapshotStore

AS_OF = date(2026, 8, 10)


def _row(opp_id, **over):
    row = {
        "opp_id": opp_id, "account": "Ironlabs Corp", "opp_name": "Deal",
        "owner": "Rowan Pemberton", "stage": "propose", "amount": 40_000.0,
        "currency": "USD", "created_date": date(2026, 1, 5),
        "close_date": date(2026, 9, 30), "last_activity_date": AS_OF,
        "next_step": "Send updated proposal to procurement",
        "next_step_date": date(2026, 9, 1), "forecast_category": "pipeline",
        "contact_count": 3, "product_line": "CorePlatform",
        "stage_entered_date": date(2026, 8, 1), "close_date_changes": 0,
    }
    row.update(over)
    return row


def test_prompt_table_covers_risky_rules_with_questions():
    assert set(COACHING_PROMPTS) == set(RISKY_RULES)
    for prompt in COACHING_PROMPTS.values():
        assert prompt.endswith("?")


def test_risky_commits_selection_and_dominant_rule(config):
    rows = [
        # commit + H2 (high, w20) + H7 (medium) -> included, dominant H2
        _row("OPP-0001", forecast_category="commit",
             close_date=date(2026, 8, 1), amount=200_000.0, contact_count=1),
        # best_case + H4 -> included
        _row("OPP-0002", forecast_category="best_case", next_step="",
             next_step_date=None, amount=90_000.0),
        # pipeline + H2 -> excluded (not commit/best_case)
        _row("OPP-0003", close_date=date(2026, 8, 1)),
        # clean commit -> excluded
        _row("OPP-0004", forecast_category="commit"),
        # commit + only H9 (not a risky rule) -> excluded
        _row("OPP-0005", forecast_category="commit",
             next_step="Waiting on customer response timeline"),
    ]
    results = evaluate_snapshot(rows, config, AS_OF)
    entries = risky_commits(rows, results, config)
    assert [e["row"]["opp_id"] for e in entries] == ["OPP-0001", "OPP-0002"]
    assert entries[0]["dominant"] == "H2"
    assert entries[0]["prompt"] == COACHING_PROMPTS["H2"]
    assert entries[0]["risky_rules"] == ["H2", "H7"]


def test_page_one_order_and_appendix(tmp_path, config):
    rng = random.Random(42)
    org = build_org(rng)
    rows, _ = generate_snapshot(org, 400, AS_OF, config, rng)
    csv_path = tmp_path / f"opps_{AS_OF.isoformat()}.csv"
    write_csv(csv_path, rows)
    cfg = dict(config)
    cfg["quotas"] = {s.name: s.quota for s in org.sellers}
    store = SnapshotStore(":memory:", cfg)
    assert store.ingest_csv(csv_path, AS_OF).rejected == 0

    markdown = brief.render(brief.build(store, AS_OF, AS_OF, cfg), cfg)
    order = ["## Headline", "## Risky commits", "## Trajectory",
             "## Since last run", "## Slipping pipeline", "## Appendix",
             "### Fiscal quarters", "### Top 10 exceptions", "### Owners",
             "### Forecast integrity (H5)", "### Since last run (detail)"]
    positions = [markdown.index(h) for h in order]
    assert positions == sorted(positions)

    # risky commits: capped at 10 rows, all commit/best_case
    section = markdown.split("## Risky commits")[1].split("## ")[0]
    table_rows = [l for l in section.splitlines() if l.startswith("| ")][1:]
    assert 0 < len(table_rows) <= 10
    assert all(("| commit |" in l) or ("| best_case |" in l)
               for l in table_rows)
    # every listed opp gets a coaching prompt from the fixed table
    assert all(any(p in l for p in COACHING_PROMPTS.values())
               for l in table_rows)
    # coverage basis is always printed
    assert "- Basis: " in markdown


def test_trajectory_fallback_basis_without_closed_history(config):
    cfg = dict(config)
    cfg["quotas"] = {"Rowan Pemberton": 500_000.0}
    rows = [_row("OPP-0001")]
    data = brief.build_from_rows(rows, AS_OF, AS_OF, cfg)
    coverage = data["trajectory"]["coverage"]
    assert "coverage_ratio_min 3.0x" in coverage["basis"]
    assert "insufficient" in coverage["basis"]
    markdown = brief.render(data, cfg)
    assert "- Basis: config coverage_ratio_min 3.0x" in markdown


def test_trajectory_win_rate_basis_and_flow(tmp_path, config):
    rng = random.Random(42)
    org = build_org(rng)
    dates, snapshots, manifest = generate_series(org, 150, 4, AS_OF, config,
                                                 rng)
    cfg = dict(config)
    cfg["quotas"] = {s.name: s.quota for s in org.sellers}
    store = SnapshotStore(":memory:", cfg)
    for d, rows in snapshots:
        csv_path = tmp_path / f"opps_{d.isoformat()}.csv"
        write_csv(csv_path, rows)
        assert store.ingest_csv(csv_path, d).rejected == 0

    outcomes = store.closed_outcomes(AS_OF)
    assert len(outcomes) >= cfg["min_closed_for_win_rate"]

    brief.run(store, dates[-2], dates[-2], cfg, tmp_path / "out")
    data = brief.build(store, AS_OF, AS_OF, cfg)
    coverage = data["trajectory"]["coverage"]
    assert coverage["basis"].startswith("trailing win rate")
    won = sum(1 for o in outcomes if o["stage"] == "closed_won")
    assert abs(coverage["required_multiple"] - len(outcomes) / won) < 1e-9

    # flow mirrors the scripted delta between the last two snapshots
    scripted = manifest["deltas"][-1]
    flow = data["trajectory"]["flow"]
    assert flow["created_n"] == len(scripted["added"])
    n_won = sum(1 for i in scripted["closed"].values()
                if i["stage"] == "closed_won")
    n_lost = len(scripted["closed"]) - n_won
    assert (flow["won_n"], flow["lost_n"]) == (n_won, n_lost)
