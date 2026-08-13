"""Org-specific backtest: for every hygiene rule, how did the opps it flagged
actually resolve?

Deterministic and auditable — replays the exact rule engine over every stored
snapshot at-or-before the evaluation date, collects the distinct opps each rule
ever flagged (while open), and joins them to their final observed outcome
(won / lost / still open). Turns a vendor's benchmark claim ("this pattern
predicts slippage") into evidence from the org's own history.

Coaching signal, never a comp input — same footing as the forecast-integrity
patterns. Pure over a SnapshotStore, so it is unit-testable without the CLI.
"""
import argparse
import os
import sys
from datetime import date

from .ingest import load_config
from .brief import merge_quota_payload
from .rules import RULE_LABELS, evaluate_snapshot
from .snapshots import SnapshotStore


def backtest(store, config, as_of):
    """Per-rule flagged-vs-outcome records over stored history <= as_of.

    Returns (records, meta). Each record:
        rule, flag, flagged (distinct opps this rule ever flagged while open),
        won, lost, still_open, resolved (won+lost), lost_share (lost/resolved,
        or None when resolved < the min-resolution basis).
    meta carries the window (snapshot dates used) and min_n for the caption.
    An opp flagged by a rule in several snapshots counts once for that rule.
    """
    from .funnel import resolve_aging_config
    config = resolve_aging_config(store, config, as_of)
    dates = [d for d in store.snapshot_dates() if d <= as_of]
    # Final outcome per opp: closed rows are terminal, so closed_outcomes (the
    # last observed row per opp, kept only if closed) is the whole resolution
    # set — every other flagged opp is still open.
    closed = {o["opp_id"]: o["stage"] for o in store.closed_outcomes(as_of)}

    flagged = {rule: set() for rule in RULE_LABELS}
    for d in dates:
        results = evaluate_snapshot(store.rows_with_history(d), config, d)
        for result in results.values():
            for v in result.violations:
                flagged[v.rule_id].add(result.opp_id)

    min_n = config["min_closed_for_win_rate"]
    records = []
    for rule, label in RULE_LABELS.items():
        opps = flagged[rule]
        won = sum(1 for o in opps if closed.get(o) == "closed_won")
        lost = sum(1 for o in opps if closed.get(o) == "closed_lost")
        resolved = won + lost
        records.append({
            "rule": rule,
            "flag": label,
            "flagged": len(opps),
            "won": won,
            "lost": lost,
            "still_open": len(opps) - resolved,
            "resolved": resolved,
            "lost_share": (lost / resolved) if resolved >= min_n else None,
        })
    return records, {"dates": dates, "min_n": min_n}


def render_backtest(records, meta, config, as_of):
    """Standalone markdown report — a parity-exempt artifact, like the brief
    and digests, so it never touches the frozen dashboard/brief goldens."""
    dates = meta["dates"]
    lines = [f"# Rule backtest — {as_of.isoformat()}", ""]
    if not dates:
        lines.append("No stored snapshots at or before this date — nothing to "
                     "backtest. Run `python -m src.ingest` first.")
        return "\n".join(lines) + "\n"

    lines += [
        f"History: {len(dates)} snapshot(s), {dates[0].isoformat()} → "
        f"{dates[-1].isoformat()}.",
        "",
        "For every rule, the distinct open opps it flagged anywhere in stored "
        "history, joined to each opp's final outcome. Still-open deals are "
        "unresolved (no verdict yet). Coaching signal, never a comp input.",
        "",
        "| rule | flag | flagged | won | lost | still open | lost when resolved |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for r in records:
        share = "n/a" if r["lost_share"] is None else f"{r['lost_share']:.0%}"
        lines.append(
            f"| {r['rule']} | {r['flag']} | {r['flagged']} | {r['won']} "
            f"| {r['lost']} | {r['still_open']} | {share} |")
    lines += [
        "",
        f"*lost when resolved* = lost / (won + lost), shown only once at least "
        f"{meta['min_n']} flagged opps have resolved (n/a below that — too few "
        f"to read). A high share means the flag tended to precede losses in "
        f"this org's own data.",
    ]
    return "\n".join(lines) + "\n"


def run(store, as_of, config, out_dir):
    """Compute + write the backtest report. Returns (path, records)."""
    from pathlib import Path
    records, meta = backtest(store, config, as_of)
    markdown = render_backtest(records, meta, config, as_of)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"backtest_{as_of.isoformat()}.md"
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(markdown)
    return path, records


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="python -m src.backtest",
        description="Backtest each hygiene rule against its flagged opps' "
                    "final outcomes, from the org's own stored history")
    p.add_argument("--as-of", type=date.fromisoformat, default=None)
    p.add_argument("--db", default=os.environ.get("PIPELINE_HYGIENE_DB",
                                                  "data/pipeline.db"))
    p.add_argument("--config", default=os.environ.get(
        "PIPELINE_HYGIENE_CONFIG", "config.yaml"))
    p.add_argument("--out-dir", default=os.environ.get(
        "PIPELINE_HYGIENE_OUT", "out"))
    p.add_argument("--quotas", default=os.environ.get("PIPELINE_HYGIENE_QUOTAS"),
                   help="JSON file whose 'quotas' mapping is merged into config "
                        "at run time (only min_closed_for_win_rate matters here)")
    args = p.parse_args(argv)

    # CLI entry point: the only place date.today() is allowed (spec).
    as_of = args.as_of if args.as_of is not None else date.today()
    config = load_config(args.config)
    if args.quotas:
        import json
        with open(args.quotas, encoding="utf-8") as f:
            config = merge_quota_payload(config, json.load(f))

    print(f"reading snapshot store {args.db}", file=sys.stderr)
    store = SnapshotStore(args.db, config)
    if not [d for d in store.snapshot_dates() if d <= as_of]:
        print(f"error: no snapshot at or before {as_of} in {args.db}; "
              f"run python -m src.ingest first", file=sys.stderr)
        return 2
    path, records = run(store, as_of, config, args.out_dir)
    flagged = sum(r["flagged"] for r in records)
    print(f"wrote {path} ({flagged} rule-flag instances across "
          f"{len(RULE_LABELS)} rules)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
