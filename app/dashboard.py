"""Read-only Streamlit dashboard: streamlit run app/dashboard.py

Tabs mirror the brief structure — "Forecast call" is the landing view and
stands alone for a Monday meeting; the full exception list survives only as
the Appendix drill-down. Loads the latest snapshot from the store by
default; an uploaded CSV runs the same ingest validation and is evaluated in
memory — nothing on this page writes to the store or to source data, and
viewing/downloading a brief here does not record a run. as_of defaults to
the snapshot date (date.today() is banned outside CLI --as-of defaults).

Charts use Streamlit built-ins + bundled Altair only (no new hard deps).
Severity palette is Okabe-Ito (colorblind-safe): high vermillion, medium
orange, low blue — one palette everywhere, colored text instead of emoji.
"""
import json
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import brief
from src.ingest import AllRowsRejectedError, IngestError, load_config, \
    snapshot_date_from_filename, validate_csv
from src.patterns import commit_ledger, ledger_rollups, owner_patterns
from src.rules import RULE_LABELS, is_open
from src.scoring import opp_score
from src.snapshots import SnapshotStore

CONFIG_PATH = os.environ.get("PIPELINE_HYGIENE_CONFIG", "config.yaml")
DB_PATH = os.environ.get("PIPELINE_HYGIENE_DB", "data/pipeline.db")
QUOTAS_PATH = os.environ.get("PIPELINE_HYGIENE_QUOTAS", "data/seed_manifest.json")
_SEV_RANK = {"high": 0, "medium": 1, "low": 2}
# Okabe-Ito, colorblind-safe; one palette everywhere.
SEVERITY_COLORS = {"high": "#D55E00", "medium": "#E69F00", "low": "#0072B2"}
FLOW_COLORS = {"created": "#0072B2", "won": "#009E73", "lost": "#D55E00"}

st.set_page_config(page_title="pipeline-hygiene", layout="wide")
st.title("pipeline-hygiene — forecast-call prep")
st.caption("Read-only: agents inspect, people sell. Nothing on this page "
           "writes to the store or to source data.")

config = load_config(CONFIG_PATH)
if Path(QUOTAS_PATH).exists():
    with open(QUOTAS_PATH, encoding="utf-8") as f:
        payload = json.load(f)
    config = brief.merge_quota_payload(config, payload)
    st.sidebar.caption(f"Quotas and team/region merged from `{QUOTAS_PATH}`.")
CURRENCY = brief.currency_symbol(config)

# --- data source ---

st.sidebar.header("Data source")
uploaded = st.sidebar.file_uploader("Upload a snapshot CSV (validated, "
                                    "evaluated in memory, never stored)",
                                    type="csv")
stage_map_name = st.sidebar.selectbox(
    "Stage vocabulary (stage_map)", sorted(config.get("stage_map", {})),
    index=sorted(config.get("stage_map", {})).index("default"))

store = None
if uploaded is not None:
    snapshot_date = snapshot_date_from_filename(uploaded.name)
    if snapshot_date is None:
        st.error("No YYYY-MM-DD snapshot date found in the uploaded filename; "
                 "rename the file like opps_2026-08-10.csv.")
        st.stop()
    with tempfile.NamedTemporaryFile("wb", suffix=".csv", delete=False) as tmp:
        tmp.write(uploaded.getvalue())
        tmp_path = tmp.name
    try:
        rows, report = validate_csv(tmp_path, config, stage_map_name)
        if report.total_rows > 0 and not rows:
            raise AllRowsRejectedError(snapshot_date, report)
    except AllRowsRejectedError as exc:
        st.error(f"Upload rejected: {exc}")
        if exc.report.row_reasons:
            st.dataframe(pd.DataFrame(exc.report.row_reasons,
                                      columns=["row", "opp_id", "reason"]),
                         width="stretch", hide_index=True)
        st.stop()
    except IngestError as exc:
        st.error(f"Upload rejected: {exc}")
        st.stop()
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    validation = dict(report.to_dict(), source_file=uploaded.name)
    st.sidebar.caption(f"Uploaded snapshot {snapshot_date} — single snapshot, "
                       f"so H3/H6 may report insufficient history.")
    prev_summary, prev_opens, outcomes, patterns = None, [], None, None
