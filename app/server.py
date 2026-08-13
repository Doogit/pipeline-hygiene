"""FastHTML dashboard: python -m app.server (binds 127.0.0.1).

Offline-first, zero-CDN: htmx, vega/vega-lite/vega-embed, the CSP-safe AST
interpreter, and the Tailwind stylesheet are all served locally from
app/static (versions + SHA-256 in app/static/vendor/VENDOR.md). A strict CSP
(`default-src 'self'; style-src 'self' 'unsafe-inline'; form-action 'self';
connect-src 'self'`) is sent from the app.

Renders strictly from the pure view model (src/pipeline_hygiene_view); no
business logic here.

Read-only boundary (refined, Monday Packet PR E): the server is READ-ONLY
AGAINST SOURCE SYSTEMS — it never writes to `opportunities`, snapshots, or any
ingested source data, and viewing/downloading records nothing. The ONE writable
surface is the packet ledger (`work_items` / `work_item_events`), and only via
the /work-item/* mutation routes, all gated behind PIPELINE_HYGIENE_PACKETS
(absent -> those routes 404 and no WorkItemStore is ever opened, so the app is
byte-identical to the packets-disabled build).

Auth posture (accepted risk, threat-modelled): single-operator, localhost-only.
The app binds 127.0.0.1 explicitly; there is no login, session, or role check.
The `by` actor recorded on every work_item_event is an operator identity read
from PIPELINE_HYGIENE_OPERATOR (default "operator"). This is acceptable ONLY
because the surface is one trusted operator on loopback. As a defence-in-depth
control the mutation routes still enforce a same-origin guard (below) so a
cross-site browser request cannot forge a write.

This is the sole UI: the prior Streamlit page (app/dashboard.py) was retired
once parity + manual acceptance were signed off (migration Task 5); the parity
gate (tests/parity) remains as the regression baseline against its frozen
goldens (it runs with packets OFF).
"""
import json
import os
import secrets
import tempfile
import time
from datetime import date
from pathlib import Path

from fasthtml.common import Details, Div, FastHTML, Link, Script, Summary, Title
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from starlette.staticfiles import StaticFiles

from src import brief, packet, pipeline_hygiene_view as V
from src.extract import FIELD_TYPES, _coerce as _coerce_field
from src.ingest import AllRowsRejectedError, IngestError, load_config, \
    snapshot_date_from_filename, validate_csv
from src.work_items import WorkItemStore, normalize_owner, packets_enabled
from app import render as R

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
MAX_UPLOAD_BYTES = int(os.environ.get("PIPELINE_HYGIENE_UPLOAD_LIMIT_BYTES",
                                      str(5 * 1024 * 1024)))

# In-memory upload sessions: a validated CSV is rendered as a full page (store
# is None) and its parsed rows are held here — keyed by an opaque token — only
# so the drill-down and .md download can operate on the uploaded snapshot too.
# Nothing is written to disk or the snapshot store; sessions are transient
# (bounded count, idle TTL) and vanish on process exit or reload.
_UPLOAD_SESSIONS = {}
_UPLOAD_TTL_SECONDS = int(os.environ.get("PIPELINE_HYGIENE_UPLOAD_TTL", "1800"))
_UPLOAD_MAX_SESSIONS = 8


def _prune_sessions(now):
    for tok in [t for t, s in _UPLOAD_SESSIONS.items()
                if now - s["ts"] > _UPLOAD_TTL_SECONDS]:
        _UPLOAD_SESSIONS.pop(tok, None)
    while len(_UPLOAD_SESSIONS) >= _UPLOAD_MAX_SESSIONS:
        oldest = min(_UPLOAD_SESSIONS, key=lambda t: _UPLOAD_SESSIONS[t]["ts"])
        _UPLOAD_SESSIONS.pop(oldest, None)


def _paths():
    # Read per-request so tests (and redeploys) can repoint the store via env.
    return (os.environ.get("PIPELINE_HYGIENE_CONFIG", "config.yaml"),
            os.environ.get("PIPELINE_HYGIENE_DB", "data/pipeline.db"),
            os.environ.get("PIPELINE_HYGIENE_QUOTAS", "data/seed_manifest.json"))

_HDRS = (
    Link(rel="stylesheet", href="/static/app.css"),
    Script(src="/static/vendor/htmx.min.js"),
    Script(src="/static/vendor/vega.min.js"),
    Script(src="/static/vendor/vega-lite.min.js"),
    Script(src="/static/vendor/vega-interpreter.bundle.js"),
    Script(src="/static/vendor/vega-embed.min.js"),
    Script(src="/static/vendor/embed.js"),
    Script(src="/static/vendor/ui.js"),
)

