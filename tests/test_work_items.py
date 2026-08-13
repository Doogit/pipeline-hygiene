"""PR A — work-items ledger: schema, idempotency, audit trail, expiry, flag.

All time is threaded (as_of); no wall clock. Tests load tests/config_test.yaml
via the shared `config` fixture, never repo config.yaml.
"""
import sqlite3
from datetime import date, timedelta

import pytest

from src.snapshots import SnapshotStore
from src.work_items import (WorkItemStore, packets_enabled, normalize_owner,
                            payload_hash)

AS_OF = date(2026, 8, 1)


@pytest.fixture
def wi():
    store = WorkItemStore(":memory:")
    yield store
    store.close()


def _clean_row(**overrides):
    """A fully clean opportunity row (scores 100 — no rule fires) so a single
    overridden field isolates one violation. Carries every field the rules read
    so evaluate_row never KeyErrors."""
    row = {
        "opp_id": "OPP-1", "account": "Acme", "opp_name": "Acme Expansion",
        "owner": "Dana Lee", "stage": "develop", "amount": 50000.0,
        "currency": "USD", "created_date": AS_OF - timedelta(days=20),
        "close_date": AS_OF + timedelta(days=30),
        "last_activity_date": AS_OF, "next_step": "Send signed MSA to buyer",
        "next_step_date": AS_OF + timedelta(days=5),
        "forecast_category": "pipeline", "contact_count": 3,
        "product_line": "Platform",
        "stage_entered_date": AS_OF - timedelta(days=5), "close_date_changes": 0,
    }
    row.update(overrides)
    return row


# --- feature flag (R6.4) ---

def test_flag_default_off():
    assert packets_enabled(env={}) is False


@pytest.mark.parametrize("val,expected", [
    ("1", True), ("true", True), ("TRUE", True), ("on", True), ("yes", True),
    ("0", False), ("false", False), ("", False), ("nope", False),
])
def test_flag_parsing(val, expected):
    assert packets_enabled(env={"PIPELINE_HYGIENE_PACKETS": val}) is expected


