"""Streamlit dashboard tests with isolated runtime paths.

Task 12 note: the original PR-#2 smoke test asserted the pre-redesign layout
literally (exactly 5 metrics, exactly 2 dataframes). The tabbed redesign
mandated by Task 12 (charts + column_config tables per tab) necessarily
changes those counts, so this file re-asserts the same SEMANTICS — store
data loads, the owner filter narrows the exception table, zero exceptions —
plus the new per-tab element presence and graceful degradation.
"""
import json
import random
from datetime import date, timedelta
from pathlib import Path

import yaml
from streamlit.testing.v1 import AppTest

from src import brief
from src.seed import write_csv
from src.seed.org import build_org
from src.seed.pathologies import generate_snapshot
from src.seed.series import generate_series
from src.snapshots import SnapshotStore

AS_OF = date(2026, 8, 10)


def _env(tmp_path, monkeypatch, config, org):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    quotas_path = tmp_path / "seed_manifest.json"
    quotas_path.write_text(
        json.dumps({"quotas": {s.name: s.quota for s in org.sellers}}),
        encoding="utf-8")
    db_path = tmp_path / "pipeline.db"
    monkeypatch.setenv("PIPELINE_HYGIENE_CONFIG", str(config_path))
    monkeypatch.setenv("PIPELINE_HYGIENE_DB", str(db_path))
    monkeypatch.setenv("PIPELINE_HYGIENE_QUOTAS", str(quotas_path))
    return db_path


def _find_exceptions_df(app):
    for df in app.dataframe:
        if "detail" in df.value.columns:
            return df
    raise AssertionError("exceptions table not rendered")


def test_dashboard_series_tabs_charts_and_filters(tmp_path, config,
                                                  monkeypatch):
    rng = random.Random(42)
    org = build_org(rng)
    dates, snapshots, _ = generate_series(org, 150, 4, AS_OF, config, rng)
    db_path = _env(tmp_path, monkeypatch, config, org)
    store = SnapshotStore(db_path, config)
    cfg = dict(config)
    cfg["quotas"] = {s.name: s.quota for s in org.sellers}
    for d, rows in snapshots:
        csv_path = tmp_path / f"opps_{d.isoformat()}.csv"
        write_csv(csv_path, rows)
        assert store.ingest_csv(csv_path, d).rejected == 0
    # two recorded runs feed the score trend + since-last-run deltas
    brief.run(store, dates[-2], dates[-2], cfg, tmp_path / "out")
    brief.run(store, dates[-1], dates[-1], cfg, tmp_path / "out")
    store.close()

    app = AppTest.from_file(str(Path("app") / "dashboard.py"))
    app.run(timeout=60)
    assert not app.exception
    # headline metrics, with since-last-run deltas wired
    assert len(app.metric) == 5
    assert any(m.delta is not None for m in app.metric)
    # all five tabs render
    assert [t.label for t in app.tabs] == \
        ["Forecast call", "Slippage", "Trajectory", "Owners", "Appendix"]
    # charts: severity mix + coverage line + flow bars + score trend
    assert len(app.get("vega_lite_chart")) >= 4
    # tables: risky commits, slippage, owners, full exceptions
    assert len(app.dataframe) >= 4
    # read-only: no write affordances anywhere
    assert not app.button and not app.text_input and not app.number_input \
        and not app.text_area and not app.checkbox

    all_exceptions = len(_find_exceptions_df(app).value)
    assert all_exceptions > 0
    owner = _find_exceptions_df(app).value["owner"].iloc[0]
    app.sidebar.multiselect[0].set_value([owner])
    app.run(timeout=60)
    assert not app.exception
    filtered = _find_exceptions_df(app).value
    assert 0 < len(filtered) <= all_exceptions
    assert set(filtered["owner"]) <= {owner}


def test_dashboard_single_snapshot_degrades_gracefully(tmp_path, config,
                                                       monkeypatch):
    rng = random.Random(42)
    org = build_org(rng)
    rows, _ = generate_snapshot(org, 120, AS_OF, config, rng)
    db_path = _env(tmp_path, monkeypatch, config, org)
    store = SnapshotStore(db_path, config)
    csv_path = tmp_path / f"opps_{AS_OF.isoformat()}.csv"
    write_csv(csv_path, rows)
    assert store.ingest_csv(csv_path, AS_OF).rejected == 0
    store.close()

    app = AppTest.from_file(str(Path("app") / "dashboard.py"))
    app.run(timeout=60)
    assert not app.exception
    captions = " ".join(c.value for c in app.caption)
    assert "at least 2 stored snapshots" in captions
    assert "Desk score trend appears after" in captions
    # severity mix still renders from a single snapshot
    assert len(app.get("vega_lite_chart")) >= 1