# Zero-CDN: default_hdrs=False drops FastHTML's CDN htmx/Pico; htmx=False so we
# serve our own vendored htmx.
app = FastHTML(default_hdrs=False, htmx=False, hdrs=_HDRS)
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


class CSPMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        resp = await call_next(request)
        # script-src stays strict 'self' (no inline JS / no unsafe-eval — the
        # bundled AST interpreter avoids eval); Vega applies inline element
        # styles, so style-src allows inline.
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "form-action 'self'; connect-src 'self'")
        return resp


app.add_middleware(CSPMiddleware)


def _selections(request, form=None):
    q = request.query_params
    def scalar(key, default=None):
        if form is not None:
            val = form.get(key)
            if val is not None:
                return val
        return q.get(key, default)

    def multi(key):
        if form is not None:
            return form.getlist(key)
        return q.getlist(key)

    return {
        "stage_map": scalar("stage_map", "default"),
        "snapshot": scalar("snapshot"),
        "as_of": scalar("as_of"),
        "owners": multi("owner"),
        "teams": multi("team"),
        "stages": multi("stage"),
        "sev": multi("sev"),
        "upload_token": scalar("upload_token"),
        "packet_owner": scalar("packet_owner"),
    }


def _page(sel):
    config_path, db_path, quotas_path = _paths()
    snap = date.fromisoformat(sel["snapshot"]) if sel.get("snapshot") else None
    as_of = date.fromisoformat(sel["as_of"]) if sel.get("as_of") else None
    return V.build_from_store(
        config_path, db_path, quotas_path, snapshot_date=snap, as_of=as_of,
        f_owners=sel["owners"], f_teams=sel["teams"], f_stages=sel["stages"],
        f_sev=sel["sev"], packet_owner=sel.get("packet_owner"))


def _upload_page(token, sel):
    """Rebuild the PageModel for an uploaded snapshot from its held rows (store
    is None), applying the active filters. Returns None if the token is unknown
    or expired (caller falls back to the stored view)."""
    s = _UPLOAD_SESSIONS.get(token)
    if s is None:
        return None
    now = time.monotonic()
    if now - s["ts"] > _UPLOAD_TTL_SECONDS:
        _UPLOAD_SESSIONS.pop(token, None)
        return None
    s["ts"] = now  # refresh idle TTL on use
    config, rows = s["config"], s["rows"]
    controls = {
        "config": config,
        "data": s["data"],              # byte-identical brief.render download
        "rows": rows,                   # owner drill-down
        "snapshot_dates": [],           # single uploaded snapshot; no selector
        "snapshot_date": s["snapshot_date"],
        "as_of": s["as_of"],
        "upload_token": token,
        "stage_maps": sorted(config.get("stage_map", {})),
        "owners": sorted({r["owner"] for r in rows if V.is_open(r)}),
        "stages": sorted({r["stage"] for r in rows if V.is_open(r)}),
        "teams": sorted({(m or {}).get("team")
                         for m in (config.get("owner_meta") or {}).values()}
                        - {None}),
    }
    return V.build_page_model(
        store=None, config=config, snapshot_date=s["snapshot_date"],
        as_of=s["as_of"], rows=rows, data=s["data"], prev_summary=None,
        prev_opens=[], outcomes=None, validation=s["validation"],
        f_owners=sel["owners"], f_teams=sel["teams"], f_stages=sel["stages"],
        f_sev=sel["sev"], controls=controls)


def _page_for(sel):
    """Route a request to the uploaded-snapshot view when a valid upload_token
    is present, otherwise the stored view."""
    token = sel.get("upload_token")
    if token:
        page = _upload_page(token, sel)
        if page is not None:
            return page
    return _page(sel)


def _upload_status(message, *, warning=False, report=None):
    parts = [Div(message, cls="warn" if warning else "caption")]
    if report is not None and report.row_reasons:
        rows = [dict(zip(("row", "opp_id", "reason"), r))
                for r in report.row_reasons[:10]]
        parts.append(Details(
            Summary(f"Rejected rows ({report.rejected})"),
            R.render_table(V.Table("upload_rejects",
                                   ["row", "opp_id", "reason"], rows), "$"),
            cls="expander"))
    return Div(*parts, id="upload-status")


# --- Monday Packet (PR E): operator identity + write-route defences ---


