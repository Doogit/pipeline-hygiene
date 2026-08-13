"""PR D — packet assembly + export (R4).

Section ordering + dollar ranking, CRM block filtered to ACCEPTED items only
(fixtures — the full accept/dismiss E2E lands with PR E), the verbatim
drafts-only header and computed minutes-to-clear in the rendered .html, and a
golden .md (create-on-first-run -> fail-asking-to-commit -> byte-compare).

Work items are seeded from TEMPLATE-sourced fixtures only; captured/accepted
items are recorded facts, not recomputable, so the golden stays deterministic.
Tests load tests/config_test.yaml via the shared `config` fixture; as_of is
threaded, never date.today().
"""
from datetime import date, timedelta
from pathlib import Path

import pytest

from src import packet
from src.packet import build_owner_packet, render_html, render_markdown
from src.seed import write_csv
from src.snapshots import SnapshotStore
from src.work_items import WorkItemStore

AS_OF = date(2026, 8, 10)
OWNER = "Rowan Pemberton"
GOLDEN = Path(__file__).parent / "golden" / "packet_owner_golden.md"


def _row(opp_id, amount, **over):
    row = {
        "opp_id": opp_id, "account": f"Acct {opp_id}",
        "opp_name": f"Deal {opp_id}", "owner": OWNER, "stage": "propose",
        "amount": amount, "currency": "USD", "created_date": date(2026, 1, 5),
        "close_date": date(2026, 9, 30), "last_activity_date": AS_OF,
        "next_step": "Send updated proposal to procurement",
        "next_step_date": date(2026, 10, 1), "forecast_category": "commit",
        "contact_count": 3, "product_line": "CorePlatform",
        "stage_entered_date": date(2026, 8, 1), "close_date_changes": 0,
    }
    row.update(over)
    return row


def _seeded_stores(tmp_path, config):
    """A SnapshotStore with three owner opps of known amounts, plus a
    WorkItemStore seeded with one drafted item per section, all TEMPLATE-worded
    so the golden is byte-stable."""
    store = SnapshotStore(":memory:", config)
    rows = [_row("OPP-BIG", 300_000.0), _row("OPP-MID", 200_000.0),
            _row("OPP-SML", 100_000.0)]
    path = tmp_path / f"opps_{AS_OF.isoformat()}.csv"
    write_csv(path, rows)
    assert store.ingest_csv(path, AS_OF).rejected == 0

    wi = WorkItemStore(":memory:", config)
    sid = AS_OF.isoformat()

    def add(opp_id, item_type, payload):
        return wi.upsert_item(opp_id=opp_id, owner=OWNER, source="H2",
                              item_type=item_type, payload=payload, as_of=AS_OF,
                              snapshot_id=sid)

    # two field updates (dollar-ranked BIG before SML), each a template fact
    add("OPP-SML", "field_update",
        {"field": "close_date", "proposed_value": date(2026, 10, 15),
         "basis": "stage template (no push history)"})
    add("OPP-BIG", "field_update",
        {"field": "close_date", "proposed_value": date(2026, 10, 20),
         "basis": "median of 2 observed push(es)"})
    add("OPP-MID", "next_step",
        {"draft_text": "Confirm pricing and terms are with the buyer for "
                       "review by 2026-08-24", "due_date": date(2026, 8, 24),
         "stage": "propose", "criterion": "pricing and terms are with the buyer"})
    add("OPP-BIG", "email_draft",
        {"subject": "Confirming the timeline for Acct OPP-BIG",
         "body": "Hi Acct OPP-BIG team,\n\nCould you confirm the date?\n\n"
                 "Thanks,\nRowan Pemberton",
         "merge_fields": {"account": "Acct OPP-BIG"}})
    add("OPP-MID", "agenda_item",
        {"title": "Acct OPP-MID (OPP-MID)",
         "prompt": "Walk me through what has to happen for this to close.",
         "risky_rules": ["H2"], "dollar_rank": 1, "amount": 200_000.0,
         "forecast_category": "commit"})
    return store, wi


def test_sections_ordered_and_dollar_ranked(tmp_path, config):
    store, wi = _seeded_stores(tmp_path, config)
    model = build_owner_packet(wi, OWNER, config, AS_OF,
                               snapshot_store=store, snapshot_date=AS_OF)

    # field updates dollar-ranked: BIG (300k) before SML (100k)
    fu = model.sections["field_updates"]
    assert [it.opp_id for it in fu] == ["OPP-BIG", "OPP-SML"]

    # markdown section headings appear in the R4.1 order
    md = render_markdown(model, config)
    order = [md.index(f"## {t}") for t in
             ("Field updates", "Next steps", "Emails (drafts)",
              "Review docs", "Hygiene score")]
    assert order == sorted(order)
    wi.close()


def test_crm_block_includes_only_accepted_field_updates(tmp_path, config):
    store, wi = _seeded_stores(tmp_path, config)
    # accept exactly one field update (fixture stand-in for PR E's accept UI)
    big = next(i for i in wi.items(open_only=True)
               if i["item_type"] == "field_update" and i["opp_id"] == "OPP-BIG")
    wi.accept(big["id"], at=AS_OF)

    model = build_owner_packet(wi, OWNER, config, AS_OF,
                               snapshot_store=store, snapshot_date=AS_OF)
    assert "OPP-BIG" in model.crm_block
    assert "OPP-SML" not in model.crm_block          # proposed, not accepted
    assert model.crm_block == "OPP-BIG · close_date = 2026-10-20"
    wi.close()


def test_crm_block_empty_state_when_nothing_accepted(tmp_path, config):
    store, wi = _seeded_stores(tmp_path, config)
    model = build_owner_packet(wi, OWNER, config, AS_OF,
                               snapshot_store=store, snapshot_date=AS_OF)
    assert model.crm_block == "(no accepted field updates yet)"
    wi.close()


