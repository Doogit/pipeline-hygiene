"""Task 5 tests: fiscal quarters, golden-file brief, series since-last-run
deltas vs the delta manifest, insufficient-history and validation surfacing.

The golden file is created on first run if absent (test fails asking for
review); delete tests/golden/desk_brief_golden.md and rerun to regenerate.
"""
import csv
import random
from datetime import date
from pathlib import Path

import pytest

from src import brief
from src.brief import fiscal_quarter
from src.ingest import REQUIRED_COLUMNS
from src.seed import write_csv
from src.seed.org import build_org
from src.seed.pathologies import CSV_COLUMNS, generate_snapshot
from src.seed.series import generate_series
from src.snapshots import SnapshotStore

AS_OF = date(2026, 8, 10)
ROWS = 400
GOLDEN = Path(__file__).parent / "golden" / "desk_brief_golden.md"


def test_fiscal_quarter_labels():
    # fy_start_month 7: FY named for the calendar year it ends in
    assert fiscal_quarter(date(2026, 8, 10), 7) == "FY2027-Q1"
    assert fiscal_quarter(date(2026, 7, 1), 7) == "FY2027-Q1"
    assert fiscal_quarter(date(2026, 6, 30), 7) == "FY2026-Q4"
    assert fiscal_quarter(date(2026, 12, 31), 7) == "FY2027-Q2"
    assert fiscal_quarter(date(2027, 1, 1), 7) == "FY2027-Q3"
    # fy_start_month 1: calendar year
    assert fiscal_quarter(date(2026, 2, 15), 1) == "FY2026-Q1"
    assert fiscal_quarter(date(2026, 12, 1), 1) == "FY2026-Q4"


def _seeded_store(tmp_path, config):
    """Seed CSV (same rng sequence as the CLI) ingested into a fresh store."""
    rng = random.Random(42)
    org = build_org(rng)
    rows, expected = generate_snapshot(org, ROWS, AS_OF, config, rng)
    csv_path = tmp_path / f"opps_{AS_OF.isoformat()}.csv"
    write_csv(csv_path, rows)
    cfg = dict(config)
    cfg["quotas"] = {s.name: s.quota for s in org.sellers}
    cfg["owner_meta"] = {s.name: {"team": s.team, "region": s.region}
                         for s in org.sellers}
    store = SnapshotStore(":memory:", cfg)
    report = store.ingest_csv(csv_path, AS_OF)
    assert report.rejected == 0
    return store, cfg, expected


def test_golden_brief(tmp_path, config):
    store, cfg, _ = _seeded_store(tmp_path, config)
    path, _ = brief.run(store, AS_OF, AS_OF, cfg, tmp_path / "out")
    generated = path.read_text(encoding="utf-8")
    for section in ("# Desk Brief", "## Headline", "### Validation",
                    "## Since last run", "## Fiscal quarters",
                    "## Top 10 exceptions", "## Owners",
                    "### Teams and regions", "### Teams", "### Regions",
                    "## Forecast integrity (H5)"):
        assert section in generated
    if not GOLDEN.exists():
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(generated, encoding="utf-8", newline="\n")
        pytest.fail(f"golden file created at {GOLDEN}; review and commit it")
    assert generated == GOLDEN.read_text(encoding="utf-8")


def test_series_since_last_run_matches_delta_manifest(tmp_path, config):
    rng = random.Random(42)
    org = build_org(rng)
    dates, snapshots, manifest = generate_series(org, ROWS, 4, AS_OF, config, rng)
    cfg = dict(config)
    cfg["quotas"] = {s.name: s.quota for s in org.sellers}
    store = SnapshotStore(":memory:", cfg)
    for d, rows in snapshots:
        csv_path = tmp_path / f"opps_{d.isoformat()}.csv"
        write_csv(csv_path, rows)
        assert store.ingest_csv(csv_path, d).rejected == 0

    open_by_date = {d: {r["opp_id"] for r in rows
                        if r["stage"] not in ("closed_won", "closed_lost")}
                    for d, rows in snapshots}
    for i, d in enumerate(dates):
        _, data = brief.run(store, d, d, cfg, tmp_path / "out")

        # engine <-> series-manifest consistency at every snapshot
        expected = manifest["expected_by_snapshot"][d.isoformat()]
        engine = data["summary"]["open"]
        assert set(engine) == open_by_date[d]
        assert engine == {o: expected[o] for o in engine}

        delta = data["since_last_run"]
        if i == 0:
            assert delta is None
            continue
        scripted = manifest["deltas"][i - 1]
        assert delta["new_violations"] == \
            {o: sorted(v) for o, v in scripted["introduced"].items()}
        assert delta["cleared_violations"] == \
            {o: sorted(v) for o, v in scripted["cleared"].items()}
        assert delta["closed"] == scripted["closed"]
        # created cohort: the run diff's "added" list is oracled by the
        # delta manifest's field-by-field "added" record
        assert delta["added"] == sorted(scripted["added"])
        assert delta["removed"] == []
        assert delta["prev_snapshot_date"] == dates[i - 1].isoformat()


