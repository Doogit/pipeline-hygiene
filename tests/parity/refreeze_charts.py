"""Re-baseline ONLY the chart specs in the parity goldens (migration step 2, PR 2).

Background: the goldens (tests/parity/golden/*.json) were frozen from the live
Streamlit render before the view model existed, so the parity gate was
non-circular (plan §10 Task 4). Step 2 redesigns the Vega-Lite chart specs to the
new visual language (rounded bar tops, categorical trend palette, emphasized line
points) — a deliberate, reviewed divergence that `test_chart_parity` is meant to
catch. The Streamlit re-freeze tool was deleted with the dashboard (Task 5), so
there is no non-circular source to re-freeze charts from; charts are re-anchored
to the view model's own output. This makes the gate CIRCULAR **for charts only** —
an accepted, documented tradeoff (plan §8). Text / table / metric goldens stay
frozen from Streamlit and are NEVER touched here.

This script rewrites just the `charts` array of each golden from the new view
model, leaving every other key byte-identical. Run it, then review the golden
diff in the PR (it must be confined to `charts`):

    python -m tests.parity.refreeze_charts

It refuses to run unless the ONLY parity difference is charts — if any text /
table / metric key would change, that is an unintended regression, not a
re-baseline, and the script aborts without writing.
"""
import json
import tempfile
from pathlib import Path

from src import pipeline_hygiene_view as V
from tests.parity._build import BUILDERS, FIXTURES
from tests.parity._extract import _redact, _redactions
from tests.parity._flatten import canon_value, flatten, normalize_chart

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"

# The Streamlit-frozen goldens stored data out-of-band (`data: {name: <hash>}`,
# no `datasets`/`config`). Altair's to_dict() inlines the full `datasets` rows and
# an Altair `config` block — both stripped by normalize_chart(), so they are pure
# diff noise. Drop them so the stored golden keeps the frozen shape and the
# re-baseline diff shows only the real mark/encoding/scale changes.
_DROP_FROM_GOLDEN = ("datasets", "config")


def _slim(chart):
    if not isinstance(chart, dict):
        return chart
    return {k: v for k, v in chart.items() if k not in _DROP_FROM_GOLDEN}


def _view_dump(fixture):
    """Identical to the parity test's _view_dump: build the fixture store, render
    the view model, flatten, and redact the volatile temp paths."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        env = BUILDERS[fixture](tmp)
        page = V.build_from_store(env["PIPELINE_HYGIENE_CONFIG"],
                                  env["PIPELINE_HYGIENE_DB"],
                                  env["PIPELINE_HYGIENE_QUOTAS"])
        return _redact(flatten(page), _redactions(env))


def _non_chart_changed(view, gold):
    """True if any non-chart parity surface differs (text multisets, metrics,
    tables) — the guard that this is a chart-only re-baseline."""
    for kind in ("caption", "markdown", "subheader", "header", "warning"):
        if sorted(view[kind]) != sorted(gold[kind]):
            return f"{kind} strings"
    if view["metric"] != gold["metric"]:
        return "metrics"
    tv = [{"columns": t["columns"],
           "records": [canon_value(r) for r in t["records"]]}
          for t in view["dataframe"]]
    tg = [{"columns": t["columns"],
           "records": [canon_value(r) for r in t["records"]]}
          for t in gold["dataframe"]]
    if tv != tg:
        return "tables"
    return None


def main():
    written = []
    for fixture in FIXTURES:
        golden_path = GOLDEN_DIR / f"{fixture}.json"
        gold = json.loads(golden_path.read_text(encoding="utf-8"))
        view = _view_dump(fixture)

        drift = _non_chart_changed(view, gold)
        if drift is not None:
            raise SystemExit(
                f"ABORT [{fixture}]: {drift} differ from the frozen golden — "
                f"this is a regression, not a chart re-baseline. Nothing written.")

        old_norm = sorted(normalize_chart(c) for c in gold["charts"])
        new_norm = sorted(normalize_chart(c) for c in view["charts"])
        if old_norm == new_norm:
            print(f"  {fixture}: chart specs unchanged, skipping")
            continue

        gold["charts"] = [_slim(c) for c in view["charts"]]
        # Same dump settings the goldens were frozen with (verified round-trip):
        # indent=2, ensure_ascii=False, single trailing newline.
        golden_path.write_text(
            json.dumps(gold, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")
        written.append(fixture)
        print(f"  {fixture}: re-baselined {len(view['charts'])} chart spec(s)")

    if written:
        print(f"Re-baselined chart goldens: {', '.join(written)}. "
              f"Review the golden diff (charts[] only) before committing.")
    else:
        print("No chart goldens changed.")


if __name__ == "__main__":
    main()
