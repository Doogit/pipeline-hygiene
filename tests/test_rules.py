"""Per-rule boundary unit tests — the correctness oracle.

Each rule is probed exactly at its threshold, one step past it, and at
empty/null field states, against the frozen tests/config_test.yaml.
"""
from datetime import date, timedelta

from src.rules import (
    HIGH, LOW, MEDIUM, InsufficientHistory, Violation, evaluate_row,
    evaluate_snapshot, has_valid_next_step, h1_stale_by_stage,
    h2_close_date_in_past, h3_serial_slippage, h4_missing_or_expired_next_step,
    h5_forecast_mismatch, h6_aging_in_stage, h7_single_threaded_big_deal,
    h8_amount_hygiene, h9_vague_next_step, h10_parked_close_date,
)

AS_OF = date(2026, 8, 10)


def clean_row(**overrides):
    row = {
        "opp_id": "OPP-0001", "account": "Bluepine Corp", "opp_name": "deal",
        "owner": "Avery Ashford", "stage": "develop", "amount": 50_000.0,
        "currency": "USD",
        "created_date": AS_OF - timedelta(days=90),
        "close_date": AS_OF + timedelta(days=30),
        "last_activity_date": AS_OF - timedelta(days=5),
        "next_step": "Send proposal to procurement team",
        "next_step_date": AS_OF + timedelta(days=7),
        "forecast_category": "pipeline", "contact_count": 3,
        "product_line": "CorePlatform",
        "stage_entered_date": AS_OF - timedelta(days=10),
        "close_date_changes": 0,
    }
    row.update(overrides)
    return row


def test_clean_base_row_is_clean(config):
    result = evaluate_row(clean_row(), config, AS_OF)
    assert result.violations == () and result.insufficient == ()


# --- shared predicate ---

def test_has_valid_next_step_boundaries():
    assert has_valid_next_step(clean_row(next_step_date=AS_OF), AS_OF)
    assert not has_valid_next_step(clean_row(next_step_date=AS_OF - timedelta(days=1)), AS_OF)
    assert not has_valid_next_step(clean_row(next_step=""), AS_OF)
    assert not has_valid_next_step(clean_row(next_step="   "), AS_OF)
    assert not has_valid_next_step(clean_row(next_step_date=None), AS_OF)


# --- H1 stale by stage (staleness_days: commit 7, propose 14, develop 21) ---

def test_h1_boundaries(config):
    at = clean_row(last_activity_date=AS_OF - timedelta(days=21))
    assert h1_stale_by_stage(at, config, AS_OF) is None
    past = clean_row(last_activity_date=AS_OF - timedelta(days=22))
    violation = h1_stale_by_stage(past, config, AS_OF)
    assert violation.rule_id == "H1" and violation.severity == MEDIUM


def test_h1_severity_high_for_commit_and_propose(config):
    for stage, threshold in (("commit", 7), ("propose", 14)):
        row = clean_row(stage=stage,
                        last_activity_date=AS_OF - timedelta(days=threshold + 1),
                        stage_entered_date=AS_OF - timedelta(days=1))
        assert h1_stale_by_stage(row, config, AS_OF).severity == HIGH
    row = clean_row(stage="prospect",
                    last_activity_date=AS_OF - timedelta(days=46),
                    stage_entered_date=AS_OF - timedelta(days=1))
    assert h1_stale_by_stage(row, config, AS_OF).severity == MEDIUM


# --- H2 close date in past ---

def test_h2_boundaries(config):
    assert h2_close_date_in_past(clean_row(close_date=AS_OF), config, AS_OF) is None
    violation = h2_close_date_in_past(
        clean_row(close_date=AS_OF - timedelta(days=1)), config, AS_OF)
    assert violation.rule_id == "H2" and violation.severity == HIGH


# --- H3 serial slippage ---

def test_h3_boundaries(config):
    assert h3_serial_slippage(clean_row(close_date_changes=1), config, AS_OF) is None
    assert h3_serial_slippage(clean_row(close_date_changes=2), config, AS_OF).severity == MEDIUM
    assert h3_serial_slippage(clean_row(close_date_changes=3), config, AS_OF).severity == HIGH
    commit2 = clean_row(stage="commit", close_date_changes=2,
                        stage_entered_date=AS_OF - timedelta(days=1))
    assert h3_serial_slippage(commit2, config, AS_OF).severity == HIGH


def test_h3_insufficient_history(config):
    outcome = h3_serial_slippage(clean_row(close_date_changes=None), config, AS_OF)
    assert isinstance(outcome, InsufficientHistory) and outcome.rule_id == "H3"


# --- H4 missing/expired next step ---

def test_h4_boundaries(config):
    assert h4_missing_or_expired_next_step(
        clean_row(next_step_date=AS_OF), config, AS_OF) is None
    for bad in (clean_row(next_step=""),
                clean_row(next_step_date=None),
                clean_row(next_step_date=AS_OF - timedelta(days=1))):
        violation = h4_missing_or_expired_next_step(bad, config, AS_OF)
        assert violation.rule_id == "H4" and violation.severity == HIGH


