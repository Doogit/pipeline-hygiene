"""Dynamic aging norms (Item 1): H6 evaluates against per-stage norms derived
from observed dwell when aging_norm_mode == 'derived'. Default 'static' is
byte-identical to before (parity-safe). The derivation only becomes meaningful
because seed progression makes stage_entered_date != created_date.
"""
from datetime import date, timedelta

import pytest

from src import brief
from src import funnel as fn
from src.ingest import ConfigError, validate_config
from src.rules import evaluate_snapshot
from src.seed import write_csv
from src.snapshots import SnapshotStore

D1 = date(2026, 7, 27)
D2 = date(2026, 8, 3)
D3 = date(2026, 8, 10)


def _row(opp_id, snap_date, stage, **over):
    row = {
        "opp_id": opp_id, "account": "Granitefreight LLC", "opp_name": "Deal",
        "owner": "Avery Farrow", "stage": stage, "amount": 50_000.0,
        "currency": "USD", "created_date": date(2026, 1, 5),
        "close_date": date(2026, 10, 1), "last_activity_date": snap_date,
        "next_step": "Send updated proposal to procurement",
        "next_step_date": snap_date + timedelta(days=10),
        "forecast_category": "pipeline", "contact_count": 3,
        "product_line": "CorePlatform",
        "stage_entered_date": snap_date, "close_date_changes": 0,
    }
    row.update(over)
    return row


def _ingest(store, tmp_path, snap_date, rows):
    path = tmp_path / f"opps_{snap_date.isoformat()}.csv"
    write_csv(path, rows)
    assert store.ingest_csv(path, snap_date).rejected == 0


def _store(tmp_path, config):
    """Three opps dwell 14d in develop then advance to propose (dwell samples).
    SITTER sits in develop the whole time, 30d in stage at D3 (its dwell is
    censored, so it adds no sample)."""
    store = SnapshotStore(":memory:", config)
    movers = [f"DEV-{i}" for i in range(1, 4)]
    _ingest(store, tmp_path, D1,
            [_row(o, D1, "develop") for o in movers]
            + [_row("SITTER", D1, "develop", stage_entered_date=D3 - timedelta(days=30))])
    _ingest(store, tmp_path, D2,
            [_row(o, D2, "develop") for o in movers]
            + [_row("SITTER", D2, "develop", stage_entered_date=D3 - timedelta(days=30))])
    _ingest(store, tmp_path, D3,
            [_row(o, D3, "propose") for o in movers]
            + [_row("SITTER", D3, "develop", stage_entered_date=D3 - timedelta(days=30))])
    return store


def _derived_cfg(config):
    cfg = dict(config)
    cfg["aging_norm_mode"] = "derived"
    cfg["aging_norm_derived_multiple"] = 2.0
    cfg["min_closed_for_win_rate"] = 2      # let 3 dwell samples qualify
    return cfg


def _future_dwell_store(tmp_path, config):
    """At D2 the store has only one completed develop dwell, below the derived
    norm sample floor. At D3 it has enough dwell evidence. Historical replay
    must not use the D3-derived norm while evaluating D2."""
    store = SnapshotStore(":memory:", config)
    _ingest(store, tmp_path, D1, [
        _row("M1", D1, "develop"),
        _row("SITTER", D1, "develop",
             stage_entered_date=D2 - timedelta(days=30)),
    ])
    _ingest(store, tmp_path, D2, [
        _row("M1", D2, "propose"),
        _row("M2", D2, "develop"),
        _row("SITTER", D2, "develop",
             stage_entered_date=D2 - timedelta(days=30)),
    ])
    _ingest(store, tmp_path, D3, [
        _row("M1", D3, "propose"),
        _row("M2", D3, "propose"),
        _row("SITTER", D3, "closed_lost",
             stage_entered_date=D2 - timedelta(days=30)),
    ])
    return store