else:
    if not Path(DB_PATH).exists():
        st.warning(f"No store at `{DB_PATH}`. Run `python -m src.ingest "
                   f"<csv>` first, or upload a CSV in the sidebar.")
        st.stop()
    store = SnapshotStore(DB_PATH, config)
    dates = store.snapshot_dates()
    if not dates:
        st.warning("Store is empty; ingest a snapshot or upload a CSV.")
        st.stop()
    snapshot_date = st.sidebar.selectbox("Snapshot", dates,
                                         index=len(dates) - 1,
                                         format_func=lambda d: d.isoformat())
    rows = store.rows_with_history(snapshot_date)
    validation = store.validation_report_dict(snapshot_date)
    # Diff against the run for the previous snapshot, not the globally latest
    # run — otherwise the selected snapshot diffs against itself (every delta
    # reads +0.0), and an older selection diffs backwards against a newer run.
    prev = store.last_run_before_snapshot(snapshot_date)
    prev_summary = prev["summary"] if prev else None
    prev_opens = store.run_opens(before_snapshot_date=snapshot_date)
    outcomes = store.closed_outcomes(snapshot_date)

as_of = st.sidebar.date_input("Evaluate as of", value=snapshot_date)
patterns = (owner_patterns(store, as_of, config, snapshot_date)
            if store else None)
ledger = (commit_ledger(store, as_of, config, snapshot_date)
          if store else None)
data = brief.build_from_rows(rows, snapshot_date, as_of, config,
                             validation=validation, prev_summary=prev_summary,
                             outcomes=outcomes, prev_opens=prev_opens,
                             patterns=patterns, ledger=ledger)
results, desk = data["results"], data["desk"]
rows_by_id = {r["opp_id"]: r for r in rows}
delta = data["since_last_run"]

# --- headline metrics with since-last-run deltas ---

open_pipeline = sum(r["amount"] or 0.0 for r in rows if is_open(r))
prev_desk = prev_summary["desk"] if prev_summary else None
prev_open_pipeline = None
if store is not None and prev_summary is not None:
    prev_rows = store.load_rows(date.fromisoformat(
        prev_summary["snapshot_date"]))
    prev_open_pipeline = sum(r["amount"] or 0.0
                             for r in prev_rows if is_open(r))

cols = st.columns(5)
cols[0].metric("Open opps", desk.n_open)
cols[1].metric(
    "Desk score (weighted mean)",
    "n/a" if desk.weighted_mean_score is None
    else f"{desk.weighted_mean_score:.1f}",
    delta=(None if prev_desk is None or desk.weighted_mean_score is None
           or prev_desk["weighted_mean_score"] is None
           else f"{desk.weighted_mean_score - prev_desk['weighted_mean_score']:+.1f}"))
cols[2].metric(f"Healthy (score >= {config['healthy_score_threshold']})",
               "n/a" if desk.pct_healthy is None else f"{desk.pct_healthy:.1f}%")
cols[3].metric(
    "Open pipeline", f"{CURRENCY}{open_pipeline:,.0f}",
    delta=(None if prev_open_pipeline is None
           else f"{open_pipeline - prev_open_pipeline:+,.0f}"))
cols[4].metric(
    "At-risk dollars", f"{CURRENCY}{desk.at_risk_dollars:,.0f}",
    delta=(None if prev_desk is None
           else f"{desk.at_risk_dollars - prev_desk['at_risk_dollars']:+,.0f}"),
    delta_color="inverse",
    help="Distinct open opps with at least one high-severity violation; "
         "each opp's amount counted once.")

severity_counts = desk.violation_counts_by_severity
insufficient_note = (
    "" if not any(data["insufficient"].values()) else
    " — insufficient history: " + ", ".join(
        f"{rule} on {n} opps"
        for rule, n in sorted(data["insufficient"].items()) if n))