# --- H5 forecast mismatch ---

def test_h5_boundaries(config):
    early = clean_row(forecast_category="commit", stage="qualify")
    assert h5_forecast_mismatch(early, config, AS_OF).severity == HIGH
    ok = clean_row(forecast_category="commit", stage="propose",
                   stage_entered_date=AS_OF - timedelta(days=1))
    assert h5_forecast_mismatch(ok, config, AS_OF) is None
    badstep = clean_row(forecast_category="commit", stage="propose",
                        stage_entered_date=AS_OF - timedelta(days=1),
                        next_step_date=AS_OF - timedelta(days=1))
    assert h5_forecast_mismatch(badstep, config, AS_OF).severity == HIGH
    not_commit = clean_row(forecast_category="best_case", stage="qualify")
    assert h5_forecast_mismatch(not_commit, config, AS_OF) is None


# --- H6 aging in stage (aging_norm_days develop: 45) ---

def test_h6_boundaries(config):
    at = clean_row(stage_entered_date=AS_OF - timedelta(days=45))
    assert h6_aging_in_stage(at, config, AS_OF) is None
    past = clean_row(stage_entered_date=AS_OF - timedelta(days=46))
    violation = h6_aging_in_stage(past, config, AS_OF)
    assert violation.rule_id == "H6" and violation.severity == MEDIUM


def test_h6_insufficient_history(config):
    outcome = h6_aging_in_stage(clean_row(stage_entered_date=None), config, AS_OF)
    assert isinstance(outcome, InsufficientHistory) and outcome.rule_id == "H6"


# --- H7 single-threaded big deal (threshold 100000) ---

def test_h7_boundaries(config):
    assert h7_single_threaded_big_deal(
        clean_row(amount=100_000.0, contact_count=1), config, AS_OF).severity == MEDIUM
    assert h7_single_threaded_big_deal(
        clean_row(amount=99_999.99, contact_count=1), config, AS_OF) is None
    assert h7_single_threaded_big_deal(
        clean_row(amount=100_000.0, contact_count=2), config, AS_OF) is None
    assert h7_single_threaded_big_deal(
        clean_row(amount=None, contact_count=0), config, AS_OF) is None


# --- H8 amount hygiene ---

def test_h8_boundaries(config):
    assert h8_amount_hygiene(clean_row(amount=None), config, AS_OF).severity == LOW
    assert h8_amount_hygiene(clean_row(amount=0.0), config, AS_OF).severity == LOW
    assert h8_amount_hygiene(clean_row(amount=-5.0), config, AS_OF).severity == LOW
    assert h8_amount_hygiene(clean_row(amount=0.01), config, AS_OF) is None


# --- H9 vague next step (min_chars 15) ---

def test_h9_boundaries(config):
    assert h9_vague_next_step(clean_row(next_step="Call CFO Monday"), config, AS_OF) is None
    short = h9_vague_next_step(clean_row(next_step="Call CFO Mon."), config, AS_OF)
    assert short is not None and short.severity == LOW
    filler = h9_vague_next_step(
        clean_row(next_step="Follow up with the exec sponsor"), config, AS_OF)
    assert filler is not None and "filler" in filler.detail
    no_verb = h9_vague_next_step(
        clean_row(next_step="Waiting on customer response timeline"), config, AS_OF)
    assert no_verb is not None and "no action verb" in no_verb.detail
    invalid = clean_row(next_step="tbd", next_step_date=None)
    assert h9_vague_next_step(invalid, config, AS_OF) is None  # H4 territory, not H9


# --- H10 parked close date (horizon 270) ---

def test_h10_boundaries(config):
    at = clean_row(close_date=AS_OF + timedelta(days=270))
    assert h10_parked_close_date(at, config, AS_OF) is None
    low = h10_parked_close_date(
        clean_row(close_date=AS_OF + timedelta(days=271)), config, AS_OF)
    assert low.severity == LOW
    for fc in ("commit", "best_case"):
        row = clean_row(close_date=AS_OF + timedelta(days=271),
                        forecast_category=fc, stage="propose",
                        stage_entered_date=AS_OF - timedelta(days=1))
        assert h10_parked_close_date(row, config, AS_OF).severity == MEDIUM


# --- engine plumbing ---

def test_closed_rows_are_skipped(config):
    stale_closed = clean_row(stage="closed_won",
                             last_activity_date=AS_OF - timedelta(days=400),
                             close_date=AS_OF - timedelta(days=200))
    assert evaluate_row(stale_closed, config, AS_OF).violations == ()
    results = evaluate_snapshot([stale_closed, clean_row(opp_id="OPP-0002")],
                                config, AS_OF)
    assert set(results) == {"OPP-0002"}


def test_stacked_violations(config):
    row = clean_row(last_activity_date=AS_OF - timedelta(days=60),
                    next_step="", next_step_date=None,
                    stage_entered_date=AS_OF - timedelta(days=60))
    assert evaluate_row(row, config, AS_OF).rule_ids() == ["H1", "H4", "H6"]
