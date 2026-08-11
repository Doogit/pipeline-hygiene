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


@dataclass(frozen=True)
class OwnerStats:
    owner: str
    n_open: int
    mean_score: float
    median_score: float
    violation_count: int
    open_pipeline: float
    coverage_ratio: float          # None when no quota configured
    coverage_flagged: bool
    small_n: bool


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


def owner_rollups(rows, results, config):
    quotas = config.get("quotas") or {}
    by_owner = {}
    for row in _open_rows(rows):
        by_owner.setdefault(row["owner"], []).append(row)
    stats = {}
    for owner in sorted(by_owner):
        owned = by_owner[owner]
        scores = [opp_score(results[r["opp_id"]], config) for r in owned]
        pipeline = sum(r["amount"] or 0.0 for r in owned)
        quota = quotas.get(owner)
        coverage = (pipeline / quota) if quota else None
        stats[owner] = OwnerStats(
            owner=owner,
            n_open=len(owned),
            mean_score=mean(scores),
            median_score=median(scores),
            violation_count=sum(len(results[r["opp_id"]].violations) for r in owned),
            open_pipeline=pipeline,
            coverage_ratio=coverage,
            coverage_flagged=coverage is not None and coverage < config["coverage_ratio_min"],
            small_n=len(owned) < config["min_opps_for_owner_score"],
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