st.markdown(f"Violations: :red[{severity_counts['high']} high] · "
            f":orange[{severity_counts['medium']} medium] · "
            f":blue[{severity_counts['low']} low]" + insufficient_note)

# compact severity mix (stacked bar, not a pie)
if sum(severity_counts.values()):
    mix = pd.DataFrame([{"severity": s, "count": n, "y": "violations"}
                        for s, n in severity_counts.items() if n])
    # explicit pixel sizes throughout: charts inside initially-hidden tabs
    # measure a zero-size container, so container sizing collapses them
    st.altair_chart(
        alt.Chart(mix).mark_bar(size=26).encode(
            x=alt.X("count:Q", stack="zero", title=None),
            y=alt.Y("y:N", title=None, axis=None),
            color=alt.Color("severity:N",
                            scale=alt.Scale(domain=list(SEVERITY_COLORS),
                                            range=list(SEVERITY_COLORS.values())),
                            legend=alt.Legend(orient="right", title=None)),
            order=alt.Order("severity:N"),
            tooltip=["severity", "count"]).properties(height=40, width=950))

# --- filters (apply to Slippage, Owners, Appendix tables) ---

st.sidebar.header("Filters")
owners_all = sorted({r["owner"] for r in rows if is_open(r)})
stages_all = sorted({r["stage"] for r in rows if is_open(r)})
f_owners = st.sidebar.multiselect("Owner", owners_all)
_owner_meta = config.get("owner_meta") or {}
teams_all = sorted({(m or {}).get("team")
                    for m in _owner_meta.values()} - {None})
f_teams = st.sidebar.multiselect("Team", teams_all) if teams_all else []
f_stages = st.sidebar.multiselect("Stage", stages_all)
f_sev = st.sidebar.multiselect("Severity", ["high", "medium", "low"])
st.sidebar.caption("Filters apply to the Risky commits, Slippage, Owners "
                   "and Appendix tables; headline metrics and team/region "
                   "rollups stay desk-wide. Severity filter: an opp appears "
                   "under **each** severity it carries, so one opp can "
                   "match several selections; empty filters mean no "
                   "restriction.")
# Team selections expand to their rosters and union with the Owner picks,
# so a frontline manager filters to her team without picking 8 of 60 names.
f_owner_set = set(f_owners) | {o for o, m in _owner_meta.items()
                               if (m or {}).get("team") in f_teams}


def _matches(row, result):
    if f_owner_set and row["owner"] not in f_owner_set:
        return False
    if f_stages and row["stage"] not in f_stages:
        return False
    if f_sev and not any(v.severity in f_sev for v in result.violations):
        return False
    return True


_MONEY_COL = st.column_config.NumberColumn(format=f"{CURRENCY}%d")
_SCORE_COL = st.column_config.ProgressColumn(format="%d", min_value=0,
                                             max_value=100)

LEDGER_CAPTION = ("Of opps ever forecast commit in stored history: outcome "
                  "to date. Pushed = still open with a close-date move after "
                  "the first commit snapshot. Won/resolved counts closed "
                  "opps only, shown as won/closed; the percentage appears "
                  "once {min_n}+ have resolved (small-n rates mislead). "
                  "Coaching signal, not a comp input.")


def _ledger_df(entries, key_fn, label):
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
        })
    return pd.DataFrame(records)


def _ledger_section(entries, key_fn, label):
    """One commit-accuracy table with the standing disclaimer; degrades to a
    clear caption outside the store or with no ever-commit history."""
    st.caption(LEDGER_CAPTION.format(min_n=config["min_opps_for_owner_score"]))
    if entries is None:
        st.caption("Unavailable outside the snapshot store.")
    elif not entries:
        st.caption("No opp in stored history has ever been forecast commit.")
    else:
        st.dataframe(_ledger_df(entries, key_fn, label), width="stretch",
                     hide_index=True)

tab_call, tab_slip, tab_traj, tab_owners, tab_teams, tab_appendix = st.tabs(
    ["Forecast call", "Slippage", "Trajectory", "Owners", "Teams", "Appendix"])

# --- Forecast call (landing view; stands alone for a Monday meeting) ---

