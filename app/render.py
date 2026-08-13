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
    s = v or 0
    pct = max(0, min(100, int(round(s))))
    # Color tier by score (presentation only; mirrors the healthy>=80 threshold).
    tier = "good" if s >= 80 else "warn" if s >= 60 else "bad"
    return Div(
        Div(cls="score-bar-fill", style=f"width:{pct}%"),
        Span(str(int(round(s))), cls="score-bar-label"),
        cls=f"score-bar {tier}", title=str(v))


def _sparkline(values):
    if not values:
        return ""
    xs = list(range(len(values)))
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1
    w, h = 72, 20
    coords = [((x/(len(values)-1 or 1))*w, h-2-((y-lo)/span)*(h-4))
              for x, y in zip(xs, values)]
    pts = " ".join(f"{px:.1f},{py:.1f}" for px, py in coords)
    ex, ey = coords[-1]
    return NotStr(
        f'<svg class="spark" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
        f'preserveAspectRatio="none"><polyline points="{pts}" fill="none" '
        f'stroke="#0072B2" stroke-width="1.5"/>'
        f'<circle class="spark-end" cx="{ex:.1f}" cy="{ey:.1f}" r="1.8"/></svg>')


# Rule -> severity tier fallback. H1/H3/H10 are context-sensitive in rules.py,
# so _rule_severity handles them from row context where the table carries it.
_CANON_SEV = {"H2": "high", "H4": "high",
              "H5": "high", "H6": "medium", "H7": "medium", "H8": "low",
              "H9": "low", "H11": "high"}
_SEV_RANK = {"high": 0, "medium": 1, "low": 2}
_SEV_CLS = {"high": "high", "medium": "med", "low": "low"}

# Table id -> (column holding codes, are they H rule-codes?). Non-rule columns
# hold coverage flags (low_coverage, small_n) and get neutral chips.
_CHIP_COL = {"risky_commits": ("flags", True), "slippage": ("rules", True),
             "exceptions": ("rules", True), "owner_drilldown": ("rules", True),
             "owner_scoreboard": ("flags", False), "teams": ("flags", False),
             "regions": ("flags", False)}


def _codes(text):
    return [p for p in re.split(r"[ ,]+", str(text or "").strip())
            if p and p != "-"]


def _rule_severity(code, row):
    stage = str((row or {}).get("stage") or "")
    if code == "H1":
        return "high" if stage in ("commit", "propose") else "medium"
    if code == "H3":
        try:
            pushes = int((row or {}).get("pushes") or 0)
        except (TypeError, ValueError):
            pushes = 0
        return "high" if stage == "commit" or pushes >= 3 else "medium"
    if code == "H10":
        forecast = str((row or {}).get("forecast") or "")
        return "medium" if forecast in ("commit", "best_case") else "low"
    return _CANON_SEV.get(code)


def _worst_sev(codes, row=None):
    ranks = [_SEV_RANK[sev] for c in codes
             for sev in [_rule_severity(c, row)] if sev in _SEV_RANK]
    return ["high", "medium", "low"][min(ranks)] if ranks else None


def _row_severity(t, r):
    """Row-level severity tier for the stripe, or None. exceptions/owner_drilldown
    carry an authoritative 'worst'; risky_commits/slippage derive it from codes."""
    if "worst" in r:
        sev = r.get("worst")
        return sev if sev in _SEV_CLS else None
    if t.id in ("risky_commits", "slippage"):
        col = "flags" if t.id == "risky_commits" else "rules"
        return _worst_sev(_codes(r.get(col)), r)
    return None


def _chip_cell(text, sev, is_rule):
    codes = _codes(text)
    if not codes:
        return Td("")
    sev_cls = _SEV_CLS.get(sev)
    chips = []
    for c in codes:
        if is_rule:
            cls = "chip mono" + (f" {sev_cls}" if sev_cls else "")
            chips.append(Span(Span(cls="cd"), c, cls=cls))
        else:
            chips.append(Span(c.replace("_", " "), cls="chip"))
    return Td(Div(*chips, cls="chips"))


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
    if col in ("opp_id", "forecast"):
        cls = "mono"
    text = "" if val is None else str(val)
    return Td(text, cls=cls)


