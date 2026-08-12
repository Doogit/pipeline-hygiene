"""Presentation view model for the Pipeline-Hygiene dashboard — the parity
boundary between the pure aggregation layer (`src/brief.py`) and any UI.

This module owns the *second* layer the Streamlit page used to hold inline:
currency/percent/score formatting, table row shaping, conditional captions and
empty states, and the Vega-Lite chart specs. It is pure — **no Streamlit import**
(it may build Altair specs and read the snapshot store, both read-only) — so both
the FastHTML page and the parity test render from one source of truth.

`build_page_model(...)` returns a `PageModel`: an ordered tree of typed blocks.
`app/server.py` renders it to HTML; the parity test flattens it and diffs the
result against goldens frozen from the live Streamlit render (tests/parity).

Every user-visible string here is ported verbatim from the retired dashboard;
the parity gate guards that this duplication never drifts.
"""
from dataclasses import dataclass, field
from datetime import date

import altair as alt
import pandas as pd

from src import brief
from src.patterns import ledger_rollups
from src.rules import RULE_LABELS, evaluate_snapshot, is_open
from src.scoring import (fiscal_quarter, group_rollups, opp_score,
                         required_coverage_multiple)

# Okabe-Ito, colorblind-safe; one palette everywhere (ported from dashboard).
SEVERITY_COLORS = {"high": "#D55E00", "medium": "#E69F00", "low": "#0072B2"}
FLOW_COLORS = {"created": "#0072B2", "won": "#009E73", "lost": "#D55E00"}
_SEV_RANK = {"high": 0, "medium": 1, "low": 2}
TAB_LABELS = ["Forecast call", "Slippage", "Trajectory", "Flow", "Owners",
              "Teams", "Appendix"]


# --- block types (the render/flatten contract) ---

@dataclass
class Heading:
    text: str
    level: int = 3  # 3 = st.subheader, 2 = st.header


@dataclass
class Caption:
    text: str


@dataclass
class Markdown:
    text: str


@dataclass
class Warning:
    text: str


@dataclass
class Divider:
    pass


@dataclass
class Metric:
    label: str
    value: str
    delta: str = ""          # "" when no delta (matches AppTest)
    delta_color: str = "normal"
    help: str = None


@dataclass
class MetricRow:
    metrics: list


@dataclass
class Chart:
    id: str
    spec: dict


@dataclass
class Table:
    id: str
    columns: list
    rows: list                # raw values, list[dict]
    formats: dict = field(default_factory=dict)   # column -> render hint


@dataclass
class Popover:
    label: str
    body: str                 # rendered as a caption inside the popover


@dataclass
class Expander:
    label: str
    blocks: list


@dataclass
class Drilldown:
    """Owner drill-down region. `prompt` is the unselected-state caption; the
    selected states are produced on demand by owner_drilldown_blocks()."""
    prompt: str


@dataclass
class Tab:
    label: str
    blocks: list


@dataclass
class PageModel:
    blocks: list                              # ordered top-level blocks
    tabs: list                                # list[Tab] (empty when stopped)
    controls: dict = field(default_factory=dict)   # sidebar control metadata
    sidebar: list = field(default_factory=list)    # sidebar section headings


# --- formatting helpers (formatted ONCE, here) ---

def _cur(config):
    return brief.currency_symbol(config)


def _money(x, config):
    return "n/a" if x is None else f"{_cur(config)}{x:,.0f}"


# Column render hints for the HTML tables (Streamlit column_config equivalents).
_MONEY = "money"        # $%d integer, right-aligned
_SCORE = "score"        # 0-100 progress bar
_COVER = "coverage"     # %.2fx
_SPARK = "sparkline"    # inline SVG line
_WIDE = "wide"          # wide text cell
_SMALL = "small"        # narrow text cell


def _matcher(config, f_owner_set, f_stages, f_sev):
    def _matches(row, result):
        if f_owner_set and row["owner"] not in f_owner_set:
            return False
        if f_stages and row["stage"] not in f_stages:
            return False
        if f_sev and not any(v.severity in f_sev for v in result.violations):
            return False
        return True
    return _matches


# --- ledger component (mirrors dashboard _ledger_df / _ledger_section) ---

_LEDGER_CAPTION = ("Of opps ever forecast commit in stored history: outcome "
                   "to date. Pushed = still open with a close-date move after "
                   "the first commit snapshot. Won/resolved counts closed "
                   "opps only, shown as won/closed; the percentage appears "
                   "once {min_n}+ have resolved (small-n rates mislead). "
                   "Of which pushed first = resolved commits that pushed "
                   "after the commit and before closing. "
                   "Coaching signal, not a comp input.")


def _ledger_rows(entries, key_fn, label, config):
    rollups = ledger_rollups(entries, key_fn)
    min_n = config["min_opps_for_owner_score"]
    records = []
    for key in sorted(rollups, key=lambda k: (k is None, k or "")):
        g = rollups[key]
        records.append({
            label: "unknown" if key is None else key,
            "ever commit": g["n"], "won": g["won"], "lost": g["lost"],
            "pushed": g["pushed"], "still open": g["open"],
            "won/resolved": brief.ledger_rate(g, min_n),
            "of which pushed first": g["resolved_pushed_first"],
        })
    return records


