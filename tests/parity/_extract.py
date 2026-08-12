"""Canonicalisation helpers shared by the parity gate.

The parity goldens (tests/parity/golden/*.json) were frozen from the live
Streamlit `app/dashboard.py` render (plan §10 Task 4) before Streamlit was
retired (Task 5). They are immutable and committed; the freezing tool
(`freeze_goldens.py`, which drove Streamlit AppTest) was removed with the
dashboard — git history preserves how they were produced. The parity test now
diffs the FastHTML view model against those frozen goldens using the pure
helpers below (no Streamlit dependency).
"""
import math
from pathlib import Path


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
    if hasattr(value, "tolist"):  # numpy array/scalar, pandas Series
        return _clean(value.tolist())
    if hasattr(value, "item"):  # numpy scalar fallback
        try:
            return _clean(value.item())
        except Exception:
            return str(value)
    return str(value)


def _redactions(env):
    """Volatile absolute paths (the per-run temp dir) -> stable tokens, so the
    golden is reproducible across the parity test's own tmp dir.
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
