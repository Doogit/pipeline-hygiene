"""Render a PageModel (src/pipeline_hygiene_view) to FastHTML FT elements.

Presentation only: every user-visible string and value already lives in the view
model; this module just maps typed blocks to HTML with Tailwind classes. Charts
are emitted as a target Div + a non-executable JSON spec block that the vendored
embed.js renders (CSP-safe). No business logic here.
"""
import json
import re
from urllib.parse import urlencode

from fasthtml.common import (NotStr, Div, Span, P, H1, H2, H3, H4, Table, Thead,
                             Tbody, Tr, Th, Td, Details, Summary, Button, Form,
                             Input, Select, Option, Label, A)

from src import pipeline_hygiene_view as V

# Okabe-Ito semantic colors for the :red[]/:orange[]/:blue[] markdown spans.
_MD_COLORS = {"red": "#D55E00", "orange": "#E69F00", "blue": "#0072B2"}
_SEV_TEXT = {"high": "#D55E00", "medium": "#E69F00", "low": "#0072B2"}


def _md(text):
    """Render the small markdown subset the view model emits: :color[..],
    **bold**, `code`. Returns a list of FT nodes."""
    nodes, i = [], 0
    pattern = re.compile(
        r":(red|orange|blue)\[(.+?)\]|\*\*(.+?)\*\*|`([^`]+?)`")
    for m in pattern.finditer(text):
        if m.start() > i:
            nodes.append(text[i:m.start()])
        if m.group(1):
            nodes.append(Span(m.group(2),
                              style=f"color:{_MD_COLORS[m.group(1)]};font-weight:600"))
        elif m.group(3):
            nodes.append(Span(m.group(3), cls="font-semibold"))
        else:
            nodes.append(Span(m.group(4), cls="font-mono text-[0.85em] "
                                              "bg-slate-100 px-1 rounded"))
        i = m.end()
    if i < len(text):
        nodes.append(text[i:])
    return nodes


def _money(v, cur):
    # Matches the retired dashboard's column_config NumberColumn("$%d").
    return "" if v is None else f"{cur}{int(round(v))}"


def _coverage(v):
    return "" if v is None else f"{v:.2f}x"


def _score_bar(v):
    pct = max(0, min(100, int(round(v or 0))))
    return Div(
        Div(cls="score-bar-fill", style=f"width:{pct}%"),
        Span(str(int(round(v or 0))), cls="score-bar-label"),
        cls="score-bar", title=str(v))


def _sparkline(values):
    if not values:
        return ""
    xs = list(range(len(values)))
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1
    w, h = 72, 20
    pts = " ".join(
        f"{(x/(len(values)-1 or 1))*w:.1f},{h-2-((y-lo)/span)*(h-4):.1f}"
        for x, y in zip(xs, values))
    return NotStr(
        f'<svg class="spark" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
        f'preserveAspectRatio="none"><polyline points="{pts}" fill="none" '
        f'stroke="#0072B2" stroke-width="1.5"/></svg>')


_NUMERIC = {"amount", "score", "mean", "median", "pipeline", "quota", "at-risk",
            "gap to cover", "coverage", "open", "owners", "violations", "pushes",
            "days later", "max push", "ever commit", "won", "lost", "pushed",
            "still open", "of which pushed first"}


def _cell(col, val, fmt, cur):
    if fmt.get(col) == V._MONEY:
        return Td(_money(val, cur), cls="num money")
    if fmt.get(col) == V._SCORE:
        return Td(_score_bar(val), cls="num")
    if fmt.get(col) == V._COVER:
        return Td(_coverage(val), cls="num")
    if fmt.get(col) == V._SPARK:
        return Td(_sparkline(val), cls="spark-cell")
    cls = "num" if col in _NUMERIC else ""
    if fmt.get(col) == V._WIDE:
        cls = "wide"
    text = "" if val is None else str(val)
    return Td(text, cls=cls)


