"""Engine <-> generator consistency at scale.

The seed manifest's expected sets were constructed field-by-field by the
generator (never by running the engine), so agreement here is a non-circular
proof that the engine detects exactly what was planted. Per-rule counts are
printed side by side (run with -s to see them on success).
"""
import random
from datetime import date

from src.ingest import validate_csv
from src.rules import evaluate_snapshot
from src.seed import write_csv
from src.seed.org import build_org
from src.seed.pathologies import ALL_RULES, generate_snapshot, rule_counts

AS_OF = date(2026, 8, 10)
ROWS = 400


def test_manifest_consistency(tmp_path, config):
    rng = random.Random(42)
    org = build_org(rng)
    generated, expected = generate_snapshot(org, ROWS, AS_OF, config, rng)
    csv_path = tmp_path / f"opps_{AS_OF.isoformat()}.csv"
    write_csv(csv_path, generated)

    rows, report = validate_csv(csv_path, config)
    assert report.rejected == 0, report.row_reasons

    results = evaluate_snapshot(rows, config, AS_OF)  # open opps only
    engine = {opp_id: result.rule_ids() for opp_id, result in results.items()}

    engine_counts = rule_counts(engine)
    manifest_counts = rule_counts(expected)
    print("\nrule  engine  manifest")
    for rule in ALL_RULES:
        marker = "" if engine_counts[rule] == manifest_counts[rule] else "  <-- MISMATCH"
        print(f"{rule:<5} {engine_counts[rule]:>6}  {manifest_counts[rule]:>8}{marker}")

    mismatches = {
        opp_id: {"engine": engine.get(opp_id, []), "manifest": planted}
        for opp_id, planted in expected.items()
        if engine.get(opp_id, []) != planted
    }
    assert not mismatches, f"{len(mismatches)} opps disagree: " \
                           f"{dict(list(mismatches.items())[:5])}"
    assert engine_counts == manifest_counts
