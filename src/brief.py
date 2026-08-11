"""Desk brief generator: python -m src.brief --as-of YYYY-MM-DD

Writes out/desk_brief_<as_of>.md from the snapshot store: desk headline,
validation summary, fiscal-quarter segmentation, top exceptions, per-owner
table, forecast-integrity (H5) detail, and since-last-run deltas from the
runs table.

Every time evaluation takes the explicit as_of; date.today() appears only as
the CLI --as-of default. Since-last-run semantics match the seed delta
manifest: violations that vanish because a deal closed are reported under
"closed", never under "cleared".
"""
import argparse
import json
import sys
from datetime import date
from pathlib import Path

from .ingest import load_config
from .rules import evaluate_snapshot, is_open
from .scoring import desk_rollup, opp_score, owner_rollups
from .snapshots import SnapshotStore

_SEV_RANK = {"high": 0, "medium": 1, "low": 2}


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


def build(store, snapshot_date, as_of, config):
    """Compute all brief data for one snapshot. No side effects."""
    rows = store.rows_with_history(snapshot_date)
    results = evaluate_snapshot(rows, config, as_of)
    desk = desk_rollup(rows, results, config)
    insufficient = {"H3": 0, "H6": 0}
    for result in results.values():
        for item in result.insufficient:
            insufficient[item.rule_id] += 1
    prev = store.last_run()
    return {
        "snapshot_date": snapshot_date,
        "as_of": as_of,
        "rows": rows,
        "results": results,
        "desk": desk,
        "owners": owner_rollups(rows, results, config),
        "validation": store.validation_report_dict(snapshot_date),
        "insufficient": insufficient,
        "since_last_run": since_last_run(
            prev["summary"] if prev else None, rows, results, desk),
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


def _since_last_run(lines, data):
    lines.append("## Since last run")
    lines.append("")
    delta = data["since_last_run"]
    if delta is None:
        lines.append("No previous run recorded.")
        return
    lines.append(f"Previous run: snapshot {delta['prev_snapshot_date']}, "
                 f"as of {delta['prev_as_of']}.")
    lines.append("")
    change = delta["score_change"]
    if change["prev"] is not None and change["current"] is not None:
        lines.append(f"- Desk score (weighted mean): {change['prev']:.1f} -> "
                     f"{change['current']:.1f} "
                     f"({change['current'] - change['prev']:+.1f})")
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


def _fiscal_quarters(lines, data, config):
    fy_start = config["fiscal_year_start_month"]
    lines.append(f"## Fiscal quarters (fiscal year starts month {fy_start})")
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
    lines.append("## Top 10 exceptions")
    lines.append("")
    rows_by_id = {r["opp_id"]: r for r in data["rows"]}
    flagged = [r for r in data["results"].values() if r.violations]
    if not flagged:
        lines.append("No violations.")
        return

    def rank(result):
        worst = min(_SEV_RANK[v.severity] for v in result.violations)
        return (worst, -(rows_by_id[result.opp_id]["amount"] or 0.0), result.opp_id)

    lines.append("| # | Opp | Account | Owner | Stage | Amount | Score | Rules | Detail |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for i, result in enumerate(sorted(flagged, key=rank)[:10], start=1):
        row = rows_by_id[result.opp_id]
        badges = " ".join(v.rule_id for v in result.violations)
        detail = "; ".join(v.detail for v in result.violations)
        lines.append(f"| {i} | {result.opp_id} | {row['account']} | {row['owner']} "
                     f"| {row['stage']} | {_money(row['amount'])} "
                     f"| {opp_score(result, config)} | {badges} | {detail} |")


def _owner_table(lines, data):
    lines.append("## Owners")
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
    lines.append("## Forecast integrity (H5)")
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
    _since_last_run(lines, data)
    lines.append("")
    _fiscal_quarters(lines, data, config)
    lines.append("")
    _top_exceptions(lines, data, config)
    lines.append("")
    _owner_table(lines, data)
    lines.append("")
    _forecast_integrity(lines, data)
    lines.append("")
    return "\n".join(lines)


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
    return 0


if __name__ == "__main__":
    sys.exit(main())