def render_table(t, cur):
    head = Tr(*[Th(c, cls="num" if c in _NUMERIC else "") for c in t.columns])
    chip_col, is_rule = _CHIP_COL.get(t.id, (None, False))
    body = []
    for r in t.rows:
        sev = _row_severity(t, r)
        cells = []
        for c in t.columns:
            if c == chip_col:
                cells.append(_chip_cell(r.get(c), sev if is_rule else None,
                                        is_rule))
            else:
                cells.append(_cell(c, r.get(c), t.formats, cur))
        row_cls = []
        attrs = {}
        # Owner scoreboard rows drive the drill-down (replaces Streamlit's
        # on_select rerun): hx-get returns just the drill-down partial, and
        # hx-include carries the active filter set so it stays consistent.
        if t.id == "owner_scoreboard":
            attrs = {"hx_get": f"/drilldown?owner={_q(r.get('owner'))}",
                     "hx_target": "#owner-drilldown", "hx_swap": "outerHTML",
                     "hx_include": "#sidebar-form", "hx_indicator": "#load",
                     "role": "button", "tabindex": "0"}
            row_cls.append("row-click")
        sev_cls = _SEV_CLS.get(sev)
        if sev_cls:
            row_cls.append("sev-" + sev_cls)
        if row_cls:
            attrs["cls"] = " ".join(row_cls)
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
    # The desk score is the headline KPI — give it the primary (tinted) card.
    primary = " primary" if m.label.startswith("Desk score") else ""
    return Div(
        Div(m.label, cls="metric-label", title=m.help or ""),
        Div(m.value, cls="metric-value"),
        Div(m.delta or " ", cls=f"metric-delta {delta_cls}"),
        cls=f"metric-card{primary}")


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
    for key in ("stage_map", "snapshot", "as_of", "upload_token"):
        if sel and sel.get(key):
            pairs.append((key, sel[key]))
    for key, param in (("owners", "owner"), ("teams", "team"),
                       ("stages", "stage"), ("sev", "sev")):
        for value in (sel or {}).get(key) or []:
            pairs.append((param, value))
    return f"?{urlencode(pairs)}" if pairs else ""


def render_content(page, sel=None, *, oob=False):
    """The headline + tabs region (htmx-swappable target). oob=True marks it as
    an out-of-band swap target so /upload can replace #content while its primary
    response updates #upload-status."""
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
    attrs = {"id": "content", "cls": "content", "aria_live": "polite"}
    if oob:
        attrs["hx_swap_oob"] = "true"
    return Div(*body, **attrs)


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


def _sidebar_dynamic(page, sel, *, oob=False):
    """The stage-vocab + snapshot/filter controls (an htmx OOB target). /upload
    swaps this so filters reflect the uploaded rows and carry upload_token; the
    static upload field + #upload-status above it stay in place."""
    c = page.controls
    stage_maps = c.get("stage_maps") or ["default"]
    dates = [d.isoformat() for d in c.get("snapshot_dates") or []]
    snap = (c.get("snapshot_date").isoformat() if c.get("snapshot_date")
            else (dates[-1] if dates else ""))
    as_of = c.get("as_of").isoformat() if c.get("as_of") else snap
    upload_token = c.get("upload_token")

    parts = [Div(Label("Stage vocabulary (stage_map)", cls="lbl"),
                 Select(*[Option(s, value=s,
                                 selected=(s == sel.get("stage_map", "default")))
                          for s in stage_maps],
                        name="stage_map", hx_trigger="change", **_HX),
                 cls="field")]
    if upload_token:
        # Carry the temp-session token on every filter/drill-down/download
        # request; offer a way back to the stored view (a plain reload).
        parts.append(Input(type="hidden", name="upload_token",
                           value=upload_token))
        parts.append(P("Viewing an uploaded snapshot — not added to the store. ",
                       A("Return to the stored view", href="/", cls="download"),
                       cls="caption"))
    if dates:
        parts.append(Div(
            Label("Snapshot", cls="lbl"),
            Select(*[Option(d, value=d, selected=(d == snap)) for d in dates],
                   name="snapshot", hx_trigger="change", **_HX), cls="field"))
        parts.append(Div(
            Label("Evaluate as of", cls="lbl"),
            Input(type="date", name="as_of", value=as_of,
                  hx_trigger="change", **_HX), cls="field"))
    if dates or upload_token:
        parts.append(H2("Filters", cls="h2"))
        parts.append(Div(Label("Owner", cls="lbl"),
                         _checkbox_list("owner", c.get("owners") or [],
                                        sel.get("owners"), searchable=True),
                         cls="field"))
        if c.get("teams"):
            parts.append(Div(Label("Team", cls="lbl"),
                             _checkbox_list("team", c["teams"],
                                            sel.get("teams"),
                                            searchable=True), cls="field"))
        parts.append(Div(Label("Stage", cls="lbl"),
                         _checkbox_list("stage", c.get("stages") or [],
                                        sel.get("stages")), cls="field"))
        parts.append(Div(Label("Severity", cls="lbl"),
                         _checkbox_list("sev", ["high", "medium", "low"],
                                        sel.get("sev")), cls="field"))
        parts.append(Button("Apply filters", type="button", cls="apply",
                            hx_trigger="click", **_HX))
    attrs = {"id": "sidebar-dynamic"}
    if oob:
        attrs["hx_swap_oob"] = "true"
    return Div(*parts, **attrs)


def render_sidebar(page, sel):
    controls = [
        H2("Data source", cls="h2"),
        Div(Label("Upload a snapshot CSV (validated and rendered below; not "
                  "added to the snapshot store)", cls="lbl"),
            Input(type="file", name="upload", accept=".csv",
                  hx_post="/upload", hx_target="#upload-status",
                  hx_swap="outerHTML", hx_encoding="multipart/form-data",
                  hx_trigger="change", hx_indicator="#load"),
            Div(id="upload-status"), cls="field"),
        _sidebar_dynamic(page, sel),
    ]
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