def _operator():
    """The single-operator actor recorded on every work_item_event `by` field
    (accepted-risk auth model; see module docstring)."""
    return os.environ.get("PIPELINE_HYGIENE_OPERATOR", "operator")


def _same_origin_ok(request):
    """Defence-in-depth CSRF guard for mutation routes. Reject cross-site
    browser requests. Modern browsers send Sec-Fetch-Site; when absent (e.g. a
    server-side TestClient) fall back to an Origin/base-URL match, and allow the
    no-Origin/no-Sec-Fetch-Site case (server-side clients)."""
    site = request.headers.get("sec-fetch-site")
    if site is not None:
        return site in ("same-origin", "none")
    origin = request.headers.get("origin")
    if origin is None:
        return True
    base = str(request.base_url).rstrip("/")
    return origin.rstrip("/") == base


def _packets_panel(request, item_id=None, *, form=None, error_for=None):
    """Rebuild the current page for the active selection and return JUST the
    #packets-panel div for an htmx outerHTML swap after a mutation."""
    sel = _selections(request, form=form)
    page = _page(sel)
    cur = V._cur(page.controls.get("config") or {})
    for tab in page.tabs:
        for b in tab.blocks:
            if isinstance(b, V.Packets):
                return R.render_packets(b, cur, error_for=error_for)
    # No Packets block (e.g. no selection) — return an empty stable container.
    return R.Div(id="packets-panel", cls="packets-panel")


@app.get("/")
def index(request):
    sel = _selections(request)
    page = _page(sel)
    return Title("pipeline-hygiene"), R.render_page(page, sel)


@app.get("/healthz")
def healthz():
    return Response("ok", media_type="text/plain")


@app.get("/content")
def content(request):
    sel = _selections(request)
    return R.render_content(_page_for(sel), sel)


@app.get("/drilldown")
def drilldown(request):
    sel = _selections(request)
    owner = request.query_params.get("owner")
    page = _page_for(sel)
    data = page.controls.get("data")
    if not owner or data is None:
        return R.render_block(V.Drilldown("Select an owner row to drill into "
                                          "their open flagged opps."), "$")
    rows_by_id = {r["opp_id"]: r for r in page.controls.get("rows", [])}
    blocks = V.owner_drilldown_blocks(owner, data, page.controls["config"],
                                      rows_by_id)
    cur = V._cur(page.controls["config"])
    from fasthtml.common import Div
    return Div(*[R.render_block(b, cur) for b in blocks],
               id="owner-drilldown", cls="drilldown")


@app.get("/download")
def download(request):
    sel = _selections(request)
    page = _page_for(sel)
    data = page.controls.get("data")
    if data is None:
        return Response("No snapshot loaded.", status_code=404)
    config = page.controls["config"]
    as_of = page.controls.get("as_of") or date.today()
    md = brief.render(data, config)
    return Response(
        md, media_type="text/markdown",
        headers={"Content-Disposition":
                 f'attachment; filename="desk_brief_{as_of.isoformat()}.md"'})


