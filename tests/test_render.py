from types import SimpleNamespace

from fasthtml.common import to_xml

from app import render as R
from src import pipeline_hygiene_view as V


def _html(table_id, columns, rows):
    return to_xml(R.render_table(V.Table(table_id, columns, rows), "$"))


def test_unknown_authoritative_severity_fails_soft():
    html = _html("exceptions", ["worst", "rules"],
                 [{"worst": "critical", "rules": "H2"}])

    assert "sev-" not in html
    assert 'class="chip mono"' in html


def test_contextual_h1_severity_uses_stage():
    html = _html("risky_commits", ["stage", "flags"],
                 [{"stage": "commit", "flags": "H1"}])

    assert 'tr class="sev-high"' in html
    assert 'class="chip mono high"' in html


def test_contextual_h3_severity_uses_push_count():
    html = _html("slippage", ["stage", "pushes", "rules"],
                 [{"stage": "develop", "pushes": 3, "rules": "H3"}])

    assert 'tr class="sev-high"' in html
    assert 'class="chip mono high"' in html


def test_contextual_h10_without_forecast_keeps_low_fallback():
    html = _html("slippage", ["rules"], [{"rules": "H10"}])

    assert 'tr class="sev-low"' in html
    assert 'class="chip mono low"' in html


def test_packet_item_controls_submit_the_typed_value():
    # The dismiss-reason and edit-value inputs live outside any form, so htmx
    # includes them by id. The REAL (typed) inputs must carry those ids — a
    # hidden shadow input would silently submit an empty/stale value instead.
    item = {"id": 7, "item_type": "field_update", "status": "proposed",
            "opp_id": "OPP-1", "target_field": "close_date",
            "payload_json": '{"field": "close_date", '
                            '"proposed_value": "2026-10-15"}'}
    b = V.Packets(owners=[], selected="rowan", packet=None, items=[item])
    html = to_xml(R.render_packets(b, "$"))

    # No hidden shadow inputs for the typed reason/edit controls.
    assert 'type="hidden" name="reason"' not in html
    assert 'type="hidden" name="proposed_value"' not in html
    # The typed reason input owns the id the confirm button includes.
    assert 'id="wi-reason-7"' in html and 'name="reason"' in html
    assert 'hx-include="#sidebar-form, #packet-owner, #wi-reason-7"' in html
    # The edit input owns its id and is seeded with the current value.
    assert 'id="wi-edit-7"' in html and 'name="proposed_value"' in html
    assert 'value="2026-10-15"' in html
    assert 'hx-include="#sidebar-form, #packet-owner, #wi-edit-7"' in html


def test_edited_packet_item_can_still_be_accepted():
    item = {"id": 7, "item_type": "field_update", "status": "edited",
            "opp_id": "OPP-1", "target_field": "close_date",
            "payload_json": '{"field": "close_date", '
                            '"proposed_value": "2026-12-01"}'}
    b = V.Packets(owners=[], selected="rowan", packet=None, items=[item])
    html = to_xml(R.render_packets(b, "$"))

    assert 'hx-post="/work-item/7/accept"' in html


def test_packet_panel_carries_selected_owner_for_mutation_refresh():
    item = {"id": 7, "item_type": "field_update", "status": "proposed",
            "opp_id": "OPP-1", "target_field": "close_date",
            "payload_json": '{"field": "close_date", '
                            '"proposed_value": "2026-10-15"}'}
    b = V.Packets(owners=[], selected="rowan", packet=None, items=[item])
    html = to_xml(R.render_packets(b, "$"))

    assert 'id="packet-owner"' in html
    assert 'name="packet_owner"' in html
    assert 'value="rowan"' in html
    assert 'hx-include="#sidebar-form, #packet-owner"' in html


def test_packet_export_links_carry_selection_query():
    pkt = SimpleNamespace(item_count=1, header="h", score_delta_line="s",
                          minutes_to_clear=1, crm_block="c")
    b = V.Packets(owners=[], selected="rowan", packet=pkt, items=[],
                  selection_query="?snapshot=2026-08-03&as_of=2026-08-04")
    html = to_xml(R.render_packets(b, "$"))

    assert '/packet/rowan.md?snapshot=2026-08-03&amp;as_of=2026-08-04' in html
    assert '/packet/rowan.html?snapshot=2026-08-03&amp;as_of=2026-08-04' in html