with tab_call:
    st.subheader("Risky commits")
    entries = [e for e in data["risky_commits"]
               if _matches(e["row"], e["result"])]
    if not entries:
        st.caption("No commit/best_case opp carries a risk flag "
                   f"({', '.join(brief.RISKY_RULES)}) matching the filters.")
    else:
        total = sum(e["row"]["amount"] or 0.0 for e in entries)
        filtered_note = (" matching the filters"
                         if f_owner_set or f_stages or f_sev else "")
        st.markdown(f"**{len(entries)}** commit/best_case opps carry a risk "
                    f"flag{filtered_note} — **{CURRENCY}{total:,.0f}** (distinct "
                    f"opps), dollar-ranked. Coaching prompts, not gotchas.")
        risky_df = pd.DataFrame([{
            "opp_id": e["row"]["opp_id"], "account": e["row"]["account"],
            "owner": e["row"]["owner"],
            "stage": e["row"]["stage"], "amount": e["row"]["amount"],
            "forecast": e["row"]["forecast_category"],
            "score": opp_score(e["result"], config),
            "flags": " ".join(e["risky_rules"]),
            "ask the seller": e["prompt"],
        } for e in entries])
        st.dataframe(
            risky_df, width="stretch", hide_index=True,
            column_config={
                "amount": _MONEY_COL, "score": _SCORE_COL,
                "flags": st.column_config.TextColumn(width="small"),
                "ask the seller": st.column_config.TextColumn(width="large"),
            })
    if delta is None:
        st.caption("Since last run: no previous run recorded.")
    else:
        new_n = sum(len(v) for v in delta["new_violations"].values())
        cleared_n = sum(len(v) for v in delta["cleared_violations"].values())
        st.caption(f"Since last run (snapshot {delta['prev_snapshot_date']}):"
                   f" {new_n} new flags, {cleared_n} cleared, "
                   f"{len(delta['closed'])} closed, "
                   f"{len(delta['added'])} added, "
                   f"{len(delta['removed'])} removed.")

# --- Slippage (Task 8 data) ---

with tab_slip:
    st.subheader("Slipping pipeline")
    st.caption(f"Close-date pushes observed in stored history. H11 fires on "
               f"a single push >= {config['push_alarm_days']}d or "
               f">= {config['cumulative_push_alarm_days']}d cumulative "
               f"later-drift; movement per se is not risk. Review marker at "
               f"push_count >= {config['disqualify_review_pushes']}.")
    open_rows = [r for r in rows if is_open(r)]
    if not any(r.get("push_count") is not None for r in open_rows):
        st.caption("No push history available (rows evaluated outside the "
                   "snapshot store).")
    else:
        slipping = [r for r in open_rows
                    if (r.get("push_count") or 0) >= 1
                    and _matches(r, results[r["opp_id"]])]
        if not slipping:
            st.caption("No close-date pushes observed in stored history "
                       "(matching the filters).")
        else:
            slipping.sort(key=lambda r: (-(r["amount"] or 0.0), r["opp_id"]))
            total = sum(r["amount"] or 0.0 for r in slipping)
            st.markdown(f"**{CURRENCY}{total:,.0f}** slipping across "
                        f"**{len(slipping)}** distinct opps.")
            slip_records = []
            for r in slipping:
                result = results[r["opp_id"]]
                timeline = None
                if store is not None:
                    history = store._history(r["opp_id"], snapshot_date)
                    closes = [date.fromisoformat(c) for _, _, c in history
                              if c is not None]
                    if closes:
                        timeline = [(c - closes[0]).days for c in closes]
                slip_records.append({
                    "opp_id": r["opp_id"], "owner": r["owner"],
                    "stage": r["stage"], "amount": r["amount"],
                    "pushes": r["push_count"],
                    "days later": r["cumulative_extension_days"],
                    "max push": r["max_push_days"],
                    "rules": " ".join(v.rule_id
                                      for v in result.violations) or "-",
                    "close-date drift": timeline,
                    "review": ("recommend disqualification review"
                               if r["push_count"]
                               >= config["disqualify_review_pushes"] else ""),
                })
            st.dataframe(
                pd.DataFrame(slip_records), width="stretch", hide_index=True,
                column_config={
                    "amount": _MONEY_COL,
                    "close-date drift": st.column_config.LineChartColumn(
                        "close-date drift (days)"),
                })

