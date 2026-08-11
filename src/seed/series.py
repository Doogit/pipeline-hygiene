"""Longitudinal weekly snapshot series with a controlled delta script.

Between consecutive snapshots the script: clears X violations, introduces Y,
closes Z deals, pushes N close dates. All other open rows get maintenance
shifts (activity/next-step dates advance with the calendar) so their expected
violation sets are invariant by construction as the evaluation date moves.

Couplings are bookkept field-by-field, never by running the rules engine:
- a close-date push increments close_date_changes and may introduce H3;
- a scripted BIG push (single push >= push_alarm_days) introduces H11 (push
  derivations are history-only, so H11 can only ever appear from week 2 on);
  regular pushes are sized below push_alarm_days AND kept under the
  cumulative_push_alarm_days budget per opp, so they never trip H11;
- H11 is never clearable: push history is immutable once snapshotted;
- clearing is restricted to uncoupled rules {H1, H4 (only where H5 is not
  expected), H7, H8, H9} — H2/H3/H6/H10 cannot be cleared by editing history
  and H5 clears would couple to stage/next-step state;
- closing a deal removes the opp from rule scope; its violations are recorded
  under "closed", not "cleared".
"""
from datetime import timedelta

from .pathologies import generate_snapshot

CLEARS_PER_WEEK = 6
INTRODUCES_PER_WEEK = 5
CLOSES_PER_WEEK = 4
PUSHES_PER_WEEK = 5
BIG_PUSHES_PER_WEEK = 1

CLEARABLE = ("H1", "H4", "H7", "H8", "H9")

_CLEAN_STEPS = [
    "Send signed order form to procurement",
    "Schedule implementation kickoff with IT",
    "Review final pricing with the CFO office",
    "Confirm contract start date with legal",
]
_VAGUE_STEPS = [
    "Touch base after their team offsite",
    "Waiting on customer legal team feedback",
]


def _is_open(row):
    return row["stage"] not in ("closed_won", "closed_lost")


def _maintenance(rows, exp, rng):
    """Advance time-anchored clean fields by one week; violating fields age."""
    for opp_id in sorted(rows):
        row = rows[opp_id]
        if not _is_open(row):
            continue
        e = exp[opp_id]
        if "H1" not in e:
            row["last_activity_date"] += timedelta(days=7)
        has_valid_step = row["next_step"] and row["next_step_date"] is not None and "H4" not in e
        if has_valid_step:
            row["next_step_date"] += timedelta(days=7)


def _apply_clear(row, rule, d_next, org, config, rng):
    if rule == "H1":
        row["last_activity_date"] = d_next - timedelta(days=rng.randint(0, 3))
    elif rule == "H4":
        row["next_step"] = rng.choice(_CLEAN_STEPS)
        row["next_step_date"] = d_next + timedelta(days=rng.randint(1, 14))
    elif rule == "H9":
        row["next_step"] = rng.choice(_CLEAN_STEPS)
    elif rule == "H7":
        row["contact_count"] = rng.randint(2, 6)
    elif rule == "H8":
        from .org import sample_amount
        row["amount"] = sample_amount(rng, org.by_name(row["owner"]).segment)


