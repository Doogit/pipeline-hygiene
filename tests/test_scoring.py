"""Scoring: opp floor, distinct at-risk dollars, owner/desk rollups, flags."""
from datetime import date, timedelta

from src.rules import evaluate_snapshot
from src.scoring import (at_risk_dollars, desk_rollup, group_rollups,
                         opp_score, owner_rollups, required_coverage_multiple)

from tests.test_rules import AS_OF, clean_row


def _score_of(row, config):
    results = evaluate_snapshot([row], config, AS_OF)
    return opp_score(results[row["opp_id"]], config)


def test_opp_score_deducts_weights(config):
    assert _score_of(clean_row(), config) == 100
    # H1 (15, develop=medium) + H4 (20) + H6 (10) = 45 deducted
    row = clean_row(last_activity_date=AS_OF - timedelta(days=60),
                    next_step="", next_step_date=None,
                    stage_entered_date=AS_OF - timedelta(days=60))
    assert _score_of(row, config) == 55


def test_opp_score_floors_at_zero(config):
    # H1+H2+H3+H4+H5+H6+H7 = 15+20+10+20+25+10+10 = 110 -> floor 0
    row = clean_row(stage="qualify", forecast_category="commit",
                    amount=150_000.0, contact_count=1,
                    last_activity_date=AS_OF - timedelta(days=40),
                    close_date=AS_OF - timedelta(days=10),
                    close_date_changes=4,
                    next_step="", next_step_date=None,
                    stage_entered_date=AS_OF - timedelta(days=40))
    assert _score_of(row, config) == 0


def test_at_risk_dollars_distinct_not_per_violation(config):
    # Two high violations (H2 + H4) on one 80k opp: count 80k once.
    risky = clean_row(close_date=AS_OF - timedelta(days=5),
                      next_step="", next_step_date=None, amount=80_000.0)
    fine = clean_row(opp_id="OPP-0002", amount=50_000.0)
    medium_only = clean_row(opp_id="OPP-0003", amount=30_000.0,
                            stage_entered_date=AS_OF - timedelta(days=60))
    rows = [risky, fine, medium_only]
    results = evaluate_snapshot(rows, config, AS_OF)
    assert len([v for v in results["OPP-0001"].violations if v.severity == "high"]) == 2
    assert at_risk_dollars(rows, results) == 80_000.0


def test_at_risk_excludes_closed_and_handles_null_amount(config):
    closed = clean_row(stage="closed_lost", close_date=AS_OF - timedelta(days=5))
    null_amount = clean_row(opp_id="OPP-0002", amount=None,
                            close_date=AS_OF - timedelta(days=3))  # high H2, None amount
    rows = [closed, null_amount]
    results = evaluate_snapshot(rows, config, AS_OF)
    assert at_risk_dollars(rows, results) == 0.0


def test_owner_rollups_flags(config):
    config_with_quota = dict(config)
    config_with_quota["quotas"] = {"Avery Ashford": 1_000_000.0}
    rows = [clean_row(opp_id=f"OPP-{i:04d}", amount=100_000.0, contact_count=3)
            for i in range(1, 4)]                      # 3 open opps -> small_n
    rows[2] = dict(rows[2], next_step="", next_step_date=None)  # one H4
    results = evaluate_snapshot(rows, config_with_quota, AS_OF)
    stats = owner_rollups(rows, results, config_with_quota)["Avery Ashford"]
    assert stats.n_open == 3 and stats.small_n
    assert stats.violation_count == 1
    assert stats.open_pipeline == 300_000.0
    # coverage vs required: 300k / (1M quota x 3.0 config floor) = 0.1, < 1.0
    assert abs(stats.coverage_ratio - 0.1) < 1e-9 and stats.coverage_flagged
    assert stats.mean_score == (100 + 100 + 80) / 3
    assert stats.median_score == 100


def test_owner_without_quota_has_no_coverage(config):
    rows = [clean_row()]
    results = evaluate_snapshot(rows, config, AS_OF)
    stats = owner_rollups(rows, results, config)["Avery Ashford"]
    assert stats.coverage_ratio is None and not stats.coverage_flagged


