"""Desk brief generator: python -m src.brief --as-of YYYY-MM-DD

Writes out/desk_brief_<as_of>.md structured as forecast-call prep (the
weekly review's practitioner-standard agenda opens with hygiene of stalled/
pushed/stale deals). Page 1: headline, risky commits with deterministic
coaching prompts (questions to ask the seller — never gotchas), trajectory
(created-vs-closed flow + coverage vs remaining quota), since-last-run
summary, slipping pipeline. Everything else — fiscal quarters, exceptions,
owners, teams/regions, H5 detail — is drill-down under the Appendix header.

Every time evaluation takes the explicit as_of; date.today() appears only as
the CLI --as-of default. Since-last-run semantics match the seed delta
manifest: violations that vanish because a deal closed are reported under
"closed", never under "cleared".
"""
import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

from .ingest import load_config
from .patterns import commit_ledger, ledger_rollups, owner_patterns
from .rules import RULE_LABELS, evaluate_snapshot, is_open
from .scoring import (desk_rollup, fiscal_quarter, group_rollups, opp_score,
                      owner_rollups, required_coverage_multiple)
from .snapshots import SnapshotStore

_SEV_RANK = {"high": 0, "medium": 1, "low": 2}

# Rules that make a commit/best_case forecast "risky" on the forecast call.
RISKY_RULES = ("H1", "H2", "H4", "H5", "H7", "H11")

# Fixed, deterministic coaching prompts per dominant rule — phrased as
# questions to ask the seller (coaching, not interrogation; the H11 wording
# follows the research framing on re-confirming budget timelines).
COACHING_PROMPTS = {
    "H1": "When did the buyer last engage, and what interaction have they "
          "agreed to next?",
    "H2": "The close date has passed — what close date has the buyer "
          "actually committed to?",
    "H4": "What specific step did the buyer agree to take next, and by when?",
    "H5": "What buyer evidence supports commit while the deal is still "
          "early-stage or has no valid next step?",
    "H7": "Who beyond the single contact has confirmed budget and sign-off?",
    "H11": "Can you re-confirm the buyer's actual budget timeline before "
           "re-committing this deal?",
}

# Every rule gets a "what to do about it" ask for the private digest's coaching
# focus, so the focus line is never advice-free. RISKY_RULES reuse the
# forecast-call prompt above; the rest are covered here.
COACHING_ASKS = {
    **COACHING_PROMPTS,
    "H3": "The close date keeps moving — what has to be true for this date to "
          "hold?",
    "H6": "This deal has sat in stage past the norm — what is the next stage "
          "gate and when does it clear?",
    "H8": "The amount is missing or zero — what is the current expected deal "
          "value?",
    "H9": "The next step is vague — can you restate it as a specific action "
          "with a date?",
    "H10": "The close date is parked far out — is this a real timeline or a "
           "placeholder to revisit?",
}


def risky_commits(rows, results, config):
    """Open commit/best_case opps carrying any RISKY_RULES violation,
    dollar-ranked, each with the coaching prompt of its dominant rule
    (deterministic: worst severity, then heaviest weight, then rule number)."""
    weights = config["rule_weights"]
    entries = []
    for row in rows:
        if not is_open(row) \
                or row["forecast_category"] not in ("commit", "best_case"):
            continue
        result = results[row["opp_id"]]
        risky = [v for v in result.violations if v.rule_id in RISKY_RULES]
        if not risky:
            continue
        dominant = min(risky, key=lambda v: (_SEV_RANK[v.severity],
                                             -weights[v.rule_id],
                                             int(v.rule_id[1:])))
        entries.append({"row": row, "result": result,
                        "dominant": dominant.rule_id,
                        "prompt": COACHING_PROMPTS[dominant.rule_id],
                        "risky_rules": [v.rule_id for v in risky]})
    entries.sort(key=lambda e: (-(e["row"]["amount"] or 0.0),
                                e["row"]["opp_id"]))
    return entries


def trajectory_data(rows, delta, config, as_of, outcomes, multiple=None,
                    basis=None):
    """Created-vs-closed flow since last run plus coverage vs remaining
    quota. Required multiple = 1 / trailing win rate from stored closed
    outcomes; falls back to config coverage_ratio_min when closed history is
    insufficient. The basis used is always recorded verbatim. Callers that
    already hold the desk-wide (multiple, basis) pass them in — an
    owner-filtered brief restricts outcomes to the selection for
    won-this-quarter, but the multiple must stay desk-wide (one coverage
    basis everywhere)."""
    rows_by_id = {r["opp_id"]: r for r in rows}

    def dollars(opp_ids):
        return sum(rows_by_id[o]["amount"] or 0.0
                   for o in opp_ids if o in rows_by_id)

    flow = None
    if delta is not None:
        won = sorted(o for o, i in delta["closed"].items()
                     if i["stage"] == "closed_won")
        lost = sorted(o for o, i in delta["closed"].items()
                      if i["stage"] == "closed_lost")
        flow = {"created_n": len(delta["added"]),
                "created_dollars": dollars(delta["added"]),
                "won_n": len(won), "won_dollars": dollars(won),
                "lost_n": len(lost), "lost_dollars": dollars(lost)}

    open_pipeline = sum(r["amount"] or 0.0 for r in rows if is_open(r))
    coverage = None
    total_quota = sum((config.get("quotas") or {}).values())
    if total_quota > 0:
        outcomes = outcomes or []
        won_out = [o for o in outcomes if o["stage"] == "closed_won"]
        if multiple is None:
            multiple, basis = required_coverage_multiple(outcomes, config)
        fy_start = config["fiscal_year_start_month"]
        quarter = fiscal_quarter(as_of, fy_start)
        won_this_quarter = sum(
            o["amount"] or 0.0 for o in won_out
            if o["close_date"] is not None
            and fiscal_quarter(o["close_date"], fy_start) == quarter)
        remaining_quota = max(total_quota - won_this_quarter, 0.0)
        required = remaining_quota * multiple
        coverage = {"quarter": quarter, "total_quota": total_quota,
                    "won_this_quarter": won_this_quarter,
                    "remaining_quota": remaining_quota,
                    "required_multiple": multiple,
                    "required_pipeline": required,
                    "ratio": (open_pipeline / required) if required > 0
                    else None,
                    "basis": basis}
    return {"flow": flow, "open_pipeline": open_pipeline,
            "coverage": coverage}


