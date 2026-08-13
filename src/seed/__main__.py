"""CLI entry: python -m src.seed

Single snapshot:  python -m src.seed --rows 400 --as-of 2026-08-10
Weekly series:    python -m src.seed --series 4 --as-of 2026-08-10
"""
import argparse
import random
from datetime import date
from pathlib import Path

import yaml

from . import write_csv, write_json
from .org import build_org
from .pathologies import generate_snapshot, rule_counts
from .series import generate_series


def owners_metadata(org):
    return {
        "quotas": {s.name: s.quota for s in org.sellers},
        "owners": {s.name: {"persona": s.persona, "segment": s.segment,
                            "region": s.region, "team": s.team, "quota": s.quota}
                   for s in org.sellers},
    }


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m src.seed",
                                description="Deterministic org simulator seed")
    p.add_argument("--rows", type=int, default=400)
    p.add_argument("--as-of", type=date.fromisoformat, default=None)
    p.add_argument("--series", type=int, default=None,
                   help="emit N weekly snapshots ending at --as-of")
    p.add_argument("--progress-per-week", type=int, default=0,
                   help="series only: advance this many clean opps one stage "
                        "forward each week (0 = off; isolated rng)")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--out-dir", default="data")
    p.add_argument("--config", default="config.yaml")
    args = p.parse_args(argv)

    # CLI entry point: the only place date.today() is allowed (spec).
    as_of = args.as_of if args.as_of is not None else date.today()
    with open(args.config, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    rng = random.Random(args.seed)
    org = build_org(rng)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.series and args.series > 1:
        dates, snapshots, manifest = generate_series(
            org, args.rows, args.series, as_of, config, rng,
            args.progress_per_week)
        snap_dir = out_dir / "snapshots"
        snap_dir.mkdir(parents=True, exist_ok=True)
        for d, rows in snapshots:
            write_csv(snap_dir / f"opps_{d.isoformat()}.csv", rows)
        manifest.update({"generated_as_of": as_of.isoformat(), "seed": args.seed,
                         "rows": args.rows, "series": args.series})
        manifest.update(owners_metadata(org))
        write_json(out_dir / "delta_manifest.json", manifest)
        print(f"wrote {len(snapshots)} snapshots to {snap_dir} "
              f"({dates[0]}..{dates[-1]}) and {out_dir / 'delta_manifest.json'}")
    else:
        rows, expected = generate_snapshot(org, args.rows, as_of, config, rng)
        csv_path = out_dir / f"opps_{as_of.isoformat()}.csv"
        write_csv(csv_path, rows)
        manifest = {"generated_as_of": as_of.isoformat(), "seed": args.seed,
                    "rows": args.rows, "expected": expected,
                    "rule_counts": rule_counts(expected)}
        manifest.update(owners_metadata(org))
        write_json(out_dir / "seed_manifest.json", manifest)
        print(f"wrote {csv_path} and {out_dir / 'seed_manifest.json'} "
              f"(rule counts: {rule_counts(expected)})")


if __name__ == "__main__":
    main()
