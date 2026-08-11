"""Sandbagging / happy-ears detection from stored forecast-category history.

Uniquely enabled by the snapshot store: a single CRM export cannot show that
a deal was called commit and then slipped, or won without ever being called.
Everything here is a COACHING SIGNAL, NOT A COMP INPUT — render that
disclaimer wherever these metrics are shown (Goodhart: reps game categories
that feed comp).

Per owner, over stored history <= as_of (explicit as_of; no date.today()):

- Overcall ("happy ears"): among opps that ever showed forecast_category
  commit, the share subsequently pushed (any later close-date move in a
  snapshot pair after the first commit snapshot) or closed_lost.
- Undercall ("sandbagging"): share of closed-won opps never in
  commit/best_case before the winning snapshot (counting only wins observed
  open in at least one earlier snapshot — an outcome with no pre-win history
  cannot inform forecast integrity), plus omitted-heavy far-out open
  pipeline: the omitted dollar share AND the far-out dollar share
  (close_date beyond as_of + close_date_horizon_days) must both be high —
  a conjunction, because omitted-dollar share alone is dominated by
  single-big-deal noise at per-owner sample sizes.

Metrics are reported with their n and suppressed (never flagged) under
min_opps_for_owner_score, carrying small_n flags instead.
"""
from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class OwnerPatterns:
    owner: str
    # overcall
    n_ever_commit: int
    overcall_share: float          # None when n_ever_commit == 0
    overcall_small_n: bool
    overcall_flagged: bool
    # undercall
    n_won: int                     # wins with observed pre-win history
    undercall_won_share: float     # None when n_won == 0
    n_open: int
    omitted_share: float           # None when no open pipeline dollars
    farout_share: float            # None when no open pipeline dollars
    undercall_small_n: bool
    undercall_flagged: bool


def _histories(store, snapshot_date):
    """{opp_id: [(snapshot_date, owner, stage, forecast, close_date), ...]}
    ascending, over snapshots <= snapshot_date. One query, one pass."""
    cur = store.conn.execute(
        "SELECT opp_id, snapshot_date, owner, stage, forecast_category, "
        "close_date FROM opportunities WHERE snapshot_date <= ? "
        "ORDER BY opp_id, snapshot_date",
        (snapshot_date.isoformat(),))
    histories = {}
    for opp_id, snap, owner, stage, forecast, close in cur:
        histories.setdefault(opp_id, []).append(
            (snap, owner, stage, forecast, close))
    return histories


def owner_patterns(store, as_of, config, snapshot_date=None):
    """Per-owner OwnerPatterns over stored history <= snapshot_date, keyed by
    owner, sorted. Owners are attributed from each opp's row at that snapshot.

    snapshot_date defaults to as_of for compatibility, but callers evaluating a
    specific stored snapshot should pass it explicitly so forecast-pattern
    metrics cannot read future snapshots while the rest of the brief/dashboard
    is anchored to an older snapshot.
    """
    snapshot_date = snapshot_date or as_of
    thresholds = config["patterns"]
    min_n = config["min_opps_for_owner_score"]
    horizon_cap = as_of + timedelta(days=config["close_date_horizon_days"])

    per_owner = {}

    def bucket(owner):
        return per_owner.setdefault(owner, {
            "ever_commit": 0, "overcalled": 0,
            "won": 0, "won_never_called": 0,
            "open_dollars": 0.0, "omitted_dollars": 0.0,
            "farout_dollars": 0.0, "n_open": 0})

    snapshot_dates = [d for d in store.snapshot_dates() if d <= snapshot_date]
    last_snapshot = snapshot_dates[-1].isoformat() if snapshot_dates else None

    for opp_id, history in _histories(store, snapshot_date).items():
        owner = history[-1][1]
        stats = bucket(owner)
        last_stage = history[-1][2]

        # overcall: ever commit -> subsequently pushed or lost
        commit_at = next((snap for snap, _o, _s, forecast, _c in history
                          if forecast == "commit"), None)
        if commit_at is not None:
            stats["ever_commit"] += 1
            pushed = any(
                prev_close is not None and cur_close is not None
                and cur_close > prev_close and cur_snap > commit_at
                for (_, _, _, _, prev_close), (cur_snap, _, _, _, cur_close)
                in zip(history, history[1:]))
            if pushed or last_stage == "closed_lost":
                stats["overcalled"] += 1

        # undercall: wins observed open before the winning snapshot
        if last_stage == "closed_won":
            win_snap = next(snap for snap, _, stage, _, _ in history
                            if stage == "closed_won")
            before = [h for h in history if h[0] < win_snap]
            if any(h[2] not in ("closed_won", "closed_lost") for h in before):
                stats["won"] += 1
                if all(h[3] not in ("commit", "best_case") for h in before):
                    stats["won_never_called"] += 1

    # omitted/far-out open pipeline from the evaluated snapshot
    if last_snapshot is not None:
        cur = store.conn.execute(
            "SELECT owner, forecast_category, close_date, amount "
            "FROM opportunities WHERE snapshot_date = ? "
            "AND stage NOT IN ('closed_won', 'closed_lost')",
            (last_snapshot,))
        for owner, forecast, close, amount in cur:
            stats = bucket(owner)
            dollars = amount or 0.0
            stats["n_open"] += 1
            stats["open_dollars"] += dollars
            if forecast == "omitted":
                stats["omitted_dollars"] += dollars
            if close is not None and date.fromisoformat(close) > horizon_cap:
                stats["farout_dollars"] += dollars

    patterns = {}
    for owner in sorted(per_owner):
        stats = per_owner[owner]
        n_commit, n_won, n_open = (stats["ever_commit"], stats["won"],
                                   stats["n_open"])
        overcall = (stats["overcalled"] / n_commit) if n_commit else None
        won_share = (stats["won_never_called"] / n_won) if n_won else None
        open_dollars = stats["open_dollars"]
        omitted_share = (stats["omitted_dollars"] / open_dollars
                         if open_dollars > 0 else None)
        farout_share = (stats["farout_dollars"] / open_dollars
                        if open_dollars > 0 else None)
        overcall_small = n_commit < min_n
        undercall_small = n_won < min_n and n_open < min_n
        patterns[owner] = OwnerPatterns(
            owner=owner,
            n_ever_commit=n_commit,
            overcall_share=overcall,
            overcall_small_n=overcall_small,
            overcall_flagged=(not overcall_small and overcall is not None
                             and overcall
                             >= thresholds["overcall_share_min"]),
            n_won=n_won,
            undercall_won_share=won_share,
            n_open=n_open,
            omitted_share=omitted_share,
            farout_share=farout_share,
            undercall_small_n=undercall_small,
            undercall_flagged=(
                (n_won >= min_n and won_share is not None and won_share
                 >= thresholds["undercall_won_share_min"])
                or (n_open >= min_n and omitted_share is not None
                    and omitted_share
                    >= thresholds["undercall_omitted_share_min"]
                    and farout_share
                    >= thresholds["undercall_farout_share_min"])),
        )
    return patterns