def _money(amount):
    return "n/a" if amount is None else f"${amount:,.0f}"


def merge_quota_payload(config, payload):
    """Return config with quotas and optional owner metadata merged in.

    Supports both the seed manifest shape:
    {"quotas": {...}, "owners": {...}}
    and the older bare quota mapping:
    {"Owner Name": 900000}
    """
    merged = dict(config)
    merged["quotas"] = {**(config.get("quotas") or {}),
                        **payload.get("quotas", payload)}
    owners_block = payload.get("owners") or {}
    merged["owner_meta"] = {
        **(config.get("owner_meta") or {}),
        **{name: {"team": m.get("team"), "region": m.get("region")}
           for name, m in owners_block.items()}}
    return merged


def run_summary(snapshot_date, as_of, desk, results):
    """Compact per-run record stored in the runs table; the next run diffs
    against it, so it carries the full open-opp rule sets."""
    return {
        "snapshot_date": snapshot_date.isoformat(),
        "as_of": as_of.isoformat(),
        "desk": {
            "n_open": desk.n_open,
            "weighted_mean_score": desk.weighted_mean_score,
            "median_score": desk.median_score,
            "pct_healthy": desk.pct_healthy,
            "at_risk_dollars": desk.at_risk_dollars,
        },
        "open": {opp_id: results[opp_id].rule_ids() for opp_id in sorted(results)},
    }


def since_last_run(prev_summary, rows, results, desk):
    """Diff current engine results against the previous run's stored summary.
    Returns None when there is no previous run."""
    if prev_summary is None:
        return None
    prev_open = prev_summary["open"]
    cur_rows = {r["opp_id"]: r for r in rows}
    cur_open = {opp_id: results[opp_id].rule_ids() for opp_id in results}

    delta = {
        "prev_snapshot_date": prev_summary["snapshot_date"],
        "prev_as_of": prev_summary["as_of"],
        "new_violations": {},
        "cleared_violations": {},
        "closed": {},
        "added": sorted(o for o in cur_open if o not in prev_open),
        "removed": sorted(o for o in prev_open if o not in cur_rows),
        "score_change": {
            "prev": prev_summary["desk"]["weighted_mean_score"],
            "current": desk.weighted_mean_score,
        },
    }
    for opp_id in sorted(prev_open):
        row = cur_rows.get(opp_id)
        if row is None:
            continue
        if not is_open(row):
            delta["closed"][opp_id] = {
                "stage": row["stage"],
                "violations_cleared": sorted(prev_open[opp_id]),
            }
            continue
        prev_rules, cur_rules = set(prev_open[opp_id]), set(cur_open.get(opp_id, []))
        if cur_rules - prev_rules:
            delta["new_violations"][opp_id] = sorted(cur_rules - prev_rules)
        if prev_rules - cur_rules:
            delta["cleared_violations"][opp_id] = sorted(prev_rules - cur_rules)
    return delta


def flag_streaks(prev_opens, results):
    """Consecutive-run streak per currently flagged (opp_id, rule): 1 for the
    current evaluation plus how many immediately preceding recorded runs also
    carried the flag. A cleared run breaks the streak, so re-flagging starts
    over at 1."""
    streaks = {}
    for opp_id, result in results.items():
        for rule in result.rule_ids():
            n = 1
            for open_map in reversed(prev_opens):
                if rule not in open_map.get(opp_id, []):
                    break
                n += 1
            streaks[(opp_id, rule)] = n
    return streaks


def opp_streak(streaks, result):
    """Longest current streak across an opp's flags (0 when unflagged)."""
    return max((streaks.get((result.opp_id, rule), 1)
                for rule in result.rule_ids()), default=0)


def build(store, snapshot_date, as_of, config, owner_filter=None,
          filter_label=None):
    """Compute all brief data for one stored snapshot. No side effects."""
    prev = store.last_run_before_snapshot(snapshot_date)
    return build_from_rows(
        store.rows_with_history(snapshot_date), snapshot_date, as_of, config,
        validation=store.validation_report_dict(snapshot_date),
        prev_summary=prev["summary"] if prev else None,
        outcomes=store.closed_outcomes(snapshot_date),
        prev_opens=store.run_opens(before_snapshot_date=snapshot_date),
        patterns=owner_patterns(store, as_of, config, snapshot_date),
        ledger=commit_ledger(store, as_of, config, snapshot_date),
        owner_filter=owner_filter, filter_label=filter_label)


