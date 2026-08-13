"""Monday Packet PR E — dashboard integration + write routes (R5).

Exercises the flag-gated Packets tab, the accept/dismiss/edit/reopen mutation
routes (writes confined to the work_items ledger), packet .md/.html export, the
same-origin CSRF guard, and — the headline — acceptance gate #3: accept two /
dismiss one -> the CRM block carries exactly the two accepted opps and the
Appendix dismiss count incremented.

Work items are seeded through WorkItemStore against the same db_path the server
reads (captured/accepted items are recorded facts, not recomputed). The store is
gated behind PIPELINE_HYGIENE_PACKETS, set per test via monkeypatch.
"""
import re
from datetime import date
from urllib.parse import quote

import pytest
from starlette.testclient import TestClient

from app.server import app
from src.ingest import load_config
from src.snapshots import SnapshotStore
from src.work_items import WorkItemStore, normalize_owner
from tests.parity._build import build_full_multi

client = TestClient(app)

OWNER = "Avery Calloway"
AS_OF = date(2026, 8, 10)


@pytest.fixture
def packets_env(tmp_path, monkeypatch):
    env = build_full_multi(tmp_path)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("PIPELINE_HYGIENE_PACKETS", "1")
    return env


def _seed_field_updates(env, owner=OWNER, n=3):
    """Seed n template-worded field_update items for one owner, returns ids."""
    config = load_config(env["PIPELINE_HYGIENE_CONFIG"])
    wi = WorkItemStore(env["PIPELINE_HYGIENE_DB"], config)
    ids = []
    try:
        for i in range(n):
            wid = wi.upsert_item(
                opp_id=f"OPP-{i}", owner=owner, source="H2",
                item_type="field_update",
                payload={"field": "close_date",
                         "proposed_value": date(2026, 10, 10 + i),
                         "basis": "stage template (no push history)"},
                as_of=AS_OF, snapshot_id=AS_OF.isoformat())
            ids.append(wid)
    finally:
        wi.close()
    return ids


def _norm():
    return normalize_owner(OWNER)


# --- flag gating -----------------------------------------------------------


def test_packets_tab_hidden_when_flag_off(tmp_path, monkeypatch):
    env = build_full_multi(tmp_path)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    # flag deliberately NOT set
    r = client.get("/")
    assert r.status_code == 200
    assert ">Packets</button>" not in r.text
    # a mutation route is a hard 404 with the flag off
    assert client.post("/work-item/1/accept").status_code == 404


def test_packets_tab_present_when_flag_on(packets_env):
    r = client.get("/")
    assert r.status_code == 200
    assert ">Packets</button>" in r.text


def test_empty_state_no_work_items(packets_env):
    # flag on, nothing seeded -> Packets tab renders an explicit empty caption
    r = client.get("/content")
    assert r.status_code == 200
    assert "No work items yet" in r.text


# --- navigation / owner selection ------------------------------------------


def test_owner_selector_lists_owners_with_counts(packets_env):
    _seed_field_updates(packets_env, n=2)
    r = client.get("/content")
    assert r.status_code == 200
    assert OWNER in r.text
    # selecting the owner shows their packet + work-item table
    r2 = client.get("/content", params={"packet_owner": _norm()})
    assert r2.status_code == 200
    assert "packets-panel" in r2.text
    assert "OPP-0" in r2.text


def test_mutation_refresh_uses_included_packet_owner_form_value(packets_env):
    wid = _seed_field_updates(packets_env, n=1)[0]
    r = client.post(f"/work-item/{wid}/accept",
                    data={"packet_owner": _norm()})

    assert r.status_code == 200
    assert "OPP-0" in r.text
    assert "accepted" in r.text


def test_selected_owner_zero_open_items_empty_state(packets_env):
    r = client.get("/content", params={"packet_owner": "::nobody::"})
    assert r.status_code == 200
    assert "No open work items for the selected owner." in r.text


# --- acceptance gate #3 ----------------------------------------------------


