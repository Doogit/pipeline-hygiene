"""Opportunity / owner / desk scoring over rule results.

- Opp score: start 100, deduct rule_weights per violation, floor 0.
- Owner: mean AND median of opp scores, n(open), violation count, open
  pipeline $, coverage ratio vs quota; small_n flag under
  min_opps_for_owner_score.
- Desk: amount-weighted mean, unweighted median, % open opps >= healthy
  threshold — all three, so one big clean deal cannot mask a rotten tail.
- At-risk dollars: sum of amount over DISTINCT open opps with >=1
  high-severity violation. Never summed per violation.
"""
from dataclasses import dataclass
from statistics import mean, median

from .rules import HIGH, is_open


def opp_score(result, config):
    weights = config["rule_weights"]
    return max(0, 100 - sum(weights[v.rule_id] for v in result.violations))


def fiscal_quarter(d, fy_start_month):
    """FY<year>-Q<n>; fiscal years are named for the calendar year they end in
    (fy_start_month 7 puts 2026-08 in FY2027-Q1). Lives here so brief and
    patterns share one definition without an import cycle."""
    quarter = (d.month - fy_start_month) % 12 // 3 + 1
    fy_year = d.year + (1 if fy_start_month > 1 and d.month >= fy_start_month else 0)
    return f"FY{fy_year}-Q{quarter}"


@dataclass(frozen=True)
class OwnerStats:
    owner: str
    n_open: int
    mean_score: float
    median_score: float
    violation_count: int
    open_pipeline: float
    coverage_ratio: float          # pipeline vs required (see _coverage_vs_required)
    coverage_flagged: bool         # exactly coverage_ratio < 1.0
    small_n: bool


@dataclass(frozen=True)
class GroupStats:
    """A team or region roll-up: coverage uses the same remaining-quota math
    as OwnerStats, summed over the group's roster."""
    key: str                       # team or region name
    dimension: str                 # "team" | "region"
    n_owners: int                  # distinct owners with open opps
    n_open: int
    mean_score: float              # None when the group has no open opps
    median_score: float
    violation_count: int
    open_pipeline: float
    quota: float                   # summed roster quota (0.0 when unknown)
    coverage_ratio: float          # pipeline vs required (see _coverage_vs_required)
    coverage_flagged: bool         # exactly coverage_ratio < 1.0
    at_risk_dollars: float


@dataclass(frozen=True)
class DeskStats:
    n_open: int
    weighted_mean_score: float     # amount-weighted; None when no open opps
    median_score: float
    pct_healthy: float
    at_risk_dollars: float
    violation_counts_by_severity: dict


def _open_rows(rows):
    return [r for r in rows if is_open(r)]


def at_risk_dollars(rows, results):
    """Distinct open opps with >=1 high-severity violation; None amounts add 0."""
    total = 0.0
    for row in _open_rows(rows):
        result = results.get(row["opp_id"])
        if result is not None and result.has_high():
            total += row["amount"] or 0.0
    return total


def required_coverage_multiple(outcomes, config):
    """Required pipeline coverage multiple = 1 / trailing win rate over stored
    closed outcomes; falls back to config coverage_ratio_min when closed
    history is thin. Single source of truth for the desk headline and for
    owner/team low_coverage flags, so they never disagree. Returns
    (multiple, basis) with the basis recorded verbatim."""
    outcomes = outcomes or []
    won = [o for o in outcomes if o["stage"] == "closed_won"]
    n_closed = len(outcomes)
    if n_closed >= config["min_closed_for_win_rate"] and won:
        win_rate = len(won) / n_closed
        multiple = 1.0 / win_rate
        # The fraction lets a reader reproduce the required-pipeline number
        # exactly (remaining quota x n_closed/n_won); a rounded multiple
        # alone breaks the napkin check by thousands of dollars.
        basis = (f"trailing win rate {len(won)}/{n_closed} closed won "
                 f"({win_rate:.1%}) -> required multiple "
                 f"{n_closed}/{len(won)} = {multiple:.2f}x")
    else:
        multiple = config["coverage_ratio_min"]
        basis = (f"config coverage_ratio_min {multiple:.1f}x (stored "
                 f"closed history insufficient: {n_closed} outcomes < "
                 f"{config['min_closed_for_win_rate']})")
    return multiple, basis


def _coverage_vs_required(pipeline, quota, won_this_quarter, multiple):
    """Coverage on the flag's own basis: open pipeline / required, where
    required = remaining quota (net of what is already won this quarter) x the
    required multiple. Returns (ratio, flagged) with flagged exactly
    ratio < 1.0, so the shown number and the low_coverage flag can never
    disagree (the persona pass caught a raw pipeline/quota column reading
    1.18 beside a low_coverage flag computed on this basis). No quota, or
    quota fully retired this quarter, -> (None, False)."""
    if not quota:
        return None, False
    required = max(quota - won_this_quarter, 0.0) * multiple
    if required <= 0:
        return None, False
    ratio = pipeline / required
    return ratio, ratio < 1.0


