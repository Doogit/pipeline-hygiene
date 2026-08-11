"""Deterministic hygiene rules H1-H11.

Each rule is a pure function (row, config, as_of) -> Violation | None |
InsufficientHistory. Rule IDs are stable — tests reference them. Rules never
call other rules; H4 and H5 share the has_valid_next_step predicate so
neither duplicates the other. Rules apply to open opportunities only;
evaluate_row enforces that.

Every time evaluation takes the explicit as_of parameter — date.today() is
banned outside CLI entry points.
"""
from dataclasses import dataclass

OPEN_STAGES = ("prospect", "qualify", "develop", "propose", "commit")
CLOSED_STAGES = ("closed_won", "closed_lost")

HIGH, MEDIUM, LOW = "high", "medium", "low"


@dataclass(frozen=True)
class Violation:
    rule_id: str
    severity: str
    detail: str


@dataclass(frozen=True)
class InsufficientHistory:
    rule_id: str
    reason: str


def is_open(row):
    return row["stage"] not in CLOSED_STAGES


def has_valid_next_step(row, as_of):
    """next_step non-empty AND next_step_date present AND next_step_date >= as_of."""
    return bool((row["next_step"] or "").strip()) \
        and row["next_step_date"] is not None \
        and row["next_step_date"] >= as_of


def h1_stale_by_stage(row, config, as_of):
    threshold = config["staleness_days"][row["stage"]]
    days_idle = (as_of - row["last_activity_date"]).days
    if days_idle > threshold:
        severity = HIGH if row["stage"] in ("commit", "propose") else MEDIUM
        return Violation("H1", severity,
                         f"no activity for {days_idle}d (> {threshold}d for "
                         f"{row['stage']})")
    return None


def h2_close_date_in_past(row, config, as_of):
    if row["close_date"] < as_of:
        return Violation("H2", HIGH,
                         f"close date {row['close_date'].isoformat()} is "
                         f"{(as_of - row['close_date']).days}d in the past")
    return None


def h3_serial_slippage(row, config, as_of):
    changes = row["close_date_changes"]
    if changes is None:
        return InsufficientHistory("H3", "close_date_changes unavailable: "
                                         "column absent and <2 snapshots stored")
    if changes >= 2:
        severity = HIGH if changes >= 3 or row["stage"] == "commit" else MEDIUM
        return Violation("H3", severity, f"close date changed {changes} times")
    return None


def h4_missing_or_expired_next_step(row, config, as_of):
    if not has_valid_next_step(row, as_of):
        if not (row["next_step"] or "").strip():
            what = "next step empty"
        elif row["next_step_date"] is None:
            what = "next step has no date"
        else:
            what = f"next step date {row['next_step_date'].isoformat()} expired"
        return Violation("H4", HIGH, what)
    return None


def h5_forecast_mismatch(row, config, as_of):
    if row["forecast_category"] != "commit":
        return None
    early = row["stage"] in ("prospect", "qualify", "develop")
    if early or not has_valid_next_step(row, as_of):
        why = (f"forecast commit but stage {row['stage']}" if early
               else "forecast commit without a valid next step")
        return Violation("H5", HIGH, why)
    return None


def h6_aging_in_stage(row, config, as_of):
    entered = row["stage_entered_date"]
    if entered is None:
        return InsufficientHistory("H6", "stage_entered_date unavailable: "
                                         "column absent and <2 snapshots stored")
    norm = config["aging_norm_days"][row["stage"]]
    days_in_stage = (as_of - entered).days
    if days_in_stage > norm:
        return Violation("H6", MEDIUM,
                         f"{days_in_stage}d in {row['stage']} (norm {norm}d)")
    return None


def h7_single_threaded_big_deal(row, config, as_of):
    amount = row["amount"]
    if amount is not None and amount >= config["big_deal_threshold"] \
            and row["contact_count"] < 2:
        return Violation("H7", MEDIUM,
                         f"${amount:,.0f} deal with {row['contact_count']} contact(s)")
    return None