@app.post("/upload")
async def upload(request):
    form = await request.form()
    uploaded = form.get("upload")
    filename = Path(getattr(uploaded, "filename", "") or "").name
    if uploaded is None or not filename:
        return _upload_status("Choose a CSV file to validate.", warning=True)
    if not filename.lower().endswith(".csv"):
        return _upload_status("Upload rejected: choose a .csv file.",
                              warning=True)

    snapshot_date = snapshot_date_from_filename(filename)
    if snapshot_date is None:
        return _upload_status(
            "No YYYY-MM-DD snapshot date found in the uploaded filename; "
            "rename the file like opps_2026-08-10.csv.",
            warning=True)

    payload = await uploaded.read(MAX_UPLOAD_BYTES + 1)
    await uploaded.close()
    if len(payload) > MAX_UPLOAD_BYTES:
        return _upload_status(
            f"Upload rejected: file exceeds the {MAX_UPLOAD_BYTES:,} byte "
            "limit.", warning=True)

    config_path, _, quotas_path = _paths()
    config = load_config(config_path)
    if quotas_path and Path(quotas_path).exists():
        import json
        with open(quotas_path, encoding="utf-8") as f:
            config = brief.merge_quota_payload(config, json.load(f))
    stage_map = str(form.get("stage_map") or "default")

    with tempfile.TemporaryDirectory(prefix="pipeline_hygiene_upload_") as tmp:
        tmp_path = Path(tmp) / filename
        tmp_path.write_bytes(payload)
        try:
            rows, report = validate_csv(tmp_path, config, stage_map)
            if report.total_rows > 0 and not rows:
                raise AllRowsRejectedError(snapshot_date, report)
        except AllRowsRejectedError as exc:
            return _upload_status(f"Upload rejected: {exc}", warning=True,
                                  report=exc.report)
        except IngestError as exc:
            return _upload_status(f"Upload rejected: {exc}", warning=True)

    # Render the uploaded rows as a full page (store is None → the multi-snapshot
    # sections degrade to their empty-state notes) and hold the parsed rows in a
    # transient session so the drill-down and .md download work on it too. The
    # status swaps #upload-status; #content and the sidebar filters swap out of
    # band so the stored view is replaced without a reload.
    validation = dict(report.to_dict(), source_file=filename)
    data = brief.build_from_rows(
        rows, snapshot_date, snapshot_date, config, validation=validation,
        prev_summary=None, outcomes=None, prev_opens=[], patterns=None,
        ledger=None)
    now = time.monotonic()
    _prune_sessions(now)
    token = secrets.token_urlsafe(16)
    _UPLOAD_SESSIONS[token] = {
        "rows": rows, "data": data, "config": config,
        "snapshot_date": snapshot_date, "as_of": snapshot_date,
        "validation": validation, "ts": now}

    sel = {"stage_map": stage_map, "snapshot": None, "as_of": None,
           "owners": [], "teams": [], "stages": [], "sev": [],
           "upload_token": token}
    page = _upload_page(token, sel)
    status = _upload_status(
        f"Uploaded snapshot {snapshot_date.isoformat()} rendered below — single "
        f"snapshot, so H3/H6 may report insufficient history. Accepted "
        f"{report.accepted}/{report.total_rows} rows, rejected "
        f"{report.rejected}.",
        report=report if report.rejected else None)
    return (status,
            R.render_content(page, sel, oob=True),
            R._sidebar_dynamic(page, sel, oob=True))


# --- Monday Packet mutation + export routes (PR E) -------------------------
# All gated behind PIPELINE_HYGIENE_PACKETS (404 when off) and the same-origin
# guard (403 cross-site). Writes go ONLY to the work_items ledger, never to
# source data. `at=date.today()` here is correct: this is a request-entry point
# stamping the actor/event time, not an evaluation function (SPEC's as_of ban is
# for time-EVALUATING code, not mutation-event boundaries).


def _mutation_guard(request):
    if not packets_enabled():
        return Response(status_code=404)
    if not _same_origin_ok(request):
        return Response("cross-site request refused", status_code=403)
    return None


def _item_or_response(wi, item_id):
    item = next((it for it in wi.items() if it["id"] == item_id), None)
    if item is None:
        return None, Response(status_code=404)
    return item, None


def _require_status(item, allowed):
    if item["status"] not in allowed:
        allowed_text = ", ".join(allowed)
        return Response(f"item status must be one of: {allowed_text}",
                        status_code=409)
    return None


@app.post("/work-item/{item_id:int}/accept")
async def wi_accept(request, item_id: int):
    guard = _mutation_guard(request)
    if guard is not None:
        return guard
    form = await request.form()
    config_path, db_path, _ = _paths()
    config = load_config(config_path)
    wi = WorkItemStore(db_path, config)
    try:
        item, response = _item_or_response(wi, item_id)
        if response is not None:
            return response
        response = _require_status(item, ("proposed", "edited"))
        if response is not None:
            return response
        wi.accept(item_id, at=date.today(), by=_operator())
    finally:
        wi.close()
    return _packets_panel(request, item_id, form=form)


@app.post("/work-item/{item_id:int}/dismiss")
async def wi_dismiss(request, item_id: int):
    guard = _mutation_guard(request)
    if guard is not None:
        return guard
    form = await request.form()
    reason = (form.get("reason") or "").strip()
    if not reason:
        return Response("dismiss requires a reason", status_code=400)
    config_path, db_path, _ = _paths()
    config = load_config(config_path)
    wi = WorkItemStore(db_path, config)
    try:
        item, response = _item_or_response(wi, item_id)
        if response is not None:
            return response
        response = _require_status(item, ("proposed", "accepted", "edited"))
        if response is not None:
            return response
        wi.dismiss(item_id, reason=reason, at=date.today(), by=_operator())
    finally:
        wi.close()
    return _packets_panel(request, item_id, form=form)


