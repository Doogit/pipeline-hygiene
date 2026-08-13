"""Offline smoke tests for the FastHTML server (app/server.py) via Starlette's
TestClient — no browser, deterministic in CI.

Verify the read-only/offline posture is enforced (CSP header, no external
origins), the page renders from the view model (metrics/tabs/charts/tables),
the owner drill-down partial responds, and the .md download is byte-identical to
the shared brief.render (plan §1, §9, §12). The Task 0 spike already proved the
charts actually draw offline in a real browser.
"""
import re
from datetime import date
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app import server as server_module
from src import brief, pipeline_hygiene_view as V
from app.server import app
from tests.parity._build import build_empty, build_full_multi

client = TestClient(app)


def test_healthz_is_lightweight():
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.text == "ok"


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
    assert 'hx-swap="outerHTML"' in html


def test_content_partial(full_env):
    r = client.get("/content", params={"snapshot": "2026-08-10",
                                       "as_of": "2026-08-09"})
    assert r.status_code == 200
    assert "Risky commits" in r.text and "Owner scoreboard" in r.text
    assert r.text.count('id="content"') == 1
    assert "href=\"/download?stage_map=default&amp;snapshot=2026-08-10" \
        in r.text
    assert "as_of=2026-08-09" in r.text


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


def test_owner_drilldown_explainability_panel(full_env):
    # an owner with flagged opps renders the per-flag explanation panel,
    # pairing each rule with its trip line and observed value.
    import src.brief as brief
    from html import escape
    from src.rules import is_open
    page = V.build_from_store(full_env["PIPELINE_HYGIENE_CONFIG"],
                              full_env["PIPELINE_HYGIENE_DB"],
                              full_env["PIPELINE_HYGIENE_QUOTAS"])
    data = page.controls["data"]
    rows_by_id = {r["opp_id"]: r for r in page.controls["rows"]}
    owner = next(rows_by_id[r.opp_id]["owner"]
                 for r in data["results"].values()
                 if r.violations and is_open(rows_by_id[r.opp_id]))
    exp = brief.flag_explanations(owner, data["results"], rows_by_id,
                                  page.controls["config"])
    r = client.get("/drilldown", params={"owner": owner})
    assert r.status_code == 200
    assert "Why flagged — rule, threshold, and observed value per flag" in r.text
    # a real trip line and its rule label are both surfaced
    assert escape(exp[0]["trips when"], quote=False) in r.text
    assert exp[0]["flag"] in r.text


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


def _post_upload(tmp_path, name="opps_2026-08-10.csv"):
    csv_path = Path(tmp_path) / name
    with csv_path.open("rb") as f:
        return client.post("/upload", data={"stage_map": "default"},
                           files={"upload": (name, f, "text/csv")})


def _upload_token(response):
    m = re.search(r'name="upload_token"\s+value="([\w-]+)"', response.text)
    assert m, "upload response must carry an upload_token"
    return m.group(1)


def test_upload_renders_uploaded_snapshot(tmp_path, monkeypatch):
    env = build_full_multi(tmp_path)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    r = _post_upload(tmp_path)
    assert r.status_code == 200
    # Status caption swaps #upload-status; the page renders below.
    assert "id=\"upload-status\"" in r.text
    assert "Uploaded snapshot 2026-08-10 rendered below" in r.text
    assert "does not yet re-render" not in r.text
    # #content and the sidebar filters come back as out-of-band swaps.
    assert 'hx-swap-oob="true"' in r.text
    assert "metric-card" in r.text and "tab-btn" in r.text
    assert 'id="sidebar-dynamic"' in r.text
    assert re.search(r'name="upload_token"\s+value="[\w-]+"', r.text)
    assert "Return to the stored view" in r.text


def test_uploaded_snapshot_drilldown_and_download_use_token(tmp_path,
                                                            monkeypatch):
    import json

    from src.ingest import load_config, validate_csv
    env = build_full_multi(tmp_path)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    r = _post_upload(tmp_path)
    token = _upload_token(r)

    # Rebuild the expected uploaded view independently for parity checks.
    config = load_config(env["PIPELINE_HYGIENE_CONFIG"])
    with open(env["PIPELINE_HYGIENE_QUOTAS"], encoding="utf-8") as f:
        config = brief.merge_quota_payload(config, json.load(f))
    csv_path = Path(tmp_path) / "opps_2026-08-10.csv"
    rows, report = validate_csv(csv_path, config, "default")
    validation = dict(report.to_dict(), source_file="opps_2026-08-10.csv")
    data = brief.build_from_rows(
        rows, date(2026, 8, 10), date(2026, 8, 10), config,
        validation=validation, prev_summary=None, outcomes=None,
        prev_opens=[], patterns=None, ledger=None)

    # /download with the token renders the UPLOADED snapshot, byte-identical to
    # the shared brief.render over the uploaded rows (not the stored snapshot).
    d = client.get("/download", params={"upload_token": token})
    assert d.status_code == 200
    assert d.text == brief.render(data, config)

    # /content with the token stays in the uploaded view.
    c = client.get("/content", params={"upload_token": token})
    assert c.status_code == 200 and "metric-card" in c.text

    # /drilldown with the token resolves owners from the uploaded rows.
    owner = next(iter(data["owners"].values())).owner
    dd = client.get("/drilldown", params={"owner": owner,
                                          "upload_token": token})
    assert dd.status_code == 200 and "owner-drilldown" in dd.text

    # An unknown token silently falls back to the stored view (no crash).
    fb = client.get("/content", params={"upload_token": "nope"})
    assert fb.status_code == 200 and "metric-card" in fb.text


def test_upload_sessions_expire_and_remain_bounded(tmp_path, monkeypatch):
    env = build_full_multi(tmp_path)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(server_module, "_UPLOAD_TTL_SECONDS", 10)
    monkeypatch.setattr(server_module, "_UPLOAD_MAX_SESSIONS", 2)
    server_module._UPLOAD_SESSIONS.clear()

    r1 = _post_upload(tmp_path)
    t1 = _upload_token(r1)
    server_module._UPLOAD_SESSIONS[t1]["ts"] -= 11
    assert client.get("/content", params={"upload_token": t1}).status_code == 200
    assert t1 not in server_module._UPLOAD_SESSIONS

    t2 = _upload_token(_post_upload(tmp_path))
    t3 = _upload_token(_post_upload(tmp_path))
    t4 = _upload_token(_post_upload(tmp_path))
    assert set(server_module._UPLOAD_SESSIONS) == {t3, t4}
    assert t2 not in server_module._UPLOAD_SESSIONS


def test_upload_rejects_filename_without_snapshot_date(full_env):
    r = client.post("/upload",
                    data={"stage_map": "default"},
                    files={"upload": ("opps.csv", b"opp_id\n", "text/csv")})
    assert r.status_code == 200
    assert "id=\"upload-status\"" in r.text
    assert "No YYYY-MM-DD snapshot date found" in r.text
