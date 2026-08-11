"""Hypothesis property tests.

1) Arbitrary well-typed rows never raise.
2) Monotonicity: degrading any single field (older activity date, more slips,
   emptier next step) never increases an opp score.
"""
from datetime import date, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from src.rules import evaluate_row
from src.scoring import opp_score

AS_OF = date(2026, 8, 10)

_dates = st.dates(min_value=date(2020, 1, 1), max_value=date(2030, 12, 31))
_opt_dates = st.none() | _dates
_amounts = st.none() | st.floats(min_value=-1e9, max_value=1e9,
                                 allow_nan=False, allow_infinity=False)
_stages = st.sampled_from(["prospect", "qualify", "develop", "propose",
                           "commit", "closed_won", "closed_lost"])
_open_stages = st.sampled_from(["prospect", "qualify", "develop", "propose", "commit"])
_forecasts = st.sampled_from(["pipeline", "best_case", "commit", "omitted"])


def _row_strategy(stage_strategy, changes_strategy):
    return st.fixed_dictionaries({
        "opp_id": st.just("OPP-0001"),
        "account": st.text(max_size=30),
        "opp_name": st.text(max_size=40),
        "owner": st.text(max_size=30),
        "stage": stage_strategy,
        "amount": _amounts,
        "currency": st.just("USD"),
        "created_date": _dates,
        "close_date": _dates,
        "last_activity_date": _dates,
        "next_step": st.text(max_size=60),
        "next_step_date": _opt_dates,
        "forecast_category": _forecasts,
        "contact_count": st.integers(min_value=0, max_value=50),
        "product_line": st.text(max_size=20),
        "stage_entered_date": _opt_dates,
        "close_date_changes": changes_strategy,
    })


@settings(max_examples=300)
@given(row=_row_strategy(_stages, st.none() | st.integers(0, 15)))
def test_well_typed_rows_never_raise(config, row):
    result = evaluate_row(row, config, AS_OF)
    assert 0 <= opp_score(result, config) <= 100


@settings(max_examples=200)
@given(row=_row_strategy(_open_stages, st.none() | st.integers(0, 15)),
       days_older=st.integers(min_value=1, max_value=500))
def test_older_activity_never_raises_score(config, row, days_older):
    before = opp_score(evaluate_row(row, config, AS_OF), config)
    degraded = dict(row, last_activity_date=row["last_activity_date"]
                    - timedelta(days=days_older))
    after = opp_score(evaluate_row(degraded, config, AS_OF), config)
    assert after <= before


@settings(max_examples=200)
@given(row=_row_strategy(_open_stages, st.integers(0, 15)),
       extra_slips=st.integers(min_value=1, max_value=10))
def test_more_slips_never_raises_score(config, row, extra_slips):
    before = opp_score(evaluate_row(row, config, AS_OF), config)
    degraded = dict(row, close_date_changes=row["close_date_changes"] + extra_slips)
    after = opp_score(evaluate_row(degraded, config, AS_OF), config)
    assert after <= before


@settings(max_examples=200)
@given(row=_row_strategy(_open_stages, st.none() | st.integers(0, 15)))
def test_emptier_next_step_never_raises_score(config, row):
    before = opp_score(evaluate_row(row, config, AS_OF), config)
    degraded = dict(row, next_step="", next_step_date=None)
    after = opp_score(evaluate_row(degraded, config, AS_OF), config)
    assert after <= before