def owner_rollups(rows, results, config, multiple=None, won_by_owner=None):
    """Per-owner open-pipeline roll-up. multiple/won_by_owner carry the
    remaining-quota coverage basis (from required_coverage_multiple and the
    outcomes for this snapshot); when omitted the flag falls back to the
    static coverage_ratio_min, matching in-memory uploads with no history."""
    quotas = config.get("quotas") or {}
    won_by_owner = won_by_owner or {}
    threshold = multiple if multiple is not None else config["coverage_ratio_min"]
    by_owner = {}
    for row in _open_rows(rows):
        by_owner.setdefault(row["owner"], []).append(row)
    stats = {}
    for owner in sorted(by_owner):
        owned = by_owner[owner]
        scores = [opp_score(results[r["opp_id"]], config) for r in owned]
        pipeline = sum(r["amount"] or 0.0 for r in owned)
        coverage, flagged = _coverage_vs_required(
            pipeline, quotas.get(owner) or 0.0,
            won_by_owner.get(owner, 0.0), threshold)
        stats[owner] = OwnerStats(
            owner=owner,
            n_open=len(owned),
            mean_score=mean(scores),
            median_score=median(scores),
            violation_count=sum(len(results[r["opp_id"]].violations) for r in owned),
            open_pipeline=pipeline,
            coverage_ratio=coverage,
            coverage_flagged=flagged,
            small_n=len(owned) < config["min_opps_for_owner_score"],
        )
    return stats


def group_rollups(rows, results, config, owner_meta, dimension,
                  multiple=None, won_by_owner=None):
    """Aggregate open opps by team or region (dimension keyed in owner_meta:
    {owner: {"team", "region"}}). Quota, won-this-quarter and the coverage
    flag sum over the group's full roster, so coverage stays comparable to the
    per-owner numbers. Returns {} when no owner_meta is configured."""
    if not owner_meta:
        return {}
    quotas = config.get("quotas") or {}
    won_by_owner = won_by_owner or {}
    threshold = multiple if multiple is not None else config["coverage_ratio_min"]

    # Roster quota and won-this-quarter per group, over every known owner
    # (an owner with a quota but no open opps still counts toward coverage).
    quota_by_group, won_by_group = {}, {}
    for owner, meta in owner_meta.items():
        group = meta.get(dimension)
        if group is None:
            continue
        quota_by_group[group] = (quota_by_group.get(group, 0.0)
                                 + (quotas.get(owner) or 0.0))
        won_by_group[group] = (won_by_group.get(group, 0.0)
                               + won_by_owner.get(owner, 0.0))

    grouped = {}
    for row in _open_rows(rows):
        meta = owner_meta.get(row["owner"])
        group = meta.get(dimension) if meta else None
        if group is None:
            continue
        grouped.setdefault(group, []).append(row)

    stats = {}
    for group in sorted(set(grouped) | set(quota_by_group)):
        owned = grouped.get(group, [])
        scores = [opp_score(results[r["opp_id"]], config) for r in owned]
        pipeline = sum(r["amount"] or 0.0 for r in owned)
        quota = quota_by_group.get(group, 0.0)
        coverage, flagged = _coverage_vs_required(
            pipeline, quota, won_by_group.get(group, 0.0), threshold)
        stats[group] = GroupStats(
            key=group,
            dimension=dimension,
            n_owners=len({r["owner"] for r in owned}),
            n_open=len(owned),
            mean_score=mean(scores) if scores else None,
            median_score=median(scores) if scores else None,
            violation_count=sum(len(results[r["opp_id"]].violations)
                                for r in owned),
            open_pipeline=pipeline,
            quota=quota,
            coverage_ratio=coverage,
            coverage_flagged=flagged,
            at_risk_dollars=at_risk_dollars(owned, results),
        )
    return stats


def desk_rollup(rows, results, config):
    open_rows = _open_rows(rows)
    severity_counts = {"high": 0, "medium": 0, "low": 0}
    for result in results.values():
        for violation in result.violations:
            severity_counts[violation.severity] += 1
    if not open_rows:
        return DeskStats(0, None, None, None, 0.0, severity_counts)
    scores = {r["opp_id"]: opp_score(results[r["opp_id"]], config) for r in open_rows}
    weighted = [(scores[r["opp_id"]], r["amount"] or 0.0) for r in open_rows]
    total_weight = sum(w for _, w in weighted)
    if total_weight > 0:
        weighted_mean = sum(s * w for s, w in weighted) / total_weight
    else:
        weighted_mean = mean(scores.values())   # degenerate: no dollar weights
    healthy = sum(1 for s in scores.values() if s >= config["healthy_score_threshold"])
    return DeskStats(
        n_open=len(open_rows),
        weighted_mean_score=weighted_mean,
        median_score=median(scores.values()),
        pct_healthy=100.0 * healthy / len(open_rows),
        at_risk_dollars=at_risk_dollars(rows, results),
        violation_counts_by_severity=severity_counts,
    )
