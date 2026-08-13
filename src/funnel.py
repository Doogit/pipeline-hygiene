"""Stage-funnel analytics: how opps move through the pipeline over stored history.

Deterministic and auditable — derives every metric from the stored snapshots
alone (consecutive-pair stage observations, one pass per opp, same shape as
SnapshotStore.push_stats). It never reads the seed's delta manifest, so it works
on any ingested CRM export, not just the simulator's output.

Per open stage it reports:
- width: distinct opps ever observed in the stage;
- advancement: of those opps, the share that reached a LATER open stage, vs
  stalled (still open, never advanced), vs closed_won / closed_lost while still
  in the stage-or-earlier;
- median dwell: median observed days between entering a stage and leaving it
  (a snapshot-derived value; for opps already in a stage at the first stored
  snapshot it is a lower bound, since the true entry predates the history).

Coaching signal, never a comp input. Pure over a SnapshotStore, so it is
unit-testable without the CLI.
"""
import argparse
import os
import sys
from datetime import date
from statistics import median

from .ingest import load_config
from .snapshots import SnapshotStore

OPEN_STAGES = ["prospect", "qualify", "develop", "propose", "commit"]
STAGE_LABELS = {s: s for s in OPEN_STAGES}
_IDX = {s: i for i, s in enumerate(OPEN_STAGES)}
_CLOSED = ("closed_won", "closed_lost")


def _opp_trajectory(history):
    """One opp's observed (snapshot_date, stage) run, ascending. Returns
    (open_stages_seen, max_open_idx, terminal_stage, dwell_samples) where
    dwell_samples is a list of (stage, days) for each COMPLETED open-stage run
    (the opp was seen leaving that stage — to a later stage or to closed). The
    final run is censored (still sitting there at the last snapshot) and is not
    counted as dwell."""
    seen, max_idx, dwells = set(), -1, []
    run_stage, run_start = None, None
    for snap_date, stage in history:
        if stage in _IDX:
            seen.add(stage)
            max_idx = max(max_idx, _IDX[stage])
        if stage != run_stage:
            if run_stage in _IDX:                       # left an open stage
                dwells.append((run_stage, (snap_date - run_start).days))
            run_stage, run_start = stage, snap_date
    terminal = history[-1][1] if history else None
    return seen, max_idx, terminal, dwells


def _walk_opps(store, as_of):
    """Yield _opp_trajectory(...) for each opp over stored history <= as_of.
    One query, ascending by opp then snapshot — the shared single pass behind
    the funnel report and the derived-aging dwell medians."""
    cur = store.conn.execute(
        "SELECT opp_id, snapshot_date, stage FROM opportunities "
        "WHERE snapshot_date <= ? ORDER BY opp_id, snapshot_date",
        (as_of.isoformat(),))
    prev_opp, history = None, []
    for opp_id, snap_date, stage in cur:
        if opp_id != prev_opp:
            if history:
                yield _opp_trajectory(history)
            prev_opp, history = opp_id, []
        history.append((date.fromisoformat(snap_date), stage))
    if history:
        yield _opp_trajectory(history)


def stage_dwell_medians(store, as_of):
    """Per open stage: (median observed dwell days, sample count) over stored
    history <= as_of. A sample is one completed dwell (an opp seen leaving the
    stage). Stages with no completed dwell report (None, 0). The primitive the
    derived aging norms are built from."""
    dwell_by_stage = {s: [] for s in OPEN_STAGES}
    for _seen, _max_idx, _terminal, dwells in _walk_opps(store, as_of):
        for stage, days in dwells:
            dwell_by_stage[stage].append(days)
    return {s: (int(median(v)), len(v)) if v else (None, 0)
            for s, v in dwell_by_stage.items()}


def funnel(store, config, as_of):
    """Per-open-stage width / advancement / median-dwell records over stored
    history <= as_of. Returns (records, meta). One query, one pass per opp."""
    dates = [d for d in store.snapshot_dates() if d <= as_of]

    # accumulate per stage: opp fates and dwell samples
    fates = {s: {"advanced": 0, "stalled": 0, "won": 0, "lost": 0}
             for s in OPEN_STAGES}
    widths = {s: 0 for s in OPEN_STAGES}
    dwell_by_stage = {s: [] for s in OPEN_STAGES}
    transitions = 0

    for seen, max_idx, terminal, dwells in _walk_opps(store, as_of):
        for stage, days in dwells:
            dwell_by_stage[stage].append(days)
        transitions += len(dwells)
        for stage in seen:
            widths[stage] += 1
            if max_idx > _IDX[stage]:
                fates[stage]["advanced"] += 1
            elif terminal == "closed_won":
                fates[stage]["won"] += 1
            elif terminal == "closed_lost":
                fates[stage]["lost"] += 1
            else:
                fates[stage]["stalled"] += 1

    records = []
    for stage in OPEN_STAGES:
        w = widths[stage]
        f = fates[stage]
        samples = dwell_by_stage[stage]
        records.append({
            "stage": stage,
            "width": w,
            "advanced": f["advanced"],
            "stalled": f["stalled"],
            "won": f["won"],
            "lost": f["lost"],
            "advancement_rate": (f["advanced"] / w) if w else None,
            "median_dwell_days": (int(median(samples)) if samples else None),
            "dwell_n": len(samples),
        })
    return records, {"dates": dates, "transitions": transitions}