def test_dwell_medians_from_progression(tmp_path, config):
    store = _store(tmp_path, config)
    medians = fn.stage_dwell_medians(store, D3)
    assert medians["develop"] == (14, 3)    # three 14d completed dwells
    # SITTER never left -> no sample; other stages have none either
    assert medians["propose"] == (None, 0)
    assert medians["qualify"] == (None, 0)


def test_derived_norms_preserve_fractional_median(config):
    cfg = _derived_cfg(config)
    norms = fn.derived_aging_norms(
        cfg, {"develop": (10.5, cfg["min_closed_for_win_rate"])})
    assert norms["develop"] == 21


def test_derived_norms_with_static_fallback(tmp_path, config):
    cfg = _derived_cfg(config)
    store = _store(tmp_path, config)
    norms = fn.derived_aging_norms(cfg, fn.stage_dwell_medians(store, D3))
    assert norms["develop"] == 28           # round(2.0 x 14)
    # stages below the sample floor keep their static norm
    assert norms["qualify"] == cfg["aging_norm_days"]["qualify"]
    assert norms["propose"] == cfg["aging_norm_days"]["propose"]


def test_static_mode_returns_config_unchanged(tmp_path, config):
    """The parity-safety contract: default mode returns the SAME object, so H6
    and every frozen golden are untouched."""
    store = _store(tmp_path, config)
    assert fn.resolve_aging_config(store, config, D3) is config


def test_derived_config_flips_h6(tmp_path, config):
    """A develop opp 30d in stage: clean under the static norm (45), flagged
    under the derived norm (28)."""
    store = _store(tmp_path, config)
    sitter = _row("SITTER", D3, "develop",
                  stage_entered_date=D3 - timedelta(days=30))

    static = evaluate_snapshot([sitter], config, D3)["SITTER"]
    assert "H6" not in static.rule_ids()

    resolved = fn.resolve_aging_config(store, _derived_cfg(config), D3)
    derived = evaluate_snapshot([sitter], resolved, D3)["SITTER"]
    assert "H6" in derived.rule_ids()


def test_brief_build_uses_derived_norm(tmp_path, config):
    """End-to-end: brief.build resolves derived norms and H6 flags the sitter
    that the static brief leaves clean."""
    cfg = _derived_cfg(config)
    store = _store(tmp_path, config)

    static_data = brief.build(store, D3, D3, config)
    assert "H6" not in static_data["results"]["SITTER"].rule_ids()

    derived_data = brief.build(store, D3, D3, cfg)
    assert "H6" in derived_data["results"]["SITTER"].rule_ids()


def test_backtest_resolves_derived_norms_per_snapshot(tmp_path, config):
    cfg = _derived_cfg(config)
    store = _future_dwell_store(tmp_path, cfg)

    resolved = fn.resolve_aging_config(store, cfg, D3)
    assert resolved["aging_norm_days"]["develop"] == 14

    from src import backtest as bt
    h6 = next(r for r in bt.backtest(store, cfg, D3)[0] if r["rule"] == "H6")
    assert h6["flagged"] == 0


def test_brief_trend_resolves_derived_norms_per_snapshot(tmp_path, config):
    cfg = _derived_cfg(config)
    store = _future_dwell_store(tmp_path, cfg)

    data = brief.build(store, D3, D3, cfg)
    assert data["score_trend"] == [(D1, 100.0), (D2, 100.0), (D3, 100.0)]


def test_config_schema_validates_aging_keys(config):
    good = dict(config)
    good["aging_norm_mode"] = "derived"
    good["aging_norm_derived_multiple"] = 1.5
    assert validate_config(good) is good

    bad_mode = dict(config)
    bad_mode["aging_norm_mode"] = "dynamic"
    with pytest.raises(ConfigError):
        validate_config(bad_mode)

    bad_mult = dict(config)
    bad_mult["aging_norm_derived_multiple"] = 0
    with pytest.raises(ConfigError):
        validate_config(bad_mult)
