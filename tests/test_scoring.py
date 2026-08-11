"""Scoring: opp floor, distinct at-risk dollars, owner/desk rollups, flags."""
from datetime import date, timedelta

from src.rules import evaluate_snapshot
from src.scoring import at_risk_dollars, desk_rollup, opp_score, owner_rollups

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
    assert stats.coverage_ratio == 0.3 and stats.coverage_flagged  # < 3.0
    assert stats.mean_score == (100 + 100 + 80) / 3
    assert stats.median_score == 100


def test_owner_without_quota_has_no_coverage(config):
    rows = [clean_row()]
    results = evaluate_snapshot(rows, config, AS_OF)
    stats = owner_rollups(rows, results, config)["Avery Ashford"]
    assert stats.coverage_ratio is None and not stats.coverage_flagged


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
