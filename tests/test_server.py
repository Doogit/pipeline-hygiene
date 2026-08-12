"""Offline smoke tests for the FastHTML server (app/server.py) via Starlette's
TestClient — no browser, deterministic in CI.

Verify the read-only/offline posture is enforced (CSP header, no external
origins), the page renders from the view model (metrics/tabs/charts/tables),
the owner drill-down partial responds, and the .md download is byte-identical to
the shared brief.render (plan §1, §9, §12). The Task 0 spike already proved the
charts actually draw offline in a real browser.
"""
import re

import pytest
from starlette.testclient import TestClient

from src import brief, pipeline_hygiene_view as V
from app.server import app
from tests.parity._build import build_empty, build_full_multi

client = TestClient(app)


@pytest.fixture
def full_env(tmp_path, monkeypatch):
    env = build_full_multi(tmp_path)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return env


def _external_origins(html):
    urls = re.findall(r"https?://[a-z0-9.\-]+", html, re.I)
    # testserver = TestClient's own host (127.0.0.1 in production); vega.github.io
    # = the inert Vega-Lite $schema identifier, never dereferenced (Task 0 §4).
    return sorted({u for u in urls
                  if not re.search(r"127\.0\.0\.1|localhost|testserver|"
                                   r"vega\.github\.io", u)})


def test_index_renders_offline_read_only(full_env):
    r = client.get("/")
    assert r.status_code == 200
    # CSP: strict scripts (no unsafe-eval), inline styles allowed for Vega.
    assert r.headers["content-security-policy"] == \
        "default-src 'self'; style-src 'self' 'unsafe-inline'"
    html = r.text
    assert "metric-card" in html and "tab-btn" in html
    # 8 chart spec blocks + only-local asset references
    assert html.count('type="application/json" id="') == 8
    assert _external_origins(html) == [], "page must make no external requests"
    assert "/static/vendor/htmx.min.js" in html
    assert "Download desk brief (markdown)" in html


def test_content_partial(full_env):
    r = client.get("/content", params={"snapshot": "2026-08-10"})
    assert r.status_code == 200
    assert "Risky commits" in r.text and "Owner scoreboard" in r.text


def test_owner_drilldown_partial(full_env):
    # discover an owner from the scoreboard, then drill in
    page = V.build_from_store(full_env["PIPELINE_HYGIENE_CONFIG"],
                              full_env["PIPELINE_HYGIENE_DB"],
                              full_env["PIPELINE_HYGIENE_QUOTAS"])
    owner = next(iter(page.controls["data"]["owners"].values())).owner
    r = client.get("/drilldown", params={"owner": owner})
    assert r.status_code == 200
    assert "open flagged opps" in r.text
    assert "owner-drilldown" in r.text


def test_download_is_byte_identical_to_brief_render(full_env):
    r = client.get("/download")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    assert "attachment; filename=" in r.headers["content-disposition"]
    page = V.build_from_store(full_env["PIPELINE_HYGIENE_CONFIG"],
                              full_env["PIPELINE_HYGIENE_DB"],
                              full_env["PIPELINE_HYGIENE_QUOTAS"])
    expected = brief.render(page.controls["data"], page.controls["config"])
    assert r.text == expected


def test_empty_store_shows_warning(tmp_path, monkeypatch):
    env = build_empty(tmp_path)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    r = client.get("/")
    assert r.status_code == 200
    assert "Store is empty; ingest a snapshot or upload a CSV." in r.text
    # stopped page: no tabs
    assert "tab-btn" not in r.text