def _ledger_section(blocks, entries, key_fn, label, config, table_id):
    """Popover + one of: unavailable note / empty note / table (mirrors
    dashboard _ledger_section)."""
    blocks.append(Popover("How commit accuracy is computed",
                          _LEDGER_CAPTION.format(
                              min_n=config["min_opps_for_owner_score"])))
    if entries is None:
        blocks.append(Caption("Unavailable outside the snapshot store."))
    elif not entries:
        blocks.append(Caption(
            "No opp in stored history has ever been forecast commit."))
    else:
        rows = _ledger_rows(entries, key_fn, label, config)
        blocks.append(Table(table_id, [label, "ever commit", "won", "lost",
                                       "pushed", "still open", "won/resolved",
                                       "of which pushed first"], rows))


# --- chart builders (Altair -> Vega-Lite spec dict) ---

def _severity_mix_spec(severity_counts):
    mix = pd.DataFrame([{"severity": s, "count": n, "y": "violations"}
                        for s, n in severity_counts.items() if n])
    return alt.Chart(mix).mark_bar(size=26).encode(
        x=alt.X("count:Q", stack="zero", title=None),
        y=alt.Y("y:N", title=None, axis=None),
        color=alt.Color("severity:N",
                        scale=alt.Scale(domain=list(SEVERITY_COLORS),
                                        range=list(SEVERITY_COLORS.values())),
                        legend=alt.Legend(orient="right", title=None)),
        order=alt.Order("severity:N"),
        tooltip=["severity", "count"]).properties(height=40).to_dict()


def _group_coverage_series(store, config, dates, dimension):
    """Coverage ratio per group per stored snapshot, each on that snapshot's own
    win-rate basis (ported verbatim from dashboard _group_coverage_series)."""
    owner_meta = config.get("owner_meta") or {}
    fy_start = config["fiscal_year_start_month"]
    records = []
    for d in dates:
        snap = store.rows_with_history(d)
        results = evaluate_snapshot(snap, config, d)
        outcomes = store.closed_outcomes(d)
        multiple, _ = required_coverage_multiple(outcomes, config)
        quarter = fiscal_quarter(d, fy_start)
        won_by_owner = {}
        for o in outcomes:
            if o["stage"] == "closed_won" and o["close_date"] is not None \
                    and fiscal_quarter(o["close_date"], fy_start) == quarter:
                won_by_owner[o["owner"]] = (won_by_owner.get(o["owner"], 0.0)
                                            + (o["amount"] or 0.0))
        groups = group_rollups(snap, results, config, owner_meta, dimension,
                               multiple, won_by_owner)
        for g in groups.values():
            if g.coverage_ratio is not None:
                records.append({"snapshot": d.isoformat(), dimension: g.key,
                                "coverage": round(g.coverage_ratio, 3)})
    return records