def evolve_week(rows, exp, d_prev, d_next, d0, org, config, rng, pushed_days):
    """Mutate rows/exp from snapshot d_prev to d_next; return the delta record.

    pushed_days: per-opp cumulative later-drift (days) scripted so far across
    the series — the field-by-field book that keeps regular pushes under the
    H11 thresholds and makes scripted big pushes the only H11 source."""
    delta = {"from": d_prev.isoformat(), "to": d_next.isoformat(),
             "cleared": {}, "introduced": {}, "closed": {}, "close_dates_pushed": {}}
    _maintenance(rows, exp, rng)
    touched = set()
    horizon = config["close_date_horizon_days"]

    # 1) clear violations
    clear_candidates = []
    for opp_id in sorted(rows):
        if not _is_open(rows[opp_id]):
            continue
        for rule in sorted(exp[opp_id]):
            if rule in CLEARABLE and not (rule == "H4" and "H5" in exp[opp_id]):
                clear_candidates.append((opp_id, rule))
    for opp_id, rule in rng.sample(clear_candidates, min(CLEARS_PER_WEEK, len(clear_candidates))):
        _apply_clear(rows[opp_id], rule, d_next, org, config, rng)
        exp[opp_id].discard(rule)
        delta["cleared"].setdefault(opp_id, []).append(rule)
        touched.add(opp_id)

    # 2) introduce violations on previously clean open rows
    intro_candidates = [o for o in sorted(rows)
                        if _is_open(rows[o]) and not exp[o] and o not in touched]
    for opp_id in rng.sample(intro_candidates, min(INTRODUCES_PER_WEEK, len(intro_candidates))):
        row = rows[opp_id]
        feasible = ["H9"]
        if row["forecast_category"] != "commit":
            feasible.append("H4")
        feasible.append("H1")
        if row["amount"] is not None and row["amount"] >= config["big_deal_threshold"]:
            feasible.append("H7")
        rule = rng.choice(sorted(feasible))
        if rule == "H4":
            row["next_step"], row["next_step_date"] = "", None
        elif rule == "H9":
            row["next_step"] = rng.choice(_VAGUE_STEPS)
        elif rule == "H1":
            staleness = config["staleness_days"][row["stage"]]
            row["last_activity_date"] = d_next - timedelta(days=staleness + rng.randint(1, 15))
        elif rule == "H7":
            row["contact_count"] = rng.choice([0, 1])
        exp[opp_id].add(rule)
        delta["introduced"].setdefault(opp_id, []).append(rule)
        touched.add(opp_id)

    # 3) close deals (late-stage preferred). Persona-driven outcome skew:
    #    a happy-ears seller's commit-forecast deal loses more often than it
    #    wins — the behavioral pattern the Task 11 detector recovers from
    #    history alone (plant = persona field patterns, detect = statistics;
    #    the detector never sees the persona labels, so it stays
    #    non-circular).
    close_pool = [o for o in sorted(rows) if _is_open(rows[o]) and o not in touched]
    late = [o for o in close_pool if rows[o]["stage"] in ("propose", "commit")]
    picked = rng.sample(late, min(CLOSES_PER_WEEK, len(late)))
    if len(picked) < CLOSES_PER_WEEK:
        rest = [o for o in close_pool if o not in set(picked)]
        picked += rng.sample(rest, min(CLOSES_PER_WEEK - len(picked), len(rest)))
    for opp_id in picked:
        row = rows[opp_id]
        happy_commit = (row["forecast_category"] == "commit"
                        and org.by_name(row["owner"]).persona == "happy_ears")
        p_won = 0.25 if happy_commit else 0.6
        new_stage = "closed_won" if rng.random() < p_won else "closed_lost"
        if row["close_date"] != d_next:
            row["close_date_changes"] += 1
            row["close_date"] = d_next
        row["stage"] = new_stage
        delta["closed"][opp_id] = {"stage": new_stage,
                                   "violations_cleared": sorted(exp[opp_id])}
        exp[opp_id] = set()
        touched.add(opp_id)

    # 4) close-date pushes (never past d0+horizon, so no uncontrolled H10):
    #    one scripted BIG push (>= push_alarm_days -> introduces H11), then
    #    regular pushes sized below the single-push alarm and kept under the
    #    per-opp cumulative budget so they never trip H11 (Gong: frequent
    #    small updates are normal; serial/LARGE drift is the signal).
    cap = d0 + timedelta(days=horizon)
    alarm = config["push_alarm_days"]
    cum_alarm = config["cumulative_push_alarm_days"]

    def _record_push(opp_id, old, introduces_h11):
        row = rows[opp_id]
        row["close_date_changes"] += 1
        pushed_days[opp_id] = (pushed_days.get(opp_id, 0)
                               + (row["close_date"] - old).days)
        if row["close_date_changes"] >= 2 and "H3" not in exp[opp_id]:
            exp[opp_id].add("H3")
            delta["introduced"].setdefault(opp_id, []).append("H3")
        if introduces_h11 and "H11" not in exp[opp_id]:
            exp[opp_id].add("H11")
            delta["introduced"].setdefault(opp_id, []).append("H11")
        delta["close_dates_pushed"][opp_id] = {
            "old": old.isoformat(), "new": row["close_date"].isoformat(),
            "close_date_changes": row["close_date_changes"]}

    def _push_pool(exclude_h11_budget):
        return [o for o in sorted(rows)
                if _is_open(rows[o]) and o not in touched
                and "H2" not in exp[o] and "H10" not in exp[o]
                and "H11" not in exp[o]
                and rows[o]["close_date"] <= cap - timedelta(days=28)
                and (not exclude_h11_budget
                     or (cum_alarm - 1) - pushed_days.get(o, 0) >= 7)]

    big_pool = _push_pool(exclude_h11_budget=False)
    for opp_id in rng.sample(big_pool, min(BIG_PUSHES_PER_WEEK, len(big_pool))):
        row = rows[opp_id]
        old = row["close_date"]
        row["close_date"] = old + timedelta(days=alarm + rng.randint(0, 7))
        _record_push(opp_id, old, introduces_h11=True)
        touched.add(opp_id)

    # Regular pushes preferentially hit commit-forecast deals of happy-ears
    # sellers (same non-circular plant-vs-detect rationale as the close
    # skew above): a uniformly random push schedule carries zero per-persona
    # signal, which no series length could recover.
    push_pool = _push_pool(exclude_h11_budget=True)
    priority = [o for o in push_pool
                if rows[o]["forecast_category"] == "commit"
                and org.by_name(rows[o]["owner"]).persona == "happy_ears"]
    picked = rng.sample(priority, min(PUSHES_PER_WEEK, len(priority)))
    if len(picked) < PUSHES_PER_WEEK:
        rest = [o for o in push_pool if o not in set(picked)]
        picked += rng.sample(rest,
                             min(PUSHES_PER_WEEK - len(picked), len(rest)))
    for opp_id in picked:
        row = rows[opp_id]
        old = row["close_date"]
        headroom = min(alarm - 1, 28,
                       (cum_alarm - 1) - pushed_days.get(opp_id, 0))
        row["close_date"] = old + timedelta(days=rng.randint(7, headroom))
        _record_push(opp_id, old, introduces_h11=False)
    return delta