def test_brief_reports_insufficient_history(tmp_path, config):
    rng = random.Random(42)
    org = build_org(rng)
    rows, _ = generate_snapshot(org, 120, AS_OF, config, rng)
    cols = [c for c in CSV_COLUMNS
            if c not in ("stage_entered_date", "close_date_changes")]
    csv_path = tmp_path / f"opps_{AS_OF.isoformat()}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(cols)
        for row in rows:
            writer.writerow([
                row[c].isoformat() if hasattr(row[c], "isoformat")
                else ("" if row[c] is None else
                      f"{row[c]:.2f}" if c == "amount" else str(row[c]))
                for c in cols])
    store = SnapshotStore(":memory:", config)
    store.ingest_csv(csv_path, AS_OF)

    data = brief.build(store, AS_OF, AS_OF, config)
    n_open = len(data["results"])
    assert data["insufficient"] == {"H3": n_open, "H6": n_open}
    markdown = brief.render(data, config)
    assert (f"- Insufficient history: H3 on {n_open} opps, H6 on {n_open} opps "
            f"(reported, not counted as violations)") in markdown


def test_quota_owner_mismatch_warning(tmp_path, config):
    store, cfg, _ = _seeded_store(tmp_path, config)
    # matched sets: no warning
    data = brief.build(store, AS_OF, AS_OF, cfg)
    assert data["owner_mismatch"] is None
    assert "Quota/owner mismatch" not in brief.render(data, cfg)
    # a typo'd quota owner and a snapshot owner with no quota both surface
    bad = dict(cfg)
    quotas = dict(cfg["quotas"])
    dropped = sorted(quotas)[0]
    del quotas[dropped]
    quotas["Nonexistent Seller"] = 500000
    bad["quotas"] = quotas
    markdown = brief.render(brief.build(store, AS_OF, AS_OF, bad), bad)
    assert ("- Warning: Quota/owner mismatch — 1 quota owner(s) not in this "
            "snapshot: Nonexistent Seller; 1 snapshot owner(s) without a "
            "quota: " + dropped) in markdown


def test_brief_surfaces_validation_rejects(tmp_path, config):
    good = {
        "opp_id": "OPP-A", "account": "Acme Synthetic", "opp_name": "Deal A",
        "owner": "Avery Calloway", "stage": "prospect", "amount": "1000",
        "currency": "USD", "created_date": "2026-01-01",
        "close_date": "2026-09-01", "last_activity_date": "2026-08-01",
        "next_step": "Send contract to procurement",
        "next_step_date": "2026-08-15", "forecast_category": "pipeline",
        "contact_count": "2", "product_line": "widgets",
    }
    bad_stage = dict(good, opp_id="OPP-B", stage="weird")
    duplicate = dict(good)
    csv_path = tmp_path / f"opps_{AS_OF.isoformat()}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=REQUIRED_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows([good, bad_stage, duplicate])
    store = SnapshotStore(":memory:", config)
    store.ingest_csv(csv_path, AS_OF)

    data = brief.build(store, AS_OF, AS_OF, config)
    markdown = brief.render(data, config)
    assert f"- Source `{csv_path.name}`: accepted 1/3 rows, rejected 2" in markdown
    assert "unknown stage" in markdown
    assert "duplicate opp_id" in markdown
