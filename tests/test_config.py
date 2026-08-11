"""Config validation (fail fast, exact key path) and display currency.

Every config key added after the frozen test config must be OPTIONAL with a
default read via config.get(...) at point of use — these tests also prove the
frozen tests/config_test.yaml passes validation unchanged.
"""
from datetime import date, timedelta

import pytest
import yaml

from src import brief
from src.ingest import ConfigError, load_config, validate_config
from src.rules import h7_single_threaded_big_deal

AS_OF = date(2026, 8, 10)


def _write(tmp_path, cfg):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return path


def test_frozen_test_config_is_valid(tmp_path, config):
    loaded = load_config(_write(tmp_path, dict(config)))
    assert loaded["big_deal_threshold"] == config["big_deal_threshold"]


def test_missing_top_level_key_names_it(tmp_path, config):
    cfg = dict(config)
    del cfg["rule_weights"]
    with pytest.raises(ConfigError, match="missing config key rule_weights"):
        load_config(_write(tmp_path, cfg))


def test_missing_nested_key_names_the_path(config):
    cfg = dict(config)
    cfg["staleness_days"] = {k: v for k, v in config["staleness_days"].items()
                             if k != "commit"}
    with pytest.raises(ConfigError,
                       match=r"missing config key staleness_days\.commit"):
        validate_config(cfg)


def test_wrong_typed_nested_key_names_the_path(config):
    cfg = dict(config)
    cfg["staleness_days"] = dict(config["staleness_days"], commit="soon")
    with pytest.raises(ConfigError,
                       match=r"config key 'staleness_days\.commit' must be "
                             r"number, got str"):
        validate_config(cfg)


def test_wrong_typed_top_level_key(config):
    cfg = dict(config)
    cfg["min_opps_for_owner_score"] = "five"
    with pytest.raises(ConfigError,
                       match="'min_opps_for_owner_score' must be int"):
        validate_config(cfg)


def test_unknown_top_level_key_warns_but_loads(tmp_path, config):
    cfg = dict(config)
    cfg["push_alarm_dayz"] = 21
    with pytest.warns(UserWarning, match="push_alarm_dayz"):
        loaded = load_config(_write(tmp_path, cfg))
    assert loaded["push_alarm_dayz"] == 21


def test_non_mapping_config_is_fatal(config):
    with pytest.raises(ConfigError, match="YAML mapping"):
        validate_config(["not", "a", "mapping"])


# --- display currency ---

def _row(**overrides):
    row = {
        "opp_id": "OPP-1", "account": "Acme Synthetic", "opp_name": "Deal",
        "owner": "Avery Calloway", "stage": "propose", "amount": 50000.0,
        "currency": "EUR", "created_date": AS_OF - timedelta(days=60),
        "close_date": AS_OF + timedelta(days=30),
        "last_activity_date": AS_OF - timedelta(days=1),
        "next_step": "Send updated proposal to procurement",
        "next_step_date": AS_OF + timedelta(days=7),
        "forecast_category": "pipeline", "contact_count": 3,
        "product_line": "widgets", "stage_entered_date": AS_OF,
        "close_date_changes": 0,
    }
    row.update(overrides)
    return row


def test_display_currency_symbol_in_brief_money(config):
    cfg = dict(config)
    cfg["display_currency_symbol"] = "€"
    data = brief.build_from_rows([_row()], AS_OF, AS_OF, cfg)
    markdown = brief.render(data, cfg)
    assert "- Open pipeline: €50,000" in markdown
    assert "$" not in markdown


def test_default_currency_symbol_is_dollar(config):
    data = brief.build_from_rows([_row()], AS_OF, AS_OF, config)
    assert "- Open pipeline: $50,000" in brief.render(data, config)


def test_h7_detail_uses_display_currency(config):
    cfg = dict(config)
    cfg["display_currency_symbol"] = "€"
    row = _row(amount=250000.0, contact_count=1)
    violation = h7_single_threaded_big_deal(row, cfg, AS_OF)
    assert violation.detail == "€250,000 deal with 1 contact(s)"