@app.post("/work-item/{item_id:int}/edit")
async def wi_edit(request, item_id: int):
    guard = _mutation_guard(request)
    if guard is not None:
        return guard
    form = await request.form()
    raw_value = form.get("proposed_value")
    config_path, db_path, _ = _paths()
    config = load_config(config_path)
    wi = WorkItemStore(db_path, config)
    try:
        item, response = _item_or_response(wi, item_id)
        if response is not None:
            return response
        response = _require_status(item, ("proposed", "edited"))
        if response is not None:
            return response
        if item["item_type"] != "field_update":
            return Response("only field_update items can be edited",
                            status_code=409)
        payload = json.loads(item["payload_json"])
        field_name = payload.get("field")
        kind = FIELD_TYPES.get(field_name)
        # Re-run the new value through the field's TYPE check before saving; on
        # failure return the panel unchanged with an inline error (200).
        try:
            coerced = _coerce_field(raw_value, kind) if kind else None
            if kind is None:
                raise ValueError("not a typed field")
        except (ValueError, TypeError):
            return _packets_panel(request, item_id, form=form,
                                  error_for=item_id)
        new_payload = dict(payload, proposed_value=coerced)
        wi.edit(item_id, new_payload, at=date.today(), by=_operator())
    finally:
        wi.close()
    return _packets_panel(request, item_id, form=form)


@app.post("/work-item/{item_id:int}/reopen")
async def wi_reopen(request, item_id: int):
    guard = _mutation_guard(request)
    if guard is not None:
        return guard
    form = await request.form()
    config_path, db_path, _ = _paths()
    config = load_config(config_path)
    wi = WorkItemStore(db_path, config)
    try:
        item, response = _item_or_response(wi, item_id)
        if response is not None:
            return response
        # Reversible while pre-export: dismissed -> proposed. expired items are
        # NOT reopenable (terminal resolution in source).
        response = _require_status(item, ("dismissed",))
        if response is not None:
            return response
        wi.transition(item_id, "proposed", at=date.today(), by=_operator(),
                      reason="reopened")
    finally:
        wi.close()
    return _packets_panel(request, item_id, form=form)


@app.get("/packet/{owner}.md")
def packet_md(request, owner: str):
    if not packets_enabled():
        return Response(status_code=404)
    return _packet_export(request, owner, "md")


@app.get("/packet/{owner}.html")
def packet_html(request, owner: str):
    if not packets_enabled():
        return Response(status_code=404)
    return _packet_export(request, owner, "html")


def _packet_export(request, owner, fmt):
    sel = _selections(request)
    config_path, db_path, _ = _paths()
    config = load_config(config_path)
    snap = date.fromisoformat(sel["snapshot"]) if sel.get("snapshot") else None
    as_of = date.fromisoformat(sel["as_of"]) if sel.get("as_of") else None
    from src.snapshots import SnapshotStore
    store = None
    if Path(db_path).exists():
        store = SnapshotStore(db_path, config)
        dates = store.snapshot_dates()
        if dates:
            snap = snap or dates[-1]
            from src.funnel import resolve_aging_config
            config = resolve_aging_config(store, config, snap)
            store.config = config
    as_of = as_of or snap
    wi = WorkItemStore(db_path, config)
    try:
        model = packet.build_owner_packet(
            wi, normalize_owner(owner), config, as_of,
            snapshot_store=store, snapshot_date=snap)
    finally:
        wi.close()
        if store is not None:
            store.close()
    if fmt == "md":
        body = packet.render_markdown(model, config)
        media, ext = "text/markdown", "md"
    else:
        body = packet.render_html(model, config)
        media, ext = "text/html", "html"
    return Response(
        body, media_type=media,
        headers={"Content-Disposition":
                 f'attachment; filename="packet_{normalize_owner(owner)}.{ext}"'})


if __name__ == "__main__":
    import uvicorn
    # Explicit localhost bind (read-only, offline posture).
    host = os.environ.get("PIPELINE_HYGIENE_HOST", "127.0.0.1")
    local_hosts = {"127.0.0.1", "localhost", "::1"}
    if host not in local_hosts and \
            os.environ.get("PIPELINE_HYGIENE_ALLOW_NONLOCAL_HOST") != "1":
        raise SystemExit("Refusing non-local host bind without "
                         "PIPELINE_HYGIENE_ALLOW_NONLOCAL_HOST=1")
    port = int(os.environ.get("PIPELINE_HYGIENE_PORT", "5100"))
    uvicorn.run(app, host=host, port=port)
