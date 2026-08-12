"""Deterministic parity fixtures — shared by the golden-freeze harness and the
parity test, so both build byte-identical stores.

Every fixture is generated from `random.Random(42)` + the seed simulator (no
committed binary DB), exactly like tests/test_dashboard.py, so goldens are
reproducible in CI. Each builder writes config.yaml / seed_manifest.json /
pipeline.db under a caller-supplied dir and returns the env-var mapping the
dashboard (and later the FastHTML server) reads.
"""
import json
import random
from datetime import date
from pathlib import Path

import yaml

from src import brief
from src.seed import write_csv
from src.seed.org import build_org
from src.seed.pathologies import generate_snapshot
from src.seed.series import generate_series
from src.snapshots import SnapshotStore

AS_OF = date(2026, 8, 10)
_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config_test.yaml"

# The three parity fixtures (plan §10 Task 4): a full multi-snapshot store that
# exercises trajectory/flow/charts/since-last-run, a single-snapshot store with
# bare quotas (no owner_meta) that exercises the degradation notes, and an empty
# store that exercises the empty-store stop path.
FIXTURES = ("full_multi", "single_minimal", "empty")


def _base_config():
    with open(_CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _write_env(target, config, quota_payload):
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    config_path = target / "config.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    quotas_path = target / "seed_manifest.json"
    quotas_path.write_text(json.dumps(quota_payload), encoding="utf-8")
    db_path = target / "pipeline.db"
    return {
        "PIPELINE_HYGIENE_CONFIG": str(config_path),
        "PIPELINE_HYGIENE_DB": str(db_path),
        "PIPELINE_HYGIENE_QUOTAS": str(quotas_path),
    }, db_path


def build_full_multi(target):
    """4 weekly snapshots + 2 recorded runs. Owners block carries team/region so
    the Teams tab and trajectory/flow/charts all render."""
    rng = random.Random(42)
    org = build_org(rng)
    config = _base_config()
    dates, snapshots, _ = generate_series(org, 150, 4, AS_OF, config, rng)
    quota_payload = {
        "quotas": {s.name: s.quota for s in org.sellers},
        "owners": {s.name: {"team": s.team, "region": s.region, "quota": s.quota}
                   for s in org.sellers},
    }
    env, db_path = _write_env(target, config, quota_payload)
    store = SnapshotStore(db_path, config)
    cfg = dict(config)
    cfg["quotas"] = {s.name: s.quota for s in org.sellers}
    for d, rows in snapshots:
        csv_path = Path(target) / f"opps_{d.isoformat()}.csv"
        write_csv(csv_path, rows)
        assert store.ingest_csv(csv_path, d).rejected == 0
    brief.run(store, dates[-2], dates[-2], cfg, Path(target) / "out")
    brief.run(store, dates[-1], dates[-1], cfg, Path(target) / "out")
    store.close()
    return env


def build_single_minimal(target):
    """One snapshot, bare quota mapping (no owners block -> no owner_meta): the
    Teams tab and multi-snapshot charts degrade to their notes."""
    rng = random.Random(42)
    org = build_org(rng)
    config = _base_config()
    rows, _ = generate_snapshot(org, 120, AS_OF, config, rng)
    quota_payload = {s.name: s.quota for s in org.sellers}  # bare mapping
    env, db_path = _write_env(target, config, quota_payload)
    store = SnapshotStore(db_path, config)
    csv_path = Path(target) / f"opps_{AS_OF.isoformat()}.csv"
    write_csv(csv_path, rows)
    assert store.ingest_csv(csv_path, AS_OF).rejected == 0
    store.close()
    return env


def build_empty(target):
    """Empty store (created, never ingested): the empty-store stop path."""
    config = _base_config()
    env, db_path = _write_env(target, config, {})
    SnapshotStore(db_path, config).close()
    return env


BUILDERS = {
    "full_multi": build_full_multi,
    "single_minimal": build_single_minimal,
    "empty": build_empty,
}