def build_from_rows(rows, snapshot_date, as_of, config,
                    validation=None, prev_summary=None, outcomes=None,
                    prev_opens=None, patterns=None, ledger=None,
                    owner_filter=None, filter_label=None):
    """Compute all brief data from in-memory rows (e.g. a validated upload).
    outcomes: stored closed outcomes (store.closed_outcomes) for the
    trailing-win-rate coverage basis; None outside the store. prev_opens:
    prior runs' rule-set maps (store.run_opens) for flag streaks. patterns:
    per-owner forecast-integrity patterns (patterns.owner_patterns); None
    outside the store. ledger: forecast-accuracy entries
    (patterns.commit_ledger); None outside the store.

    owner_filter (a set of owner names) restricts the whole brief to those
    owners: rows, quotas, owner_meta, outcomes, patterns and ledger are cut
    to the selection, so every rollup and dollar figure is recomputed over
    it — EXCEPT the required coverage multiple, which stays desk-wide (one
    coverage basis everywhere; a team's filtered coverage must equal its row
    in the unfiltered Teams table). The previous run's desk score is dropped
    (it was desk-wide, not comparable) and opps that left the snapshot
    cannot be owner-attributed, so a filtered "removed" list is always
    empty."""
    # Desk-wide multiple/basis BEFORE any filtering (the one-basis rule).
    multiple, basis = required_coverage_multiple(outcomes, config)
    if owner_filter is not None:
        rows = [r for r in rows if r["owner"] in owner_filter]
        config = dict(config)
        config["quotas"] = {o: q for o, q in (config.get("quotas") or {}).items()
                            if o in owner_filter}
        config["owner_meta"] = {o: m for o, m
                                in (config.get("owner_meta") or {}).items()
                                if o in owner_filter}
        if outcomes is not None:
            outcomes = [o for o in outcomes if o["owner"] in owner_filter]
        if patterns is not None:
            patterns = {o: p for o, p in patterns.items() if o in owner_filter}
        if ledger is not None:
            ledger = [e for e in ledger if e.owner in owner_filter]
        if prev_summary is not None:
            kept = {r["opp_id"] for r in rows}
            prev_summary = {
                "snapshot_date": prev_summary["snapshot_date"],
                "as_of": prev_summary["as_of"],
                "desk": {"weighted_mean_score": None},
                "open": {o: v for o, v in prev_summary["open"].items()
                         if o in kept},
            }
    results = evaluate_snapshot(rows, config, as_of)
    desk = desk_rollup(rows, results, config)
    insufficient = {"H3": 0, "H6": 0}
    for result in results.values():
        for item in result.insufficient:
            insufficient[item.rule_id] += 1
    delta = since_last_run(prev_summary, rows, results, desk)

    # Shared coverage basis: the win-rate-derived multiple (computed above,
    # before filtering) plus each owner's won-this-quarter dollars, so owner
    # and team low_coverage flags use the same remaining-quota math as the
    # desk headline (not the static floor).
    fy_start = config["fiscal_year_start_month"]
    quarter = fiscal_quarter(as_of, fy_start)
    won_by_owner = {}
    for o in (outcomes or []):
        if o["stage"] == "closed_won" and o["close_date"] is not None \
                and fiscal_quarter(o["close_date"], fy_start) == quarter:
            won_by_owner[o["owner"]] = (won_by_owner.get(o["owner"], 0.0)
                                        + (o["amount"] or 0.0))
    owner_meta = config.get("owner_meta") or {}
    return {
        "snapshot_date": snapshot_date,
        "as_of": as_of,
        "rows": rows,
        "results": results,
        "desk": desk,
        "owners": owner_rollups(rows, results, config, multiple, won_by_owner),
        "teams": group_rollups(rows, results, config, owner_meta, "team",
                               multiple, won_by_owner),
        "regions": group_rollups(rows, results, config, owner_meta, "region",
                                 multiple, won_by_owner),
        "coverage_multiple": multiple,
        "coverage_basis": basis,
        "validation": validation,
        "insufficient": insufficient,
        "since_last_run": delta,
        "risky_commits": risky_commits(rows, results, config),
        "trajectory": trajectory_data(rows, delta, config, as_of, outcomes,
                                      multiple, basis),
        "streaks": flag_streaks(prev_opens or [], results),
        "patterns": patterns,
        "ledger": ledger,
        "filter_label": filter_label,
        "summary": run_summary(snapshot_date, as_of, desk, results),
    }


# --- rendering ---

def _headline(lines, data, config):
    desk = data["desk"]
    lines.append("## Headline")
    lines.append("")
    if desk.n_open == 0:
        lines.append("- No open opportunities in this snapshot.")
    else:
        lines.append(f"- Desk score: {desk.weighted_mean_score:.1f} amount-weighted "
                     f"mean / {desk.median_score:.1f} median")
        lines.append(f"- Healthy opps (score >= "
                     f"{config['healthy_score_threshold']}): {desk.pct_healthy:.1f}% "
                     f"of {desk.n_open} open")
        open_pipeline = sum(r["amount"] or 0.0 for r in data["rows"] if is_open(r))
        lines.append(f"- Open pipeline: {_money(open_pipeline)}")
        lines.append(f"- At-risk dollars (distinct opps with a high-severity "
                     f"violation): {_money(desk.at_risk_dollars)}")
    counts = desk.violation_counts_by_severity
    lines.append(f"- Violations: {counts['high']} high, {counts['medium']} medium, "
                 f"{counts['low']} low")
    insufficient = data["insufficient"]
    if any(insufficient.values()):
        detail = ", ".join(f"{rule} on {n} opps"
                           for rule, n in sorted(insufficient.items()) if n)
        lines.append(f"- Insufficient history: {detail} "
                     f"(reported, not counted as violations)")
    else:
        lines.append("- Insufficient history: none")

    lines.append("")
    lines.append("### Validation")
    lines.append("")
    validation = data["validation"]
    if validation is None:
        lines.append("- No validation report stored for this snapshot.")
        return
    source = Path(validation["source_file"]).name
    lines.append(f"- Source `{source}`: accepted {validation['accepted']}/"
                 f"{validation['total_rows']} rows, rejected {validation['rejected']}")
    reasons = list(validation["reason_counts"].items())[:5]
    if reasons:
        lines.append("- Top rejection reasons: "
                     + ", ".join(f"{reason} ({n})" for reason, n in reasons))
    for warning in validation["warnings"]:
        lines.append(f"- Warning: {warning}")


