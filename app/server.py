"""Read-only FastHTML dashboard: python -m app.server (binds 127.0.0.1).

Offline-first, zero-CDN: htmx, vega/vega-lite/vega-embed, the CSP-safe AST
interpreter, and the Tailwind stylesheet are all served locally from
app/static (versions + SHA-256 in app/static/vendor/VENDOR.md). A strict CSP
(`default-src 'self'; style-src 'self' 'unsafe-inline'`) is sent from the app.

Renders strictly from the pure view model (src/pipeline_hygiene_view); no
business logic here. The page is read-only — nothing writes outside the
snapshot store, and viewing/downloading records nothing. The Streamlit page
(app/dashboard.py) stays live until parity + manual acceptance are signed off.
"""
import os
from datetime import date
from pathlib import Path

from fasthtml.common import FastHTML, Link, Script, Title
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from starlette.staticfiles import StaticFiles

from src import brief, pipeline_hygiene_view as V
from app import render as R

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"

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
            "default-src 'self'; style-src 'self' 'unsafe-inline'")
        return resp


app.add_middleware(CSPMiddleware)


def _selections(request):
    q = request.query_params
    return {
        "stage_map": q.get("stage_map", "default"),
        "snapshot": q.get("snapshot"),
        "as_of": q.get("as_of"),
        "owners": q.getlist("owner"),
        "teams": q.getlist("team"),
        "stages": q.getlist("stage"),
        "sev": q.getlist("sev"),
    }


def _page(sel):
    config_path, db_path, quotas_path = _paths()
    snap = date.fromisoformat(sel["snapshot"]) if sel.get("snapshot") else None
    as_of = date.fromisoformat(sel["as_of"]) if sel.get("as_of") else None
    return V.build_from_store(
        config_path, db_path, quotas_path, snapshot_date=snap, as_of=as_of,
        f_owners=sel["owners"], f_teams=sel["teams"], f_stages=sel["stages"],
        f_sev=sel["sev"])


@app.get("/")
def index(request):
    sel = _selections(request)
    page = _page(sel)
    return Title("pipeline-hygiene"), R.render_page(page, sel)


@app.get("/content")
def content(request):
    sel = _selections(request)
    return R.render_content(_page(sel))


@app.get("/drilldown")
def drilldown(request):
    sel = _selections(request)
    owner = request.query_params.get("owner")
    page = _page(sel)
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
    page = _page(sel)
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


if __name__ == "__main__":
    import uvicorn
    # Explicit localhost bind (read-only, offline posture); no non-local bind.
    host = os.environ.get("PIPELINE_HYGIENE_HOST", "127.0.0.1")
    port = int(os.environ.get("PIPELINE_HYGIENE_PORT", "5100"))
    uvicorn.run(app, host=host, port=port)