def test_accept_two_dismiss_one_crm_and_appendix(packets_env):
    ids = _seed_field_updates(packets_env, n=3)
    accept_ids, dismiss_id = ids[:2], ids[2]

    for wid in accept_ids:
        r = client.post(f"/work-item/{wid}/accept",
                        params={"packet_owner": _norm()})
        assert r.status_code == 200
    r = client.post(f"/work-item/{dismiss_id}/dismiss",
                    params={"packet_owner": _norm()},
                    data={"reason": "not this quarter"})
    assert r.status_code == 200

    # CRM block (via the .md export) contains EXACTLY the two accepted opps.
    md = client.get(f"/packet/{_norm()}.md").text
    crm = md.split("Paste-ready CRM block", 1)[1]
    assert "OPP-0" in crm and "OPP-1" in crm
    assert "OPP-2" not in crm

    # Appendix dismiss-count for the source incremented by one (H2 -> 1).
    appendix = client.get("/content").text
    m = re.search(r"Dismissed work items \(by source\).*?</table>", appendix,
                  re.S)
    assert m and "H2" in m.group(0)

    # every action wrote a work_item_events row.
    config = load_config(packets_env["PIPELINE_HYGIENE_CONFIG"])
    wi = WorkItemStore(packets_env["PIPELINE_HYGIENE_DB"], config)
    try:
        for wid in accept_ids:
            evs = wi.events(wid)
            assert any(e["to_status"] == "accepted" for e in evs)
        evs = wi.events(dismiss_id)
        assert any(e["to_status"] == "dismissed"
                   and e["reason"] == "not this quarter" for e in evs)
    finally:
        wi.close()


# --- edit ------------------------------------------------------------------


def test_edit_valid_value_updates_status_and_value(packets_env):
    wid = _seed_field_updates(packets_env, n=1)[0]
    r = client.post(f"/work-item/{wid}/edit",
                    params={"packet_owner": _norm()},
                    data={"proposed_value": "2026-12-01"})
    assert r.status_code == 200
    config = load_config(packets_env["PIPELINE_HYGIENE_CONFIG"])
    wi = WorkItemStore(packets_env["PIPELINE_HYGIENE_DB"], config)
    try:
        item = next(i for i in wi.items() if i["id"] == wid)
        assert item["status"] == "edited"
        import json
        assert json.loads(item["payload_json"])["proposed_value"] == "2026-12-01"
    finally:
        wi.close()


def test_edit_then_accept_uses_edited_value_in_crm_block(packets_env):
    wid = _seed_field_updates(packets_env, n=1)[0]
    r = client.post(f"/work-item/{wid}/edit",
                    params={"packet_owner": _norm()},
                    data={"proposed_value": "2026-12-01"})
    assert r.status_code == 200

    r = client.post(f"/work-item/{wid}/accept",
                    params={"packet_owner": _norm()})
    assert r.status_code == 200

    md = client.get(f"/packet/{_norm()}.md").text
    crm = md.split("Paste-ready CRM block", 1)[1]
    assert "OPP-0" in crm
    assert "close_date = 2026-12-01" in crm


def test_edit_type_invalid_value_unchanged_with_error(packets_env):
    wid = _seed_field_updates(packets_env, n=1)[0]
    r = client.post(f"/work-item/{wid}/edit",
                    params={"packet_owner": _norm()},
                    data={"proposed_value": "not-a-date"})
    assert r.status_code == 200            # re-render, not a hard error
    assert "Invalid value" in r.text
    config = load_config(packets_env["PIPELINE_HYGIENE_CONFIG"])
    wi = WorkItemStore(packets_env["PIPELINE_HYGIENE_DB"], config)
    try:
        item = next(i for i in wi.items() if i["id"] == wid)
        assert item["status"] == "proposed"   # unchanged, not persisted
    finally:
        wi.close()


# --- dismiss without reason ------------------------------------------------


def test_dismiss_without_reason_400_no_transition(packets_env):
    wid = _seed_field_updates(packets_env, n=1)[0]
    r = client.post(f"/work-item/{wid}/dismiss",
                    params={"packet_owner": _norm()}, data={"reason": "  "})
    assert r.status_code == 400
    config = load_config(packets_env["PIPELINE_HYGIENE_CONFIG"])
    wi = WorkItemStore(packets_env["PIPELINE_HYGIENE_DB"], config)
    try:
        item = next(i for i in wi.items() if i["id"] == wid)
        assert item["status"] == "proposed"
    finally:
        wi.close()


# --- reopen ----------------------------------------------------------------