# --- Trajectory ---

with tab_traj:
    st.subheader("Trajectory")
    snapshot_dates = ([d for d in store.snapshot_dates() if d <= snapshot_date]
                      if store else [])
    if store is None or len(snapshot_dates) < 2:
        st.caption("Trajectory charts need at least 2 stored snapshots; "
                   "showing nothing rather than a one-point trend.")
    else:
        snap_rows = {d: store.rows_with_history(d) for d in snapshot_dates}
        # coverage vs quota across snapshots
        cov_records = []
        for d in snapshot_dates:
            t = brief.trajectory_data(snap_rows[d], None, config, d,
                                      store.closed_outcomes(d))
            cov_records.append({"snapshot": d.isoformat(),
                                "open pipeline": t["open_pipeline"],
                                "required pipeline":
                                (t["coverage"] or {}).get("required_pipeline")})
        cov_df = pd.DataFrame(cov_records).melt("snapshot",
                                                var_name="series",
                                                value_name="dollars")
        st.altair_chart(
            alt.Chart(cov_df.dropna()).mark_line(point=True).encode(
                x=alt.X("snapshot:N", title=None),
                y=alt.Y("dollars:Q", title=CURRENCY),
                color=alt.Color("series:N",
                                scale=alt.Scale(range=["#0072B2", "#D55E00"]),
                                legend=alt.Legend(title=None)),
                tooltip=["snapshot", "series",
                         alt.Tooltip("dollars:Q", format=",.0f")])
            .properties(height=220, width=950,
                        title="Coverage: open vs required pipeline "
                              "(1 / trailing win rate)"))
        basis = (data["trajectory"]["coverage"] or {}).get("basis")
        if basis:
            st.caption(f"Coverage basis: {basis}")

        # created-vs-closed weekly flow
        flow_records = []
        for prev_d, cur_d in zip(snapshot_dates, snapshot_dates[1:]):
            prev_ids = {r["opp_id"] for r in snap_rows[prev_d]}
            prev_open = {r["opp_id"] for r in snap_rows[prev_d] if is_open(r)}
            cur = snap_rows[cur_d]
            week = cur_d.isoformat()
            created = [r for r in cur if r["opp_id"] not in prev_ids]
            closed = [r for r in cur
                      if r["opp_id"] in prev_open and not is_open(r)]
            flow_records.append({"week": week, "flow": "created",
                                 "dollars": sum(r["amount"] or 0.0
                                                for r in created),
                                 "count": len(created)})
            for stage, label in (("closed_won", "won"),
                                 ("closed_lost", "lost")):
                subset = [r for r in closed if r["stage"] == stage]
                flow_records.append({"week": week, "flow": label,
                                     "dollars": sum(r["amount"] or 0.0
                                                    for r in subset),
                                     "count": len(subset)})
        st.altair_chart(
            alt.Chart(pd.DataFrame(flow_records)).mark_bar().encode(
                x=alt.X("week:N", title=None),
                y=alt.Y("dollars:Q", title=CURRENCY),
                color=alt.Color("flow:N",
                                scale=alt.Scale(domain=list(FLOW_COLORS),
                                                range=list(FLOW_COLORS.values())),
                                legend=alt.Legend(title=None)),
                xOffset="flow:N",
                tooltip=["week", "flow", "count",
                         alt.Tooltip("dollars:Q", format=",.0f")])
            .properties(height=220, width=950,
                        title="Created vs closed per snapshot week"))

    # desk score trend from the runs table
    run_scores = []
    if store is not None:
        for i, summary in enumerate(
                json.loads(r[0]) for r in store.conn.execute(
                    "SELECT summary_json FROM runs ORDER BY run_id")
                if r[0]):
            score = summary.get("desk", {}).get("weighted_mean_score")
            if score is not None:
                run_scores.append({"run": i + 1,
                                   "as_of": summary.get("as_of"),
                                   "desk score": score})
    if len(run_scores) >= 2:
        st.altair_chart(
            alt.Chart(pd.DataFrame(run_scores)).mark_line(point=True).encode(
                x=alt.X("run:O", title="run"),
                y=alt.Y("desk score:Q",
                        scale=alt.Scale(domain=[0, 100])),
                tooltip=["run", "as_of", "desk score"])
            .properties(height=180, width=950,
                        title="Desk score trend (recorded brief runs)"))
    else:
        st.caption("Desk score trend appears after 2+ recorded brief runs "
                   "(python -m src.brief).")

    st.subheader("Commit accuracy by committed-for quarter")
    st.caption("Committed-for quarter = fiscal quarter of the close date "
               "when the deal was first called commit — immune to later "
               "pushes, so a slipped commit stays counted against the "
               "quarter it was promised for.")
    _ledger_section(ledger, lambda e: e.committed_quarter, "quarter")

