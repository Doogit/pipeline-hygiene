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

    # No hidden shadow inputs anywhere in the panel.
    assert 'type="hidden"' not in html
    # The typed reason input owns the id the confirm button includes.
    assert 'id="wi-reason-7"' in html and 'name="reason"' in html
    assert 'hx-include="#sidebar-form, #wi-reason-7"' in html
    # The edit input owns its id and is seeded with the current value.
    assert 'id="wi-edit-7"' in html and 'name="proposed_value"' in html
    assert 'value="2026-10-15"' in html
    assert 'hx-include="#sidebar-form, #wi-edit-7"' in html