def h8_amount_hygiene(row, config, as_of):
    if row["amount"] is None or row["amount"] <= 0:
        return Violation("H8", LOW, f"amount is {row['amount']}")
    return None


def h9_vague_next_step(row, config, as_of):
    if not has_valid_next_step(row, as_of):
        return None
    quality = config["next_step_quality"]
    text = row["next_step"].strip()
    lowered = text.lower()
    if len(text) < quality["min_chars"]:
        return Violation("H9", LOW, f"next step too short ({len(text)} chars)")
    filler = next((p for p in quality["filler_phrases"] if p in lowered), None)
    if filler is not None:
        return Violation("H9", LOW, f"next step is filler ({filler!r})")
    if not any(v in lowered for v in quality["action_verbs"]):
        return Violation("H9", LOW, "next step has no action verb")
    return None


def h10_parked_close_date(row, config, as_of):
    horizon = config["close_date_horizon_days"]
    days_out = (row["close_date"] - as_of).days
    if days_out > horizon:
        severity = (MEDIUM if row["forecast_category"] in ("commit", "best_case")
                    else LOW)
        return Violation("H10", severity,
                         f"close date {days_out}d out (horizon {horizon}d)")
    return None


def h11_lost_deal_control(row, config, as_of):
    """Serial/LARGE pushes signal lost deal control (Gong n=13,439: movement
    per se is NOT risk — won deals update dates more than lost — so frequent
    small updates and pull-ins never trip this). Push stats are history-only
    derivations attached by the snapshot store; rows without them (single
    snapshot, in-memory uploads) have zero observed pushes and stay silent."""
    max_push = row.get("max_push_days")
    cumulative = row.get("cumulative_extension_days")
    if max_push is None or cumulative is None:
        return None
    big = max_push >= config["push_alarm_days"]
    if big or cumulative >= config["cumulative_push_alarm_days"]:
        why = (f"single push of {max_push}d "
               f"(alarm {config['push_alarm_days']}d)" if big
               else f"close date drifted {cumulative}d later across "
                    f"{row.get('push_count')} pushes "
                    f"(alarm {config['cumulative_push_alarm_days']}d)")
        return Violation("H11", HIGH, why)
    return None


RULES = {
    "H1": h1_stale_by_stage,
    "H2": h2_close_date_in_past,
    "H3": h3_serial_slippage,
    "H4": h4_missing_or_expired_next_step,
    "H5": h5_forecast_mismatch,
    "H6": h6_aging_in_stage,
    "H7": h7_single_threaded_big_deal,
    "H8": h8_amount_hygiene,
    "H9": h9_vague_next_step,
    "H10": h10_parked_close_date,
    "H11": h11_lost_deal_control,
}

RULE_LABELS = {
    "H1": "stale by stage",
    "H2": "close date in past",
    "H3": "serial slippage",
    "H4": "missing/expired next step",
    "H5": "forecast mismatch",
    "H6": "aging in stage",
    "H7": "single-threaded big deal",
    "H8": "amount hygiene",
    "H9": "vague next step",
    "H10": "parked close date",
    "H11": "lost deal control",
}


@dataclass(frozen=True)
class OppResult:
    opp_id: str
    violations: tuple      # of Violation
    insufficient: tuple    # of InsufficientHistory

    def rule_ids(self):
        return sorted(v.rule_id for v in self.violations)

    def has_high(self):
        return any(v.severity == HIGH for v in self.violations)


def evaluate_row(row, config, as_of):
    """Run all rules on one row. Closed opportunities yield an empty result."""
    if not is_open(row):
        return OppResult(row["opp_id"], (), ())
    violations, insufficient = [], []
    for rule in RULES.values():
        outcome = rule(row, config, as_of)
        if isinstance(outcome, Violation):
            violations.append(outcome)
        elif isinstance(outcome, InsufficientHistory):
            insufficient.append(outcome)
    return OppResult(row["opp_id"], tuple(violations), tuple(insufficient))


def evaluate_snapshot(rows, config, as_of):
    """Results for open opportunities only, keyed by opp_id."""
    return {row["opp_id"]: evaluate_row(row, config, as_of)
            for row in rows if is_open(row)}