# --- Owners ---

with tab_owners:
    st.subheader("Owner scoreboard")
    owner_records = []
    for stats in data["owners"].values():
        if f_owner_set and stats.owner not in f_owner_set:
            continue
        owner_records.append({
            "owner": stats.owner, "open": stats.n_open,
            "mean": stats.mean_score, "median": stats.median_score,
            "violations": stats.violation_count,
            "pipeline": stats.open_pipeline,
            "coverage": None if stats.coverage_ratio is None
            else round(stats.coverage_ratio, 2),
            "flags": ", ".join(f for f, on in
                               (("small_n", stats.small_n),
                                ("low_coverage", stats.coverage_flagged))
                               if on),
        })
    if owner_records:
        st.dataframe(
            pd.DataFrame(owner_records), width="stretch", hide_index=True,
            column_config={
                "mean": _SCORE_COL, "median": _SCORE_COL,
                "pipeline": _MONEY_COL,
                "coverage": st.column_config.NumberColumn(format="%.2fx"),
            })
    st.caption(f"small_n = fewer than {config['min_opps_for_owner_score']} "
               f"open opps, treat the score as anecdotal. Coverage = open "
               f"pipeline vs required pipeline (remaining quota net of wins "
               f"this quarter x the required multiple); low_coverage means "
               f"under 1.00x. Basis: {data['coverage_basis']}.")

    st.subheader("Forecast integrity patterns")
    st.caption("Coaching signal, not a comp input.")
    if patterns is None:
        st.caption("Unavailable outside the snapshot store.")
    else:
        over = [p for p in patterns.values() if p.overcall_flagged]
        under = [p for p in patterns.values() if p.undercall_flagged]
        if not over and not under:
            st.caption("No overcall/undercall patterns flagged.")
        for p in over:
            st.markdown(f"- :orange[Overcall pattern] {p.owner} — "
                        f"{p.overcall_share:.0%} of {p.n_ever_commit} "
                        f"ever-commit opps later pushed or lost")
        for p in under:
            om = "n/a" if p.omitted_share is None else f"{p.omitted_share:.0%}"
            far = "n/a" if p.farout_share is None else f"{p.farout_share:.0%}"
            st.markdown(f"- :blue[Undercall pattern] {p.owner} — open "
                        f"pipeline {om} omitted, {far} far-out "
                        f"(n={p.n_open})")
        small = sum(1 for p in patterns.values()
                    if p.overcall_small_n and p.undercall_small_n)
        st.caption(f"Suppressed as small_n: {small} owners with too little "
                   f"history to score.")

    st.subheader("Commit accuracy")
    owner_entries = (None if ledger is None else
                     [e for e in ledger
                      if not f_owner_set or e.owner in f_owner_set])
    _ledger_section(owner_entries, lambda e: e.owner, "owner")

# --- Teams (team/region rollups) ---

