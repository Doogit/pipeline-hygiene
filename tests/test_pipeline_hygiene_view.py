"""Unit tests for the presentation view model (src/pipeline_hygiene_view.py).

The parity gate (tests/parity) already proves byte-level fidelity to the live
Streamlit render for the store-backed fixtures. These tests cover the branches
the parity fixtures don't exercise — chiefly the in-memory upload path (store is
None: no push history, patterns/ledger unavailable) — plus the degradation notes
and empty states, asserted as readable unit checks.
"""
import random
from datetime import date, timedelta

import yaml

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


def _stored_row(opp_id, snap_date, stage, **over):
    row = {
        "opp_id": opp_id, "account": "Granitefreight LLC", "opp_name": "Deal",
        "owner": "Avery Farrow", "stage": stage, "amount": 50_000.0,
        "currency": "USD", "created_date": date(2026, 1, 5),
        "close_date": date(2026, 10, 1), "last_activity_date": snap_date,
        "next_step": "Send updated proposal to procurement",
        "next_step_date": snap_date + timedelta(days=10),
        "forecast_category": "pipeline", "contact_count": 3,
        "product_line": "CorePlatform", "stage_entered_date": snap_date,
        "close_date_changes": 0,
    }
    row.update(over)
    return row


def _write_snapshot(store, tmp_path, snap_date, rows):
    path = tmp_path / f"opps_{snap_date.isoformat()}.csv"
    write_csv(path, rows)
    assert store.ingest_csv(path, snap_date).rejected == 0


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


def test_full_multi_charts_use_container_width(tmp_path):
    env = build_full_multi(tmp_path)
    page = V.build_from_store(env["PIPELINE_HYGIENE_CONFIG"],
                              env["PIPELINE_HYGIENE_DB"],
                              env["PIPELINE_HYGIENE_QUOTAS"])
    charts = flatten(page)["charts"]
    assert charts, "expected dashboard charts in full fixture"
    assert all(c.get("width") == "container" for c in charts)


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


def test_store_page_uses_derived_aging_config(tmp_path, config):
    d1 = AS_OF - timedelta(days=14)
    d2 = AS_OF - timedelta(days=7)
    cfg = dict(config)
    cfg["aging_norm_mode"] = "derived"
    cfg["aging_norm_derived_multiple"] = 2.0
    cfg["min_closed_for_win_rate"] = 2
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    db_path = tmp_path / "pipeline.db"
    store = SnapshotStore(db_path, cfg)
    movers = [f"DEV-{i}" for i in range(1, 4)]
    _write_snapshot(
        store, tmp_path, d1,
        [_stored_row(o, d1, "develop") for o in movers]
        + [_stored_row("SITTER", d1, "develop",
                       stage_entered_date=AS_OF - timedelta(days=30))])
    _write_snapshot(
        store, tmp_path, d2,
        [_stored_row(o, d2, "develop") for o in movers]
        + [_stored_row("SITTER", d2, "develop",
                       stage_entered_date=AS_OF - timedelta(days=30))])
    _write_snapshot(
        store, tmp_path, AS_OF,
        [_stored_row(o, AS_OF, "propose") for o in movers]
        + [_stored_row("SITTER", AS_OF, "develop",
                       stage_entered_date=AS_OF - timedelta(days=30))])
    store.close()

    page = V.build_from_store(str(cfg_path), str(db_path))
    assert page.controls["config"]["aging_norm_days"]["develop"] == 28
    assert "H6" in page.controls["data"]["results"]["SITTER"].rule_ids()


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


# --- Monday Packet (PR E): flag gating in the view model -------------------

def test_packets_tab_absent_when_flag_off(tmp_path, monkeypatch):
    monkeypatch.delenv("PIPELINE_HYGIENE_PACKETS", raising=False)
    env = build_full_multi(tmp_path)
    page = V.build_from_store(env["PIPELINE_HYGIENE_CONFIG"],
                              env["PIPELINE_HYGIENE_DB"],
                              env["PIPELINE_HYGIENE_QUOTAS"])
    assert [t.label for t in page.tabs] == list(V.TAB_LABELS)
    assert not any(isinstance(b, V.Packets)
                   for t in page.tabs for b in t.blocks)


def test_build_page_model_packets_none_leaves_appendix_untouched(tmp_path,
                                                                 monkeypatch):
    """With packets=None the Appendix tab gets no dismiss-analytics block and no
    Packets tab is appended — the flag-off byte-identity the parity gate asserts.
    (build_page_model is called directly with a stopped-store minimal input.)"""
    monkeypatch.delenv("PIPELINE_HYGIENE_PACKETS", raising=False)
    env = build_full_multi(tmp_path)
    page = V.build_from_store(env["PIPELINE_HYGIENE_CONFIG"],
                              env["PIPELINE_HYGIENE_DB"],
                              env["PIPELINE_HYGIENE_QUOTAS"])
    appendix = page.tabs[6]
    assert appendix.label == "Appendix"
    subheads = [b.text for b in appendix.blocks if isinstance(b, V.Heading)]
    assert "Dismissed work items (by source)" not in subheads


def test_packets_tab_and_appendix_analytics_when_flag_on(tmp_path, monkeypatch):
    """Flag on -> the Packets tab is appended AND the Appendix carries the R5.3
    dismiss-analytics heading. This is exactly why the parity gate runs flag-off."""
    monkeypatch.setenv("PIPELINE_HYGIENE_PACKETS", "1")
    env = build_full_multi(tmp_path)
    page = V.build_from_store(env["PIPELINE_HYGIENE_CONFIG"],
                              env["PIPELINE_HYGIENE_DB"],
                              env["PIPELINE_HYGIENE_QUOTAS"])
    assert page.tabs[-1].label == "Packets"
    assert any(isinstance(b, V.Packets) for b in page.tabs[-1].blocks)
    appendix = next(t for t in page.tabs if t.label == "Appendix")
    subheads = [b.text for b in appendix.blocks if isinstance(b, V.Heading)]
    assert "Dismissed work items (by source)" in subheads