def derived_aging_norms(config, dwell_medians):
    """Per-stage H6 aging norm from observed dwell: round(multiple x median),
    clamped to >= 1 day. Any stage with fewer than the min-sample basis keeps
    its static aging_norm_days value (so a thinly-observed stage never gets a
    noisy norm). Pure: (config, {stage: (median, n)}) -> {stage: days}."""
    static = config["aging_norm_days"]
    multiple = config.get("aging_norm_derived_multiple", 1.75)
    min_n = config["min_closed_for_win_rate"]
    norms = {}
    for stage, base in static.items():
        median_days, n = dwell_medians.get(stage, (None, 0))
        if median_days is not None and n >= min_n:
            norms[stage] = max(1, round(multiple * median_days))
        else:
            norms[stage] = base
    return norms


def resolve_aging_config(store, config, as_of):
    """Return the config H6 should evaluate against. Identity (same object)
    unless aging_norm_mode == 'derived', in which case a COPY with derived
    aging_norm_days is returned — so H6 in rules.py stays a pure
    (row, config, as_of) function and every default-mode caller is untouched
    (parity-safe). Needs a store (multi-snapshot history); irrelevant to
    single-upload paths, which never call it."""
    if config.get("aging_norm_mode", "static") != "derived":
        return config
    norms = derived_aging_norms(config, stage_dwell_medians(store, as_of))
    resolved = dict(config)
    resolved["aging_norm_days"] = norms
    return resolved


def render_funnel(records, meta, config, as_of):
    """Standalone markdown report — a parity-exempt artifact, like the brief,
    digests and backtest, so it never touches the frozen dashboard/brief
    goldens."""
    dates = meta["dates"]
    lines = [f"# Stage funnel — {as_of.isoformat()}", ""]
    if not dates:
        lines.append("No stored snapshots at or before this date — nothing to "
                     "analyze. Run `python -m src.ingest` first.")
        return "\n".join(lines) + "\n"

    lines += [
        f"History: {len(dates)} snapshot(s), {dates[0].isoformat()} → "
        f"{dates[-1].isoformat()}; {meta['transitions']} observed stage exits.",
        "",
        "For every open stage, the distinct opps ever observed in it, how they "
        "moved on (advanced to a later stage / stalled / closed), and the median "
        "observed dwell before leaving. Coaching signal, never a comp input.",
        "",
        "| stage | width | advanced | stalled | won | lost | advance rate | "
        "median dwell |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for r in records:
        rate = ("n/a" if r["advancement_rate"] is None
                else f"{r['advancement_rate']:.0%}")
        dwell = ("n/a" if r["median_dwell_days"] is None
                 else f"{r['median_dwell_days']}d")
        lines.append(
            f"| {r['stage']} | {r['width']} | {r['advanced']} | {r['stalled']} "
            f"| {r['won']} | {r['lost']} | {rate} | {dwell} |")
    lines += [
        "",
        "*advance rate* = advanced / width (share of a stage's opps that reached "
        "a later open stage). *median dwell* is snapshot-derived days in stage "
        "before leaving; for opps already in a stage at the first snapshot it is "
        "a lower bound.",
    ]
    return "\n".join(lines) + "\n"


def run(store, as_of, config, out_dir):
    """Compute + write the funnel report. Returns (path, records)."""
    from pathlib import Path
    records, meta = funnel(store, config, as_of)
    markdown = render_funnel(records, meta, config, as_of)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"funnel_{as_of.isoformat()}.md"
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(markdown)
    return path, records


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="python -m src.funnel",
        description="Stage-funnel analytics (width, advancement, dwell) from "
                    "the org's own stored snapshot history")
    p.add_argument("--as-of", type=date.fromisoformat, default=None)
    p.add_argument("--db", default=os.environ.get("PIPELINE_HYGIENE_DB",
                                                  "data/pipeline.db"))
    p.add_argument("--config", default=os.environ.get(
        "PIPELINE_HYGIENE_CONFIG", "config.yaml"))
    p.add_argument("--out-dir", default=os.environ.get(
        "PIPELINE_HYGIENE_OUT", "out"))
    p.add_argument("--quotas", default=os.environ.get("PIPELINE_HYGIENE_QUOTAS"),
                   help="JSON file whose 'quotas' mapping is merged into config "
                        "at run time (kept for parity with the other reports)")
    args = p.parse_args(argv)

    # CLI entry point: the only place date.today() is allowed (spec).
    as_of = args.as_of if args.as_of is not None else date.today()
    config = load_config(args.config)
    if args.quotas:
        import json
        from .brief import merge_quota_payload
        with open(args.quotas, encoding="utf-8") as f:
            config = merge_quota_payload(config, json.load(f))

    print(f"reading snapshot store {args.db}", file=sys.stderr)
    store = SnapshotStore(args.db, config)
    if not [d for d in store.snapshot_dates() if d <= as_of]:
        print(f"error: no snapshot at or before {as_of} in {args.db}; "
              f"run python -m src.ingest first", file=sys.stderr)
        return 2
    path, records = run(store, as_of, config, args.out_dir)
    transitions = sum(r["advanced"] for r in records)
    print(f"wrote {path} ({transitions} advancing opps across "
          f"{len(OPEN_STAGES)} stages)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
