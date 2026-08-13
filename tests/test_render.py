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
