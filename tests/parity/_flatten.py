"""Flatten a PageModel into the same canonical shape the goldens use, so the
parity test can diff the view model against the frozen Streamlit render.

Text kinds are collected as (order-independent) lists — the parity test compares
them as multisets, since AppTest's sidebar/main element interleaving is an
implementation detail. Tables and charts are collected in order.
"""
import copy
import json
import re

from src import pipeline_hygiene_view as V
from tests.parity._extract import _clean

# Streamlit wraps Altair's chart.to_dict() with its own theme/config and moves
# the data out-of-band, so chart parity is at the spec-structure level (mark,
# encoding, scales, params, title, height) — the data VALUES are parity-checked
# via tables/metrics/captions. normalize() strips the Streamlit/Altair wrapper
# noise so the two specs are comparable.
_STRIP_TOP = {"config", "datasets", "$schema", "autosize", "width", "usermeta",
              "background", "padding"}


def _scrub(o):
    if isinstance(o, dict):
        return {k: _scrub(v) for k, v in o.items() if k != "data"}
    if isinstance(o, list):
        return [_scrub(x) for x in o]
    # Streamlit renders a null title as " "; Altair emits null -> collapse both.
    if o is None or (isinstance(o, str) and o.strip() == ""):
        return ""
    return o


def normalize_chart(spec):
    """Canonical, comparable form of a vega-lite spec: strip wrapper keys and
    externalised data, collapse empty titles, and canonicalise the content-hash
    param/view names (Altair) vs counter names (Streamlit) to a stable token."""
    if spec is None:
        return None
    trimmed = {k: v for k, v in copy.deepcopy(spec).items()
               if k not in _STRIP_TOP}
    txt = json.dumps(_scrub(trimmed), sort_keys=True)
    return re.sub(r"(param|view|selector)_[0-9A-Za-z]+", r"\1_X", txt)


def canon_value(v):
    """Numeric-tolerant canonicaliser for table cells (int 5 == float 5.0)."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return round(float(v), 4)
    if isinstance(v, list):
        return [canon_value(x) for x in v]
    if isinstance(v, dict):
        return {k: canon_value(x) for k, x in v.items()}
    return v


def flatten(page):
    dump = {k: [] for k in ("title", "header", "subheader", "caption",
                            "markdown", "warning", "metric", "dataframe",
                            "charts")}

    def walk(blocks):
        for b in blocks:
            if isinstance(b, V.Caption):
                dump["caption"].append(b.text)
            elif isinstance(b, V.Markdown):
                dump["markdown"].append(b.text)
            elif isinstance(b, V.Warning):
                dump["warning"].append(b.text)
            elif isinstance(b, V.Heading):
                dump["header" if b.level <= 2 else "subheader"].append(b.text)
            elif isinstance(b, V.MetricRow):
                for m in b.metrics:
                    dump["metric"].append({"label": m.label, "value": m.value,
                                           "delta": m.delta})
            elif isinstance(b, V.Chart):
                dump["charts"].append(b.spec)
            elif isinstance(b, V.Table):
                dump["dataframe"].append({
                    "columns": [str(c) for c in b.columns],
                    "records": [{str(k): _clean(v) for k, v in r.items()}
                                for r in b.rows]})
            elif isinstance(b, V.Popover):
                dump["caption"].append(b.body)
            elif isinstance(b, V.Drilldown):
                dump["caption"].append(b.prompt)
            elif isinstance(b, V.Expander):
                walk(b.blocks)
            elif isinstance(b, V.Tab):
                walk(b.blocks)

    walk(getattr(page, "sidebar", []))
    walk(page.blocks)
    for tab in page.tabs:
        walk(tab.blocks)
    return dump
