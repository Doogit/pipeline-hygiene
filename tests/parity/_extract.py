"""Render app/dashboard.py under Streamlit AppTest and extract a canonical,
JSON-serialisable dump of everything it displays.

This is the parity source of truth: scalar text (captions/markdown/metrics/
warnings) is captured fully formatted for byte-diffing; dataframes are captured
as raw values + column names (Streamlit formats cells client-side via
column_config, so the view model diffs the underlying values, not the pixels);
chart specs are captured from the vega-lite proto for spec-level parity.
"""
import json
import math
import os
from contextlib import contextmanager
from pathlib import Path

from streamlit.testing.v1 import AppTest

DASHBOARD = str(Path(__file__).resolve().parent.parent.parent / "app" / "dashboard.py")


@contextmanager
def _environ(env):
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _clean(value):
    """JSON-safe: NaN/NaT -> None, dates -> iso, numpy scalars -> python,
    nested lists/dicts recursed."""
    if value is None:
        return None
    if isinstance(value, float):
        return None if math.isnan(value) else value
    if isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "item"):  # numpy scalar
        try:
            return _clean(value.item())
        except Exception:
            return str(value)
    return str(value)


def _df_dump(df):
    frame = df.value
    return {
        "columns": [str(c) for c in frame.columns],
        "records": [{str(k): _clean(v) for k, v in rec.items()}
                    for rec in frame.to_dict(orient="records")],
    }


def _texts(seq):
    return [el.value for el in seq]


def _metrics(seq):
    out = []
    for m in seq:
        out.append({"label": m.label, "value": m.value,
                    "delta": getattr(m, "delta", None)})
    return out


def _charts(app):
    specs = []
    for el in app.get("vega_lite_chart"):
        raw = getattr(el.proto, "spec", None)
        specs.append(json.loads(raw) if raw else None)
    return specs


def _redactions(env):
    """Volatile absolute paths (the per-run temp dir) -> stable tokens, so the
    golden is reproducible across freeze runs and the parity test's own tmp dir.
    Longest paths first. Both `\\` and `/` forms are covered."""
    pairs = []
    for key, token in (("PIPELINE_HYGIENE_QUOTAS", "<QUOTAS>"),
                       ("PIPELINE_HYGIENE_DB", "<DB>"),
                       ("PIPELINE_HYGIENE_CONFIG", "<CONFIG>")):
        p = env.get(key)
        if p:
            parent = str(Path(p).parent)
            pairs.append((p, token))
            pairs.append((str(Path(p)), token))
            pairs.append((parent, "<TMP>"))
    # longest source first so nested replacements are stable
    uniq = sorted({(s, t) for s, t in pairs}, key=lambda st: -len(st[0]))
    return uniq


def _redact(value, pairs):
    if isinstance(value, str):
        for src, token in pairs:
            if src in value:
                value = value.replace(src, token)
            alt = src.replace("\\", "/")
            if alt != src and alt in value:
                value = value.replace(alt, token)
        return value
    if isinstance(value, list):
        return [_redact(v, pairs) for v in value]
    if isinstance(value, dict):
        return {k: _redact(v, pairs) for k, v in value.items()}
    return value


def render(env):
    """Run the dashboard once under `env` and return the canonical dump dict
    with volatile temp paths redacted to stable tokens."""
    with _environ(env):
        app = AppTest.from_file(DASHBOARD, default_timeout=120)
        app.run()
    dump = {
        "exception": None if not app.exception else str(app.exception[0].value),
        "title": _texts(app.title),
        "header": _texts(app.header),
        "subheader": _texts(app.subheader),
        "caption": _texts(app.caption),
        "sidebar_caption": _texts(app.sidebar.caption),
        "markdown": _texts(app.markdown),
        "warning": _texts(app.warning),
        "error": _texts(app.error),
        "metric": _metrics(app.metric),
        "tabs": [t.label for t in app.tabs],
        "selectbox": [{"label": s.label, "value": _clean(s.value)}
                      for s in app.sidebar.selectbox],
        "multiselect": [{"label": s.label, "value": _clean(s.value)}
                        for s in app.sidebar.multiselect],
        "date_input": [{"label": s.label, "value": _clean(s.value)}
                       for s in app.sidebar.date_input],
        "dataframe": [_df_dump(df) for df in app.dataframe],
        "charts": _charts(app),
    }
    return _redact(dump, _redactions(env))
