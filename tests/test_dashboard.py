"""Streamlit dashboard smoke tests with isolated runtime paths."""
import json
import random
from datetime import date
from pathlib import Path

import yaml
from streamlit.testing.v1 import AppTest

from src.seed import write_csv
from src.seed.org import build_org
from src.seed.pathologies import generate_snapshot
from src.snapshots import SnapshotStore


def test_dashboard_loads_store_data_and_filters(tmp_path, config, monkeypatch):
    as_of = date(2026, 8, 10)
    rng = random.Random(42)
    org = build_org(rng)
    rows, _ = generate_snapshot(org, 120, as_of, config, rng)

    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    quotas_path = tmp_path / "seed_manifest.json"
    quotas_path.write_text(
        json.dumps({"quotas": {s.name: s.quota for s in org.sellers}}),
        encoding="utf-8")

    csv_path = tmp_path / f"opps_{as_of.isoformat()}.csv"
    write_csv(csv_path, rows)
    db_path = tmp_path / "pipeline.db"
    store = SnapshotStore(db_path, config)
    assert store.ingest_csv(csv_path, as_of).rejected == 0

    monkeypatch.setenv("PIPELINE_HYGIENE_CONFIG", str(config_path))
    monkeypatch.setenv("PIPELINE_HYGIENE_DB", str(db_path))
    monkeypatch.setenv("PIPELINE_HYGIENE_QUOTAS", str(quotas_path))

    app = AppTest.from_file(str(Path("app") / "dashboard.py"))
    app.run(timeout=30)
    assert not app.exception
    assert len(app.metric) == 5
    assert len(app.dataframe) == 2

    all_exceptions = len(app.dataframe[0].value)
    owner = app.dataframe[0].value["owner"].iloc[0]
    app.sidebar.multiselect[0].set_value([owner])
    app.run(timeout=30)
    assert not app.exception
    assert 0 <= len(app.dataframe[0].value) <= all_exceptions
    assert set(app.dataframe[0].value["owner"]) <= {owner}
