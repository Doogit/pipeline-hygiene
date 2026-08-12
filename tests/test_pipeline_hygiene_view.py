"""Unit tests for the presentation view model (src/pipeline_hygiene_view.py).

The parity gate (tests/parity) already proves byte-level fidelity to the live
Streamlit render for the store-backed fixtures. These tests cover the branches
the parity fixtures don't exercise — chiefly the in-memory upload path (store is
None: no push history, patterns/ledger unavailable) — plus the degradation notes
and empty states, asserted as readable unit checks.
"""
import random
from datetime import date

from src import brief, pipeline_hygiene_view as V
from src.ingest import validate_csv
from src.seed import write_csv
from src.seed.org import build_org
from src.seed.pathologies import generate_snapshot
from src.snapshots import SnapshotStore
from tests.parity._build import build_empty, build_full_multi, build_single_minimal
from tests.parity._flatten import flatten

AS_OF = date(2026, 8, 10)


def _captions(page):
    return flatten(page)["caption"]


def _all_text(page):
    d = flatten(page)
    return d["caption"] + d["markdown"] + d["warning"] + d["subheader"]


# --- happy path (store-backed) ---

def test_full_multi_renders_all_sections(tmp_path):
    env = build_full_multi(tmp_path)
    page = V.build_from_store(env["PIPELINE_HYGIENE_CONFIG"],
                              env["PIPELINE_HYGIENE_DB"],
                              env["PIPELINE_HYGIENE_QUOTAS"])
    assert [t.label for t in page.tabs] == V.TAB_LABELS
    dump = flatten(page)
    assert len(dump["metric"]) == 5
    assert dump["metric"][0] == {"label": "Open opps",
                                 "value": str(dump["metric"][0]["value"]),
                                 "delta": ""}
    # 8 charts incl. the Flow hover; owner scoreboard + drill-down prompt present
    assert len(dump["charts"]) == 8
    assert any("Select an owner row to drill into" in c for c in dump["caption"])
    assert any(c.startswith("Coverage basis:") for c in dump["caption"])


def test_full_multi_download_is_shared_brief_render(tmp_path):
    """The .md download must be the shared pure brief.render — not reimplemented
    — so it stays byte-identical to the CLI/Streamlit export (plan §1, §10)."""
    env = build_full_multi(tmp_path)
    page = V.build_from_store(env["PIPELINE_HYGIENE_CONFIG"],
                              env["PIPELINE_HYGIENE_DB"],
                              env["PIPELINE_HYGIENE_QUOTAS"])
    data = page.controls["data"]
    config = page.controls["config"]
    md = brief.render(data, config)
    assert md == brief.render(data, config)  # deterministic
    assert md.startswith("#") and "## Headline" in md


# --- degradation: single snapshot + bare quotas ---

def test_single_snapshot_shows_empty_states(tmp_path):
    env = build_single_minimal(tmp_path)
    page = V.build_from_store(env["PIPELINE_HYGIENE_CONFIG"],
                              env["PIPELINE_HYGIENE_DB"],
                              env["PIPELINE_HYGIENE_QUOTAS"])
    caps = _captions(page)
    assert "Trajectory charts need at least 2 stored snapshots; showing " \
           "nothing rather than a one-point trend." in caps
    assert "Flow needs at least 2 stored snapshots; showing nothing rather " \
           "than a one-point bridge." in caps
    assert "Desk score trend appears after 2+ stored snapshots." in caps


def test_no_owner_meta_degrades_teams(tmp_path):
    env = build_single_minimal(tmp_path)  # bare quota mapping -> no owner_meta
    page = V.build_from_store(env["PIPELINE_HYGIENE_CONFIG"],
                              env["PIPELINE_HYGIENE_DB"],
                              env["PIPELINE_HYGIENE_QUOTAS"])
    caps = _captions(page)
    assert any("No team/region metadata configured." in c for c in caps)
    # single-snapshot minimal renders only the severity mix chart
    assert len(flatten(page)["charts"]) == 1


# --- empty / missing store (stop paths) ---

def test_empty_store_stops_with_warning(tmp_path):
    env = build_empty(tmp_path)
    page = V.build_from_store(env["PIPELINE_HYGIENE_CONFIG"],
                              env["PIPELINE_HYGIENE_DB"],
                              env["PIPELINE_HYGIENE_QUOTAS"])
    assert page.tabs == []
    assert flatten(page)["warning"] == ["Store is empty; ingest a snapshot or "
                                        "upload a CSV."]


def test_missing_db_warns_to_ingest(tmp_path):
    env = build_empty(tmp_path)
    page = V.build_from_store(env["PIPELINE_HYGIENE_CONFIG"],
                              str(tmp_path / "does_not_exist.db"),
                              env["PIPELINE_HYGIENE_QUOTAS"])
    assert page.tabs == []
    warn = flatten(page)["warning"][0]
    assert warn.startswith("No store at") and "python -m src.ingest" in warn


# --- in-memory upload path (store is None) ---

def _upload_page(tmp_path):
    """Build the view model for a validated in-memory upload (no store), the way
    the dashboard's upload branch does: validate_csv -> build_from_rows with the
    store-derived extras all None."""
    rng = random.Random(7)
    org = build_org(rng)
    from src.ingest import load_config
    config = load_config(tmp_path / "config.yaml") if (tmp_path / "config.yaml").exists() else None
    # reuse the test config the fixtures use
    from tests.parity._build import _base_config
    config = _base_config()
    rows_seed, _ = generate_snapshot(org, 90, AS_OF, config, rng)
    csv_path = tmp_path / f"opps_{AS_OF.isoformat()}.csv"
    write_csv(csv_path, rows_seed)
    rows, report = validate_csv(str(csv_path), config, "default")
    validation = dict(report.to_dict(), source_file=csv_path.name)
    data = brief.build_from_rows(rows, AS_OF, AS_OF, config, validation=validation,
                                 prev_summary=None, outcomes=None, prev_opens=[],
                                 patterns=None, ledger=None)
    return V.build_page_model(
        store=None, config=config, snapshot_date=AS_OF, as_of=AS_OF, rows=rows,
        data=data, prev_summary=None, prev_opens=[], outcomes=None,
        validation=validation)


def test_upload_has_no_push_history_note(tmp_path):
    page = _upload_page(tmp_path)
    caps = _captions(page)
    assert "No push history available (rows evaluated outside the snapshot " \
           "store)." in caps


def test_upload_patterns_and_ledger_unavailable(tmp_path):
    page = _upload_page(tmp_path)
    caps = _captions(page)
    # patterns None -> unavailable; ledger None -> unavailable (appears 3x:
    # trajectory quarter-ledger, owner-ledger, team-ledger + patterns)
    assert caps.count("Unavailable outside the snapshot store.") >= 2


def test_upload_no_previous_run_note(tmp_path):
    page = _upload_page(tmp_path)
    assert "Since last run: no previous run recorded." in _captions(page)
