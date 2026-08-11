"""Single snapshot without optional columns: H3/H6 report insufficient_history
for every open opp — never counted as violations, never silently skipped."""
import csv
import random
from datetime import date

from src.rules import evaluate_snapshot
from src.seed.org import build_org
from src.seed.pathologies import CSV_COLUMNS, generate_snapshot
from src.snapshots import SnapshotStore

AS_OF = date(2026, 8, 10)
OPTIONAL = ("stage_entered_date", "close_date_changes")


def test_single_snapshot_without_optional_columns(tmp_path, config):
    rng = random.Random(42)
    org = build_org(rng)
    rows, _ = generate_snapshot(org, 120, AS_OF, config, rng)

    cols = [c for c in CSV_COLUMNS if c not in OPTIONAL]
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
    report = store.ingest_csv(csv_path, AS_OF)
    assert report.rejected == 0

    loaded = store.rows_with_history(AS_OF)
    results = evaluate_snapshot(loaded, config, AS_OF)
    assert results, "no open opps in fixture"
    for result in results.values():
        assert sorted(i.rule_id for i in result.insufficient) == ["H3", "H6"]
        assert not any(v.rule_id in ("H3", "H6") for v in result.violations)
    # other rules still evaluate normally on the same pass
    assert any(v.rule_id not in ("H3", "H6")
               for r in results.values() for v in r.violations)
