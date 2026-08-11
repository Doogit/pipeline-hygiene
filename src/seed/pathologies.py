"""Pathology injectors: build every row field-by-field from a target violation set.

The critical invariant (spec 2B): planted != detected unless controlled. For
every row, every rule-relevant field NOT in the row's target set is explicitly
set clean. Expected violations are constructed here from field decisions,
NEVER by running the rules engine.

Field/rule coupling handled explicitly:
- H2 and H10 both own close_date and are mutually exclusive.
- H4 and H9 both own the next step; H9 requires a valid next step, so they
  are mutually exclusive.
- H7 requires amount >= big_deal_threshold, H8 requires amount null/<=0, so
  they are mutually exclusive.
- H5 via the bad-next-step route implies H4 (an invalid next step on an open
  opp always trips H4), so that cohort targets {H5, H4} together.
- Rows targeting H4 without H5 must NOT carry forecast_category=commit, and
  clean rows may carry commit forecast only in propose/commit stages with a
  valid next step (otherwise H5 would fire uncontrolled).
"""
from datetime import timedelta

from .org import (
    ACCOUNT_ADJECTIVES,
    ACCOUNT_NOUNS,
    ACCOUNT_SUFFIXES,
    DEAL_MOTIONS,
    PRODUCT_LINES,
    latest_quarter_start_on_or_before,
    quarter_ends_between,
    sample_amount,
)

OPEN_STAGES = ["prospect", "qualify", "develop", "propose", "commit"]
EARLY_STAGES = ["prospect", "qualify", "develop"]
CLOSED_STAGES = ["closed_won", "closed_lost"]
OPEN_STAGE_WEIGHTS = [20, 25, 25, 20, 10]

CSV_COLUMNS = [
    "opp_id", "account", "opp_name", "owner", "stage", "amount", "currency",
    "created_date", "close_date", "last_activity_date", "next_step",
    "next_step_date", "forecast_category", "contact_count", "product_line",
    "stage_entered_date", "close_date_changes",
]

ALL_RULES = ["H1", "H2", "H3", "H4", "H5", "H6", "H7", "H8", "H9", "H10"]

# Injector cohorts (spec 2B): zombie pipeline, serial-slippage cohort,
# single-threaded whales, forecast/stage contradictions, vague next steps,
# parked dates, amount hygiene, plus stacked combinations. Quarter-end date
# pileup and bulk quarter-start creation are date-texture patterns applied to
# all rows, not violation cohorts.
COHORTS = {
    "clean": [],
    "stale_only": ["H1"],
    "zombie": ["H1", "H4"],
    "zombie_deep": ["H1", "H4", "H6"],           # 3-violation stack
    "ghost_stack": ["H1", "H2", "H4", "H6"],     # 4-violation stack
    "past_close": ["H2"],
    "slippage": ["H3"],
    "slippage_past": ["H2", "H3"],
    "no_next_step": ["H4"],
    "contradiction_early": ["H5"],               # forecast commit in early stage
    "contradiction_badstep": ["H4", "H5"],       # forecast commit + invalid step
    "aging": ["H6"],
    "whale": ["H7"],
    "amount_hygiene": ["H8"],
    "vague": ["H9"],
    "parked": ["H10"],
    "parked_vague": ["H9", "H10"],
}

PERSONA_COHORTS = {
    "clean_operator": [("clean", 68), ("vague", 8), ("aging", 8), ("amount_hygiene", 6),
                       ("whale", 5), ("past_close", 5)],
    "sandbagger": [("clean", 28), ("parked", 25), ("parked_vague", 10), ("vague", 10),
                   ("aging", 10), ("whale", 12), ("amount_hygiene", 5)],
    "happy_ears": [("clean", 24), ("contradiction_early", 20), ("contradiction_badstep", 10),
                   ("slippage", 20), ("slippage_past", 11), ("past_close", 10), ("vague", 5)],
    "ghost": [("clean", 14), ("zombie", 24), ("zombie_deep", 14), ("ghost_stack", 10),
              ("stale_only", 18), ("past_close", 10), ("no_next_step", 10)],
}

CLOSED_ROW_FRACTION = 0.08

# Clean next-step templates: each contains an action verb from the default
# config, has >= min_chars characters, and contains no filler phrase.
CLEAN_STEP_TEMPLATES = [
    "Send updated proposal to {who}",
    "Schedule technical deep-dive with {who}",
    "Present pricing options to {who}",
    "Review contract redlines with {who}",
    "Confirm security questionnaire with {who}",
    "Deliver ROI analysis to {who}",
    "Negotiate payment terms with {who}",
    "Propose rollout plan to {who}",
]
CLEAN_STEP_WHO = ["procurement", "the CFO office", "IT steering group", "the exec sponsor",
                  "legal counsel", "the platform team"]

