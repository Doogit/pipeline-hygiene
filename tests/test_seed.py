"""Seed determinism (byte-identical via file hash) and ground-truth sanity."""
import hashlib
import random
from datetime import date

from src.rules import evaluate_snapshot
from src.seed import write_csv, write_json
from src.seed.org import build_org
from src.seed.pathologies import ALL_RULES, generate_snapshot, rule_counts
from src.seed.series import generate_series
from src.snapshots import SnapshotStore

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
        # progression is off by default, so no transitions are ever scripted
        assert delta["stage_transitions"] == {}
        # mid-series creation: every week records new opps with their
        # field-by-field expected sets
        assert delta["added"]
        for opp_id, rules in delta["added"].items():
            assert rules == manifest["expected_by_snapshot"][delta["to"]][opp_id]


def test_progression_off_is_byte_identical(tmp_path, config):
    """The opt-in isolation contract: generating with progression off must be
    byte-identical to omitting the argument entirely (the main rng stream, and
    thus every snapshot/delta, is untouched)."""
    def _hashes(progress):
        d = tmp_path / f"p{progress}"
        d.mkdir()
        rng = random.Random(42)
        org = build_org(rng)
        _, snapshots, manifest = generate_series(
            org, 150, 4, AS_OF, config, rng, progress)
        paths = []
        for sd, rows in snapshots:
            p = d / f"opps_{sd.isoformat()}.csv"
            write_csv(p, rows)
            paths.append(p)
        return [_sha256(p) for p in paths]

    default = _hashes(0)
    rng = random.Random(42)
    org = build_org(rng)
    _, snaps_no_arg, _ = generate_series(org, 150, 4, AS_OF, config, rng)
    for i, (sd, rows) in enumerate(snaps_no_arg):
        p = tmp_path / f"noarg_{sd.isoformat()}.csv"
        write_csv(p, rows)
        assert _sha256(p) == default[i]


def test_progression_on_stays_clean_and_consistent(tmp_path, config):
    """With progression ON, transitions are actually scripted, progressed opps
    stay CLEAN, and the engine reproduces the field-by-field expected set at
    every snapshot (mirror of test_manifest_consistency, over a series). The
    engine is run from a store so history-only H11 is included."""
    rng = random.Random(42)
    org = build_org(rng)
    dates, snapshots, manifest = generate_series(
        org, 400, 4, AS_OF, config, rng, progress_per_week=8)

    transitioned = {o for delta in manifest["deltas"]
                    for o in delta["stage_transitions"]}
    assert transitioned, "progression never advanced any opp"

    store = SnapshotStore(":memory:", config)
    for d, rows in snapshots:
        csv_path = tmp_path / f"opps_{d.isoformat()}.csv"
        write_csv(csv_path, rows)
        assert store.ingest_csv(csv_path, d).rejected == 0

    for d in dates:
        expected = manifest["expected_by_snapshot"][d.isoformat()]
        stored = store.rows_with_history(d)
        results = evaluate_snapshot(stored, config, d)
        engine = {opp_id: r.rule_ids() for opp_id, r in results.items()}
        mismatches = {o: {"engine": engine.get(o, []), "manifest": expected[o]}
                      for o in engine if engine[o] != expected[o]}
        assert not mismatches, f"{d}: {dict(list(mismatches.items())[:5])}"

    # every transitioned opp is clean at the snapshot it landed in, and stays
    # clean through the final snapshot (unless later closed/re-scripted)
    for delta in manifest["deltas"]:
        to_date = delta["to"]
        for opp_id in delta["stage_transitions"]:
            assert manifest["expected_by_snapshot"][to_date][opp_id] == []
