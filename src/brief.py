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
import os
import re
import sys
from datetime import date
from pathlib import Path

from .ingest import load_config
from .patterns import commit_ledger, ledger_rollups, owner_patterns
from .rules import (RULE_LABELS, evaluate_snapshot, is_open,
                    staleness_tier)
from .scoring import (days_left_in_quarter, desk_rollup, fiscal_quarter,
                      fiscal_quarter_end, group_rollups, opp_score,
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

def rule_trip(rule, config):
    """The line each rule crosses, for the digest's rule reference — every
    rule, not just the two whose engine detail omits the threshold. Numbers
    come from config so the seller can check the observed value in Top risks
    against the exact trip line (bare rule tokens read as surveillance)."""
    sym = currency_symbol(config)
    q = config.get("next_step_quality") or {}
    return {
        "H1": "no activity beyond the per-stage staleness threshold",
        "H2": "close date earlier than the evaluation date",
        "H3": "2+ close-date changes (high severity at 3+)",
        "H4": "next step empty, or its date missing or in the past",
        "H5": "commit forecast on a pre-develop stage, or with no valid "
              "next step",
        "H6": "days in stage beyond the per-stage aging norm",
        "H7": f"amount >= {sym}{config['big_deal_threshold']:,.0f} with fewer "
              "than 2 contacts",
        "H8": "amount missing or <= 0",
        "H9": f"next step under {q.get('min_chars', 15)} chars, filler, or "
              "without an action verb",
        "H10": f"close date more than {config['close_date_horizon_days']}d out",
        "H11": f"a single push >= {config['push_alarm_days']}d, or cumulative "
               f"later-drift >= {config['cumulative_push_alarm_days']}d",
    }.get(rule)


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
        # informational split: how much of the open pipeline can even land
        # in the quarter the coverage is computed for (overdue close dates
        # count as in-quarter — they are due, not late-quarter)
        q_end = fiscal_quarter_end(as_of, fy_start)
        in_quarter_open = sum(
            r["amount"] or 0.0 for r in rows
            if is_open(r) and r["close_date"] is not None
            and r["close_date"] <= q_end)
        coverage = {"quarter": quarter, "total_quota": total_quota,
                    "won_this_quarter": won_this_quarter,
                    "remaining_quota": remaining_quota,
                    "required_multiple": multiple,
                    "required_pipeline": required,
                    "ratio": (open_pipeline / required) if required > 0
                    else None,
                    "in_quarter_open": in_quarter_open,
                    "later_open": open_pipeline - in_quarter_open,
                    "basis": basis}
    return {"flow": flow, "open_pipeline": open_pipeline,
            "coverage": coverage}


def currency_symbol(config):
    return (config or {}).get("display_currency_symbol") or "$"


def _money(amount, config=None):
    return ("n/a" if amount is None
            else f"{currency_symbol(config)}{amount:,.0f}")


def desk_score_series(store, config, up_to_snapshot, n=None):
    """Snapshot-anchored desk score trend: the engine re-run per stored
    snapshot with as_of = that snapshot's own date. Deterministic and
    cache-free — unlike the runs table it has no holes for snapshots
    without recorded runs and cannot be skewed by re-running an old brief.
    Returns [(snapshot_date, weighted_mean_score)] over the last n stored
    snapshots at-or-before up_to_snapshot (n from optional config
    trend_snapshots, default 8)."""
    if n is None:
        n = config.get("trend_snapshots") or 8
    dates = [d for d in store.snapshot_dates() if d <= up_to_snapshot][-n:]
    series = []
    for d in dates:
        rows = store.rows_with_history(d)
        results = evaluate_snapshot(rows, config, d)
        series.append((d, desk_rollup(rows, results, config)
                       .weighted_mean_score))
    return series


def group_trends(store, config, up_to_snapshot, n=None):
    """Per-team and per-region coverage + at-risk trend over the last n stored
    snapshots, each snapshot on its OWN win-rate basis. The VP reviews off the
    printed brief, not the dashboard, so the direction of a team's coverage and
    risk has to live here too. Snapshot-anchored and cache-free like
    desk_score_series. Returns {"team": {key: [(date, coverage, at_risk)]},
    "region": {...}}; {} when no owner metadata is configured."""
    owner_meta = config.get("owner_meta") or {}
    if not owner_meta:
        return {}
    if n is None:
        n = config.get("trend_snapshots") or 8
    fy_start = config["fiscal_year_start_month"]
    dates = [d for d in store.snapshot_dates() if d <= up_to_snapshot][-n:]
    out = {"team": {}, "region": {}}
    for d in dates:
        rows = store.rows_with_history(d)
        results = evaluate_snapshot(rows, config, d)
        outcomes = store.closed_outcomes(d)
        multiple, _ = required_coverage_multiple(outcomes, config)
        quarter = fiscal_quarter(d, fy_start)
        won_by_owner = {}
        for o in outcomes:
            if o["stage"] == "closed_won" and o["close_date"] is not None \
                    and fiscal_quarter(o["close_date"], fy_start) == quarter:
                won_by_owner[o["owner"]] = (won_by_owner.get(o["owner"], 0.0)
                                            + (o["amount"] or 0.0))
        for dimension in ("team", "region"):
            groups = group_rollups(rows, results, config, owner_meta,
                                   dimension, multiple, won_by_owner)
            for key, g in groups.items():
                out[dimension].setdefault(key, []).append(
                    (d, g.coverage_ratio, g.at_risk_dollars))
    return out


def pacing_data(waterfalls, config):
    """Pipeline-generation pacing from the per-pair waterfalls: created
    dollars/count per snapshot week, vs the optional
    pipeline_gen_weekly_target (default null -> no target comparison is
    rendered — a target is configured, never guessed)."""
    if not waterfalls:
        return None
    weekly = [{"week": w["cur_date"], "n": w["created"]["n"],
               "dollars": w["created"]["dollars"]} for w in waterfalls]
    return {"weekly": weekly,
            "mean_dollars": (sum(w["dollars"] for w in weekly)
                             / len(weekly)),
            "target": config.get("pipeline_gen_weekly_target")}


def quota_owner_mismatch(quota_owners, snapshot_owners):
    """Symmetric difference between configured quota owners and the owners
    present in the evaluated snapshot. Returns None when no quotas are
    configured or the sets match — the warning only fires on a real
    mismatch (typo'd names, departed sellers, stale quota files)."""
    quota_owners, snapshot_owners = set(quota_owners), set(snapshot_owners)
    if not quota_owners:
        return None
    quota_only = sorted(quota_owners - snapshot_owners)
    snapshot_only = sorted(snapshot_owners - quota_owners)
    if not quota_only and not snapshot_only:
        return None
    return {"quota_only": quota_only, "snapshot_only": snapshot_only}


def mismatch_summary(mismatch):
    """One warning sentence shared by the brief Validation section and the
    dashboard."""
    parts = []
    if mismatch["quota_only"]:
        parts.append(f"{len(mismatch['quota_only'])} quota owner(s) not in "
                     f"this snapshot: {', '.join(mismatch['quota_only'])}")
    if mismatch["snapshot_only"]:
        parts.append(f"{len(mismatch['snapshot_only'])} snapshot owner(s) "
                     f"without a quota: {', '.join(mismatch['snapshot_only'])}")
    return ("Quota/owner mismatch — " + "; ".join(parts) + ". Check for "
            "renamed or typo'd owners: an absent quota owner still adds "
            "quota to required pipeline; an unmatched snapshot owner shows "
            "no coverage.")


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
    # Quota/owner mismatch is a desk-wide data-quality fact; a filtered brief
    # would list every unselected quota owner as "missing", so it skips it.
    mismatch = None
    if owner_filter is None:
        mismatch = quota_owner_mismatch(config.get("quotas") or {},
                                        store.owners(snapshot_date))
    # One waterfall per consecutive stored pair <= the evaluated snapshot:
    # the last one renders as the bridge table, the whole list feeds pacing.
    stored = [d for d in store.snapshot_dates() if d <= snapshot_date]
    waterfalls = [store.waterfall(a, b, owners=owner_filter)
                  for a, b in zip(stored, stored[1:])]
    # The headline trend is a desk-wide series; a filtered brief drops it
    # (same reasoning as dropping the previous run's desk score).
    score_trend = (desk_score_series(store, config, snapshot_date)
                   if owner_filter is None else None)
    # Per-team/region coverage+risk trend is desk-wide (dropped on a filtered
    # brief, same as the desk score trend).
    grp_trends = (group_trends(store, config, snapshot_date)
                  if owner_filter is None else None)
    return build_from_rows(
        store.rows_with_history(snapshot_date), snapshot_date, as_of, config,
        validation=store.validation_report_dict(snapshot_date),
        prev_summary=prev["summary"] if prev else None,
        outcomes=store.closed_outcomes(snapshot_date),
        prev_opens=store.run_opens(before_snapshot_date=snapshot_date),
        patterns=owner_patterns(store, as_of, config, snapshot_date),
        ledger=commit_ledger(store, as_of, config, snapshot_date),
        owner_mismatch=mismatch, waterfalls=waterfalls,
        score_trend=score_trend, group_trends=grp_trends,
        owner_filter=owner_filter, filter_label=filter_label)


def build_from_rows(rows, snapshot_date, as_of, config,
                    validation=None, prev_summary=None, outcomes=None,
                    prev_opens=None, patterns=None, ledger=None,
                    owner_mismatch=None, waterfalls=None, score_trend=None,
                    group_trends=None,
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
        "owner_mismatch": owner_mismatch,
        "waterfall": waterfalls[-1] if waterfalls else None,
        "pacing": pacing_data(waterfalls, config),
        "score_trend": score_trend,
        "group_trends": group_trends,
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
    score_label = "Selection score" if data.get("filter_label") else "Desk score"
    if desk.n_open == 0:
        lines.append("- No open opportunities in this snapshot.")
    else:
        lines.append(f"- {score_label}: {desk.weighted_mean_score:.1f} amount-weighted "
                     f"mean / {desk.median_score:.1f} median")
        trend = [(d, s) for d, s in (data.get("score_trend") or [])
                 if s is not None]
        if len(trend) >= 2:
            lines.append(f"- Desk score, last {len(trend)} snapshots: "
                         + " ".join(f"{s:.1f}" for _, s in trend)
                         + " (snapshot-anchored, oldest first)")
        lines.append(f"- Healthy opps (score >= "
                     f"{config['healthy_score_threshold']}): {desk.pct_healthy:.1f}% "
                     f"of {desk.n_open} open")
        open_pipeline = sum(r["amount"] or 0.0 for r in data["rows"] if is_open(r))
        lines.append(f"- Open pipeline: {_money(open_pipeline, config)}")
        lines.append(f"- At-risk dollars (distinct opps with a high-severity "
                     f"violation): {_money(desk.at_risk_dollars, config)}")
        # Forecast-call number: the committed dollars a manager defends to the
        # VP, with the at-risk share of each broken out. Deterministic
        # arithmetic over the current snapshot — NOT a probability-weighted
        # forecast (the tool inspects, it does not predict).
        open_rows = [r for r in data["rows"] if is_open(r)]
        results = data["results"]

        def _cat(cat):
            rows = [r for r in open_rows if r["forecast_category"] == cat]
            total = sum(r["amount"] or 0.0 for r in rows)
            at_risk = sum(r["amount"] or 0.0 for r in rows
                          if results[r["opp_id"]].has_high())
            return len(rows), total, at_risk

        c_n, c_total, c_risk = _cat("commit")
        b_n, b_total, b_risk = _cat("best_case")
        lines.append(
            f"- Forecast call: commit {_money(c_total, config)} across {c_n} "
            f"deal(s) (at-risk within {_money(c_risk, config)}); best_case "
            f"{_money(b_total, config)} across {b_n} deal(s) (at-risk within "
            f"{_money(b_risk, config)})")
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
    else:
        source = Path(validation["source_file"]).name
        lines.append(f"- Source `{source}`: accepted {validation['accepted']}/"
                     f"{validation['total_rows']} rows, "
                     f"rejected {validation['rejected']}")
        reasons = list(validation["reason_counts"].items())[:5]
        if reasons:
            lines.append("- Top rejection reasons: "
                         + ", ".join(f"{reason} ({n})"
                                     for reason, n in reasons))
        for warning in validation["warnings"]:
            lines.append(f"- Warning: {warning}")
    if data.get("owner_mismatch"):
        lines.append(f"- Warning: {mismatch_summary(data['owner_mismatch'])}")


def _risky_commit_row(lines, i, entry, config, with_owner):
    row = entry["row"]
    owner = f"| {row['owner']} " if with_owner else ""
    lines.append(f"| {i} | {row['opp_id']} | {row['account']} "
                 f"{owner}| {row['stage']} | {_money(row['amount'], config)} "
                 f"| {row['forecast_category']} "
                 f"| {' '.join(entry['risky_rules'])} "
                 f"| {entry['prompt']} |")


def _risky_commits(lines, data, config):
    lines.append("## Risky commits")
    lines.append("")
    entries = data["risky_commits"]
    if not entries:
        lines.append("No commit/best_case opp carries a risk flag "
                     f"({', '.join(RISKY_RULES)}).")
        return
    total = sum(e["row"]["amount"] or 0.0 for e in entries)
    # On a filtered (team/owner) brief the call is run rep-by-rep, so group by
    # owner instead of one interleaved dollar-ranked list — a frontline manager
    # walks her people, not a merged desk list.
    filtered = bool(data.get("filter_label"))
    lines.append(f"{len(entries)} commit/best_case opps carry a risk flag — "
                 f"{_money(total, config)} (distinct opps), dollar-ranked"
                 + (", grouped by owner" if filtered
                    else (", top 10 shown" if len(entries) > 10 else ""))
                 + ". Coaching prompts, not gotchas.")
    # Print the commit-scrub's "at-risk within" figure so the two documents
    # reconcile on the page, not live in front of the VP. It is a DIFFERENT cut
    # from this list — every open commit/best_case opp with a high-severity
    # flag, which includes high-but-not-RISKY rules (e.g. H3 at 3+ pushes) and
    # excludes the medium/low risky flags shown here — so compute it over that
    # universe, exactly as the scrub does, rather than over these entries.
    open_cb = [r for r in data["rows"] if is_open(r)
               and r["forecast_category"] in ("commit", "best_case")]
    high_cb = [r for r in open_cb
               if data["results"][r["opp_id"]].has_high()]
    if high_cb:
        lines.append(f"High-severity at-risk — the figure the commit-scrub "
                     f"carries forward — is "
                     f"{_money(sum(r['amount'] or 0.0 for r in high_cb), config)} "
                     f"across {len(high_cb)} of {len(open_cb)} open "
                     f"commit/best_case opp(s) (a different cut from the "
                     f"risk-flagged list above).")
    lines.append("")
    if filtered:
        by_owner = {}
        for e in entries:
            by_owner.setdefault(e["row"]["owner"], []).append(e)
        for owner in sorted(by_owner):
            owned = by_owner[owner]
            sub = sum(e["row"]["amount"] or 0.0 for e in owned)
            lines.append(f"### {owner} — {len(owned)} risky, "
                         f"{_money(sub, config)}")
            lines.append("")
            lines.append("| # | Opp | Account | Stage | Amount | Forecast "
                         "| Flags | Ask the seller |")
            lines.append("|---|---|---|---|---|---|---|---|")
            for i, entry in enumerate(owned, start=1):
                _risky_commit_row(lines, i, entry, config, with_owner=False)
            lines.append("")
        return
    lines.append("| # | Opp | Account | Owner | Stage | Amount | Forecast "
                 "| Flags | Ask the seller |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for i, entry in enumerate(entries[:10], start=1):
        _risky_commit_row(lines, i, entry, config, with_owner=True)


def _trajectory(lines, data, config):
    lines.append("## Trajectory")
    lines.append("")
    trajectory = data["trajectory"]
    flow = trajectory["flow"]
    if flow is None:
        lines.append("- Flow since last run: no previous run recorded.")
    else:
        lines.append(f"- Flow since last run: {flow['created_n']} created "
                     f"({_money(flow['created_dollars'], config)}) vs "
                     f"{flow['won_n'] + flow['lost_n']} closed "
                     f"({flow['won_n']} won {_money(flow['won_dollars'], config)}, "
                     f"{flow['lost_n']} lost {_money(flow['lost_dollars'], config)})")
    _waterfall_table(lines, data, config)
    _pacing_line(lines, data, config)
    coverage = trajectory["coverage"]
    if coverage is None:
        lines.append("- Coverage: no quotas configured.")
        return
    ratio = ("n/a" if coverage["ratio"] is None
             else f"{coverage['ratio']:.2f}x")
    lines.append(f"- Coverage ({coverage['quarter']}): open pipeline "
                 f"{_money(trajectory['open_pipeline'], config)} vs required "
                 f"{_money(coverage['required_pipeline'], config)} -> {ratio}")
    lines.append(f"  - Remaining quota {_money(coverage['remaining_quota'], config)} "
                 f"(quota {_money(coverage['total_quota'], config)} - won this "
                 f"quarter {_money(coverage['won_this_quarter'], config)}) x "
                 f"{coverage['required_multiple']:.2f}")
    lines.append(f"  - Basis: {coverage['basis']}")
    # Print the later share as printed-total minus printed-in-quarter so the
    # two halves sum exactly to the printed open pipeline (independently
    # rounded halves were $1 short in a persona napkin check).
    later_display = (round(trajectory["open_pipeline"])
                     - round(coverage["in_quarter_open"]))
    lines.append(f"  - Of open pipeline: "
                 f"{_money(round(coverage['in_quarter_open']), config)} closes in "
                 f"{coverage['quarter']} or earlier (overdue included), "
                 f"{_money(later_display, config)} later "
                 f"(informational — the ratio above is unchanged)")


_WF_SIGNS = {"created": 1, "increased": 1, "decreased": -1, "won": -1,
             "lost": -1, "removed": -1}


def waterfall_display_dollars(wf):
    """Integer display dollars per waterfall bucket, adjusted so the PRINTED
    bridge reconciles exactly. Amounts carry cents; rounding each bucket
    independently broke the identity by $1 in a persona hand-check — on the
    one table whose caption promises exact reconciliation. Beginning/ending
    print as plain rounds; any residual (at most a few dollars) folds into
    the largest flow bucket, invisible at whole-dollar scale."""
    display = {name: round(wf[name]["dollars"]) for name in _WF_SIGNS}
    display["beginning"] = round(wf["beginning"]["dollars"])
    display["ending"] = round(wf["ending"]["dollars"])
    residual = (display["ending"] - display["beginning"]
                - sum(s * display[n] for n, s in _WF_SIGNS.items()))
    if residual:
        biggest = max(_WF_SIGNS, key=lambda n: (display[n], n))
        display[biggest] += _WF_SIGNS[biggest] * residual
    return display


def _waterfall_table(lines, data, config):
    """Open-pipeline bridge between the last two stored snapshots. Pushes
    move no dollars — they render as the annotation line, never a bucket."""
    wf = data["waterfall"]
    if wf is None:
        lines.append("- Pipeline waterfall: needs a previous stored "
                     "snapshot.")
        return
    lines.append(f"- Pipeline waterfall ({wf['prev_date'].isoformat()} -> "
                 f"{wf['cur_date'].isoformat()}):")
    lines.append("")
    lines.append("| Bucket | Opps | Dollars |")
    lines.append("|---|---|---|")
    display = waterfall_display_dollars(wf)
    for sign, name in (("", "beginning"), ("+ ", "created"),
                       ("+ ", "increased"), ("- ", "decreased"),
                       ("- ", "won"), ("- ", "lost"), ("- ", "removed"),
                       ("= ", "ending")):
        label = {"beginning": "beginning open", "ending": "ending open"} \
            .get(name, name)
        lines.append(f"| {sign}{label} | {wf[name]['n']} "
                     f"| {_money(display[name], config)} |")
    lines.append("")
    pushed = wf["pushed"]
    lines.append(f"  - Close-date pushes moved no dollars: {pushed['n']} "
                 f"open opp(s) ({_money(pushed['dollars'], config)}) pushed "
                 f"later.")


def _pacing_line(lines, data, config):
    pacing = data["pacing"]
    if pacing is None:
        return
    latest = pacing["weekly"][-1]
    line = (f"- Pipeline generation: {_money(latest['dollars'], config)} "
            f"created across {latest['n']} opp(s) since the last snapshot; "
            f"mean {_money(pacing['mean_dollars'], config)}/week over "
            f"{len(pacing['weekly'])} snapshot pair(s)")
    target = pacing["target"]
    if target:
        line += (f"; weekly target {_money(target, config)} "
                 f"(latest {latest['dollars'] / target:.2f}x, "
                 f"mean {pacing['mean_dollars'] / target:.2f}x)")
    lines.append(line)


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


def _since_last_run_detail(lines, data, config):
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

    # A mass delta (a threshold change, a bulk CRM edit) turns the per-opp
    # detail into hundreds of identical lines: any rule exceeding
    # delta_detail_max_per_rule opps collapses to one summary line.
    max_per_rule = config.get("delta_detail_max_per_rule") or 20
    for label, key in (("New violations", "new_violations"),
                       ("Cleared violations", "cleared_violations")):
        opps = delta[key]
        total = sum(len(v) for v in opps.values())
        lines.append(f"- {label}: {total} on {len(opps)} opps")
        rule_counts = {}
        for rules in opps.values():
            for rule in rules:
                rule_counts[rule] = rule_counts.get(rule, 0) + 1
        collapsed = {rule for rule, n in rule_counts.items()
                     if n > max_per_rule}
        for rule in sorted(collapsed, key=lambda r: int(r[1:])):
            lines.append(f"  - {rule}: {rule_counts[rule]} opps "
                         f"(detail collapsed, > {max_per_rule} per rule)")
        for opp_id in sorted(opps):
            kept = [r for r in opps[opp_id] if r not in collapsed]
            if kept:
                lines.append(f"  - {opp_id}{_who(opp_id)}: "
                             f"{', '.join(kept)}")
    lines.append(f"- Closed: {len(delta['closed'])} opps")
    for opp_id in sorted(delta["closed"]):
        info = delta["closed"][opp_id]
        cleared = (f" (violations cleared: {', '.join(info['violations_cleared'])})"
                   if info["violations_cleared"] else "")
        lines.append(f"  - {opp_id}: {info['stage']}, "
                     f"{_money(rows_by_id[opp_id]['amount'], config)}{cleared}")
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
                 f"{_money(total, config)} across {len(slipping)} opps.")
    lines.append("")
    lines.append("| Opp | Owner | Stage | Amount | Pushes | Cum. days later "
                 "| Max push | Rules | Review |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for row in slipping:
        result = data["results"][row["opp_id"]]
        badges = " ".join(v.rule_id for v in result.violations) or "-"
        # Neutral, coaching-framed marker: the same deterministic signal
        # (push_count >= threshold) without the career-loaded "disqualification"
        # wording, since this table is name-attributed in a shared brief.
        review = ("review close plan"
                  if row["push_count"] >= config["disqualify_review_pushes"]
                  else "-")
        lines.append(f"| {row['opp_id']} | {row['owner']} | {row['stage']} "
                     f"| {_money(row['amount'], config)} | {row['push_count']} "
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
        lines.append(f"| {quarter} | {_money(bucket['at_risk'], config)} "
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
                     f"| {row['stage']} | {_money(row['amount'], config)} "
                     f"| {opp_score(result, config)} | {badges} "
                     f"| {streak_cell} | {detail} |")


def owner_flagged_opps(owner, results, rows_by_id, config):
    """Open flagged opps for one owner, worst-severity then dollar ranked.

    The read-only drill-down the dashboard renders when a scoreboard row is
    selected. Pure (no Streamlit, no store) so it is unit-testable without
    running the app script; mirrors the Appendix exception ordering.
    """
    recs = []
    for result in results.values():
        if not result.violations:
            continue
        row = rows_by_id.get(result.opp_id)
        if row is None or row["owner"] != owner or not is_open(row):
            continue
        worst = min(_SEV_RANK[v.severity] for v in result.violations)
        recs.append({
            "opp_id": result.opp_id, "account": row["account"],
            "stage": row["stage"], "amount": row["amount"],
            "score": opp_score(result, config),
            "worst": ["high", "medium", "low"][worst],
            "rules": " ".join(v.rule_id for v in result.violations),
            "detail": "; ".join(v.detail for v in result.violations),
            "_rank": (worst, -(row["amount"] or 0.0), result.opp_id),
        })
    recs.sort(key=lambda e: e.pop("_rank"))
    return recs


def _staleness_tiers(lines, data, config):
    """Escalation ladder over H1-stale opps, rendered only when the OPTIONAL
    staleness_escalation config block is present (absent -> the brief is
    byte-identical). Groups are most-urgent first; opps dollar-ranked like
    every other deal list."""
    esc = config.get("staleness_escalation")
    if not esc:
        return
    lines.append("### Staleness escalation")
    lines.append("")
    lines.append(f"Tiers beyond the per-stage staleness threshold: flag "
                 f"(over threshold), escalate (> threshold + "
                 f"{esc['escalate_days']}d), review (> threshold + "
                 f"{esc['review_days']}d).")
    lines.append("")
    tiers = {"review": [], "escalate": [], "flag": []}
    for row in data["rows"]:
        if is_open(row):
            tier = staleness_tier(row, config, data["as_of"])
            if tier is not None:
                tiers[tier].append(row)
    if not any(tiers.values()):
        lines.append("No stale opps.")
        lines.append("")
        return
    for tier, rows in tiers.items():
        total = sum(r["amount"] or 0.0 for r in rows)
        lines.append(f"- {tier}: {len(rows)} opp(s) "
                     f"({_money(total, config)})")
        rows.sort(key=lambda r: (-(r["amount"] or 0.0), r["opp_id"]))
        for row in rows:
            days_idle = (data["as_of"] - row["last_activity_date"]).days
            threshold = config["staleness_days"][row["stage"]]
            lines.append(f"  - {row['opp_id']} {row['account']} — "
                         f"{row['owner']} ({row['stage']}, "
                         f"{_money(row['amount'], config)}): idle "
                         f"{days_idle}d (threshold {threshold}d)")
    lines.append("")


def _coverage_note(lines, data):
    """The coverage definition and basis, printed adjacent to every table that
    carries a Coverage column (the persona pass found the note 67 lines from
    the Owners table it explained)."""
    lines.append("Coverage = open pipeline vs required pipeline (remaining "
                 "quota net of wins this quarter x the required multiple); "
                 "low_coverage means under 1.00x. Basis: "
                 f"{data['coverage_basis']}.")


def _gap_to_cover(stats, config):
    """Dollars still needed to reach required pipeline on the flag's own
    basis; $0 when covered, n/a without a quota."""
    if stats.required_pipeline is None:
        return "n/a"
    return _money(max(stats.required_pipeline - stats.open_pipeline, 0.0),
                  config)


def desk_coverage_note(stats_map, config, unit="quota'd owners"):
    """When most rows (owners, or teams/regions) are under 1.00x, low coverage
    is a desk condition, not an individual finding — say so (the flag and its
    basis do not change). None when under the threshold share. unit names the
    rows so the note fits whichever table it sits above."""
    share_min = config.get("low_coverage_desk_note_share")
    share_min = 0.75 if share_min is None else share_min
    with_cov = [s for s in stats_map.values() if s.coverage_ratio is not None]
    flagged = [s for s in with_cov if s.coverage_flagged]
    if not with_cov or len(flagged) / len(with_cov) <= share_min:
        return None
    return (f"Note: {len(flagged)} of {len(with_cov)} {unit} are "
            f"under 1.00x — desk-wide under-coverage, not individual "
            f"laggards; ordering carries the signal. The flag and its "
            f"basis are unchanged.")


def _owner_table(lines, data, config):
    lines.append("### Owners")
    lines.append("")
    owners = data["owners"]
    if not owners:
        lines.append("No open opportunities.")
        return
    _coverage_note(lines, data)
    note = desk_coverage_note(owners, config)
    if note:
        lines.append("")
        lines.append(note)
    lines.append("")
    lines.append("| Owner | Open | Mean | Median | Violations | Pipeline | Coverage | Gap to cover | Flags |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for stats in owners.values():
        coverage = ("n/a" if stats.coverage_ratio is None
                    else f"{stats.coverage_ratio:.2f}")
        flags = ", ".join(f for f, on in (("small_n", stats.small_n),
                                          ("low_coverage", stats.coverage_flagged))
                          if on) or "-"
        lines.append(f"| {stats.owner} | {stats.n_open} | {stats.mean_score:.1f} "
                     f"| {stats.median_score:.1f} | {stats.violation_count} "
                     f"| {_money(stats.open_pipeline, config)} | {coverage} "
                     f"| {_gap_to_cover(stats, config)} | {flags} |")


def _group_table(lines, groups, label, config):
    """Render one team/region roll-up table, worst coverage first so ordering
    itself carries the signal (empty groups produce a note)."""
    lines.append(f"### {label}s")
    lines.append("")
    if not groups:
        lines.append("No team/region metadata configured "
                     "(pass --quotas data/seed_manifest.json).")
        return
    note = desk_coverage_note(groups, config, unit=f"{label.lower()}s")
    if note:
        lines.append(note)
        lines.append("")
    lines.append(f"| {label} | Owners | Open | Mean | Pipeline | Quota "
                 "| Coverage | Gap to cover | Violations | At-risk $ | Flags |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    ranked = sorted(groups.values(),
                    key=lambda g: (g.coverage_ratio is None,
                                   g.coverage_ratio or 0.0, g.key))
    for g in ranked:
        coverage = ("n/a" if g.coverage_ratio is None
                    else f"{g.coverage_ratio:.2f}")
        mean_cell = "n/a" if g.mean_score is None else f"{g.mean_score:.1f}"
        flags = "low_coverage" if g.coverage_flagged else "-"
        lines.append(f"| {g.key} | {g.n_owners} | {g.n_open} | {mean_cell} "
                     f"| {_money(g.open_pipeline, config)} | {_money(g.quota, config)} "
                     f"| {coverage} | {_gap_to_cover(g, config)} "
                     f"| {g.violation_count} "
                     f"| {_money(g.at_risk_dollars, config)} | {flags} |")


def _group_trend_lines(lines, series, label, config):
    """Per-group coverage (oldest->newest) and at-risk (start->end) trend,
    worst latest-coverage first, plus the widest coverage move — so 'is EMEA
    improving?' has an answer on the printed brief, and a flat month reads as
    the finding it is."""
    def latest_cov(items):
        return next((c for _, c, _ in reversed(items) if c is not None), None)

    ranked = sorted(series.items(),
                    key=lambda kv: (latest_cov(kv[1]) is None,
                                    latest_cov(kv[1]) or 0.0, kv[0]))
    n = len(next(iter(series.values())))
    lines.append(f"Per {label.lower()}, coverage oldest->newest (1.00x is the "
                 f"bar) and at-risk start->end, over {n} snapshots:")
    flat_threshold = config.get("trend_flat_threshold")
    flat_threshold = 0.05 if flat_threshold is None else flat_threshold
    widest, diverging = None, []
    for key, items in ranked:
        covs = " ".join("n/a" if c is None else f"{c:.2f}"
                        for _, c, _ in items)
        lines.append(f"- {key}: coverage {covs}; at-risk "
                     f"{_money(items[0][2], config)} -> "
                     f"{_money(items[-1][2], config)}")
        c0 = next((c for _, c, _ in items if c is not None), None)
        cN = latest_cov(items)
        if c0 is not None and cN is not None:
            if widest is None or abs(cN - c0) > abs(widest[1]):
                widest = (key, cN - c0)
            # coverage rose but at-risk dollars also rose: pipeline is being
            # built faster than it is being cleaned — the budget-routing signal
            if cN - c0 > 0 and items[-1][2] > items[0][2]:
                diverging.append(key)
    # A one-line verdict, so 'is the desk trending?' is answered, not squinted:
    # a flat month reads as the finding it is.
    if widest is not None:
        if abs(widest[1]) < flat_threshold:
            lines.append(f"- Verdict: {label.lower()} coverage essentially flat "
                         f"this month — no {label.lower()} moved more than "
                         f"{abs(widest[1]):.2f}.")
        else:
            lines.append(f"- Verdict: largest {label.lower()} coverage move "
                         f"{widest[0]} {widest[1]:+.2f} over the window.")
    if diverging:
        lines.append(f"- Coverage up but at-risk also up (building pipeline "
                     f"faster than cleaning it): {', '.join(diverging)}.")


def _group_trends_section(lines, data, config):
    trends = data.get("group_trends")
    if not trends:
        return
    dims = [(dim, label) for dim, label in (("team", "Team"),
                                            ("region", "Region"))
            if any(len(v) >= 2 for v in (trends.get(dim) or {}).values())]
    if not dims:
        return
    lines.append("")
    lines.append("### Coverage & at-risk trend")
    lines.append("")
    for dim, label in dims:
        lines.append(f"**{label}s**")
        lines.append("")
        _group_trend_lines(lines, trends[dim], label, config)
        lines.append("")


def _team_region_tables(lines, data, config):
    lines.append("### Teams and regions")
    lines.append("")
    _coverage_note(lines, data)
    lines.append("")
    _group_table(lines, data["teams"], "Team", config)
    lines.append("")
    _group_table(lines, data["regions"], "Region", config)
    _group_trends_section(lines, data, config)


def _forecast_integrity(lines, data, config):
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
                     f"{_money(row['amount'], config)}: {violation.detail}")


def _forecast_patterns(lines, data, config):
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

    min_n = config["min_opps_for_owner_score"]
    for p in under:
        # Lead with the evidence actually present; only cite the won-share
        # basis when there are observed wins (printing "n/a (n=0)" beside a
        # name reads as an accusation with no evidence), and only cite the
        # PERCENTAGE at the same resolved-n floor the ledger uses — a bare
        # "100% (n=1)" is the number that gets screenshot into a
        # leaderboard (same persona finding as the ledger rate).
        if not p.n_won:
            won_clause = ""
        elif p.n_won >= min_n:
            won_clause = (f"wins never called commit/best_case "
                          f"{pct(p.undercall_won_share)} (n={p.n_won}); ")
        else:
            won_clause = (f"wins never called commit/best_case "
                          f"(small_n, n={p.n_won}); ")
        lines.append(f"- Undercall pattern: {p.owner} — {won_clause}open "
                     f"pipeline {pct(p.omitted_share)} omitted, "
                     f"{pct(p.farout_share)} far-out (n={p.n_open})")
    small = sum(1 for p in patterns.values()
                if p.overcall_small_n and p.undercall_small_n)
    lines.append(f"- Suppressed as small_n: {small} owners with too little "
                 f"history to score")


def ledger_rate(group, min_n):
    """Won/resolved cell: always the fraction, the percentage only once
    enough commits have resolved. A bare 100% on n=1 is exactly the number
    that gets screenshot into a leaderboard (both persona sims hit it)."""
    resolved = group["won"] + group["lost"]
    if not resolved:
        return "n/a"
    cell = f"{group['won']}/{resolved}"
    if resolved >= min_n:
        cell += f" ({group['won'] / resolved:.0%})"
    return cell


def _commit_ledger(lines, data, config):
    """Forecast-accuracy ledger tables: by owner, by team/region (when
    owner_meta exists), by committed-for quarter. Counts and one derived
    rate; keys sorted alphabetically/chronologically — deliberately NOT
    worst-first (an accuracy leaderboard is one sort away from a comp
    weapon)."""
    min_n = config["min_opps_for_owner_score"]
    lines.append("### Commit accuracy (forecast ledger)")
    lines.append("")
    lines.append("Coaching signal, not a comp input.")
    lines.append("")
    lines.append("Of opps ever forecast commit in stored history: outcome to "
                 "date. Pushed = still open with a close-date move after the "
                 "first commit snapshot; committed-for quarter = fiscal "
                 "quarter of the close date when first called commit (immune "
                 "to later pushes). Won/resolved counts closed opps only, "
                 f"shown as won/closed; the percentage appears once {min_n}+ "
                 "have resolved (small-n rates mislead). Of which pushed "
                 "first = resolved (won/lost) commits that pushed after the "
                 "commit and before closing.")
    lines.append("")
    entries = data["ledger"]
    if entries is None:
        lines.append("Unavailable outside the snapshot store.")
        return
    if not entries:
        lines.append("No opp in stored history has ever been forecast "
                     "commit.")
        return
    # The ledger matures slowly by design; say so, and give the desk-level
    # answer, instead of letting a month-old store read as "n/a everywhere"
    # (the VP persona left the review with no number and no ETA).
    resolved = sum(1 for e in entries if e.outcome in ("won", "lost"))
    lines.append(f"{resolved} of {len(entries)} ever-commit deal(s) resolved "
                 f"so far; rates render at {min_n}+ resolved — expect a full "
                 f"read after roughly a quarter of stored snapshots.")
    lines.append("")
    # Credibility today: deterministic in-snapshot signals so a young store
    # gives a number now instead of all-n/a while win-rates mature. Lead with
    # the strongest (H5 on LIVE commits), then the historical push rate, then a
    # per-team cut that routes coaching budget. All desk/team-level, never
    # per-seller — coaching, not comp.
    ever = len(entries)
    ever_pushed = sum(1 for e in entries
                      if e.outcome == "pushed" or e.pushed_before_close)
    commit_open = [r for r in data["rows"] if is_open(r)
                   and r["forecast_category"] == "commit"]
    commit_h5 = sum(1 for r in commit_open
                    if any(v.rule_id == "H5"
                           for v in data["results"][r["opp_id"]].violations))
    if commit_open:
        lines.append(f"Current-commit integrity: {commit_h5} of "
                     f"{len(commit_open)} "
                     f"({commit_h5 / len(commit_open):.0%}) open "
                     f"commit-forecast opp(s) fail H5 — on a pre-develop stage "
                     f"or without a valid next step.")
    lines.append(f"Historical push rate: {ever_pushed} of {ever} ever-commit "
                 f"deal(s) ({ever_pushed / ever:.0%}) moved their close date "
                 f"after being called commit.")
    owner_meta = config.get("owner_meta") or {}
    if any((m or {}).get("team") for m in owner_meta.values()):
        by_team = {}
        for e in entries:
            team = (owner_meta.get(e.owner) or {}).get("team")
            if team is None:
                continue
            g = by_team.setdefault(team, [0, 0])
            if e.outcome == "pushed" or e.pushed_before_close:
                g[0] += 1
            g[1] += 1
        parts = ", ".join(f"{team} {g[0]}/{g[1]}"
                          for team, g in sorted(by_team.items()))
        lines.append(f"By team (pushed / ever-commit): {parts}.")
    lines.append("")

    def table(label, rollups):
        lines.append(f"| {label} | Ever commit | Won | Lost | Pushed "
                     "| Still open | Won/resolved | Of which pushed first |")
        lines.append("|---|---|---|---|---|---|---|---|")
        total = {"n": 0, "won": 0, "lost": 0, "pushed": 0, "open": 0,
                 "resolved_pushed_first": 0}
        for key in sorted(rollups, key=lambda k: (k is None, k or "")):
            g = rollups[key]
            for stat in total:
                total[stat] += g[stat]
            lines.append(f"| {'unknown' if key is None else key} | {g['n']} "
                         f"| {g['won']} | {g['lost']} | {g['pushed']} "
                         f"| {g['open']} | {ledger_rate(g, min_n)} "
                         f"| {g['resolved_pushed_first']} |")
        lines.append(f"| All | {total['n']} | {total['won']} "
                     f"| {total['lost']} | {total['pushed']} "
                     f"| {total['open']} | {ledger_rate(total, min_n)} "
                     f"| {total['resolved_pushed_first']} |")

    table("Owner", ledger_rollups(entries, lambda e: e.owner))
    owner_meta = config.get("owner_meta") or {}
    for dimension in ("team", "region"):
        if any((m or {}).get(dimension) for m in owner_meta.values()):
            lines.append("")
            table(dimension.capitalize(), ledger_rollups(
                entries,
                lambda e: (owner_meta.get(e.owner) or {}).get(dimension)))
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
    _trajectory(lines, data, config)
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
    _staleness_tiers(lines, data, config)
    _owner_table(lines, data, config)
    lines.append("")
    _team_region_tables(lines, data, config)
    lines.append("")
    _forecast_integrity(lines, data, config)
    lines.append("")
    _forecast_patterns(lines, data, config)
    lines.append("")
    _commit_ledger(lines, data, config)
    lines.append("")
    _since_last_run_detail(lines, data, config)
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

    # The seller's own row from the shared desk brief, verbatim: the figures a
    # manager sees ranked against peers are exactly the figures shown here, so
    # the seller is never scored on a number withheld from their private view.
    lines.append("## Your row in the shared desk brief")
    lines.append("")
    lines.append("These figures also appear, attributed to you by name, in the "
                 "desk-wide brief tables your manager and peers may see — the "
                 "Owners scoreboard, Top exceptions, the slipping-pipeline and "
                 "forecast-pattern lists, the commit ledger, and the "
                 "since-last-run detail — shown here so you see exactly what "
                 "they see. Everything else in this digest is private to you.")
    lines.append("")
    coverage = ("n/a" if stats.coverage_ratio is None
                else f"{stats.coverage_ratio:.2f}x"
                + (" (low_coverage)" if stats.coverage_flagged else ""))
    lines.append(f"- Score: {stats.mean_score:.1f} mean / "
                 f"{stats.median_score:.1f} median")
    lines.append(f"- Open pipeline {_money(stats.open_pipeline, config)}; "
                 f"coverage {coverage}; gap to cover "
                 f"{_gap_to_cover(stats, config)}")
    lines.append(f"- Violations: {stats.violation_count}")
    # When the whole desk is under-covered, say so in the digest too, so a
    # flagged coverage row does not read as a personal failing in isolation
    # (the desk brief already carries this note; the seller never sees it).
    with_cov = [s for s in data["owners"].values()
                if s.coverage_ratio is not None]
    flagged_cov = [s for s in with_cov if s.coverage_flagged]
    share_min = config.get("low_coverage_desk_note_share")
    share_min = 0.75 if share_min is None else share_min
    if stats.coverage_flagged and with_cov \
            and len(flagged_cov) / len(with_cov) > share_min:
        lines.append(f"- Context: {len(flagged_cov)} of {len(with_cov)} owners "
                     f"are under 1.00x this week — desk-wide under-coverage, "
                     f"not a personal finding.")
    lines.append("")

    lines.append("## Top risks (dollar-weighted)")
    lines.append("")
    flagged = [r for r in open_rows if results[r["opp_id"]].violations]
    if not flagged:
        lines.append("No flagged deals this week.")
    flagged.sort(key=lambda r: (-(r["amount"] or 0.0), r["opp_id"]))

    def risk_line(row):
        result = results[row["opp_id"]]
        streak = opp_streak(streaks, result)
        note = f" — flagged {streak} runs" if streak >= 2 else ""
        # Show the observed value and threshold per flag (from the engine's
        # own detail strings), so "why" is a checkable fact, not a bare code.
        evidence = "; ".join(f"{v.rule_id} ({RULE_LABELS[v.rule_id]}: "
                             f"{v.detail})" for v in result.violations)
        return (f"- {row['opp_id']} {row['account']} ({row['stage']}, "
                f"{_money(row['amount'], config)}): {evidence}{note}")

    for row in flagged[:5]:
        lines.append(risk_line(row))
    # EVERY flagged deal is enumerable from the digest alone — a capped list
    # left the owner unable to see (or clear) flags the desk brief counts
    # against them, and made the coaching-focus dollars uncheckable.
    if len(flagged) > 5:
        lines.append("")
        lines.append(f"Also flagged ({len(flagged) - 5} more):")
        for row in flagged[5:]:
            lines.append(risk_line(row))
    esc = config.get("staleness_escalation")
    if esc and any(v.rule_id == "H1" for r in flagged
                   for v in results[r["opp_id"]].violations):
        lines.append("")
        lines.append(f"Staleness tiers, counted beyond your stage's "
                     f"staleness threshold: flag (over threshold), escalate "
                     f"(> threshold + {esc['escalate_days']}d), review "
                     f"(> threshold + {esc['review_days']}d).")

    # One coaching ask per DISTINCT open flag, BOUND to the specific deals it
    # applies to (opp IDs, dollar-ranked), so the seller never has to
    # cross-reference the Top risks list to know which deals each ask touches.
    rule_to_opps = {}
    for row in flagged:
        for rule in results[row["opp_id"]].rule_ids():
            rule_to_opps.setdefault(rule, []).append(row["opp_id"])
    rules_present = sorted(rule_to_opps, key=lambda r: int(r[1:]))
    if rules_present:
        lines.append("")
        lines.append("## What to do this week")
        lines.append("")
        lines.append("One coaching question per open flag, with the deals it "
                     "applies to:")
        for rule in rules_present:
            opps = ", ".join(rule_to_opps[rule])
            lines.append(f"- {rule} ({RULE_LABELS[rule]}) — {opps}: "
                         f"{COACHING_ASKS[rule]}")

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
    lines.append("## Your commit accuracy")
    lines.append("")
    owned_ledger = [e for e in (data["ledger"] or []) if e.owner == owner]
    if data["ledger"] is None:
        lines.append("Unavailable outside the snapshot store.")
    elif not owned_ledger:
        lines.append("None of your deals has been forecast commit in stored "
                     "history.")
    else:
        g = ledger_rollups(owned_ledger, lambda e: e.owner)[owner]
        lines.append(f"Of your {g['n']} ever-commit deal(s): {g['won']} won, "
                     f"{g['lost']} lost, {g['pushed']} pushed, {g['open']} "
                     f"still open. The same numbers, and nothing more, "
                     f"appear in the desk brief's ledger.")

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
                     f"{_money(exposure[focus], config)} at risk across "
                     f"{deals[focus]} deal(s).")
        if focus in COACHING_ASKS:
            lines.append(f"Ask: {COACHING_ASKS[focus]}")

    # Foot reference: only the rules on this seller's deals, with the line each
    # crosses — so "why exactly" is fully checkable without the desk brief's
    # legend (score weights omitted; they read as a comp input).
    if rules_present:
        lines.append("")
        lines.append("## Rule reference")
        lines.append("")
        lines.append("Only the rules on your deals, and the line each one "
                     "crosses:")
        for rule in rules_present:
            trip = rule_trip(rule, config)
            lines.append(f"- {rule} {RULE_LABELS[rule]}"
                         + (f" — {trip}" if trip else ""))
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
        filter_label=None, filter_slug=None):
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
        slug = filter_slug or owner_slug(filter_label)
        name = f"desk_brief_{as_of.isoformat()}_{slug}.md"
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / name
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(markdown)
    return path, data


def _scrub_rows(data):
    """EVERY open commit/best_case opp (flagged or not), dollar-ranked."""
    rows = [r for r in data["rows"] if is_open(r)
            and r["forecast_category"] in ("commit", "best_case")]
    rows.sort(key=lambda r: (-(r["amount"] or 0.0), r["opp_id"]))
    return rows


def render_commit_scrub(data, config):
    """Pre-forecast-call scrub sheet: every open commit/best_case opp with
    its ledger context, the coaching question of its dominant risk flag,
    and blank confirmation checkboxes — grouped by owner with subtotals
    (the call runs rep-by-rep). Self-contained for print: notes and the
    rule legend are on the sheet. No advice copy — the checklist is the
    cadence."""
    as_of = data["as_of"]
    fy_start = config["fiscal_year_start_month"]
    quarter = fiscal_quarter(as_of, fy_start)
    lines = [f"# Commit scrub — {as_of.isoformat()}",
             "",
             f"Snapshot {data['snapshot_date'].isoformat()}, evaluated as "
             f"of {as_of.isoformat()}. "
             f"{days_left_in_quarter(as_of, fy_start)} day(s) left in "
             f"{quarter}.",
             ""]
    if data.get("filter_label"):
        lines.append(f"FILTERED — {data['filter_label']}. Covers only this "
                     "selection.")
        lines.append("")
    rows = _scrub_rows(data)
    quarter_end = fiscal_quarter_end(as_of, fy_start)
    commit_rows = [r for r in rows if r["forecast_category"] == "commit"
                   and r["close_date"] <= quarter_end]
    # Reconcile to the brief headline's "Forecast call" line (same universe,
    # same at-risk-within math) so the sheet the manager works from carries the
    # exact number she reads to the VP, split by bucket with the at-risk slice
    # of each named — no off-sheet arithmetic under time pressure.
    results = data["results"]

    def _bucket(cat):
        rr = [r for r in rows if r["forecast_category"] == cat]
        return (len(rr), sum(r["amount"] or 0.0 for r in rr),
                sum(r["amount"] or 0.0 for r in rr
                    if results[r["opp_id"]].has_high()),
                sum(1 for r in rr if results[r["opp_id"]].has_high()))
    c_n, c_tot, c_risk, c_risk_n = _bucket("commit")
    b_n, b_tot, b_risk, b_risk_n = _bucket("best_case")
    # Display total as the sum of the displayed (rounded) bucket parts, so the
    # printed "commit + best_case = total" ties exactly (independent rounding
    # was $1 short in a persona hand-check of the number she defends).
    total_disp = round(c_tot) + round(b_tot)
    lines.append(f"{len(rows)} open commit/best_case opp(s), "
                 f"{_money(total_disp, config)}. Commit-forecast closing "
                 f"{quarter} or earlier (overdue included): "
                 f"{_money(sum(r['amount'] or 0.0 for r in commit_rows), config)} "
                 f"across {len(commit_rows)} deal(s).")
    lines.append(f"- Forecast call: commit {_money(c_tot, config)} ({c_n}) + "
                 f"best_case {_money(b_tot, config)} ({b_n}) = "
                 f"{_money(total_disp, config)} ({len(rows)}).")
    lines.append(f"- At-risk within (high-severity flag): commit "
                 f"{_money(c_risk, config)} across {c_risk_n} deal(s); "
                 f"best_case {_money(b_risk, config)} across {b_risk_n} "
                 f"deal(s). Rows counting toward it carry a high-severity flag "
                 f"in the Rules column.")
    lines.append("")
    if not rows:
        return "\n".join(lines)
    ledger_by_id = {e.opp_id: e for e in (data.get("ledger") or [])}
    weights = config["rule_weights"]

    def ask(result):
        risky = [v for v in result.violations if v.rule_id in RISKY_RULES]
        if not risky:
            return "-"
        dominant = min(risky, key=lambda v: (_SEV_RANK[v.severity],
                                             -weights[v.rule_id],
                                             int(v.rule_id[1:])))
        return COACHING_PROMPTS[dominant.rule_id]

    by_owner = {}
    for row in rows:
        by_owner.setdefault(row["owner"], []).append(row)
    for owner in sorted(by_owner):
        owned = by_owner[owner]
        lines.append(f"## {owner} — {len(owned)} opp(s), "
                     f"{_money(sum(r['amount'] or 0.0 for r in owned), config)}")
        lines.append("")
        lines.append("| Opp | Account | Stage | Forecast | Amount "
                     "| Close date | Committed-for | Pushes | Streak "
                     "| Rules | Ask | Close date confirmed "
                     "| Next step confirmed | Budget confirmed |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|---"
                     "|---|---|---|")
        for row in owned:
            result = data["results"][row["opp_id"]]
            badges = " ".join(v.rule_id for v in result.violations) or "-"
            streak = opp_streak(data["streaks"], result)
            streak_cell = f"flagged {streak} runs" if streak >= 2 else "-"
            entry = ledger_by_id.get(row["opp_id"])
            committed = (entry.committed_quarter or "-") if entry else "-"
            pushes = row.get("push_count")
            lines.append(f"| {row['opp_id']} | {row['account']} "
                         f"| {row['stage']} | {row['forecast_category']} "
                         f"| {_money(row['amount'], config)} "
                         f"| {row['close_date'].isoformat()} | {committed} "
                         f"| {'-' if pushes is None else pushes} "
                         f"| {streak_cell} | {badges} | {ask(result)} "
                         f"| [ ] | [ ] | [ ] |")
        lines.append("")
    lines.append("Committed-for \"-\" = never forecast commit in stored "
                 "history (a best_case-only deal has no commit anchor). "
                 "\"Ask\" = the coaching prompt of the deal's dominant risk "
                 "flag; \"-\" when unflagged.")
    lines.append("")
    _rule_legend(lines, config)
    lines.append("")
    return "\n".join(lines)


def run_commit_scrub(store, snapshot_date, as_of, config, out_dir,
                     owner_filter=None, filter_label=None, filter_slug=None):
    """Build and write the scrub sheet to out/commit_scrub_<as_of>.md.
    Returns (path, data). NEVER goes through run(): no run is recorded and
    the brief filename is untouched; a filtered scrub gets a suffix like a
    filtered brief."""
    data = build(store, snapshot_date, as_of, config,
                 owner_filter=owner_filter, filter_label=filter_label)
    markdown = render_commit_scrub(data, config)
    name = f"commit_scrub_{as_of.isoformat()}.md"
    if owner_filter is not None:
        slug = filter_slug or owner_slug(filter_label)
        name = f"commit_scrub_{as_of.isoformat()}_{slug}.md"
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / name
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(markdown)
    return path, data


def resolve_owner_filter(config, snapshot_owners, owners, teams,
                         regions=None):
    """Resolve --owner/--team/--region values into (owner set, label, slug).
    Matching is case-insensitive, and a team may be named without its
    "Team " prefix ("na-east-1" finds "Team NA-East-1"); anything else
    unknown is an error naming the known values, never a guess. Owners are
    checked against the snapshot plus quotas/owner_meta (an owner with a
    quota but no open opps is still filterable)."""
    owner_meta = config.get("owner_meta") or {}
    known_owners = (set(snapshot_owners) | set(config.get("quotas") or {})
                    | set(owner_meta))

    def canonical(value, known, kind):
        if not owner_meta and kind != "owner":
            raise ValueError(
                f"--{kind} needs {kind} metadata; pass --quotas pointing at "
                "a JSON with an 'owners' block (e.g. "
                "data/seed_manifest.json)")
        by_fold = {k.casefold(): k for k in known}
        folded = value.casefold()
        match = by_fold.get(folded)
        if match is None and kind == "team":
            match = by_fold.get(f"team {folded}")
        if match is None:
            hint = (", ".join(sorted(known)) if kind != "owner"
                    else "check the snapshot's owner names (and the "
                         "--quotas file)")
            raise ValueError(f"unknown {kind} {value!r}; "
                             + (f"known {kind}s: {hint}" if kind != "owner"
                                else hint))
        return match

    selected, parts = set(), []
    for kind, values in (("team", teams), ("region", regions),
                         ("owner", owners)):
        for value in values or []:
            if kind == "owner":
                name = canonical(value, known_owners, kind)
                selected.add(name)
            else:
                known = {m.get(kind) for m in owner_meta.values()} - {None}
                name = canonical(value, known, kind)
                selected |= {o for o, m in owner_meta.items()
                             if m.get(kind) == name}
            parts.append((kind, name))
    label = "; ".join(f"{kind} {name}" for kind, name in parts)
    slug = owner_slug(" ".join(name for _, name in parts))
    return selected, label, slug


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m src.brief",
                                description="Generate the dated desk brief")
    p.add_argument("--as-of", type=date.fromisoformat, default=None)
    # Honor the same env vars as ingest and the dashboard, so a caller who
    # isolates the store with PIPELINE_HYGIENE_DB does not silently get a brief
    # off the default DB (a persona ran a plausible, entirely-wrong brief).
    p.add_argument("--db",
                   default=os.environ.get("PIPELINE_HYGIENE_DB",
                                          "data/pipeline.db"))
    p.add_argument("--config",
                   default=os.environ.get("PIPELINE_HYGIENE_CONFIG",
                                          "config.yaml"))
    p.add_argument("--out-dir",
                   default=os.environ.get("PIPELINE_HYGIENE_OUT", "out"))
    p.add_argument("--snapshot-date", type=date.fromisoformat, default=None,
                   help="defaults to the latest stored snapshot at or before --as-of")
    p.add_argument("--quotas", default=os.environ.get("PIPELINE_HYGIENE_QUOTAS"),
                   help="JSON file whose 'quotas' mapping (e.g. data/seed_manifest.json) "
                        "is merged into config at run time; its 'owners' block, when "
                        "present, also supplies per-owner team/region for the rollups")
    p.add_argument("--digests", action="store_true",
                   help="also write private per-owner coaching digests to "
                        "<out-dir>/digests/<as-of>/<owner_slug>.md; combine "
                        "with --team/--owner/--region to write digests for "
                        "just that selection (a manager's own reports)")
    p.add_argument("--commit-scrub", action="store_true",
                   help="write ONLY the pre-forecast-call scrub sheet "
                        "(every open commit/best_case opp with checklist "
                        "columns) to <out-dir>/commit_scrub_<as-of>.md; "
                        "no brief, no run recorded")
    p.add_argument("--owner", action="append", metavar="NAME",
                   help="repeatable; restrict the whole brief to these owners "
                        "(all numbers recomputed over the selection; the run "
                        "is not recorded and the file gets a suffix)")
    p.add_argument("--team", action="append", metavar="NAME",
                   help="repeatable; restrict to every owner of these teams "
                        "(team membership from the --quotas 'owners' block); "
                        "combines with --owner/--region; case-insensitive, "
                        "'Team ' prefix optional")
    p.add_argument("--region", action="append", metavar="NAME",
                   help="repeatable; restrict to every owner of these "
                        "regions (from the --quotas 'owners' block); "
                        "combines with --owner/--team")
    args = p.parse_args(argv)

    # CLI entry point: the only place date.today() is allowed (spec).
    as_of = args.as_of if args.as_of is not None else date.today()
    config = load_config(args.config)
    if args.quotas:
        with open(args.quotas, encoding="utf-8") as f:
            payload = json.load(f)
        config = merge_quota_payload(config, payload)

    # Echo the store actually read, so "cron called success on the wrong DB"
    # is visible rather than silent (the number looks plausible either way).
    print(f"reading snapshot store {args.db}", file=sys.stderr)
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

    owner_filter, filter_label, filter_slug = None, None, None
    if args.owner or args.team or args.region:
        try:
            owner_filter, filter_label, filter_slug = resolve_owner_filter(
                config, store.owners(snapshot_date), args.owner, args.team,
                args.region)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    if args.commit_scrub:
        path, data = run_commit_scrub(store, snapshot_date, as_of, config,
                                      args.out_dir,
                                      owner_filter=owner_filter,
                                      filter_label=filter_label,
                                      filter_slug=filter_slug)
        filtered = f", filtered to {filter_label}" if filter_label else ""
        print(f"wrote {path} ({len(_scrub_rows(data))} open "
              f"commit/best_case opps{filtered})")
        return 0

    path, data = run(store, snapshot_date, as_of, config, args.out_dir,
                     owner_filter=owner_filter, filter_label=filter_label,
                     filter_slug=filter_slug)
    desk = data["desk"]
    filtered = f", filtered to {filter_label}" if filter_label else ""
    print(f"wrote {path} (snapshot {snapshot_date}, {desk.n_open} open opps, "
          f"at-risk {_money(desk.at_risk_dollars, config)}{filtered})")
    if owner_filter is not None and not args.digests:
        print("tip: add --digests to write coaching digests for just this "
              "selection")
    if args.digests:
        paths = write_digests(data, config, args.out_dir)
        print(f"wrote {len(paths)} coaching digests to "
              f"{Path(args.out_dir) / 'digests' / as_of.isoformat()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