# Vague texts: valid date but fails quality (short, filler, or no action verb).
VAGUE_SHORT = ["ping them", "waiting", "next steps", "sync soon"]        # < 15 chars
VAGUE_FILLER = ["Follow up with the champion next week",
                "Touch base after their board meeting",
                "Check in on budget status internally",
                "Circle back once procurement responds"]
VAGUE_NO_VERB = ["Waiting on customer response timeline",
                 "Budget approval still with finance team",
                 "No movement until their reorg settles"]


def sample_cohort(rng, persona):
    cohorts = PERSONA_COHORTS[persona]
    names = [c for c, _ in cohorts]
    weights = [w for _, w in cohorts]
    return rng.choices(names, weights=weights, k=1)[0]


def _clean_step_text(rng):
    return rng.choice(CLEAN_STEP_TEMPLATES).format(who=rng.choice(CLEAN_STEP_WHO))


def _vague_step_text(rng):
    kind = rng.choices(["short", "filler", "no_verb"], weights=[30, 40, 30], k=1)[0]
    pool = {"short": VAGUE_SHORT, "filler": VAGUE_FILLER, "no_verb": VAGUE_NO_VERB}[kind]
    return rng.choice(pool)


def _account(rng):
    return (f"{rng.choice(ACCOUNT_ADJECTIVES)}{rng.choice(ACCOUNT_NOUNS).lower()} "
            f"{rng.choice(ACCOUNT_SUFFIXES)}")


def _clean_close_date(rng, as_of, config, min_close):
    """Hockey stick: cluster toward fiscal quarter ends, within [lo, hi]."""
    lo = max(as_of, min_close) if min_close else as_of
    hi = as_of + timedelta(days=config["close_date_horizon_days"])
    qends = quarter_ends_between(lo, hi, config["fiscal_year_start_month"])
    if not qends:
        return lo + timedelta(days=rng.randint(0, (hi - lo).days))
    weights = [55, 30, 15][:len(qends)]
    qe = rng.choices(qends, weights=weights, k=1)[0]
    offset = min(int(rng.expovariate(1 / 5.0)), 20)
    close = qe - timedelta(days=offset)
    return min(max(close, lo), hi)


def build_row(rng, seller, cohort_name, opp_id, as_of, config,
              min_close=None, entered_headroom=0, h10_base=None):
    """Construct one row. Returns (row_dict, sorted_expected_rules).

    min_close: clean close dates land at/after this date (series stability).
    entered_headroom: days subtracted from the clean aging window so H6-clean
        rows stay clean for the remainder of a series.
    h10_base: parked close dates are placed beyond h10_base + horizon so H10
        holds at every series snapshot (defaults to as_of).
    """
    rules = set(COHORTS[cohort_name])
    horizon = config["close_date_horizon_days"]
    h10_base = h10_base or as_of

    # --- stage ---
    h3_changes = None
    if "H5" in rules and "H4" not in rules:          # early-stage contradiction route
        stage = rng.choice(EARLY_STAGES)
    elif "H3" in rules:
        variant = rng.choices(["high3", "commit2", "med2"], weights=[40, 20, 40], k=1)[0]
        if variant == "high3":
            stage = rng.choices(OPEN_STAGES, weights=OPEN_STAGE_WEIGHTS, k=1)[0]
            h3_changes = rng.randint(3, 5)
        elif variant == "commit2":
            stage, h3_changes = "commit", 2
        else:
            stage = rng.choice(["prospect", "qualify", "develop", "propose"])
            h3_changes = 2
    else:
        stage = rng.choices(OPEN_STAGES, weights=OPEN_STAGE_WEIGHTS, k=1)[0]

    # --- amount / contacts ---
    if "H8" in rules:
        amount = rng.choice([None, 0.0])
    elif "H7" in rules:
        amount = round(config["big_deal_threshold"] * rng.uniform(1.0, 4.0), 2)
    else:
        amount = sample_amount(rng, seller.segment)
    contact_count = rng.choice([0, 1]) if "H7" in rules else rng.randint(2, 8)

    # --- close date ---
    if "H2" in rules:
        close_date = as_of - timedelta(days=rng.randint(3, 60))
    elif "H10" in rules:
        close_date = h10_base + timedelta(days=horizon + rng.randint(5, 120))
    else:
        close_date = _clean_close_date(rng, as_of, config, min_close)

    # --- close date changes (H3) ---
    close_date_changes = h3_changes if h3_changes is not None else rng.choice([0, 0, 0, 1])

    # --- last activity (H1) ---
    staleness = config["staleness_days"][stage]
    if "H1" in rules:
        last_activity = as_of - timedelta(days=staleness + rng.randint(1, 40))
    else:
        last_activity = as_of - timedelta(days=rng.randint(0, staleness))

    # --- stage entered (H6) ---
    aging_norm = config["aging_norm_days"][stage]
    if "H6" in rules:
        entered = as_of - timedelta(days=aging_norm + rng.randint(1, 30))
    else:
        entered = as_of - timedelta(days=rng.randint(0, max(aging_norm - entered_headroom, 0)))

    # --- created date; ~25% pile up at a fiscal quarter start ---
    base = min(entered, last_activity)
    qs = latest_quarter_start_on_or_before(base, config["fiscal_year_start_month"])
    if qs is not None and rng.random() < 0.25:
        created = qs
    else:
        created = base - timedelta(days=rng.randint(5, 120))
    if close_date is not None and created >= close_date:
        created = close_date - timedelta(days=rng.randint(1, 30))

    # --- next step (H4/H9) ---
    if "H4" in rules:
        variant = rng.choice(["empty", "expired", "no_date"])
        if variant == "empty":
            next_step, next_step_date = "", None
        elif variant == "expired":
            next_step = _clean_step_text(rng)
            next_step_date = as_of - timedelta(days=rng.randint(1, 30))
        else:
            next_step, next_step_date = _clean_step_text(rng), None
    elif "H9" in rules:
        next_step = _vague_step_text(rng)
        next_step_date = as_of + timedelta(days=rng.randint(1, 21))
    else:
        next_step = _clean_step_text(rng)
        next_step_date = as_of + timedelta(days=rng.randint(1, 21))

    # --- forecast category (H5) ---
    if "H5" in rules:
        forecast = "commit"
    else:
        commit_ok = stage in ("propose", "commit") and "H4" not in rules
        if seller.persona == "sandbagger":
            pop, w = ["omitted", "pipeline", "best_case"], [55, 30, 15]
        else:
            pop, w = ["pipeline", "best_case", "omitted"], [45, 25, 30]
        if commit_ok:
            pop = pop + ["commit"]
            w = w + ([35] if seller.persona == "happy_ears" else [15])
        forecast = rng.choices(pop, weights=w, k=1)[0]

    account = _account(rng)
    product = rng.choice(PRODUCT_LINES)
    row = {
        "opp_id": opp_id,
        "account": account,
        "opp_name": f"{account} {product} {rng.choice(DEAL_MOTIONS)}",
        "owner": seller.name,
        "stage": stage,
        "amount": amount,
        "currency": "USD",
        "created_date": created,
        "close_date": close_date,
        "last_activity_date": last_activity,
        "next_step": next_step,
        "next_step_date": next_step_date,
        "forecast_category": forecast,
        "contact_count": contact_count,
        "product_line": product,
        "stage_entered_date": entered,
        "close_date_changes": close_date_changes,
    }
    return row, sorted(rules)