def generate_series(org, n_rows, weeks, as_of, config, rng):
    """Weekly snapshots ending at as_of. Returns (dates, snapshots, manifest_body)."""
    dates = [as_of - timedelta(days=7 * (weeks - 1 - i)) for i in range(weeks)]
    d0, d_last = dates[0], dates[-1]
    initial_rows, initial_expected = generate_snapshot(
        org, n_rows, d0, config, rng,
        min_close=d_last, entered_headroom=7 * (weeks - 1), h10_base=d_last)

    rows = {r["opp_id"]: dict(r) for r in initial_rows}
    order = [r["opp_id"] for r in initial_rows]
    exp = {o: set(v) for o, v in initial_expected.items()}

    snapshots = [(d0, [dict(rows[o]) for o in order])]
    expected_by_snapshot = {d0.isoformat(): {o: sorted(exp[o]) for o in order}}
    deltas = []
    pushed_days = {}
    for d_prev, d_next in zip(dates, dates[1:]):
        deltas.append(evolve_week(rows, exp, d_prev, d_next, d0, org, config,
                                  rng, pushed_days))
        snapshots.append((d_next, [dict(rows[o]) for o in order]))
        expected_by_snapshot[d_next.isoformat()] = {o: sorted(exp[o]) for o in order}

    manifest = {
        "snapshot_dates": [d.isoformat() for d in dates],
        "expected_by_snapshot": expected_by_snapshot,
        "deltas": deltas,
    }
    return dates, snapshots, manifest