def build_page_model(*, store, config, snapshot_date, as_of, rows, data,
                     prev_summary, prev_opens, outcomes, validation,
                     f_owners=(), f_teams=(), f_stages=(), f_sev=(),
                     controls=None):
    """Build the full page model from the same inputs the dashboard held after
    loading (`data` = brief.build_from_rows(...)). `store` is None for an
    in-memory upload; the multi-snapshot sections degrade exactly as before."""
    cur = _cur(config)
    desk = data["desk"]
    results = data["results"]
    rows_by_id = {r["opp_id"]: r for r in rows}
    delta = data["since_last_run"]

    _owner_meta = config.get("owner_meta") or {}
    f_owner_set = set(f_owners) | {o for o, m in _owner_meta.items()
                                   if (m or {}).get("team") in set(f_teams)}
    _matches = _matcher(config, f_owner_set, f_stages, f_sev)
    filtered_any = bool(f_owner_set or f_stages or f_sev)

    snapshot_dates_all = ([d for d in store.snapshot_dates()
                           if d <= snapshot_date] if store else [])
    waterfalls = [store.waterfall(a, b)
                  for a, b in zip(snapshot_dates_all, snapshot_dates_all[1:])]

    blocks = []
    # read-only page caption (always present)
    blocks.append(Caption("Read-only: agents inspect, people sell. Nothing on "
                          "this page writes to the store or to source data."))

    # --- headline metrics ---
    open_pipeline = sum(r["amount"] or 0.0 for r in rows if is_open(r))
    prev_desk = prev_summary["desk"] if prev_summary else None
    prev_open_pipeline = None
    if store is not None and prev_summary is not None:
        prev_rows = store.load_rows(date.fromisoformat(
            prev_summary["snapshot_date"]))
        prev_open_pipeline = sum(r["amount"] or 0.0
                                 for r in prev_rows if is_open(r))

    metrics = [
        Metric("Open opps", str(desk.n_open)),
        Metric("Desk score (weighted mean)",
               "n/a" if desk.weighted_mean_score is None
               else f"{desk.weighted_mean_score:.1f}",
               delta=("" if prev_desk is None or desk.weighted_mean_score is None
                      or prev_desk["weighted_mean_score"] is None
                      else f"{desk.weighted_mean_score - prev_desk['weighted_mean_score']:+.1f}")),
        Metric(f"Healthy (score >= {config['healthy_score_threshold']})",
               "n/a" if desk.pct_healthy is None else f"{desk.pct_healthy:.1f}%"),
        Metric("Open pipeline", f"{cur}{open_pipeline:,.0f}",
               delta=("" if prev_open_pipeline is None
                      else f"{open_pipeline - prev_open_pipeline:+,.0f}")),
        Metric("At-risk dollars", f"{cur}{desk.at_risk_dollars:,.0f}",
               delta=("" if prev_desk is None
                      else f"{desk.at_risk_dollars - prev_desk['at_risk_dollars']:+,.0f}"),
               delta_color="inverse",
               help="Distinct open opps with at least one high-severity "
                    "violation; each opp's amount counted once."),
    ]
    blocks.append(MetricRow(metrics))

    # violations line + insufficient-history note
    severity_counts = desk.violation_counts_by_severity
    insufficient_note = (
        "" if not any(data["insufficient"].values()) else
        " — insufficient history: " + ", ".join(
            f"{rule} on {n} opps"
            for rule, n in sorted(data["insufficient"].items()) if n))
    blocks.append(Markdown(
        f"Violations: :red[{severity_counts['high']} high] · "
        f":orange[{severity_counts['medium']} medium] · "
        f":blue[{severity_counts['low']} low]" + insufficient_note))
    if sum(severity_counts.values()):
        blocks.append(Chart("severity_mix", _severity_mix_spec(severity_counts)))

    # filters scope caption (sidebar in Streamlit; a caption for parity)
    blocks.append(Caption(
        "Filters apply to the Risky commits, Slippage, Owners and Appendix "
        "tables; headline metrics, the Flow tab and team/region rollups stay "
        "desk-wide. Severity filter: an opp appears under **each** severity it "
        "carries, so one opp can match several selections; empty filters mean "
        "no restriction."))
    if config.get("_quotas_merged"):
        blocks.append(Caption(
            f"Quotas and team/region merged from `{config['_quotas_merged']}`."))

    tabs = [
        Tab("Forecast call", _forecast_call(data, config, _matches,
                                            filtered_any, delta)),
        Tab("Slippage", _slippage(data, config, store, snapshot_date,
                                  _matches, results)),
        Tab("Trajectory", _trajectory(data, config, store, snapshot_date,
                                       snapshot_dates_all)),
        Tab("Flow", _flow(config, waterfalls)),
        Tab("Owners", _owners(data, config, f_owner_set, results, rows_by_id)),
        Tab("Teams", _teams(data, config, store, snapshot_dates_all)),
        Tab("Appendix", _appendix(data, config, _matches, results, rows_by_id,
                                  prev_opens, store, snapshot_date, validation)),
    ]
    # Static sidebar section headings (st.sidebar.header) — chrome the FastHTML
    # sidebar form renders; captured here so parity sees the same strings.
    sidebar = [Heading("Data source", level=2), Heading("Filters", level=2)]
    return PageModel(blocks=blocks, tabs=tabs, controls=controls or {},
                     sidebar=sidebar)


# --- tab builders ---

def _forecast_call(data, config, _matches, filtered_any, delta):
    cur = _cur(config)
    blocks = [Heading("Risky commits")]
    entries = [e for e in data["risky_commits"]
               if _matches(e["row"], e["result"])]
    if not entries:
        blocks.append(Caption(
            "No commit/best_case opp carries a risk flag "
            f"({', '.join(brief.RISKY_RULES)}) matching the filters."))
    else:
        total = sum(e["row"]["amount"] or 0.0 for e in entries)
        filtered_note = " matching the filters" if filtered_any else ""
        blocks.append(Markdown(
            f"**{len(entries)}** commit/best_case opps carry a risk "
            f"flag{filtered_note} — **{cur}{total:,.0f}** (distinct opps), "
            f"dollar-ranked. Coaching prompts, not gotchas."))
        rows = [{
            "opp_id": e["row"]["opp_id"], "account": e["row"]["account"],
            "owner": e["row"]["owner"], "stage": e["row"]["stage"],
            "amount": e["row"]["amount"], "forecast": e["row"]["forecast_category"],
            "score": opp_score(e["result"], config),
            "flags": " ".join(e["risky_rules"]),
            "ask the seller": e["prompt"],
        } for e in entries]
        blocks.append(Table(
            "risky_commits",
            ["opp_id", "account", "owner", "stage", "amount", "forecast",
             "score", "flags", "ask the seller"], rows,
            formats={"amount": _MONEY, "score": _SCORE, "flags": _SMALL,
                     "ask the seller": _WIDE}))
    if delta is None:
        blocks.append(Caption("Since last run: no previous run recorded."))
    else:
        new_n = sum(len(v) for v in delta["new_violations"].values())
        cleared_n = sum(len(v) for v in delta["cleared_violations"].values())
        blocks.append(Caption(
            f"Since last run (snapshot {delta['prev_snapshot_date']}): "
            f"{new_n} new flags, {cleared_n} cleared, "
            f"{len(delta['closed'])} closed, {len(delta['added'])} added, "
            f"{len(delta['removed'])} removed."))
    return blocks