def test_owner_coverage_flag_uses_remaining_quota_and_multiple(config):
    cfg = dict(config)
    cfg["quotas"] = {"Avery Ashford": 1_000_000.0}
    rows = [clean_row(opp_id=f"OPP-{i:04d}", amount=200_000.0, contact_count=3)
            for i in range(1, 9)]               # 8 open -> 1.6M pipeline
    results = evaluate_snapshot(rows, cfg, AS_OF)
    # Derived multiple 1.5x, nothing booked: required 1.5M -> 1.07x, unflagged.
    # The shown ratio and the flag share one basis: flagged iff ratio < 1.0.
    stats = owner_rollups(rows, results, cfg, multiple=1.5)["Avery Ashford"]
    assert abs(stats.coverage_ratio - 1.6 / 1.5) < 1e-9
    assert not stats.coverage_flagged
    # No multiple supplied -> static 3.0x floor: 1.6M / 3M = 0.53x, flagged
    # (the over-firing the win-rate-derived basis fixes).
    fallback = owner_rollups(rows, results, cfg)["Avery Ashford"]
    assert abs(fallback.coverage_ratio - 1.6 / 3.0) < 1e-9
    assert fallback.coverage_flagged
    # Booking 700k this quarter shrinks remaining to 300k -> required 450k.
    won = {"Avery Ashford": 700_000.0}
    booked = owner_rollups(rows, results, cfg, 1.5, won)["Avery Ashford"]
    assert abs(booked.coverage_ratio - 1.6 / 0.45) < 1e-9
    assert not booked.coverage_flagged


def test_required_coverage_multiple_win_rate_and_fallback(config):
    cfg = dict(config)
    outcomes = ([{"stage": "closed_won"}] * 4 + [{"stage": "closed_lost"}] * 6)
    # too few for min_closed_for_win_rate -> config floor
    multiple, basis = required_coverage_multiple(outcomes[:5], cfg)
    assert multiple == cfg["coverage_ratio_min"] and "insufficient" in basis
    # >= threshold with a 40% win rate -> 1 / 0.4 = 2.5x
    cfg2 = dict(cfg, min_closed_for_win_rate=10)
    multiple, basis = required_coverage_multiple(outcomes, cfg2)
    assert abs(multiple - 2.5) < 1e-9 and basis.startswith("trailing win rate")


def test_group_rollups_team_and_region(config):
    cfg = dict(config)
    cfg["quotas"] = {"A": 1_000_000.0, "B": 1_000_000.0, "C": 1_000_000.0}
    owner_meta = {"A": {"team": "T1", "region": "R1"},
                  "B": {"team": "T1", "region": "R1"},
                  "C": {"team": "T2", "region": "R2"}}
    rows = ([clean_row(opp_id=f"A-{i}", owner="A", amount=100_000.0,
                       contact_count=3) for i in range(3)]
            + [clean_row(opp_id=f"B-{i}", owner="B", amount=100_000.0,
                         contact_count=3) for i in range(2)]
            + [clean_row(opp_id="C-0", owner="C", amount=500_000.0,
                         contact_count=3)])
    results = evaluate_snapshot(rows, cfg, AS_OF)
    teams = group_rollups(rows, results, cfg, owner_meta, "team", multiple=1.5)
    assert set(teams) == {"T1", "T2"}
    t1 = teams["T1"]
    assert t1.n_owners == 2 and t1.n_open == 5
    assert t1.open_pipeline == 500_000.0 and t1.quota == 2_000_000.0
    # coverage vs required: 500k / (2M roster quota x 1.5) = 0.167, flagged
    assert abs(t1.coverage_ratio - 500 / 3000) < 1e-9
    assert t1.coverage_flagged
    regions = group_rollups(rows, results, cfg, owner_meta, "region",
                            multiple=1.5)
    assert set(regions) == {"R1", "R2"}
    assert regions["R1"].open_pipeline == 500_000.0
    # No owner metadata -> no rollup at all.
    assert group_rollups(rows, results, cfg, {}, "team") == {}


def test_desk_rollup_three_measures(config):
    # One big clean deal must not mask a rotten tail: weighted mean high,
    # median low, pct_healthy low.
    big_clean = clean_row(amount=5_000_000.0, contact_count=4)
    tail = [clean_row(opp_id=f"OPP-{i:04d}", amount=10_000.0,
                      next_step="", next_step_date=None,
                      close_date=AS_OF - timedelta(days=2))   # H2+H4 -> score 60
            for i in range(2, 6)]
    rows = [big_clean] + tail
    results = evaluate_snapshot(rows, config, AS_OF)
    desk = desk_rollup(rows, results, config)
    assert desk.n_open == 5
    assert desk.weighted_mean_score > 99
    assert desk.median_score == 60
    assert desk.pct_healthy == 20.0
    assert desk.at_risk_dollars == 40_000.0
    assert desk.violation_counts_by_severity["high"] == 8


def test_desk_rollup_empty(config):
    desk = desk_rollup([], {}, config)
    assert desk.n_open == 0 and desk.at_risk_dollars == 0.0
