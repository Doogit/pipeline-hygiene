"""Seed determinism (byte-identical via file hash) and ground-truth sanity."""
import hashlib
import random
from datetime import date

from src.seed import write_csv, write_json
from src.seed.org import build_org
from src.seed.pathologies import ALL_RULES, generate_snapshot, rule_counts
from src.seed.series import generate_series

AS_OF = date(2026, 8, 10)


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _generate_single(out_dir, config, rows=400, seed=42):
    rng = random.Random(seed)
    org = build_org(rng)
    generated, expected = generate_snapshot(org, rows, AS_OF, config, rng)
    csv_path = out_dir / f"opps_{AS_OF.isoformat()}.csv"
    write_csv(csv_path, generated)
    manifest_path = out_dir / "seed_manifest.json"
    write_json(manifest_path, {"generated_as_of": AS_OF.isoformat(),
                               "expected": expected,
                               "rule_counts": rule_counts(expected)})
    return csv_path, manifest_path, expected


def test_single_snapshot_determinism(tmp_path, config):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    csv_a, man_a, _ = _generate_single(a, config)
    csv_b, man_b, _ = _generate_single(b, config)
    assert _sha256(csv_a) == _sha256(csv_b)
    assert _sha256(man_a) == _sha256(man_b)


def test_series_determinism(tmp_path, config):
    hashes = []
    for sub in ("a", "b"):
        d = tmp_path / sub
        d.mkdir()
        rng = random.Random(42)
        org = build_org(rng)
        dates, snapshots, manifest = generate_series(org, 150, 4, AS_OF, config, rng)
        paths = []
        for sd, rows in snapshots:
            p = d / f"opps_{sd.isoformat()}.csv"
            write_csv(p, rows)
            paths.append(p)
        mp = d / "delta_manifest.json"
        write_json(mp, manifest)
        hashes.append([_sha256(p) for p in paths + [mp]])
    assert hashes[0] == hashes[1]


def test_ground_truth_coverage(tmp_path, config):
    """Default-size seed exercises every rule, has clean rows, and 3+ stacks."""
    _, _, expected = _generate_single(tmp_path, config)
    counts = rule_counts(expected)
    for rule in ALL_RULES:
        assert counts[rule] > 0, f"{rule} never planted at default size"
    assert any(not rules for rules in expected.values()), "no fully clean rows"
    assert any(len(rules) >= 3 for rules in expected.values()), "no 3+ stacks"


def test_series_dates_weekly(config):
    rng = random.Random(42)
    org = build_org(rng)
    dates, snapshots, manifest = generate_series(org, 60, 4, AS_OF, config, rng)
    assert dates[-1] == AS_OF
    assert [(dates[i + 1] - dates[i]).days for i in range(3)] == [7, 7, 7]
    assert len(manifest["deltas"]) == 3
    for delta in manifest["deltas"]:
        total = (len(delta["cleared"]) + len(delta["introduced"])
                 + len(delta["closed"]) + len(delta["close_dates_pushed"]))
        assert total > 0
        # mid-series creation: every week records new opps with their
        # field-by-field expected sets
        assert delta["added"]
        for opp_id, rules in delta["added"].items():
            assert rules == manifest["expected_by_snapshot"][delta["to"]][opp_id]