def _slippage(data, config, store, snapshot_date, _matches, results):
    cur = _cur(config)
    blocks = [Heading("Slipping pipeline"),
              Caption("Close-date pushes observed in stored history; movement "
                      "per se is not risk."),
              Popover("How H11 fires",
                      f"H11 fires on a single push >= {config['push_alarm_days']}d "
                      f"or >= {config['cumulative_push_alarm_days']}d cumulative "
                      f"later-drift. Review marker at push_count >= "
                      f"{config['disqualify_review_pushes']}.")]
    open_rows = [r for r in data["rows"] if is_open(r)]
    if not any(r.get("push_count") is not None for r in open_rows):
        blocks.append(Caption("No push history available (rows evaluated "
                              "outside the snapshot store)."))
        return blocks
    slipping = [r for r in open_rows if (r.get("push_count") or 0) >= 1
                and _matches(r, results[r["opp_id"]])]
    if not slipping:
        blocks.append(Caption("No close-date pushes observed in stored "
                              "history (matching the filters)."))
        return blocks
    slipping.sort(key=lambda r: (-(r["amount"] or 0.0), r["opp_id"]))
    total = sum(r["amount"] or 0.0 for r in slipping)
    blocks.append(Markdown(f"**{cur}{total:,.0f}** slipping across "
                           f"**{len(slipping)}** distinct opps."))
    rows = []
    for r in slipping:
        result = results[r["opp_id"]]
        timeline = None
        if store is not None:
            history = store._history(r["opp_id"], snapshot_date)
            closes = [date.fromisoformat(c) for _, _, c in history
                      if c is not None]
            if closes:
                timeline = [(c - closes[0]).days for c in closes]
        rows.append({
            "opp_id": r["opp_id"], "owner": r["owner"], "stage": r["stage"],
            "amount": r["amount"], "pushes": r["push_count"],
            "days later": r["cumulative_extension_days"],
            "max push": r["max_push_days"],
            "rules": " ".join(v.rule_id for v in result.violations) or "-",
            "close-date drift": timeline,
            "review": ("review close plan"
                       if r["push_count"] >= config["disqualify_review_pushes"]
                       else ""),
        })
    blocks.append(Table(
        "slippage",
        ["opp_id", "owner", "stage", "amount", "pushes", "days later",
         "max push", "rules", "close-date drift", "review"], rows,
        formats={"amount": _MONEY, "close-date drift": _SPARK}))
    return blocks


def _trajectory(data, config, store, snapshot_date, snapshot_dates_all):
    cur = _cur(config)
    blocks = [Heading("Trajectory")]
    if store is None or len(snapshot_dates_all) < 2:
        blocks.append(Caption("Trajectory charts need at least 2 stored "
                              "snapshots; showing nothing rather than a "
                              "one-point trend."))
    else:
        snap_rows = {d: store.rows_with_history(d) for d in snapshot_dates_all}
        cov_records = []
        for d in snapshot_dates_all:
            t = brief.trajectory_data(snap_rows[d], None, config, d,
                                      store.closed_outcomes(d))
            cov_records.append({"snapshot": d.isoformat(),
                                "open pipeline": t["open_pipeline"],
                                "required pipeline":
                                (t["coverage"] or {}).get("required_pipeline")})
        cov_df = pd.DataFrame(cov_records).melt("snapshot", var_name="series",
                                                value_name="dollars")
        spec = alt.Chart(cov_df.dropna()).mark_line(
            point=True, strokeWidth=2.5).encode(
            x=alt.X("snapshot:N", title=None),
            y=alt.Y("dollars:Q", title=cur, axis=alt.Axis(format="~s")),
            color=alt.Color("series:N",
                            scale=alt.Scale(range=["#0072B2", "#D55E00"]),
                            legend=alt.Legend(title=None)),
            tooltip=["snapshot", "series",
                     alt.Tooltip("dollars:Q", format=",.0f")]).properties(
            height=220,
            title="Coverage: open vs required pipeline "
                  "(1 / trailing win rate)").to_dict()
        blocks.append(Chart("coverage_line", spec))
        basis = (data["trajectory"]["coverage"] or {}).get("basis")
        if basis:
            blocks.append(Caption(f"Coverage basis: {basis}"))

    trend = (brief.desk_score_series(store, config, snapshot_date)
             if store is not None else [])
    trend = [(d, s) for d, s in trend if s is not None]
    if len(trend) >= 2:
        spec = alt.Chart(pd.DataFrame(
            [{"snapshot": d.isoformat(), "desk score": s}
             for d, s in trend])).mark_line(point=True, strokeWidth=2.5).encode(
            x=alt.X("snapshot:N", title=None),
            y=alt.Y("desk score:Q", scale=alt.Scale(domain=[0, 100])),
            tooltip=["snapshot", "desk score"]).properties(
            height=180, title="Desk score trend (snapshot-anchored)").to_dict()
        blocks.append(Chart("desk_score_trend", spec))
    else:
        blocks.append(Caption("Desk score trend appears after 2+ stored "
                              "snapshots."))

    blocks.append(Divider())
    blocks.append(Heading("Commit accuracy by committed-for quarter"))
    blocks.append(Caption("Committed-for quarter = fiscal quarter of the close "
                          "date when the deal was first called commit — immune "
                          "to later pushes, so a slipped commit stays counted "
                          "against the quarter it was promised for."))
    _ledger_section(blocks, data["ledger"], lambda e: e.committed_quarter,
                    "quarter", config, "ledger_quarter")
    return blocks