with tab_teams:
    st.subheader("Teams and regions")
    if not (config.get("owner_meta") or {}):
        st.caption("No team/region metadata configured. Point "
                   "PIPELINE_HYGIENE_QUOTAS at a manifest with an `owners` "
                   "block (e.g. data/seed_manifest.json).")
    else:
        st.caption(f"Coverage = open pipeline vs required pipeline (remaining "
                   f"quota net of wins this quarter x the required multiple); "
                   f"low_coverage means under 1.00x. Worst coverage first. "
                   f"Basis: {data['coverage_basis']}.")

        def _group_df(groups):
            ranked = sorted(groups.values(),
                            key=lambda g: (g.coverage_ratio is None,
                                           g.coverage_ratio or 0.0, g.key))
            return pd.DataFrame([{
                "name": g.key, "owners": g.n_owners, "open": g.n_open,
                "mean": g.mean_score, "pipeline": g.open_pipeline,
                "quota": g.quota,
                "coverage": None if g.coverage_ratio is None
                else round(g.coverage_ratio, 2),
                "violations": g.violation_count,
                "at-risk": g.at_risk_dollars,
                "flags": "low_coverage" if g.coverage_flagged else "",
            } for g in ranked])

        _GROUP_COLS = {
            "mean": _SCORE_COL, "pipeline": _MONEY_COL, "quota": _MONEY_COL,
            "at-risk": _MONEY_COL,
            "coverage": st.column_config.NumberColumn(format="%.2fx"),
        }
        for label, groups in (("Teams", data["teams"]),
                              ("Regions", data["regions"])):
            st.markdown(f"**{label}**")
            st.dataframe(_group_df(groups), width="stretch", hide_index=True,
                         column_config=_GROUP_COLS)

        st.subheader("Commit accuracy by team and region")
        owner_meta = config.get("owner_meta") or {}
        _ledger_section(
            ledger,
            lambda e: (owner_meta.get(e.owner) or {}).get("team"), "team")
        if ledger:
            st.dataframe(
                _ledger_df(ledger,
                           lambda e: (owner_meta.get(e.owner) or {})
                           .get("region"), "region"),
                width="stretch", hide_index=True)

# --- Appendix: full exception list + validation (drill-down only) ---

with tab_appendix:
    st.subheader("Exceptions (full list)")
    score_histories = {}
    for open_map in prev_opens:
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
            "owner": row["owner"], "stage": row["stage"],
            "amount": row["amount"], "score": score,
            "score history": history if len(history) >= 2 else None,
            "worst": ["high", "medium", "low"][worst],
            "rules": " ".join(v.rule_id for v in result.violations),
            "streak": f"flagged {streak} runs" if streak >= 2 else "",
            "detail": "; ".join(v.detail for v in result.violations),
            "_rank": (worst, -(row["amount"] or 0.0), result.opp_id),
        })
    exceptions.sort(key=lambda e: e.pop("_rank"))
    st.caption(f"{len(exceptions)} open opps with violations match the "
               f"filters. Rule legend: "
               + "; ".join(f"{rule} {label}"
                           for rule, label in RULE_LABELS.items()))
    if exceptions:
        st.dataframe(
            pd.DataFrame(exceptions), width="stretch", hide_index=True,
            column_config={
                "amount": _MONEY_COL, "score": _SCORE_COL,
                "score history": st.column_config.LineChartColumn(
                    "score history", y_min=0, y_max=100),
                "detail": st.column_config.TextColumn(width="large"),
            })

    with st.expander("Validation report"):
        if validation is None:
            st.write("No validation report stored for this snapshot.")
        else:
            st.write(f"Source `{Path(validation['source_file']).name}`: "
                     f"accepted {validation['accepted']}/"
                     f"{validation['total_rows']} rows, "
                     f"rejected {validation['rejected']}.")
            for warning in validation["warnings"]:
                st.warning(warning)
            if validation["row_reasons"]:
                st.dataframe(pd.DataFrame(validation["row_reasons"],
                                          columns=["row", "opp_id", "reason"]),
                             width="stretch", hide_index=True)

    st.download_button(
        "Download desk brief (markdown)",
        brief.render(data, config),
        file_name=f"desk_brief_{as_of.isoformat()}.md",
        mime="text/markdown",
        help="Rendered from the data shown here; does not record a run.")