def render_table(t, cur):
    head = Tr(*[Th(c, cls="num" if c in _NUMERIC else "") for c in t.columns])
    body = []
    for r in t.rows:
        cells = [_cell(c, r.get(c), t.formats, cur) for c in t.columns]
        attrs = {}
        # Owner scoreboard rows drive the drill-down (replaces Streamlit's
        # on_select rerun): hx-get returns just the drill-down partial, and
        # hx-include carries the active filter set so it stays consistent.
        if t.id == "owner_scoreboard":
            attrs = {"hx_get": f"/drilldown?owner={_q(r.get('owner'))}",
                     "hx_target": "#owner-drilldown", "hx_swap": "outerHTML",
                     "hx_include": "#sidebar-form", "hx_indicator": "#load",
                     "cls": "row-click", "role": "button", "tabindex": "0"}
        body.append(Tr(*cells, **attrs))
    return Div(Table(Thead(head), Tbody(*body), cls="ph-table"),
               cls="table-wrap", id=t.id)


def _q(s):
    from urllib.parse import quote
    return quote(str(s or ""))


def render_chart(c):
    return Div(
        Div(id=c.id, cls="chart"),
        NotStr(f'<script type="application/json" id="{c.id}-spec">'
               f'{json.dumps(c.spec)}</script>'),
        cls="chart-wrap")


def render_metric(m):
    delta_cls = ""
    if m.delta:
        up = m.delta.startswith("+")
        good = (up if m.delta_color != "inverse" else not up)
        delta_cls = "delta-up" if good else "delta-down"
    return Div(
        Div(m.label, cls="metric-label", title=m.help or ""),
        Div(m.value, cls="metric-value"),
        Div(m.delta or " ", cls=f"metric-delta {delta_cls}"),
        cls="metric-card")


def render_block(b, cur):
    if isinstance(b, V.Heading):
        tag = {2: H2, 3: H3}.get(b.level, H4)
        return tag(b.text, cls=f"h{b.level}")
    if isinstance(b, V.Caption):
        return P(*_md(b.text), cls="caption")
    if isinstance(b, V.Markdown):
        return P(*_md(b.text), cls="md")
    if isinstance(b, V.Warning):
        return Div(*_md(b.text), cls="warn")
    if isinstance(b, V.Divider):
        return NotStr("<hr class='divider'>")
    if isinstance(b, V.MetricRow):
        return Div(*[render_metric(m) for m in b.metrics], cls="metric-row")
    if isinstance(b, V.Chart):
        return render_chart(b)
    if isinstance(b, V.Table):
        return render_table(b, cur)
    if isinstance(b, V.Popover):
        # Native Popover API: a button toggles a light-dismiss popover.
        pid = "pop_" + re.sub(r"\W+", "_", b.label).lower()
        return Div(
            Button(b.label, cls="pop-btn",
                   **{"popovertarget": pid}),
            Div(P(*_md(b.body), cls="caption"), id=pid, cls="pop-body",
                popover="auto"),
            cls="pop")
    if isinstance(b, V.Expander):
        return Details(Summary(b.label),
                       Div(*[render_block(x, cur) for x in b.blocks],
                           cls="exp-body"),
                       cls="expander")
    if isinstance(b, V.Drilldown):
        return Div(P(b.prompt, cls="caption"), id="owner-drilldown",
                   cls="drilldown")
    return ""


def render_tab_panels(page, cur):
    panels = []
    for i, tab in enumerate(page.tabs):
        panels.append(Div(*[render_block(b, cur) for b in tab.blocks],
                          cls="tab-panel", id=f"panel-{i}",
                          **{"data-active": "1" if i == 0 else "0",
                             "role": "tabpanel"}))
    return panels


def render_tabs(page, cur):
    buttons = [Button(tab.label, cls="tab-btn",
                      **{"data-tab": str(i), "aria-selected":
                         "true" if i == 0 else "false", "role": "tab"})
               for i, tab in enumerate(page.tabs)]
    return Div(
        Div(*buttons, cls="tab-bar", role="tablist"),
        *render_tab_panels(page, cur),
        cls="tabs")


def _selection_query(sel):
    pairs = []
    for key in ("stage_map", "snapshot", "as_of"):
        if sel and sel.get(key):
            pairs.append((key, sel[key]))
    for key, param in (("owners", "owner"), ("teams", "team"),
                       ("stages", "stage"), ("sev", "sev")):
        for value in (sel or {}).get(key) or []:
            pairs.append((param, value))
    return f"?{urlencode(pairs)}" if pairs else ""


