"""Desk brief generator: python -m src.brief --as-of YYYY-MM-DD

Writes out/desk_brief_<as_of>.md structured as forecast-call prep (the
weekly review's practitioner-standard agenda opens with hygiene of stalled/
pushed/stale deals). Page 1: headline, risky commits with deterministic
coaching prompts (questions to ask the seller — never gotchas), trajectory
(created-vs-closed flow + coverage vs remaining quota), since-last-run
summary, slipping pipeline. Everything else — fiscal quarters, exceptions,
owners, H5 detail — is drill-down under the Appendix header.

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
from .patterns import owner_patterns
from .rules import RULE_LABELS, evaluate_snapshot, is_open
from .scoring import desk_rollup, opp_score, owner_rollups
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


def trajectory_data(rows, delta, config, as_of, outcomes):
    """Created-vs-closed flow since last run plus coverage vs remaining
    quota. Required multiple = 1 / trailing win rate from stored closed
    outcomes; falls back to config coverage_ratio_min when closed history is
    insufficient. The basis used is always recorded verbatim."""
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
        n_closed = len(outcomes)
        if n_closed >= config["min_closed_for_win_rate"] and won_out:
            win_rate = len(won_out) / n_closed
            multiple = 1.0 / win_rate
            basis = (f"trailing win rate {win_rate:.0%} over {n_closed} "
                     f"stored closed outcomes -> required multiple "
                     f"{multiple:.1f}x")
        else:
            multiple = config["coverage_ratio_min"]
            basis = (f"config coverage_ratio_min {multiple:.1f}x (stored "
                     f"closed history insufficient: {n_closed} outcomes < "
                     f"{config['min_closed_for_win_rate']})")
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


def fiscal_quarter(d, fy_start_month):
    """FY<year>-Q<n>; fiscal years are named for the calendar year they end in
    (fy_start_month 7 puts 2026-08 in FY2027-Q1, Microsoft-style)."""
    quarter = (d.month - fy_start_month) % 12 // 3 + 1
    fy_year = d.year + (1 if fy_start_month > 1 and d.month >= fy_start_month else 0)
    return f"FY{fy_year}-Q{quarter}"


def _money(amount):
    return "n/a" if amount is None else f"${amount:,.0f}"


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


def build(store, snapshot_date, as_of, config):
    """Compute all brief data for one stored snapshot. No side effects."""
    prev = store.last_run()
    return build_from_rows(
        store.rows_with_history(snapshot_date), snapshot_date, as_of, config,
        validation=store.validation_report_dict(snapshot_date),
        prev_summary=prev["summary"] if prev else None,
        outcomes=store.closed_outcomes(snapshot_date),
        prev_opens=store.run_opens(),
        patterns=owner_patterns(store, as_of, config))


def build_from_rows(rows, snapshot_date, as_of, config,
                    validation=None, prev_summary=None, outcomes=None,
                    prev_opens=None, patterns=None):
    """Compute all brief data from in-memory rows (e.g. a validated upload).
    outcomes: stored closed outcomes (store.closed_outcomes) for the
    trailing-win-rate coverage basis; None outside the store. prev_opens:
    prior runs' rule-set maps (store.run_opens) for flag streaks. patterns:
    per-owner forecast-integrity patterns (patterns.owner_patterns); None
    outside the store."""
    results = evaluate_snapshot(rows, config, as_of)
    desk = desk_rollup(rows, results, config)
    insufficient = {"H3": 0, "H6": 0}
    for result in results.values():
        for item in result.insufficient:
            insufficient[item.rule_id] += 1
    delta = since_last_run(prev_summary, rows, results, desk)
    return {
        "snapshot_date": snapshot_date,
        "as_of": as_of,
        "rows": rows,
        "results": results,
        "desk": desk,
        "owners": owner_rollups(rows, results, config),
        "validation": validation,
        "insufficient": insufficient,
        "since_last_run": delta,
        "risky_commits": risky_commits(rows, results, config),
        "trajectory": trajectory_data(rows, delta, config, as_of, outcomes),
        "streaks": flag_streaks(prev_opens or [], results),
        "patterns": patterns,
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
    lines.append("| # | Opp | Owner | Stage | Amount | Forecast | Flags "
                 "| Ask the seller |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for i, entry in enumerate(entries[:10], start=1):
        row = entry["row"]
        lines.append(f"| {i} | {row['opp_id']} | {row['owner']} "
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
    for label, key in (("New violations", "new_violations"),
                       ("Cleared violations", "cleared_violations")):
        opps = delta[key]
        total = sum(len(v) for v in opps.values())
        lines.append(f"- {label}: {total} on {len(opps)} opps")
        for opp_id in sorted(opps):
            lines.append(f"  - {opp_id}: {', '.join(opps[opp_id])}")
    rows_by_id = {r["opp_id"]: r for r in data["rows"]}
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


def _owner_table(lines, data):
    lines.append("### Owners")
    lines.append("")
    owners = data["owners"]
    if not owners:
        lines.append("No open opportunities.")
        return
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
        lines.append(f"- Overcall (happy ears): {p.owner} — "
                     f"{p.overcall_share:.0%} of {p.n_ever_commit} "
                     f"ever-commit opps later pushed or lost")
    def pct(value):
        return "n/a" if value is None else f"{value:.0%}"

    for p in under:
        lines.append(f"- Undercall (sandbagging): {p.owner} — wins never "
                     f"called commit/best_case: "
                     f"{pct(p.undercall_won_share)} (n={p.n_won}); open "
                     f"pipeline {pct(p.omitted_share)} omitted, "
                     f"{pct(p.farout_share)} far-out (n={p.n_open})")
    small = sum(1 for p in patterns.values()
                if p.overcall_small_n and p.undercall_small_n)
    lines.append(f"- Suppressed as small_n: {small} owners with too little "
                 f"history to score")


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
    _forecast_integrity(lines, data)
    lines.append("")
    _forecast_patterns(lines, data)
    lines.append("")
    _since_last_run_detail(lines, data)
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
        lines.append(f"- {row['opp_id']} ({row['stage']}, "
                     f"{_money(row['amount'])}): "
                     f"{' '.join(result.rule_ids())}{note}")

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
        if focus in COACHING_PROMPTS:
            lines.append(f"Ask: {COACHING_PROMPTS[focus]}")
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


def run(store, snapshot_date, as_of, config, out_dir):
    """Build, render, record the run, write the brief. Returns (path, data)."""
    data = build(store, snapshot_date, as_of, config)
    markdown = render(data, config)
    store.record_run(as_of, snapshot_date, data["summary"])
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"desk_brief_{as_of.isoformat()}.md"
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(markdown)
    return path, data


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
                        "is merged into config at run time")
    p.add_argument("--digests", action="store_true",
                   help="also write private per-owner coaching digests to "
                        "<out-dir>/digests/<as-of>/<owner_slug>.md")
    args = p.parse_args(argv)

    # CLI entry point: the only place date.today() is allowed (spec).
    as_of = args.as_of if args.as_of is not None else date.today()
    config = load_config(args.config)
    if args.quotas:
        with open(args.quotas, encoding="utf-8") as f:
            payload = json.load(f)
        config["quotas"] = {**(config.get("quotas") or {}),
                            **payload.get("quotas", payload)}

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

    path, data = run(store, snapshot_date, as_of, config, args.out_dir)
    desk = data["desk"]
    print(f"wrote {path} (snapshot {snapshot_date}, {desk.n_open} open opps, "
          f"at-risk {_money(desk.at_risk_dollars)})")
    if args.digests:
        paths = write_digests(data, config, args.out_dir)
        print(f"wrote {len(paths)} coaching digests to "
              f"{Path(args.out_dir) / 'digests' / as_of.isoformat()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