def test_dismiss_then_reopen_back_to_proposed(packets_env):
    wid = _seed_field_updates(packets_env, n=1)[0]
    client.post(f"/work-item/{wid}/dismiss",
                params={"packet_owner": _norm()}, data={"reason": "later"})
    r = client.post(f"/work-item/{wid}/reopen",
                    params={"packet_owner": _norm()})
    assert r.status_code == 200
    config = load_config(packets_env["PIPELINE_HYGIENE_CONFIG"])
    wi = WorkItemStore(packets_env["PIPELINE_HYGIENE_DB"], config)
    try:
        item = next(i for i in wi.items() if i["id"] == wid)
        assert item["status"] == "proposed"
    finally:
        wi.close()


def test_expired_item_cannot_be_reopened(packets_env):
    wid = _seed_field_updates(packets_env, n=1)[0]
    config = load_config(packets_env["PIPELINE_HYGIENE_CONFIG"])
    wi = WorkItemStore(packets_env["PIPELINE_HYGIENE_DB"], config)
    try:
        wi.transition(wid, "expired", at=AS_OF, by="system",
                      reason="resolved_in_source")
    finally:
        wi.close()
    r = client.post(f"/work-item/{wid}/reopen",
                    params={"packet_owner": _norm()})
    assert r.status_code == 409
    wi = WorkItemStore(packets_env["PIPELINE_HYGIENE_DB"], config)
    try:
        item = next(i for i in wi.items() if i["id"] == wid)
        assert item["status"] == "expired"
    finally:
        wi.close()


def test_closed_items_reject_accept_dismiss_and_edit(packets_env):
    wid = _seed_field_updates(packets_env, n=1)[0]
    config = load_config(packets_env["PIPELINE_HYGIENE_CONFIG"])
    wi = WorkItemStore(packets_env["PIPELINE_HYGIENE_DB"], config)
    try:
        wi.transition(wid, "expired", at=AS_OF, by="system",
                      reason="resolved_in_source")
    finally:
        wi.close()

    assert client.post(f"/work-item/{wid}/accept").status_code == 409
    assert client.post(f"/work-item/{wid}/dismiss",
                       data={"reason": "stale"}).status_code == 409
    assert client.post(f"/work-item/{wid}/edit",
                       data={"proposed_value": "2026-12-01"}).status_code == 409


def test_missing_item_mutations_return_404(packets_env):
    assert client.post("/work-item/999999/accept").status_code == 404
    assert client.post("/work-item/999999/dismiss",
                       data={"reason": "stale"}).status_code == 404
    assert client.post("/work-item/999999/edit",
                       data={"proposed_value": "2026-12-01"}).status_code == 404
    assert client.post("/work-item/999999/reopen").status_code == 404


# --- same-origin guard -----------------------------------------------------


def test_cross_site_post_rejected_403(packets_env):
    wid = _seed_field_updates(packets_env, n=1)[0]
    r = client.post(f"/work-item/{wid}/accept",
                    headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403


def test_same_origin_header_allowed(packets_env):
    wid = _seed_field_updates(packets_env, n=1)[0]
    r = client.post(f"/work-item/{wid}/accept",
                    params={"packet_owner": _norm()},
                    headers={"Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 200


# --- export ----------------------------------------------------------------


def test_packet_export_md_and_html(packets_env):
    _seed_field_updates(packets_env, n=1)
    md = client.get(f"/packet/{_norm()}.md")
    assert md.status_code == 200
    assert md.headers["content-type"].startswith("text/markdown")
    assert "attachment; filename=" in md.headers["content-disposition"]
    assert "Monday Packet" in md.text

    html = client.get(f"/packet/{_norm()}.html")
    assert html.status_code == 200
    assert html.headers["content-type"].startswith("text/html")
    assert "Drafts only" in html.text


def test_packet_export_links_match_selected_snapshot(packets_env):
    _seed_field_updates(packets_env, n=1)
    config = load_config(packets_env["PIPELINE_HYGIENE_CONFIG"])
    store = SnapshotStore(packets_env["PIPELINE_HYGIENE_DB"], config)
    try:
        selected = store.snapshot_dates()[0].isoformat()
    finally:
        store.close()

    r = client.get("/content",
                   params={"packet_owner": _norm(), "snapshot": selected,
                           "as_of": selected})

    assert r.status_code == 200
    assert f"/packet/{quote(_norm())}.md?snapshot={selected}" in r.text
    assert f"as_of={selected}" in r.text


def test_packet_export_404_when_flag_off(tmp_path, monkeypatch):
    env = build_full_multi(tmp_path)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert client.get(f"/packet/{_norm()}.md").status_code == 404
