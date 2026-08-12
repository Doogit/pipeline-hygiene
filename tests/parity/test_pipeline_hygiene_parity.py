"""Parity gate: the FastHTML view model must reproduce, with ZERO diff, what the
live Streamlit page rendered — for every fixture.

The goldens (tests/parity/golden/*.json) were frozen from the live Streamlit
`AppTest` render BEFORE the view model existed, so this diff is non-circular
(plan §10 Task 4). It must NEVER regenerate the goldens to make itself pass.

- Scalar text (captions/markdown/subheaders/warnings): compared as multisets —
  a dropped/changed/added string fails; only pure reordering is tolerated, since
  AppTest's sidebar/main interleaving is an implementation detail.
- Metrics: compared in order (5 fixed cards).
- Tables: compared in order; cells numeric-tolerant (int 5 == float 5.0).
- Charts: compared as normalized vega-lite specs (structure, not externalised data).

ALLOWLIST is empty and expected to stay empty (plan §10 Task 4). Any intentional
divergence must be added here with a one-line justification, never hidden by
editing a golden.
"""
import json
import tempfile
from collections import Counter
from pathlib import Path

import pytest

from src import pipeline_hygiene_view as V
from tests.parity._build import BUILDERS, FIXTURES
from tests.parity._extract import _redact, _redactions
from tests.parity._flatten import canon_value, flatten, normalize_chart

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"

# Intentional, signed-off divergences from the Streamlit render. Expected empty.
ALLOWLIST = {}  # e.g. {"fixture:kind:string": "justification"}

_TEXT_KINDS = ("caption", "markdown", "subheader", "header", "warning")


def _view_dump(fixture):
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        env = BUILDERS[fixture](tmp)
        page = V.build_from_store(env["PIPELINE_HYGIENE_CONFIG"],
                                  env["PIPELINE_HYGIENE_DB"],
                                  env["PIPELINE_HYGIENE_QUOTAS"])
        return _redact(flatten(page), _redactions(env))


def _golden(fixture):
    return json.loads((GOLDEN_DIR / f"{fixture}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("fixture", FIXTURES)
def test_text_parity(fixture):
    view, gold = _view_dump(fixture), _golden(fixture)
    for kind in _TEXT_KINDS:
        cv, cg = Counter(view[kind]), Counter(gold[kind])
        view_only = list((cv - cg).elements())
        gold_only = list((cg - cv).elements())
        view_only = [s for s in view_only if ALLOWLIST.get(f"{fixture}:{kind}:{s}") is None]
        gold_only = [s for s in gold_only if ALLOWLIST.get(f"{fixture}:{kind}:{s}") is None]
        assert not view_only, f"[{fixture}/{kind}] view emitted strings absent from golden: {view_only}"
        assert not gold_only, f"[{fixture}/{kind}] view dropped golden strings: {gold_only}"


@pytest.mark.parametrize("fixture", FIXTURES)
def test_metric_parity(fixture):
    view, gold = _view_dump(fixture), _golden(fixture)
    assert view["metric"] == gold["metric"]


@pytest.mark.parametrize("fixture", FIXTURES)
def test_table_parity(fixture):
    view, gold = _view_dump(fixture), _golden(fixture)
    assert len(view["dataframe"]) == len(gold["dataframe"]), \
        f"[{fixture}] table count {len(view['dataframe'])} != {len(gold['dataframe'])}"
    for i, (tv, tg) in enumerate(zip(view["dataframe"], gold["dataframe"])):
        assert tv["columns"] == tg["columns"], f"[{fixture}] table[{i}] columns differ"
        rv = [canon_value(r) for r in tv["records"]]
        rg = [canon_value(r) for r in tg["records"]]
        assert rv == rg, f"[{fixture}] table[{i}] ({tv['columns'][0]}) rows differ"


@pytest.mark.parametrize("fixture", FIXTURES)
def test_chart_parity(fixture):
    view, gold = _view_dump(fixture), _golden(fixture)
    nv = sorted(normalize_chart(c) for c in view["charts"])
    ng = sorted(normalize_chart(c) for c in gold["charts"])
    assert nv == ng, f"[{fixture}] chart specs differ"


def test_allowlist_is_empty():
    # The expected outcome is zero intentional divergence (plan §10 Task 4).
    assert ALLOWLIST == {}, f"Unexpected parity allowlist entries: {ALLOWLIST}"