def _flow(config, waterfalls):
    cur = _cur(config)
    blocks = [Heading("Pipeline flow")]
    if not waterfalls:
        blocks.append(Caption("Flow needs at least 2 stored snapshots; showing "
                              "nothing rather than a one-point bridge."))
        return blocks
    latest = waterfalls[-1]
    blocks.append(Markdown(f"**Waterfall {latest['prev_date'].isoformat()} → "
                           f"{latest['cur_date'].isoformat()}**"))
    _WF_ORDER = ["beginning open", "+ created", "+ increased", "- decreased",
                 "- won", "- lost", "- removed", "= ending open"]
    _WF_KEYS = ["beginning", "created", "increased", "decreased", "won", "lost",
                "removed", "ending"]
    wf_disp = brief.waterfall_display_dollars(latest)
    wf_df = pd.DataFrame([
        {"bucket": label, "opps": latest[key]["n"], "dollars": wf_disp[key],
         "kind": ("level" if key in ("beginning", "ending")
                  else "in" if label.startswith("+") else "out")}
        for label, key in zip(_WF_ORDER, _WF_KEYS)])
    wf_spec = alt.Chart(wf_df).mark_bar(cornerRadiusEnd=2).encode(
        x=alt.X("bucket:N", sort=_WF_ORDER, title=None),
        y=alt.Y("dollars:Q", title=cur, axis=alt.Axis(format="~s")),
        color=alt.Color("kind:N",
                        scale=alt.Scale(domain=["level", "in", "out"],
                                        range=["#0072B2", "#009E73", "#D55E00"]),
                        legend=alt.Legend(orient="right", title=None)),
        tooltip=["bucket", "opps",
                 alt.Tooltip("dollars:Q", format=",.0f")]).properties(
        height=240,
        title="Open-pipeline waterfall (latest snapshot pair)").to_dict()
    blocks.append(Chart("waterfall", wf_spec))
    pushed = latest["pushed"]
    blocks.append(Caption(
        f"Reconciles exactly: beginning + created + increased − decreased − "
        f"won − lost − removed = ending. Close-date pushes moved no dollars: "
        f"{pushed['n']} open opp(s) ({cur}{pushed['dollars']:,.0f}) pushed "
        f"later — an annotation, never a bucket."))

    pacing = brief.pacing_data(waterfalls, config)
    pace_df = pd.DataFrame([{"week": w["week"].isoformat(),
                             "created": w["dollars"], "opps": w["n"]}
                            for w in pacing["weekly"]])
    pace_chart = alt.Chart(pace_df).mark_bar(
        size=26, cornerRadiusEnd=2, color=FLOW_COLORS["created"]).encode(
        x=alt.X("week:N", title=None),
        y=alt.Y("created:Q", title=cur, axis=alt.Axis(format="~s")),
        tooltip=["week", "opps", alt.Tooltip("created:Q", format=",.0f")])
    if pacing["target"]:
        rule = alt.Chart(pd.DataFrame([{"target": pacing["target"]}])).mark_rule(
            color="#D55E00", strokeDash=[6, 3]).encode(y="target:Q")
        pace_chart = pace_chart + rule
        pace_note = (f" Weekly target {cur}{pacing['target']:,.0f} (dashed).")
    else:
        pace_note = (" No pipeline_gen_weekly_target configured — no target "
                     "line is guessed.")
    blocks.append(Chart("pacing", pace_chart.properties(
        height=200,
        title="Pipeline generation: created per snapshot week").to_dict()))
    blocks.append(Caption(f"Mean {cur}{pacing['mean_dollars']:,.0f}/week over "
                          f"{len(pacing['weekly'])} snapshot pair(s).{pace_note}"))

    flow_records = []
    for w in waterfalls:
        week = w["cur_date"].isoformat()
        for label in ("created", "won", "lost"):
            flow_records.append({"week": week, "flow": label,
                                 "dollars": w[label]["dollars"],
                                 "count": w[label]["n"]})
    flow_hover = alt.selection_point(fields=["flow"], on="mouseover",
                                     empty=True)
    flow_spec = alt.Chart(pd.DataFrame(flow_records)).mark_bar(
        cornerRadiusEnd=2).encode(
        x=alt.X("week:N", title=None),
        y=alt.Y("dollars:Q", title=cur, axis=alt.Axis(format="~s")),
        color=alt.Color("flow:N",
                        scale=alt.Scale(domain=list(FLOW_COLORS),
                                        range=list(FLOW_COLORS.values())),
                        legend=alt.Legend(title=None)),
        xOffset="flow:N",
        opacity=alt.condition(flow_hover, alt.value(1.0), alt.value(0.35)),
        tooltip=["week", "flow", "count",
                 alt.Tooltip("dollars:Q", format=",.0f")]).add_params(
        flow_hover).properties(
        height=220, title="Created vs closed per snapshot week").to_dict()
    blocks.append(Chart("flow_created_closed", flow_spec))
    return blocks