def test_html_has_verbatim_header_and_computed_minutes(tmp_path, config):
    store, wi = _seeded_stores(tmp_path, config)
    model = build_owner_packet(wi, OWNER, config, AS_OF,
                               snapshot_store=store, snapshot_date=AS_OF)
    html = render_html(model, config)

    # R4.4 verbatim header, snapshot label substituted
    assert ("Drafts only — review before use. Generated from snapshot "
            "2026-08-10. Nothing was sent or written on your behalf.") in html

    # R4.5 minutes-to-clear is COMPUTED (2xfield_update + next_step + email +
    # agenda = 1+1+2+4+3 = 11), printed as the estimate — no fixed 15-min slogan
    assert model.minutes_to_clear == 11
    assert "Estimated 11 minute(s) to clear 5 item(s)." in html
    assert "15-minute" not in html and "15 minute" not in html
    wi.close()


def test_minutes_scale_with_item_count_not_a_fixed_slogan(tmp_path, config):
    """A larger packet prints a larger number — the estimate is item_count x
    per-type constants, never a hard-coded 15 (resolves the P2 slogan risk)."""
    store, wi = _seeded_stores(tmp_path, config)
    small = build_owner_packet(wi, OWNER, config, AS_OF,
                               snapshot_store=store, snapshot_date=AS_OF)
    wi.upsert_item(opp_id="OPP-BIG", owner=OWNER, source="H3",
                   item_type="clinic_doc",
                   payload={"markdown": "# Review\n\nQuestion?\n"},
                   as_of=AS_OF, snapshot_id=AS_OF.isoformat())
    bigger = build_owner_packet(wi, OWNER, config, AS_OF,
                                snapshot_store=store, snapshot_date=AS_OF)
    assert bigger.minutes_to_clear > small.minutes_to_clear
    assert bigger.minutes_to_clear == small.minutes_to_clear + 6  # clinic_doc
    wi.close()


def test_minutes_use_defaults_when_packet_config_absent(tmp_path, config):
    """Older configs may have the drafts block from R2 without R4's nested
    packet block; packet export should still render using defaults."""
    store, wi = _seeded_stores(tmp_path, config)
    older = dict(config)
    older["drafts"] = {k: v for k, v in config["drafts"].items()
                       if k != "packet"}

    model = build_owner_packet(wi, OWNER, older, AS_OF,
                               snapshot_store=store, snapshot_date=AS_OF)
    assert model.minutes_to_clear == 11
    assert "Estimated 11 minute(s)" in render_markdown(model, older)
    wi.close()


def test_score_delta_line_present_without_prior_snapshot(tmp_path, config):
    store, wi = _seeded_stores(tmp_path, config)
    model = build_owner_packet(wi, OWNER, config, AS_OF,
                               snapshot_store=store, snapshot_date=AS_OF)
    # single snapshot -> no prior to compare, but the line is still present
    assert model.score_delta_line.startswith("Hygiene score:")
    assert "no prior snapshot" in model.score_delta_line
    wi.close()


def test_score_delta_line_reports_change_across_snapshots(tmp_path, config):
    store = SnapshotStore(":memory:", config)
    prior = AS_OF - timedelta(days=7)
    for snap in (prior, AS_OF):
        rows = [_row("OPP-BIG", 300_000.0, last_activity_date=snap,
                     stage_entered_date=snap,
                     next_step_date=snap + timedelta(days=10))]
        path = _write(store, snap, rows)
        assert store.ingest_csv(path, snap).rejected == 0
    wi = WorkItemStore(":memory:", config)
    wi.upsert_item(opp_id="OPP-BIG", owner=OWNER, source="H2",
                   item_type="field_update",
                   payload={"field": "close_date",
                            "proposed_value": date(2026, 10, 20),
                            "basis": "median of 2 observed push(es)"},
                   as_of=AS_OF, snapshot_id=AS_OF.isoformat())
    model = build_owner_packet(wi, OWNER, config, AS_OF,
                               snapshot_store=store, snapshot_date=AS_OF)
    assert f"since {prior.isoformat()}" in model.score_delta_line
    wi.close()


def _write(store, snap, rows):
    import tempfile
    path = Path(tempfile.mkdtemp()) / f"opps_{snap.isoformat()}.csv"
    write_csv(path, rows)
    return path


def test_golden_owner_packet_md(tmp_path, config):
    store, wi = _seeded_stores(tmp_path, config)
    # one accepted field update so the CRM block is exercised in the golden
    big = next(i for i in wi.items(open_only=True)
               if i["item_type"] == "field_update" and i["opp_id"] == "OPP-BIG")
    wi.accept(big["id"], at=AS_OF)
    model = build_owner_packet(wi, OWNER, config, AS_OF,
                               snapshot_store=store, snapshot_date=AS_OF)
    generated = render_markdown(model, config)
    if not GOLDEN.exists():
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(generated, encoding="utf-8", newline="\n")
        pytest.fail(f"golden file created at {GOLDEN}; review and commit it")
    assert generated == GOLDEN.read_text(encoding="utf-8")
    wi.close()


def test_build_is_empty_for_unknown_owner(tmp_path, config):
    store, wi = _seeded_stores(tmp_path, config)
    model = build_owner_packet(wi, "::nobody::", config, AS_OF,
                               snapshot_store=store, snapshot_date=AS_OF)
    assert model.item_count == 0
    assert all(model.sections[k] == [] for k in packet._SECTION_ORDER)
    md = render_markdown(model, config)
    assert md.count("_None._") == 4          # four item sections empty
    wi.close()
