"""Read-only Streamlit dashboard: streamlit run app/dashboard.py

Loads the latest snapshot from the store by default; an uploaded CSV runs the
same ingest validation and is evaluated in memory — nothing on this page
writes to the store or to source data, and viewing/downloading a brief here
does not record a run. as_of defaults to the snapshot date (date.today() is
banned outside CLI --as-of defaults).
"""
import json
import os
import sys
import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import brief
from src.ingest import IngestError, load_config, snapshot_date_from_filename, \
    validate_csv
from src.rules import is_open
from src.scoring import opp_score
from src.snapshots import SnapshotStore

CONFIG_PATH = os.environ.get("PIPELINE_HYGIENE_CONFIG", "config.yaml")
DB_PATH = os.environ.get("PIPELINE_HYGIENE_DB", "data/pipeline.db")
QUOTAS_PATH = os.environ.get("PIPELINE_HYGIENE_QUOTAS", "data/seed_manifest.json")
_SEV_RANK = {"high": 0, "medium": 1, "low": 2}

st.set_page_config(page_title="pipeline-hygiene", layout="wide")
st.title("pipeline-hygiene — desk inspection")
st.caption("Read-only: agents inspect, people sell. Nothing on this page "
           "writes to the store or to source data.")

config = load_config(CONFIG_PATH)
if Path(QUOTAS_PATH).exists():
    with open(QUOTAS_PATH, encoding="utf-8") as f:
        config["quotas"] = {**(config.get("quotas") or {}),
                            **json.load(f).get("quotas", {})}
    st.sidebar.caption(f"Quotas merged from `{QUOTAS_PATH}`.")

# --- data source ---

st.sidebar.header("Data source")
uploaded = st.sidebar.file_uploader("Upload a snapshot CSV (validated, "
                                    "evaluated in memory, never stored)",
                                    type="csv")
stage_map_name = st.sidebar.selectbox(
    "Stage vocabulary (stage_map)", sorted(config.get("stage_map", {})),
    index=sorted(config.get("stage_map", {})).index("default"))

if uploaded is not None:
    with tempfile.NamedTemporaryFile("wb", suffix=".csv", delete=False) as tmp:
        tmp.write(uploaded.getvalue())
        tmp_path = tmp.name
    try:
        rows, report = validate_csv(tmp_path, config, stage_map_name)
    except IngestError as exc:
        st.error(f"Upload rejected: {exc}")
        st.stop()
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    validation = dict(report.to_dict(), source_file=uploaded.name)
    snapshot_date = snapshot_date_from_filename(uploaded.name)
    if snapshot_date is None:
        st.error("No YYYY-MM-DD snapshot date found in the uploaded filename; "
                 "rename the file like opps_2026-08-10.csv.")
        st.stop()
    st.sidebar.caption(f"Uploaded snapshot {snapshot_date} — single snapshot, "
                       f"so H3/H6 may report insufficient history.")
    prev_summary = None
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
    prev = store.last_run()
    prev_summary = prev["summary"] if prev else None

as_of = st.sidebar.date_input("Evaluate as of", value=snapshot_date)
data = brief.build_from_rows(rows, snapshot_date, as_of, config,
                             validation=validation, prev_summary=prev_summary)
results, desk = data["results"], data["desk"]
rows_by_id = {r["opp_id"]: r for r in rows}

# --- headline ---

open_pipeline = sum(r["amount"] or 0.0 for r in rows if is_open(r))
cols = st.columns(5)
cols[0].metric("Open opps", desk.n_open)
cols[1].metric("Desk score (weighted mean)",
               "n/a" if desk.weighted_mean_score is None
               else f"{desk.weighted_mean_score:.1f}")
cols[2].metric(f"Healthy (score >= {config['healthy_score_threshold']})",
               "n/a" if desk.pct_healthy is None else f"{desk.pct_healthy:.1f}%")
cols[3].metric("Open pipeline", f"${open_pipeline:,.0f}")
cols[4].metric("At-risk dollars", f"${desk.at_risk_dollars:,.0f}",
               help="Distinct open opps with at least one high-severity "
                    "violation; each opp's amount counted once.")