def _risky_commits(lines, data, config):
    lines.append("## Risky commits")
    lines.append("")
    entries = data["risky_commits"]
    if not entries:
        lines.append("No commit/best_case opp carries a risk flag "
                     f"({', '.join(RISKY_RULES)}).")
        return
    total = sum(e["row"]["amount"] or 0.0 for e in entries)
    lines.append(f"{len(entries)} commit/best_case opps carry a risk flag — "
                 f"{_money(total)} (distinct opps), dollar-ranked"
                 + (", top 10 shown" if len(entries) > 10 else "")
                 + ". Coaching prompts, not gotchas.")
    lines.append("")
    lines.append("| # | Opp | Account | Owner | Stage | Amount | Forecast "
                 "| Flags | Ask the seller |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for i, entry in enumerate(entries[:10], start=1):
        row = entry["row"]
        lines.append(f"| {i} | {row['opp_id']} | {row['account']} "
                     f"| {row['owner']} "
                     f"| {row['stage']} | {_money(row['amount'])} "
                     f"| {row['forecast_category']} "
                     f"| {' '.join(entry['risky_rules'])} "
                     f"| {entry['prompt']} |")


def _trajectory(lines, data):
    lines.append("## Trajectory")
    lines.append("")
    trajectory = data["trajectory"]
    flow = trajectory["flow"]
    if flow is None:
        lines.append("- Flow since last run: no previous run recorded.")
    else:
        lines.append(f"- Flow since last run: {flow['created_n']} created "
                     f"({_money(flow['created_dollars'])}) vs "
                     f"{flow['won_n'] + flow['lost_n']} closed "
                     f"({flow['won_n']} won {_money(flow['won_dollars'])}, "
                     f"{flow['lost_n']} lost {_money(flow['lost_dollars'])})")
    coverage = trajectory["coverage"]
    if coverage is None:
        lines.append("- Coverage: no quotas configured.")
        return
    ratio = ("n/a" if coverage["ratio"] is None
             else f"{coverage['ratio']:.2f}x")
    lines.append(f"- Coverage ({coverage['quarter']}): open pipeline "
                 f"{_money(trajectory['open_pipeline'])} vs required "
                 f"{_money(coverage['required_pipeline'])} -> {ratio}")
    lines.append(f"  - Remaining quota {_money(coverage['remaining_quota'])} "
                 f"(quota {_money(coverage['total_quota'])} - won this "
                 f"quarter {_money(coverage['won_this_quarter'])}) x "
                 f"{coverage['required_multiple']:.1f}")
    lines.append(f"  - Basis: {coverage['basis']}")


def _since_last_run_summary(lines, data):
    lines.append("## Since last run")
    lines.append("")
    delta = data["since_last_run"]
    if delta is None:
        lines.append("No previous run recorded.")
        return
    lines.append(f"Previous run: snapshot {delta['prev_snapshot_date']}, "
                 f"as of {delta['prev_as_of']}. Per-opp detail in the "
                 f"appendix.")
    lines.append("")
    change = delta["score_change"]
    if change["prev"] is not None and change["current"] is not None:
        lines.append(f"- Desk score (weighted mean): {change['prev']:.1f} -> "
                     f"{change['current']:.1f} "
                     f"({change['current'] - change['prev']:+.1f})")
    new = delta["new_violations"]
    cleared = delta["cleared_violations"]
    lines.append(f"- New violations: {sum(len(v) for v in new.values())} on "
                 f"{len(new)} opps; cleared: "
                 f"{sum(len(v) for v in cleared.values())} on "
                 f"{len(cleared)} opps")
    lines.append(f"- Closed: {len(delta['closed'])} opps; added: "
                 f"{len(delta['added'])}; removed: {len(delta['removed'])}")


def _since_last_run_detail(lines, data):
    lines.append("### Since last run (detail)")
    lines.append("")
    delta = data["since_last_run"]
    if delta is None:
        lines.append("No previous run recorded.")
        return
    rows_by_id = {r["opp_id"]: r for r in data["rows"]}

    def _who(opp_id):
        row = rows_by_id.get(opp_id)
        return f" {row['account']} — {row['owner']}" if row else ""

    for label, key in (("New violations", "new_violations"),
                       ("Cleared violations", "cleared_violations")):
        opps = delta[key]
        total = sum(len(v) for v in opps.values())
        lines.append(f"- {label}: {total} on {len(opps)} opps")
        for opp_id in sorted(opps):
            lines.append(f"  - {opp_id}{_who(opp_id)}: "
                         f"{', '.join(opps[opp_id])}")
    lines.append(f"- Closed: {len(delta['closed'])} opps")
    for opp_id in sorted(delta["closed"]):
        info = delta["closed"][opp_id]
        cleared = (f" (violations cleared: {', '.join(info['violations_cleared'])})"
                   if info["violations_cleared"] else "")
        lines.append(f"  - {opp_id}: {info['stage']}, "
                     f"{_money(rows_by_id[opp_id]['amount'])}{cleared}")
    lines.append(f"- Opps added: {', '.join(delta['added']) or 'none'}")
    lines.append(f"- Opps removed: {', '.join(delta['removed']) or 'none'}")


def _slipping(lines, data, config):
    """Dollar-weighted list of open opps with observed close-date pushes.

    Push stats are history-only derivations from the snapshot store; rows
    evaluated outside the store (in-memory uploads) carry none. Distinct-opp
    dollar totals — same discipline as at-risk dollars."""
    lines.append("## Slipping pipeline")
    lines.append("")
    open_rows = [r for r in data["rows"] if is_open(r)]
    if not any(r.get("push_count") is not None for r in open_rows):
        lines.append("No push history available (rows evaluated outside the "
                     "snapshot store).")
        return
    slipping = [r for r in open_rows if (r.get("push_count") or 0) >= 1]
    if not slipping:
        lines.append("No close-date pushes observed in stored history.")
        return
    slipping.sort(key=lambda r: (-(r["amount"] or 0.0), r["opp_id"]))
    total = sum(r["amount"] or 0.0 for r in slipping)
    lines.append(f"Slipping dollars (distinct opps with >= 1 observed push): "
                 f"{_money(total)} across {len(slipping)} opps.")
    lines.append("")
    lines.append("| Opp | Owner | Stage | Amount | Pushes | Cum. days later "
                 "| Max push | Rules | Review |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for row in slipping:
        result = data["results"][row["opp_id"]]
        badges = " ".join(v.rule_id for v in result.violations) or "-"
        review = ("recommend disqualification review"
                  if row["push_count"] >= config["disqualify_review_pushes"]
                  else "-")
        lines.append(f"| {row['opp_id']} | {row['owner']} | {row['stage']} "
                     f"| {_money(row['amount'])} | {row['push_count']} "
                     f"| {row['cumulative_extension_days']} "
                     f"| {row['max_push_days']} | {badges} | {review} |")


def _fiscal_quarters(lines, data, config):
    fy_start = config["fiscal_year_start_month"]
    lines.append(f"### Fiscal quarters (fiscal year starts month {fy_start})")
    lines.append("")
    buckets = {}
    for row in data["rows"]:
        if not is_open(row):
            continue
        result = data["results"][row["opp_id"]]
        h5 = any(v.rule_id == "H5" for v in result.violations)
        if not result.has_high() and not h5:
            continue
        bucket = buckets.setdefault(fiscal_quarter(row["close_date"], fy_start),
                                    {"at_risk": 0.0, "n_at_risk": 0, "h5": []})
        if result.has_high():
            bucket["at_risk"] += row["amount"] or 0.0
            bucket["n_at_risk"] += 1
        if h5:
            bucket["h5"].append(row["opp_id"])
    if not buckets:
        lines.append("No at-risk dollars or forecast mismatches.")
        return
    lines.append("| Quarter | At-risk $ | At-risk opps | H5 opps |")
    lines.append("|---|---|---|---|")
    for quarter in sorted(buckets):
        bucket = buckets[quarter]
        h5_cell = ", ".join(sorted(bucket["h5"])) or "none"
        lines.append(f"| {quarter} | {_money(bucket['at_risk'])} "
                     f"| {bucket['n_at_risk']} | {h5_cell} |")


def _top_exceptions(lines, data, config):
    lines.append("### Top 10 exceptions")
    lines.append("")
    rows_by_id = {r["opp_id"]: r for r in data["rows"]}
    flagged = [r for r in data["results"].values() if r.violations]
    if not flagged:
        lines.append("No violations.")
        return

    def rank(result):
        worst = min(_SEV_RANK[v.severity] for v in result.violations)
        return (worst, -(rows_by_id[result.opp_id]["amount"] or 0.0), result.opp_id)

    lines.append("| # | Opp | Account | Owner | Stage | Amount | Score | Rules | Streak | Detail |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for i, result in enumerate(sorted(flagged, key=rank)[:10], start=1):
        row = rows_by_id[result.opp_id]
        badges = " ".join(v.rule_id for v in result.violations)
        detail = "; ".join(v.detail for v in result.violations)
        streak = opp_streak(data["streaks"], result)
        streak_cell = f"flagged {streak} runs" if streak >= 2 else "-"
        lines.append(f"| {i} | {result.opp_id} | {row['account']} | {row['owner']} "
                     f"| {row['stage']} | {_money(row['amount'])} "
                     f"| {opp_score(result, config)} | {badges} "
                     f"| {streak_cell} | {detail} |")


def _coverage_note(lines, data):
    """The coverage definition and basis, printed adjacent to every table that
    carries a Coverage column (the persona pass found the note 67 lines from
    the Owners table it explained)."""
    lines.append("Coverage = open pipeline vs required pipeline (remaining "
                 "quota net of wins this quarter x the required multiple); "
                 "low_coverage means under 1.00x. Basis: "
                 f"{data['coverage_basis']}.")


def _owner_table(lines, data):
    lines.append("### Owners")
    lines.append("")
    owners = data["owners"]
    if not owners:
        lines.append("No open opportunities.")
        return
    _coverage_note(lines, data)
    lines.append("")
    lines.append("| Owner | Open | Mean | Median | Violations | Pipeline | Coverage | Flags |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for stats in owners.values():
        coverage = ("n/a" if stats.coverage_ratio is None
                    else f"{stats.coverage_ratio:.2f}")
        flags = ", ".join(f for f, on in (("small_n", stats.small_n),
                                          ("low_coverage", stats.coverage_flagged))
                          if on) or "-"
        lines.append(f"| {stats.owner} | {stats.n_open} | {stats.mean_score:.1f} "
                     f"| {stats.median_score:.1f} | {stats.violation_count} "
                     f"| {_money(stats.open_pipeline)} | {coverage} | {flags} |")


def _group_table(lines, groups, label):
    """Render one team/region roll-up table, worst coverage first so ordering
    itself carries the signal (empty groups produce a note)."""
    lines.append(f"### {label}s")
    lines.append("")
    if not groups:
        lines.append("No team/region metadata configured "
                     "(pass --quotas data/seed_manifest.json).")
        return
    lines.append(f"| {label} | Owners | Open | Mean | Pipeline | Quota "
                 "| Coverage | Violations | At-risk $ | Flags |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    ranked = sorted(groups.values(),
                    key=lambda g: (g.coverage_ratio is None,
                                   g.coverage_ratio or 0.0, g.key))
    for g in ranked:
        coverage = ("n/a" if g.coverage_ratio is None
                    else f"{g.coverage_ratio:.2f}")
        mean_cell = "n/a" if g.mean_score is None else f"{g.mean_score:.1f}"
        flags = "low_coverage" if g.coverage_flagged else "-"
        lines.append(f"| {g.key} | {g.n_owners} | {g.n_open} | {mean_cell} "
                     f"| {_money(g.open_pipeline)} | {_money(g.quota)} "
                     f"| {coverage} | {g.violation_count} "
                     f"| {_money(g.at_risk_dollars)} | {flags} |")


def _team_region_tables(lines, data):
    lines.append("### Teams and regions")
    lines.append("")
    _coverage_note(lines, data)
    lines.append("")
    _group_table(lines, data["teams"], "Team")
    lines.append("")
    _group_table(lines, data["regions"], "Region")


def _forecast_integrity(lines, data):
    lines.append("### Forecast integrity (H5)")
    lines.append("")
    rows_by_id = {r["opp_id"]: r for r in data["rows"]}
    entries = []
    for result in data["results"].values():
        for violation in result.violations:
            if violation.rule_id == "H5":
                entries.append((rows_by_id[result.opp_id], violation))
    if not entries:
        lines.append("No forecast mismatches.")
        return
    entries.sort(key=lambda e: (-(e[0]["amount"] or 0.0), e[0]["opp_id"]))
    for row, violation in entries:
        lines.append(f"- {row['opp_id']} — {row['owner']}, stage {row['stage']}, "
                     f"{_money(row['amount'])}: {violation.detail}")


def _forecast_patterns(lines, data):
    lines.append("#### Forecast integrity patterns")
    lines.append("")
    lines.append("Coaching signal, not a comp input.")
    lines.append("")
    patterns = data["patterns"]
    if patterns is None:
        lines.append("Unavailable outside the snapshot store.")
        return
    over = [p for p in patterns.values() if p.overcall_flagged]
    under = [p for p in patterns.values() if p.undercall_flagged]
    if not over and not under:
        lines.append("No overcall/undercall patterns flagged.")
    for p in over:
        lines.append(f"- Overcall pattern: {p.owner} — "
                     f"{p.overcall_share:.0%} of {p.n_ever_commit} "
                     f"ever-commit opps later pushed or lost")
    def pct(value):
        return "n/a" if value is None else f"{value:.0%}"

    for p in under:
        # Lead with the evidence actually present; only cite the won-share
        # basis when there are observed wins (printing "n/a (n=0)" beside a
        # name reads as an accusation with no evidence).
        won_clause = (f"wins never called commit/best_case "
                      f"{pct(p.undercall_won_share)} (n={p.n_won}); "
                      if p.n_won else "")
        lines.append(f"- Undercall pattern: {p.owner} — {won_clause}open "
                     f"pipeline {pct(p.omitted_share)} omitted, "
                     f"{pct(p.farout_share)} far-out (n={p.n_open})")
    small = sum(1 for p in patterns.values()
                if p.overcall_small_n and p.undercall_small_n)
    lines.append(f"- Suppressed as small_n: {small} owners with too little "
                 f"history to score")


def _commit_ledger(lines, data, config):
    """Forecast-accuracy ledger tables: by owner, by team (when owner_meta
    exists), by committed-for quarter. Counts and one derived rate; keys
    sorted alphabetically/chronologically — deliberately NOT worst-first
    (an accuracy leaderboard is one sort away from a comp weapon)."""
    lines.append("### Commit accuracy (forecast ledger)")
    lines.append("")
    lines.append("Of opps ever forecast commit in stored history: outcome to "
                 "date. Pushed = still open with a close-date move after the "
                 "first commit snapshot; committed-for quarter = fiscal "
                 "quarter of the close date when first called commit (immune "
                 "to later pushes). Won/resolved counts closed opps only. "
                 "Coaching signal, not a comp input.")
    lines.append("")
    entries = data["ledger"]
    if entries is None:
        lines.append("Unavailable outside the snapshot store.")
        return
    if not entries:
        lines.append("No opp in stored history has ever been forecast "
                     "commit.")
        return

    def table(label, rollups):
        lines.append(f"| {label} | Ever commit | Won | Lost | Pushed "
                     "| Still open | Won/resolved |")
        lines.append("|---|---|---|---|---|---|---|")
        for key in sorted(rollups, key=lambda k: (k is None, k or "")):
            g = rollups[key]
            resolved = g["won"] + g["lost"]
            rate = f"{g['won'] / resolved:.0%}" if resolved else "n/a"
            lines.append(f"| {'unknown' if key is None else key} | {g['n']} "
                         f"| {g['won']} | {g['lost']} | {g['pushed']} "
                         f"| {g['open']} | {rate} |")

    table("Owner", ledger_rollups(entries, lambda e: e.owner))
    owner_meta = config.get("owner_meta") or {}
    if owner_meta:
        lines.append("")
        table("Team", ledger_rollups(
            entries, lambda e: (owner_meta.get(e.owner) or {}).get("team")))
    lines.append("")
    table("Committed-for quarter",
          ledger_rollups(entries, lambda e: e.committed_quarter))


def _rule_legend(lines, config):
    lines.append("### Rule legend")
    lines.append("")
    lines.append("| Rule | Meaning | Score weight |")
    lines.append("|---|---|---|")
    weights = config["rule_weights"]
    for rule_id, label in RULE_LABELS.items():
        lines.append(f"| {rule_id} | {label} | -{weights.get(rule_id, 0)} |")


def render(data, config):
    validation = data["validation"]
    source = (f" ({Path(validation['source_file']).name})" if validation else "")
    lines = [
        f"# Desk Brief — {data['as_of'].isoformat()}",
        "",
        f"Snapshot {data['snapshot_date'].isoformat()}{source}, "
        f"evaluated as of {data['as_of'].isoformat()}.",
        "",
    ]
    if data.get("filter_label"):
        lines.append(f"FILTERED BRIEF — {data['filter_label']}. All numbers "
                     "cover only this selection (the coverage basis stays "
                     "desk-wide); opps that left the snapshot are not "
                     "attributable to a selection and are omitted from "
                     "\"removed\". Desk-wide context lives in the unfiltered "
                     "brief; filtered runs are never recorded.")
        lines.append("")
    _headline(lines, data, config)
    lines.append("")
    _risky_commits(lines, data, config)
    lines.append("")
    _trajectory(lines, data)
    lines.append("")
    _since_last_run_summary(lines, data)
    lines.append("")
    _slipping(lines, data, config)
    lines.append("")
    lines.append("## Appendix")
    lines.append("")
    lines.append("Drill-down detail. The forecast call runs off page 1.")
    lines.append("")
    _fiscal_quarters(lines, data, config)
    lines.append("")
    _top_exceptions(lines, data, config)
    lines.append("")
    _owner_table(lines, data)
    lines.append("")
    _team_region_tables(lines, data)
    lines.append("")
    _forecast_integrity(lines, data)
    lines.append("")
    _forecast_patterns(lines, data)
    lines.append("")
    _commit_ledger(lines, data, config)
    lines.append("")
    _since_last_run_detail(lines, data)
    lines.append("")
    _rule_legend(lines, config)
    lines.append("")
    return "\n".join(lines)


# --- private per-owner coaching digests ---

def owner_slug(owner):
    slug = re.sub(r"[^a-z0-9]+", "-", owner.lower()).strip("-")
    return slug or "owner"


def digest_markdown(data, owner, config):
    """One PRIVATE coaching digest: only this owner's deals, no rankings, no
    cross-owner data (coaching moves sellers; published rankings raise
    attrition). Deterministic render of the brief data."""
    results, streaks = data["results"], data["streaks"]
    stats = data["owners"][owner]
    owned_ids = {r["opp_id"] for r in data["rows"] if r["owner"] == owner}
    open_rows = [r for r in data["rows"]
                 if r["owner"] == owner and is_open(r)]
    lines = [f"# Coaching digest — {owner} — {data['as_of'].isoformat()}",
             "",
             "Private: covers only this seller's deals. Coaching input — "
             "never a scorecard, never a comp input.",
             ""]
    if stats.small_n:
        lines.append(f"Note: only {stats.n_open} open opps (small_n) — treat "
                     f"patterns as anecdotal.")
        lines.append("")

    lines.append("## Top risks (dollar-weighted)")
    lines.append("")
    flagged = [r for r in open_rows if results[r["opp_id"]].violations]
    if not flagged:
        lines.append("No flagged deals this week.")
    flagged.sort(key=lambda r: (-(r["amount"] or 0.0), r["opp_id"]))
    for row in flagged[:5]:
        result = results[row["opp_id"]]
        streak = opp_streak(streaks, result)
        note = f" — flagged {streak} runs" if streak >= 2 else ""
        # Show the observed value and threshold per flag (from the engine's
        # own detail strings), so "why" is a checkable fact, not a bare code.
        evidence = "; ".join(f"{v.rule_id} ({RULE_LABELS[v.rule_id]}: "
                             f"{v.detail})" for v in result.violations)
        lines.append(f"- {row['opp_id']} {row['account']} ({row['stage']}, "
                     f"{_money(row['amount'])}): {evidence}{note}")

    lines.append("")
    lines.append("## Week over week")
    lines.append("")
    delta = data["since_last_run"]
    if delta is None:
        lines.append("No previous run recorded.")
    else:
        for label, key in (("New flags", "new_violations"),
                           ("Cleared", "cleared_violations")):
            owned = {o: v for o, v in delta[key].items() if o in owned_ids}
            detail = "; ".join(f"{o} ({', '.join(v)})"
                               for o, v in sorted(owned.items()))
            lines.append(f"- {label}: {sum(map(len, owned.values()))}"
                         + (f" — {detail}" if detail else ""))
        closed = {o: i for o, i in delta["closed"].items() if o in owned_ids}
        won = sorted(o for o, i in closed.items()
                     if i["stage"] == "closed_won")
        lost = sorted(o for o in closed if o not in won)
        lines.append(f"- Closed: {len(won)} won"
                     + (f" ({', '.join(won)})" if won else "")
                     + f", {len(lost)} lost"
                     + (f" ({', '.join(lost)})" if lost else ""))

    lines.append("")
    lines.append("## Longest unresolved")
    lines.append("")
    unresolved = sorted(
        ((streaks[(r["opp_id"], rule)], r["opp_id"], rule)
         for r in open_rows
         for rule in results[r["opp_id"]].rule_ids()
         if streaks.get((r["opp_id"], rule), 1) >= 2),
        key=lambda t: (-t[0], t[1], int(t[2][1:])))
    if not unresolved:
        lines.append("Nothing carried over from previous runs.")
    for streak, opp_id, rule in unresolved[:3]:
        lines.append(f"- {rule} ({RULE_LABELS[rule]}) on {opp_id} — "
                     f"flagged {streak} runs")

    lines.append("")
    lines.append("## Suggested coaching focus")
    lines.append("")
    exposure, deals = {}, {}
    for row in flagged:
        for rule in results[row["opp_id"]].rule_ids():
            exposure[rule] = exposure.get(rule, 0.0) + (row["amount"] or 0.0)
            deals[rule] = deals.get(rule, 0) + 1
    if not exposure:
        lines.append("Nothing to focus on — clean week.")
    else:
        focus = min(exposure, key=lambda r: (-exposure[r], int(r[1:])))
        lines.append(f"{focus} — {RULE_LABELS[focus]}: "
                     f"{_money(exposure[focus])} at risk across "
                     f"{deals[focus]} deal(s).")
        if focus in COACHING_ASKS:
            lines.append(f"Ask: {COACHING_ASKS[focus]}")
    lines.append("")
    return "\n".join(lines)


def write_digests(data, config, out_dir):
    """Write out/digests/<as_of>/<owner_slug>.md for every owner with open
    opps. Returns the written paths."""
    digest_dir = Path(out_dir) / "digests" / data["as_of"].isoformat()
    digest_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for owner in data["owners"]:
        path = digest_dir / f"{owner_slug(owner)}.md"
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(digest_markdown(data, owner, config))
        paths.append(path)
    return paths


def run(store, snapshot_date, as_of, config, out_dir, owner_filter=None,
        filter_label=None):
    """Build, render, record the run, write the brief. Returns (path, data).

    A filtered brief NEVER records a run (its partial open-opp map would
    corrupt flag streaks and since-last-run for every later full brief) and
    writes to a suffixed filename so it cannot clobber the canonical brief."""
    data = build(store, snapshot_date, as_of, config,
                 owner_filter=owner_filter, filter_label=filter_label)
    markdown = render(data, config)
    name = f"desk_brief_{as_of.isoformat()}.md"
    if owner_filter is None:
        store.record_run(as_of, snapshot_date, data["summary"])
    else:
        name = f"desk_brief_{as_of.isoformat()}_{owner_slug(filter_label)}.md"
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / name
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(markdown)
    return path, data


def resolve_owner_filter(config, snapshot_owners, owners, teams):
    """Resolve --owner/--team values into (owner set, label). Unknown names
    are errors, never guesses: raises ValueError naming the known values.
    Owners are checked against the snapshot plus quotas/owner_meta (an owner
    with a quota but no open opps is still filterable)."""
    owner_meta = config.get("owner_meta") or {}
    known_owners = (set(snapshot_owners) | set(config.get("quotas") or {})
                    | set(owner_meta))
    known_teams = {m.get("team") for m in owner_meta.values()} - {None}
    selected = set()
    for team in teams or []:
        if not owner_meta:
            raise ValueError(
                "--team needs team metadata; pass --quotas pointing at a "
                "JSON with an 'owners' block (e.g. data/seed_manifest.json)")
        if team not in known_teams:
            raise ValueError(f"unknown team {team!r}; known teams: "
                             + ", ".join(sorted(known_teams)))
        selected |= {o for o, m in owner_meta.items()
                     if m.get("team") == team}
    for owner in owners or []:
        if owner not in known_owners:
            raise ValueError(f"unknown owner {owner!r}; check the snapshot's "
                             "owner names (and the --quotas file)")
        selected.add(owner)
    label = "; ".join([f"team {t}" for t in teams or []]
                      + [f"owner {o}" for o in owners or []])
    return selected, label


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m src.brief",
                                description="Generate the dated desk brief")
    p.add_argument("--as-of", type=date.fromisoformat, default=None)
    p.add_argument("--db", default="data/pipeline.db")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--out-dir", default="out")
    p.add_argument("--snapshot-date", type=date.fromisoformat, default=None,
                   help="defaults to the latest stored snapshot at or before --as-of")
    p.add_argument("--quotas", default=None,
                   help="JSON file whose 'quotas' mapping (e.g. data/seed_manifest.json) "
                        "is merged into config at run time; its 'owners' block, when "
                        "present, also supplies per-owner team/region for the rollups")
    p.add_argument("--digests", action="store_true",
                   help="also write private per-owner coaching digests to "
                        "<out-dir>/digests/<as-of>/<owner_slug>.md")
    p.add_argument("--owner", action="append", metavar="NAME",
                   help="repeatable; restrict the whole brief to these owners "
                        "(all numbers recomputed over the selection; the run "
                        "is not recorded and the file gets a suffix)")
    p.add_argument("--team", action="append", metavar="NAME",
                   help="repeatable; restrict to every owner of these teams "
                        "(team membership from the --quotas 'owners' block); "
                        "combines with --owner")
    args = p.parse_args(argv)

    # CLI entry point: the only place date.today() is allowed (spec).
    as_of = args.as_of if args.as_of is not None else date.today()
    config = load_config(args.config)
    if args.quotas:
        with open(args.quotas, encoding="utf-8") as f:
            payload = json.load(f)
        config = merge_quota_payload(config, payload)

    store = SnapshotStore(args.db, config)
    snapshot_date = args.snapshot_date
    if snapshot_date is None:
        candidates = [d for d in store.snapshot_dates() if d <= as_of]
        if not candidates:
            print(f"error: no snapshot at or before {as_of} in {args.db}; "
                  f"run python -m src.ingest first", file=sys.stderr)
            return 2
        snapshot_date = candidates[-1]
    elif snapshot_date not in store.snapshot_dates():
        print(f"error: no snapshot {snapshot_date} in {args.db}", file=sys.stderr)
        return 2

    owner_filter, filter_label = None, None
    if args.owner or args.team:
        try:
            owner_filter, filter_label = resolve_owner_filter(
                config, store.owners(snapshot_date), args.owner, args.team)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    path, data = run(store, snapshot_date, as_of, config, args.out_dir,
                     owner_filter=owner_filter, filter_label=filter_label)
    desk = data["desk"]
    filtered = f", filtered to {filter_label}" if filter_label else ""
    print(f"wrote {path} (snapshot {snapshot_date}, {desk.n_open} open opps, "
          f"at-risk {_money(desk.at_risk_dollars)}{filtered})")
    if args.digests:
        paths = write_digests(data, config, args.out_dir)
        print(f"wrote {len(paths)} coaching digests to "
              f"{Path(args.out_dir) / 'digests' / as_of.isoformat()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