def _owner_records(data, config, f_owner_set):
    records = []
    for stats in data["owners"].values():
        if f_owner_set and stats.owner not in f_owner_set:
            continue
        records.append({
            "owner": stats.owner, "open": stats.n_open,
            "mean": stats.mean_score, "median": stats.median_score,
            "violations": stats.violation_count, "pipeline": stats.open_pipeline,
            "coverage": None if stats.coverage_ratio is None
            else round(stats.coverage_ratio, 2),
            "gap to cover": None if stats.required_pipeline is None
            else max(stats.required_pipeline - stats.open_pipeline, 0.0),
            "flags": ", ".join(f for f, on in
                               (("small_n", stats.small_n),
                                ("low_coverage", stats.coverage_flagged)) if on),
        })
    return records


def owner_drilldown_blocks(owner, data, config, rows_by_id):
    """Selected-state drill-down (htmx target). Returns the heading + table or
    the selected-empty caption — mirrors dashboard lines 635-648."""
    detail = brief.owner_flagged_opps(owner, data["results"], rows_by_id, config)
    blocks = [Markdown(f"**{owner} — open flagged opps** "
                       f"({len(detail)}, read-only)")]
    if detail:
        blocks.append(Table("owner_drilldown",
                            ["opp_id", "account", "stage", "amount", "score",
                             "worst", "rules", "detail"], detail,
                            formats={"amount": _MONEY, "score": _SCORE,
                                     "detail": _WIDE}))
    else:
        blocks.append(Caption(f"{owner} has no open opps with violations."))
    return blocks


def _owners(data, config, f_owner_set, results, rows_by_id):
    blocks = [Heading("Owner scoreboard")]
    owner_records = _owner_records(data, config, f_owner_set)
    if owner_records:
        blocks.append(Table(
            "owner_scoreboard",
            ["owner", "open", "mean", "median", "violations", "pipeline",
             "coverage", "gap to cover", "flags"], owner_records,
            formats={"mean": _SCORE, "median": _SCORE, "pipeline": _MONEY,
                     "gap to cover": _MONEY, "coverage": _COVER}))
        blocks.append(Drilldown("Select an owner row to drill into their open "
                                "flagged opps."))
    blocks.append(Caption(
        f"small_n = fewer than {config['min_opps_for_owner_score']} open opps, "
        f"treat the score as anecdotal. Coverage = open pipeline vs required "
        f"pipeline (remaining quota net of wins this quarter x the required "
        f"multiple); low_coverage means under 1.00x; gap to cover = required "
        f"minus open pipeline on the same basis. Basis: "
        f"{data['coverage_basis']}."))
    desk_note = brief.desk_coverage_note(data["owners"], config)
    if desk_note:
        blocks.append(Caption(desk_note))

    blocks.append(Divider())
    blocks.append(Heading("Forecast integrity patterns"))
    blocks.append(Caption("Coaching signal, not a comp input."))
    patterns = data["patterns"]
    if patterns is None:
        blocks.append(Caption("Unavailable outside the snapshot store."))
    else:
        over = [p for p in patterns.values() if p.overcall_flagged]
        under = [p for p in patterns.values() if p.undercall_flagged]
        if not over and not under:
            blocks.append(Caption("No overcall/undercall patterns flagged."))
        for p in over:
            blocks.append(Markdown(
                f"- :orange[Overcall pattern] {p.owner} — "
                f"{p.overcall_share:.0%} of {p.n_ever_commit} ever-commit opps "
                f"later pushed or lost"))
        for p in under:
            om = "n/a" if p.omitted_share is None else f"{p.omitted_share:.0%}"
            far = "n/a" if p.farout_share is None else f"{p.farout_share:.0%}"
            blocks.append(Markdown(
                f"- :blue[Undercall pattern] {p.owner} — open pipeline {om} "
                f"omitted, {far} far-out (n={p.n_open})"))
        small = sum(1 for p in patterns.values()
                    if p.overcall_small_n and p.undercall_small_n)
        blocks.append(Caption(f"Suppressed as small_n: {small} owners with too "
                              f"little history to score."))

    blocks.append(Divider())
    blocks.append(Heading("Commit accuracy"))
    ledger = data["ledger"]
    owner_entries = (None if ledger is None else
                     [e for e in ledger
                      if not f_owner_set or e.owner in f_owner_set])
    _ledger_section(blocks, owner_entries, lambda e: e.owner, "owner", config,
                    "ledger_owner")
    return blocks


def _group_records(groups, config):
    ranked = sorted(groups.values(),
                    key=lambda g: (g.coverage_ratio is None,
                                   g.coverage_ratio or 0.0, g.key))
    return [{
        "name": g.key, "owners": g.n_owners, "open": g.n_open,
        "mean": g.mean_score, "pipeline": g.open_pipeline, "quota": g.quota,
        "coverage": None if g.coverage_ratio is None
        else round(g.coverage_ratio, 2),
        "gap to cover": None if g.required_pipeline is None
        else max(g.required_pipeline - g.open_pipeline, 0.0),
        "violations": g.violation_count, "at-risk": g.at_risk_dollars,
        "flags": "low_coverage" if g.coverage_flagged else "",
    } for g in ranked]