def test_snapshot_store_does_not_create_work_items_tables():
    """Flag-off invariance: nothing but WorkItemStore creates the tables, so a
    plain SnapshotStore is byte-identical to the packets-disabled build."""
    snap = SnapshotStore(":memory:", {})
    tables = {r[0] for r in snap.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "work_items" not in tables
    assert "work_item_events" not in tables
    snap.close()


# --- schema round-trip (R1.1) ---

def test_schema_round_trip(wi):
    wid = wi.upsert_item(
        opp_id="OPP-1", owner="Dana Lee", source="H4", item_type="next_step",
        payload={"draft_text": "Confirm scope by 2026-08-15"},
        as_of=AS_OF, snapshot_id="2026-08-01")
    (item,) = wi.items()
    assert item["id"] == wid
    assert item["opp_id"] == "OPP-1"
    assert item["owner_normalized"] == "dana lee"  # casefolded
    assert item["status"] == "proposed"
    assert item["source"] == "H4"
    assert item["created_at"] == "2026-08-01"
    assert item["snapshot_id"] == "2026-08-01"
    # first event is the creation, proposed
    (ev,) = wi.events(wid)
    assert (ev["from_status"], ev["to_status"], ev["reason"]) == \
        (None, "proposed", "created")


def test_owner_normalized_helper():
    assert normalize_owner("Dana LEE") == "dana lee"
    assert normalize_owner(None) == ""


# --- idempotency (R1.2) ---

def test_field_update_supersedes_on_rerun(wi):
    """A second capture proposal for the same field refreshes the open item —
    non-deterministic LLM wording must not spawn a duplicate (acceptance #1)."""
    first = wi.upsert_item(
        opp_id="OPP-1", owner="Dana", source="capture", item_type="field_update",
        payload={"field": "next_step", "proposed_value": "Book POC review"},
        as_of=AS_OF)
    second = wi.upsert_item(
        opp_id="OPP-1", owner="Dana", source="capture", item_type="field_update",
        payload={"field": "next_step", "proposed_value": "Schedule POC review 8/21"},
        as_of=AS_OF + timedelta(days=7))
    assert first == second
    items = wi.items(open_only=True)
    assert len(items) == 1
    assert "8/21" in items[0]["payload_json"]  # refreshed to latest wording
    # refresh is not a status change -> still exactly one (creation) event
    assert len(wi.events(first)) == 1


def test_field_update_distinct_fields_are_distinct_items(wi):
    wi.upsert_item(opp_id="OPP-1", owner="D", source="capture",
                   item_type="field_update",
                   payload={"field": "next_step", "proposed_value": "x"}, as_of=AS_OF)
    wi.upsert_item(opp_id="OPP-1", owner="D", source="capture",
                   item_type="field_update",
                   payload={"field": "close_date", "proposed_value": "2026-09-30"},
                   as_of=AS_OF)
    assert len(wi.items(open_only=True)) == 2


def test_template_item_dedups_by_hash(wi):
    payload = {"draft_text": "Confirm exit criteria by 2026-08-15"}
    a = wi.upsert_item(opp_id="OPP-1", owner="D", source="H4",
                       item_type="next_step", payload=payload, as_of=AS_OF)
    b = wi.upsert_item(opp_id="OPP-1", owner="D", source="H4",
                       item_type="next_step", payload=dict(payload), as_of=AS_OF)
    assert a == b
    assert len(wi.items(open_only=True)) == 1
    # different template text -> a distinct item
    wi.upsert_item(opp_id="OPP-1", owner="D", source="H4", item_type="next_step",
                   payload={"draft_text": "Different"}, as_of=AS_OF)
    assert len(wi.items(open_only=True)) == 2


def test_rebuild_produces_no_duplicates(wi):
    """Acceptance gate #1: re-running the same build yields the same open set."""
    def build():
        wi.upsert_item(opp_id="OPP-1", owner="D", source="H4",
                       item_type="next_step",
                       payload={"draft_text": "Confirm scope"}, as_of=AS_OF)
        wi.upsert_item(opp_id="OPP-2", owner="D", source="capture",
                       item_type="field_update",
                       payload={"field": "close_date", "proposed_value": "2026-10-01"},
                       as_of=AS_OF)
    build()
    build()
    assert len(wi.items(open_only=True)) == 2


def test_dismissed_item_does_not_block_new_proposal(wi):
    """Idempotency is scoped to OPEN items; a dismissed proposal for the same
    field can be re-proposed later as a fresh item."""
    wid = wi.upsert_item(opp_id="OPP-1", owner="D", source="capture",
                         item_type="field_update",
                         payload={"field": "next_step", "proposed_value": "x"},
                         as_of=AS_OF)
    wi.dismiss(wid, reason="wrong_field", at=AS_OF)
    wid2 = wi.upsert_item(opp_id="OPP-1", owner="D", source="capture",
                          item_type="field_update",
                          payload={"field": "next_step", "proposed_value": "y"},
                          as_of=AS_OF)
    assert wid2 != wid
    assert len(wi.items(open_only=True)) == 1


# --- audit trail (R1.4) ---

def test_event_appended_on_every_status_change(wi):
    wid = wi.upsert_item(opp_id="OPP-1", owner="D", source="H4",
                         item_type="next_step",
                         payload={"draft_text": "Confirm scope"}, as_of=AS_OF)
    wi.accept(wid, at=AS_OF + timedelta(days=1), by="mgr")
    wi.dismiss(wid, reason="stale", at=AS_OF + timedelta(days=2), by="mgr",
               note="deal died")
    evs = wi.events(wid)
    assert [(e["from_status"], e["to_status"]) for e in evs] == [
        (None, "proposed"), ("proposed", "accepted"), ("accepted", "dismissed")]
    (item,) = wi.items()
    assert item["status"] == "dismissed"
    assert item["dismiss_reason"] == "stale"
    assert item["dismiss_note"] == "deal died"


def test_edit_records_event_and_supersedes_payload(wi):
    wid = wi.upsert_item(opp_id="OPP-1", owner="D", source="capture",
                         item_type="field_update",
                         payload={"field": "next_step", "proposed_value": "x"},
                         as_of=AS_OF)
    wi.edit(wid, {"field": "next_step", "proposed_value": "corrected"},
            at=AS_OF + timedelta(days=1), by="mgr")
    (item,) = wi.items()
    assert item["status"] == "edited"
    assert "corrected" in item["payload_json"]
    assert wi.events(wid)[-1]["to_status"] == "edited"


# --- expiry (R1.3) ---

def test_rule_sourced_item_expires_when_resolved(wi, config):
    """H4 item expires once the opp has a valid next step in the new snapshot."""
    wid = wi.upsert_item(opp_id="OPP-1", owner="Dana", source="H4",
                         item_type="next_step",
                         payload={"draft_text": "Confirm scope"}, as_of=AS_OF)
    # still broken -> stays open
    broken = _clean_row(next_step="", next_step_date=None)
    assert wi.expire_resolved([broken], config, AS_OF) == 0
    assert wi.items()[0]["status"] == "proposed"
    # resolved in source (valid, unexpired next step) -> expired
    resolved = _clean_row(next_step_date=AS_OF + timedelta(days=30))
    assert wi.expire_resolved([resolved], config, AS_OF + timedelta(days=7)) == 1
    (item,) = wi.items()
    assert item["status"] == "expired"
    last = wi.events(wid)[-1]
    assert (last["to_status"], last["reason"]) == ("expired", "resolved_in_source")


def test_rule_sourced_item_expires_when_opp_gone(wi, config):
    wi.upsert_item(opp_id="OPP-1", owner="D", source="H4",
                   item_type="next_step", payload={"draft_text": "x"}, as_of=AS_OF)
    assert wi.expire_resolved([], config, AS_OF) == 1
    assert wi.items()[0]["status"] == "expired"


def test_capture_field_update_expires_when_field_matches(wi, config):
    wi.upsert_item(opp_id="OPP-1", owner="D", source="capture",
                   item_type="field_update",
                   payload={"field": "next_step",
                            "proposed_value": "Send signed MSA to buyer"},
                   as_of=AS_OF)
    # newest snapshot already has the proposed value -> expired
    assert wi.expire_resolved([_clean_row()], config, AS_OF) == 1
    assert wi.items()[0]["status"] == "expired"


def test_capture_field_update_stays_open_when_field_differs(wi, config):
    wi.upsert_item(opp_id="OPP-1", owner="D", source="capture",
                   item_type="field_update",
                   payload={"field": "next_step", "proposed_value": "something else"},
                   as_of=AS_OF)
    assert wi.expire_resolved([_clean_row()], config, AS_OF) == 0
    assert wi.items()[0]["status"] == "proposed"


def test_manual_item_never_auto_expires(wi, config):
    wi.upsert_item(opp_id="OPP-1", owner="D", source="manual",
                   item_type="agenda_item",
                   payload={"draft_text": "Discuss renewal"}, as_of=AS_OF)
    # opp entirely absent, yet a manual item is never auto-expired
    assert wi.expire_resolved([], config, AS_OF) == 0
    assert wi.items()[0]["status"] == "proposed"


# --- misc guards ---

def test_unknown_item_type_rejected(wi):
    with pytest.raises(ValueError):
        wi.upsert_item(opp_id="OPP-1", owner="D", source="manual",
                       item_type="bogus", payload={}, as_of=AS_OF)


def test_field_update_requires_field(wi):
    with pytest.raises(ValueError):
        wi.upsert_item(opp_id="OPP-1", owner="D", source="capture",
                       item_type="field_update",
                       payload={"proposed_value": "x"}, as_of=AS_OF)


def test_payload_hash_is_order_insensitive():
    assert payload_hash({"a": 1, "b": 2}) == payload_hash({"b": 2, "a": 1})