def build_closed_row(rng, seller, opp_id, as_of, config):
    """Closed opp: no rules apply; expected set is empty."""
    stage = rng.choices(CLOSED_STAGES, weights=[60, 40], k=1)[0]
    close_date = as_of - timedelta(days=rng.randint(1, 90))
    last_activity = close_date - timedelta(days=rng.randint(0, 5))
    entered = close_date - timedelta(days=rng.randint(5, 45))
    created = min(entered, last_activity) - timedelta(days=rng.randint(5, 120))
    account = _account(rng)
    product = rng.choice(PRODUCT_LINES)
    row = {
        "opp_id": opp_id,
        "account": account,
        "opp_name": f"{account} {product} {rng.choice(DEAL_MOTIONS)}",
        "owner": seller.name,
        "stage": stage,
        "amount": sample_amount(rng, seller.segment),
        "currency": "USD",
        "created_date": created,
        "close_date": close_date,
        "last_activity_date": last_activity,
        "next_step": "",
        "next_step_date": None,
        "forecast_category": "omitted",
        "contact_count": rng.randint(1, 6),
        "product_line": product,
        "stage_entered_date": entered,
        "close_date_changes": rng.choice([0, 1]),
    }
    return row, []


def generate_snapshot(org, n_rows, as_of, config, rng,
                      min_close=None, entered_headroom=0, h10_base=None):
    """Generate n_rows rows + per-opp expected violation sets (ground truth)."""
    sellers = list(org.sellers)
    rng.shuffle(sellers)
    rows, expected = [], {}
    for i in range(1, n_rows + 1):
        opp_id = f"OPP-{i:04d}"
        seller = sellers[(i - 1) % len(sellers)]
        if rng.random() < CLOSED_ROW_FRACTION:
            row, exp = build_closed_row(rng, seller, opp_id, as_of, config)
        else:
            cohort = sample_cohort(rng, seller.persona)
            row, exp = build_row(rng, seller, cohort, opp_id, as_of, config,
                                 min_close=min_close, entered_headroom=entered_headroom,
                                 h10_base=h10_base)
        rows.append(row)
        expected[opp_id] = exp
    return rows, expected


def rule_counts(expected):
    counts = {r: 0 for r in ALL_RULES}
    for rules in expected.values():
        for r in rules:
            counts[r] += 1
    return counts