severity_counts = desk.violation_counts_by_severity
st.caption(f"Violations: {severity_counts['high']} high, "
           f"{severity_counts['medium']} medium, {severity_counts['low']} low"
           + ("" if not any(data["insufficient"].values()) else
              " — insufficient history: " + ", ".join(
                  f"{rule} on {n} opps"
                  for rule, n in sorted(data["insufficient"].items()) if n)))

# --- filters ---

st.sidebar.header("Filters")
owners_all = sorted({r["owner"] for r in rows if is_open(r)})
stages_all = sorted({r["stage"] for r in rows if is_open(r)})
f_owners = st.sidebar.multiselect("Owner", owners_all)
f_stages = st.sidebar.multiselect("Stage", stages_all)
f_sev = st.sidebar.multiselect("Severity", ["high", "medium", "low"])
st.sidebar.caption("Severity filter: an opp appears under **each** severity "
                   "it carries, so one opp can match several selections; "
                   "empty filters mean no restriction.")


def _matches(row, result):
    if f_owners and row["owner"] not in f_owners:
        return False
    if f_stages and row["stage"] not in f_stages:
        return False
    if f_sev and not any(v.severity in f_sev for v in result.violations):
        return False
    return True


# --- exception table ---

st.subheader("Exceptions")
exceptions = []
for result in results.values():
    if not result.violations:
        continue
    row = rows_by_id[result.opp_id]
    if not _matches(row, result):
        continue
    worst = min(_SEV_RANK[v.severity] for v in result.violations)
    exceptions.append({
        "opp_id": result.opp_id, "account": row["account"],
        "owner": row["owner"], "stage": row["stage"], "amount": row["amount"],
        "score": opp_score(result, config),
        "worst": ["high", "medium", "low"][worst],
        "rules": " ".join(v.rule_id for v in result.violations),
        "detail": "; ".join(v.detail for v in result.violations),
        "_rank": (worst, -(row["amount"] or 0.0), result.opp_id),
    })
exceptions.sort(key=lambda e: e.pop("_rank"))
st.caption(f"{len(exceptions)} open opps with violations match the filters.")
if exceptions:
    st.dataframe(pd.DataFrame(exceptions), width="stretch", hide_index=True)

# --- owner scoreboard ---

st.subheader("Owner scoreboard")
owner_rows = []
for stats in data["owners"].values():
    if f_owners and stats.owner not in f_owners:
        continue
    owner_rows.append({
        "owner": stats.owner, "open": stats.n_open,
        "mean": round(stats.mean_score, 1),
        "median": round(stats.median_score, 1),
        "violations": stats.violation_count,
        "pipeline": stats.open_pipeline,
        "coverage": None if stats.coverage_ratio is None
        else round(stats.coverage_ratio, 2),
        "flags": ", ".join(f for f, on in (("small_n", stats.small_n),
                                           ("low_coverage", stats.coverage_flagged))
                           if on),
    })
if owner_rows:
    st.dataframe(pd.DataFrame(owner_rows), width="stretch", hide_index=True)
st.caption(f"small_n = fewer than {config['min_opps_for_owner_score']} open "
           f"opps, treat the score as anecdotal; low_coverage = open pipeline "
           f"under {config['coverage_ratio_min']}x quota.")

# --- validation report ---

with st.expander("Validation report"):
    if validation is None:
        st.write("No validation report stored for this snapshot.")
    else:
        st.write(f"Source `{Path(validation['source_file']).name}`: accepted "
                 f"{validation['accepted']}/{validation['total_rows']} rows, "
                 f"rejected {validation['rejected']}.")
        for warning in validation["warnings"]:
            st.warning(warning)
        if validation["row_reasons"]:
            st.dataframe(pd.DataFrame(validation["row_reasons"],
                                      columns=["row", "opp_id", "reason"]),
                         width="stretch", hide_index=True)

# --- brief download ---

st.download_button(
    "Download desk brief (markdown)",
    brief.render(data, config),
    file_name=f"desk_brief_{as_of.isoformat()}.md",
    mime="text/markdown",
    help="Rendered from the data shown here; does not record a run.")