def _teams(data, config, store, snapshot_dates_all):
    blocks = [Heading("Teams and regions")]
    if not (config.get("owner_meta") or {}):
        blocks.append(Caption("No team/region metadata configured. Point "
                              "PIPELINE_HYGIENE_QUOTAS at a manifest with an "
                              "`owners` block (e.g. data/seed_manifest.json)."))
        return blocks
    blocks.append(Caption(
        f"Coverage = open pipeline vs required pipeline (remaining quota net of "
        f"wins this quarter x the required multiple); low_coverage means under "
        f"1.00x. Worst coverage first. Basis: {data['coverage_basis']}."))
    _group_cols = ["name", "owners", "open", "mean", "pipeline", "quota",
                   "coverage", "gap to cover", "violations", "at-risk", "flags"]
    _fmts = {"mean": _SCORE, "pipeline": _MONEY, "quota": _MONEY,
             "at-risk": _MONEY, "gap to cover": _MONEY, "coverage": _COVER}
    for label, groups, tid in (("Teams", data["teams"], "teams"),
                               ("Regions", data["regions"], "regions")):
        blocks.append(Markdown(f"**{label}**"))
        blocks.append(Table(tid, _group_cols, _group_records(groups, config),
                            formats=_fmts))
        group_note = brief.desk_coverage_note(groups, config,
                                              unit=label.lower())
        if group_note:
            blocks.append(Caption(group_note))

    if store is not None and len(snapshot_dates_all) >= 2:
        blocks.append(Divider())
        blocks.append(Heading("Coverage trend by team / region"))
        blocks.append(Caption("Coverage per stored snapshot, each on its own "
                              "win-rate basis; 1.00x is the bar. A team "
                              "improving reads as a rising line — the trend the "
                              "single-snapshot tables above cannot show."))
        for dim in ("team", "region"):
            recs = _group_coverage_series(store, config, snapshot_dates_all, dim)
            if not recs:
                continue
            df = pd.DataFrame(recs)
            line = alt.Chart(df).mark_line(point=True, strokeWidth=2).encode(
                x=alt.X("snapshot:N", title=None),
                y=alt.Y("coverage:Q", title="coverage (x)"),
                color=alt.Color(f"{dim}:N",
                                legend=alt.Legend(title=dim.capitalize())),
                tooltip=["snapshot", dim,
                         alt.Tooltip("coverage:Q", format=".2f")])
            bar = alt.Chart(pd.DataFrame({"y": [1.0]})).mark_rule(
                strokeDash=[4, 4], color="#888").encode(y="y:Q")
            blocks.append(Chart(f"coverage_trend_{dim}", (line + bar).properties(
                height=260, title=f"Coverage over snapshots, by {dim}").to_dict()))

    blocks.append(Divider())
    blocks.append(Heading("Commit accuracy by team and region"))
    owner_meta = config.get("owner_meta") or {}
    _ledger_section(blocks, data["ledger"],
                    lambda e: (owner_meta.get(e.owner) or {}).get("team"),
                    "team", config, "ledger_team")
    if data["ledger"]:
        blocks.append(Table(
            "ledger_region",
            ["region", "ever commit", "won", "lost", "pushed", "still open",
             "won/resolved", "of which pushed first"],
            _ledger_rows(data["ledger"],
                         lambda e: (owner_meta.get(e.owner) or {}).get("region"),
                         "region", config)))
    return blocks


def _appendix(data, config, _matches, results, rows_by_id, prev_opens, store,
              snapshot_date, validation):
    blocks = [Heading("Exceptions (full list)")]
    score_histories = {}
    for open_map in (prev_opens or []):
        for opp_id, rule_ids in open_map.items():
            score_histories.setdefault(opp_id, []).append(
                max(0, 100 - sum(config["rule_weights"].get(rule, 0)
                                 for rule in rule_ids)))
    exceptions = []
    for result in results.values():
        if not result.violations:
            continue
        row = rows_by_id[result.opp_id]
        if not _matches(row, result):
            continue
        worst = min(_SEV_RANK[v.severity] for v in result.violations)
        streak = brief.opp_streak(data["streaks"], result)
        score = opp_score(result, config)
        history = score_histories.get(result.opp_id, []) + [score]
        exceptions.append({
            "opp_id": result.opp_id, "account": row["account"],
            "owner": row["owner"], "stage": row["stage"], "amount": row["amount"],
            "score": score,
            "score history": history if len(history) >= 2 else None,
            "worst": ["high", "medium", "low"][worst],
            "rules": " ".join(v.rule_id for v in result.violations),
            "streak": f"flagged {streak} runs" if streak >= 2 else "",
            "detail": "; ".join(v.detail for v in result.violations),
            "_rank": (worst, -(row["amount"] or 0.0), result.opp_id),
        })
    exceptions.sort(key=lambda e: e.pop("_rank"))
    blocks.append(Caption(
        f"{len(exceptions)} open opps with violations match the filters. Rule "
        f"legend: " + "; ".join(f"{rule} {label}"
                                for rule, label in RULE_LABELS.items())))
    if exceptions:
        blocks.append(Table(
            "exceptions",
            ["opp_id", "account", "owner", "stage", "amount", "score",
             "score history", "worst", "rules", "streak", "detail"], exceptions,
            formats={"amount": _MONEY, "score": _SCORE,
                     "score history": _SPARK, "detail": _WIDE}))

    # Validation report (expander)
    exp = []
    if store is not None:
        mismatch = brief.quota_owner_mismatch(
            config.get("quotas") or {}, store.owners(snapshot_date))
        if mismatch:
            exp.append(Warning(brief.mismatch_summary(mismatch)))
    if validation is None:
        exp.append(Markdown("No validation report stored for this snapshot."))
    else:
        exp.append(Markdown(
            f"Source `{_basename(validation['source_file'])}`: accepted "
            f"{validation['accepted']}/{validation['total_rows']} rows, "
            f"rejected {validation['rejected']}."))
        for warning in validation["warnings"]:
            exp.append(Warning(warning))
        if validation["row_reasons"]:
            exp.append(Table("validation_rejects", ["row", "opp_id", "reason"],
                             [dict(zip(("row", "opp_id", "reason"), r))
                              for r in validation["row_reasons"]]))
    blocks.append(Expander("Validation report", exp))
    return blocks