def render_content(page, sel=None):
    """The headline + tabs region (htmx-swappable target)."""
    cur = V._cur(page.controls.get("config") or {})
    top = [render_block(b, cur) for b in page.blocks]
    body = list(top)
    if page.tabs:
        body.append(render_tabs(page, cur))
        body.append(Div(A("Download desk brief (markdown)",
                          href="/download" + _selection_query(sel),
                          cls="download",
                          title="Rendered from the selected snapshot; does not "
                                "record a run."),
                        cls="download-row"))
    return Div(*body, id="content", cls="content", **{"aria_live": "polite"})


# --- sidebar + page shell ---

_HX = {"hx_get": "/content", "hx_target": "#content", "hx_swap": "outerHTML",
       "hx_include": "closest form", "hx_indicator": "#load"}


def _checkbox_list(name, options, selected, searchable=False):
    sel = set(selected or [])
    boxes = [Label(Input(type="checkbox", name=name, value=o,
                         checked=(o in sel)), Span(o), cls="cb")
             for o in options]
    inner = boxes
    if searchable:
        inner = [Input(type="text", cls="cb-filter",
                       placeholder="filter…", **{"data-cbfilter": name}),
                 Div(*boxes, cls="cb-scroll")]
    return Div(*inner, cls="cb-list")


def render_sidebar(page, sel):
    c = page.controls
    stage_maps = c.get("stage_maps") or ["default"]
    dates = [d.isoformat() for d in c.get("snapshot_dates") or []]
    snap = (c.get("snapshot_date").isoformat() if c.get("snapshot_date")
            else (dates[-1] if dates else ""))
    as_of = c.get("as_of").isoformat() if c.get("as_of") else snap

    controls = [
        H2("Data source", cls="h2"),
        Div(Label("Upload a snapshot CSV (validated, evaluated in memory, "
                  "never stored)", cls="lbl"),
            Input(type="file", name="upload", accept=".csv",
                  hx_post="/upload", hx_target="#upload-status",
                  hx_encoding="multipart/form-data", hx_trigger="change",
                  hx_indicator="#load"),
            Div(id="upload-status"), cls="field"),
        Div(Label("Stage vocabulary (stage_map)", cls="lbl"),
            Select(*[Option(s, value=s, selected=(s == sel.get("stage_map",
                                                               "default")))
                     for s in stage_maps],
                   name="stage_map", hx_trigger="change", **_HX), cls="field"),
    ]
    if dates:
        controls.append(Div(
            Label("Snapshot", cls="lbl"),
            Select(*[Option(d, value=d, selected=(d == snap)) for d in dates],
                   name="snapshot", hx_trigger="change", **_HX), cls="field"))
        controls.append(Div(
            Label("Evaluate as of", cls="lbl"),
            Input(type="date", name="as_of", value=as_of,
                  hx_trigger="change", **_HX), cls="field"))
        controls.append(H2("Filters", cls="h2"))
        controls.append(Div(Label("Owner", cls="lbl"),
                            _checkbox_list("owner", c.get("owners") or [],
                                           sel.get("owners"), searchable=True),
                            cls="field"))
        if c.get("teams"):
            controls.append(Div(Label("Team", cls="lbl"),
                                _checkbox_list("team", c["teams"],
                                               sel.get("teams"),
                                               searchable=True), cls="field"))
        controls.append(Div(Label("Stage", cls="lbl"),
                            _checkbox_list("stage", c.get("stages") or [],
                                           sel.get("stages")), cls="field"))
        controls.append(Div(Label("Severity", cls="lbl"),
                            _checkbox_list("sev", ["high", "medium", "low"],
                                           sel.get("sev")), cls="field"))
        controls.append(Button("Apply filters", type="button", cls="apply",
                               hx_trigger="click", **_HX))
    return Form(*controls, cls="sidebar", id="sidebar-form")


def render_page(page, sel):
    cur = V._cur(page.controls.get("config") or {})
    return Div(
        Div(H1("pipeline-hygiene — forecast-call prep", cls="title"),
            Span("", id="load", cls="htmx-indicator loader"), cls="topbar"),
        Div(render_sidebar(page, sel),
            Div(render_content(page, sel), cls="main"),
            cls="layout"),
        cls="app")
