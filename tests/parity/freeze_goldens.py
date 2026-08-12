"""Freeze the parity goldens from the LIVE Streamlit render.

Run once (and re-run only on an intentional, reviewed change to the Streamlit
page), then commit tests/parity/golden/*.json. The parity test diffs the new
view model against these frozen goldens; it must NEVER regenerate them, or the
gate becomes circular (plan §10 Task 4).

    python -m tests.parity.freeze_goldens
"""
import json
import tempfile
from pathlib import Path

from tests.parity._build import BUILDERS, FIXTURES
from tests.parity._extract import render

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"


def freeze():
    GOLDEN_DIR.mkdir(exist_ok=True)
    for name in FIXTURES:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            env = BUILDERS[name](tmp)
            dump = render(env)
        out = GOLDEN_DIR / f"{name}.json"
        out.write_text(json.dumps(dump, indent=2, sort_keys=False,
                                  ensure_ascii=False) + "\n", encoding="utf-8")
        n_text = sum(len(dump[k]) for k in ("caption", "markdown", "subheader"))
        print(f"{name}: exception={dump['exception']!r} "
              f"text_elems={n_text} metrics={len(dump['metric'])} "
              f"tables={len(dump['dataframe'])} charts={len(dump['charts'])} "
              f"-> {out.name}")


if __name__ == "__main__":
    freeze()