def _basename(path):
    from pathlib import Path
    return Path(path).name


# --- store loader (mirrors dashboard.py's store-path load; read-only) ---

def build_from_store(config_path, db_path, quotas_path=None, *,
                     snapshot_date=None, as_of=None,
                     f_owners=(), f_teams=(), f_stages=(), f_sev=()):
    """Load a snapshot store and build the PageModel, mirroring the dashboard's
    store path (no upload). Returns a 'stopped' PageModel (read-only caption +
    optional quotas caption + a warning, no tabs) for a missing/empty store."""
    from pathlib import Path

    from src.ingest import load_config
    from src.patterns import commit_ledger, owner_patterns
    from src.snapshots import SnapshotStore

    read_only = Caption("Read-only: agents inspect, people sell. Nothing on "
                        "this page writes to the store or to source data.")
    config = load_config(config_path)
    quotas_caption = None
    if quotas_path and Path(quotas_path).exists():
        import json
        with open(quotas_path, encoding="utf-8") as f:
            config = brief.merge_quota_payload(config, json.load(f))
        config = dict(config)
        config["_quotas_merged"] = str(quotas_path)
        quotas_caption = Caption(
            f"Quotas and team/region merged from `{quotas_path}`.")

    def _stopped(msg):
        blocks = [read_only]
        if quotas_caption:
            blocks.append(quotas_caption)
        blocks.append(Warning(msg))
        # The page stops before the Filters header (dashboard line 210), so only
        # the Data source section heading is present.
        return PageModel(blocks=blocks, tabs=[], controls={"config": config},
                         sidebar=[Heading("Data source", level=2)])

    if not Path(db_path).exists():
        return _stopped(f"No store at `{db_path}`. Run `python -m src.ingest "
                        f"<csv>` first, or upload a CSV in the sidebar.")
    store = SnapshotStore(db_path, config)
    dates = store.snapshot_dates()
    if not dates:
        return _stopped("Store is empty; ingest a snapshot or upload a CSV.")

    snapshot_date = snapshot_date or dates[-1]
    as_of = as_of or snapshot_date
    rows = store.rows_with_history(snapshot_date)
    validation = store.validation_report_dict(snapshot_date)
    prev = store.last_run_before_snapshot(snapshot_date)
    prev_summary = prev["summary"] if prev else None
    prev_opens = store.run_opens(before_snapshot_date=snapshot_date)
    outcomes = store.closed_outcomes(snapshot_date)
    patterns = owner_patterns(store, as_of, config, snapshot_date)
    ledger = commit_ledger(store, as_of, config, snapshot_date)
    data = brief.build_from_rows(
        rows, snapshot_date, as_of, config, validation=validation,
        prev_summary=prev_summary, outcomes=outcomes, prev_opens=prev_opens,
        patterns=patterns, ledger=ledger)

    controls = {
        "config": config,
        "data": data,           # for the byte-identical brief.render(...) download
        "snapshot_dates": dates,
        "snapshot_date": snapshot_date,
        "as_of": as_of,
        "stage_maps": sorted(config.get("stage_map", {})),
        "owners": sorted({r["owner"] for r in rows if is_open(r)}),
        "stages": sorted({r["stage"] for r in rows if is_open(r)}),
        "teams": sorted({(m or {}).get("team")
                         for m in (config.get("owner_meta") or {}).values()}
                        - {None}),
    }
    return build_page_model(
        store=store, config=config, snapshot_date=snapshot_date, as_of=as_of,
        rows=rows, data=data, prev_summary=prev_summary, prev_opens=prev_opens,
        outcomes=outcomes, validation=validation, f_owners=f_owners,
        f_teams=f_teams, f_stages=f_stages, f_sev=f_sev, controls=controls)
