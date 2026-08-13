# pipeline-hygiene

A read-only sales pipeline inspection agent. This branch ingests opportunity
CSV snapshots into a SQLite snapshot store, runs deterministic hygiene rules
(H1-H11), and scores every opportunity, owner, and desk.

Operating principle: **agents inspect, people sell.** The agent never writes to
source data and never contacts sellers.

## Demo

The read-only dashboard cycling through its seven tabs. This GIF is generated
from the stills in `docs/screenshots/` by `scripts/build_demo_reel.py`, so it
stays in sync with them (CI rebuilds it whenever the screenshots change).

![Demo reel cycling through the dashboard tabs](docs/demo-reel.gif)

## Install

```
pip install -r requirements.txt
```

Python 3.10+. Runtime needs pyyaml, pandas, python-fasthtml and altair;
pytest/hypothesis are dev-only. No services, no API keys, no LLM calls.

## Run it hosted (Azure App Service)

To put the dashboard on a URL instead of a laptop, `deploy/azure-deploy.ps1`
builds a container image *inside Azure* (no local Docker) and provisions
App Service for Containers with the committed synthetic demo data baked in:

```powershell
az login
./deploy/azure-deploy.ps1
```

It serves synthetic data with no auth by default; see
[deploy/README.md](deploy/README.md) for the one-command Entra (Microsoft
sign-in) gate to add before using real data. The `Dockerfile` also runs
anywhere Docker does (`docker build -t pipeline-hygiene . && docker run -p
8000:8000 pipeline-hygiene`).

## Bring your own CSV

The simulator is only a demo. For a real CRM export, run `python -m src.ingest
--init your_export.csv`: it inspects the file, prints a ready-to-paste
`stage_map` skeleton, and names any missing columns so you never hand-write the
mapping. The required columns are `opp_id, account, opp_name, owner, stage,
amount, currency, created_date, close_date, last_activity_date, next_step,
next_step_date, forecast_category, contact_count, product_line` (ISO dates, one
currency per file); pass a quotas JSON with `--quotas` to unlock coverage and
team/region rollups. `config.yaml` ships `default`, `dynamics_default`, and
`hubspot` stage-map presets, and ingest is safe to schedule — it stores nothing
and exits nonzero if every row is rejected.

```
python -m src.ingest your_export.csv --stage-map your_map
python -m src.brief --as-of 2026-08-10 --quotas your_quotas.json --digests
```

## For your boss (FAQ)

For an evaluator deciding between this and a commercial suite:

- **Cost of operation.** Runs locally on Python 3.10+; no API keys, no LLM
  calls, no external services, no per-seat license. Dependencies are pyyaml,
  pandas, python-fasthtml, altair (pytest/hypothesis are dev-only). A snapshot
  ingests and briefs in seconds on a laptop.
- **Auditability.** Every hygiene rule (H1–H11) is a deterministic pure
  function with a versioned threshold in `config.yaml`; there is no model and
  no randomness in the engine. The coverage number prints its own basis
  (`trailing win rate 28/43 closed won (65.1%) -> required multiple 1.54x`),
  the pipeline waterfall reconciles to the dollar, and a golden-file test pins
  the brief byte-for-byte. A skeptic can reproduce every figure by hand.
- **Extensibility.** Add a rule as a pure function in `src/rules.py` plus a
  weight in `config.yaml`; map any CRM's stage vocabulary with `stage_map`;
  get team/region rollups by adding an `owners` block to the quotas JSON. No
  schema migration, no vendor lock-in — it's plain CSV in, Markdown/SQLite out.
- **What it deliberately does NOT do** (by design, not omission):
  - No CRM write-back and no contacting sellers — it is read-only over CSV
    snapshots (*agents inspect, people sell*). The forecast-call checkboxes
    are paper, on purpose.
  - No real-time sync — it reasons over batch snapshots, which is what makes
    the since-last-run and slippage history possible.
  - No opaque AI/ML deal-risk score — the deterministic, auditable rules are
    the intended *complement* to CRM-vendor risk AI, not a copy of it.
  - One currency per file (mixed currency is a fatal ingest error), no
    activity capture, no built-in SSO/RBAC (bind the dashboard to localhost
    and distribute the private digests per seller).

## Hygiene rules (H1–H11)

Each rule is a deterministic pure function `(row, config, as_of)` with a
versioned threshold in `config.yaml` (the same legend prints at the foot of
every brief). Score starts at 100 and each violation deducts its weight.

| Rule | Meaning | Key threshold(s) in `config.yaml` | Weight |
|---|---|---|---|
| H1 | stale by stage | `staleness_days` (per stage) | 15 |
| H2 | close date in past | close_date < as_of | 20 |
| H3 | serial slippage | ≥2 close-date changes (high at 3) | 10 |
| H4 | missing/expired next step | next_step empty or next_step_date < as_of | 20 |
| H5 | forecast mismatch | commit on pre-develop stage, or no valid next step | 25 |
| H6 | aging in stage | `aging_norm_days` (per stage) | 10 |
| H7 | single-threaded big deal | `big_deal_threshold` and contact_count < 2 | 10 |
| H8 | amount hygiene | amount blank or ≤ 0 | 5 |
| H9 | vague next step | `next_step_quality` (min chars / filler / action verb) | 5 |
| H10 | parked close date | `close_date_horizon_days` | 10 |
| H11 | lost deal control | `push_alarm_days` / `cumulative_push_alarm_days` | 20 |

H3, H6, and H11 need snapshot history; with a single snapshot H3/H6 report
`insufficient_history` (never a false flag) and H11 is silent.

## Dashboard (FastHTML)

```
python -m app.server        # then open http://127.0.0.1:5100
```

The dashboard is a read-only FastHTML + htmx page (`app/server.py`) whose tabs
mirror the brief structure — it never writes to the store or to source data,
and viewing or downloading a brief from it does not record a run. It binds
`127.0.0.1` explicitly (serving the private digest data to the network needs a
deliberate `PIPELINE_HYGIENE_ALLOW_NONLOCAL_HOST=1` opt-in), sends a strict CSP
(`default-src 'self'; style-src 'self' 'unsafe-inline'`), and is offline/zero-CDN:
htmx, vega/vega-lite/vega-embed, the CSP-safe AST interpreter, and the Tailwind
stylesheet are all served locally from `app/static` (versions + SHA-256 in
`app/static/vendor/VENDOR.md`; the stylesheet is built offline by the standalone
`tailwindcss` CLI via `scripts/build_css.sh`, no Node). Styling is a light
Fluent/Microsoft-web theme (Segoe UI system stack, no CDN fonts; accent
`#0072B2`, the Okabe-Ito "low" blue that doubles as Microsoft brand blue). The
page renders strictly from a pure view model (`src/pipeline_hygiene_view.py`);
a parity gate (`tests/parity/`) freezes the values/strings/chart-specs and the
exported brief byte-for-byte against the prior Streamlit render. Every view
shares the same headline: a metric row with week-over-week delta arrows wired to
since-last-run (desk score, open pipeline, at-risk dollars — at-risk inverts, so
rising risk reads red), the violation counts as colored text, and the severity
mix as a compact stacked bar. The sidebar
holds the data source (stored snapshot picker or CSV upload), the explicit
`as_of` evaluation date, and owner/team/stage/severity filters (team
selections expand to their rosters; the sidebar states which tables the
filters apply to — headline metrics and team/region rollups stay
desk-wide).

### Forecast call (landing tab)

Built to stand alone for a Monday meeting: the risky commits table — every
open commit/best_case deal carrying a risk flag, dollar-ranked, each with
the deterministic coaching prompt of its dominant rule — plus the
since-last-run summary line. Sidebar filters apply here too, so a
frontline manager can run her 9:00 call on just her team's risky commits.

![Forecast call tab](docs/screenshots/tab-forecast-call.png)

### Slippage

The push analytics derived from snapshot history: pushes, cumulative
later-drift, max push, H11 badges, the disqualification-review marker at
3+ pushes, and a per-opp close-date drift sparkline (inline SVG).

![Slippage tab](docs/screenshots/tab-slippage.png)

### Trajectory

Altair charts across stored snapshots: coverage (open vs required pipeline
at 1 / trailing win rate, with the basis printed underneath) and the desk
score trend — snapshot-anchored (the engine re-run per stored snapshot, no
holes from snapshots without recorded runs). Needs at least 2 stored
snapshots; with fewer it degrades to a clear caption instead of a
one-point trend. Below the charts: commit accuracy by committed-for
quarter (the fiscal quarter of the close date when the deal was first
called commit — immune to later pushes, so a slipped commit stays counted
against the quarter it was promised for). The created-vs-closed flow bars
moved to the Flow tab.

![Trajectory tab](docs/screenshots/tab-trajectory.png)

### Flow

Where the pipeline dollars went between the last two snapshots, and
whether enough new pipeline is being generated. The open-pipeline
waterfall (beginning + created + increased − decreased − won − lost −
removed = ending — reconciles exactly; close-date pushes move no dollars
and render as an annotation, never a bucket), pipeline-generation pacing
per snapshot week against the optional `pipeline_gen_weekly_target` line
(no target configured → no line guessed), and created-vs-closed bars per
snapshot week. The same waterfall renders as a table on brief page 1.

![Flow tab](docs/screenshots/tab-flow.png)

### Owners

Owner scoreboard (progress-bar scores, pipeline dollars, coverage, small_n
and low_coverage flags), the forecast-integrity patterns — overcall /
undercall — and the per-owner commit-accuracy ledger, all rendered with
the "coaching signal, not a comp input" disclaimer. Coverage is open pipeline vs required pipeline (remaining quota
net of wins this quarter x the win-rate-derived required multiple — the same
basis as the desk headline), and `low_coverage` means exactly under 1.00x,
so the shown ratio and the flag can never disagree.

![Owners tab](docs/screenshots/tab-owners.png)

### Teams

Team and region roll-ups over the same open opps — owners, open pipeline,
roster quota, coverage, violations, at-risk dollars — sorted worst coverage
first so ordering itself carries the signal, each table carrying the
desk-wide under-coverage note when most groups trip the flag (so a uniform
`low_coverage` column reads as a desk condition, not "every team failing").
Below the tables, a per-team and per-region **coverage trend** across stored
snapshots (each point on its own win-rate basis, with a 1.00x reference
line), and commit accuracy by team and region. Team/region membership comes
from the `owners` block of the `--quotas`/`PIPELINE_HYGIENE_QUOTAS` JSON (the
seed manifest already carries it); without that metadata the tab degrades to
a clear caption.

![Teams tab](docs/screenshots/tab-teams.png)

### Appendix

The full exception list — the only place it appears; drill-down, never the
headline — with streak annotations ("flagged N runs") and per-opp score
history sparklines, plus the validation report and the download-brief
button.

![Appendix tab](docs/screenshots/tab-appendix.png)

Implementation notes:

- Data source: the latest stored snapshot from `data/pipeline.db` by
  default (snapshot selectable in the sidebar). An uploaded CSV runs the
  same ingest validation in memory, renders as a full single-snapshot page,
  and is kept only in a short-lived in-process session so drill-down and
  Markdown download target the uploaded rows without writing to the snapshot
  store. Sidebar
  owner/stage/severity filters apply to the Slippage, Owners, and Appendix
  tables.
- Tables render from the view model: a CSS bar for 0-100 scores, `$%d`-style
  formatting for amounts, inline SVG sparklines for close-date drift and
  score history, plain text badges for rules (Okabe-Ito colorblind-safe
  severity palette; colored text, no emoji).
- Charts emit Altair-built Vega-Lite specs, rendered client-side by the
  vendored vega/vega-lite/vega-embed served locally (no CDN); the Flow
  "created vs closed" hover is preserved.
- Runtime paths can be overridden with `PIPELINE_HYGIENE_CONFIG`,
  `PIPELINE_HYGIENE_DB`, and `PIPELINE_HYGIENE_QUOTAS` (used by the
  view-model, server, and parity suites under `tests/` to run against
  isolated fixtures).
- Source screenshots live in `docs/screenshots/` (captured from the app
  running headless on 127.0.0.1 against the committed series data); the
  README demo reel is rebuilt from them by `scripts/build_demo_reel.py`.

## Layout

```
src/            ingest.py, snapshots.py, rules.py, scoring.py, brief.py
src/seed/       org simulator: __main__.py, org.py, pathologies.py, series.py
app/            server.py + render.py (read-only FastHTML dashboard), static/ (vendored htmx/vega/Tailwind)
data/           generated CSVs, seed_manifest.json, delta_manifest.json, pipeline.db
tests/          pytest + hypothesis; loads tests/config_test.yaml ONLY
out/            generated desk briefs (Task 5)
config.yaml     runtime thresholds, stage_map, rule weights
```

## Usage

```
python -m src.seed --rows 400 --as-of 2026-08-10        # single snapshot + ground-truth manifest
python -m src.seed --series 4 --as-of 2026-08-10        # weekly snapshots T0..T3 + delta manifest
python -m src.ingest --init your_export.csv             # "doctor": inspect a CSV, print a stage_map
                                                        # skeleton (FIXME per unmatched stage); exits
                                                        # nonzero only on missing required columns
python -m src.ingest data/opps_2026-08-10.csv           # validate + load into data/pipeline.db
python -m src.brief --as-of 2026-08-10 --quotas data/seed_manifest.json
                                                        # write out/desk_brief_2026-08-10.md
                                                        # (series mode writes data/delta_manifest.json
                                                        # instead — it carries the same quotas/owners
                                                        # blocks; pass whichever file your seed run wrote)
python -m src.brief --as-of 2026-08-10 --quotas data/seed_manifest.json --digests
                                                        # ...plus out/digests/<as_of>/<owner>.md
python -m src.brief --as-of 2026-08-10 --quotas data/seed_manifest.json --team "Team EMEA-1"
                                                        # filtered brief for one team (repeatable
                                                        # --team/--owner/--region, case-insensitive;
                                                        # writes a suffixed file, never records a run)
python -m src.brief --as-of 2026-08-10 --quotas data/seed_manifest.json --commit-scrub
                                                        # ONLY the pre-forecast-call scrub sheet
                                                        # (out/commit_scrub_<as_of>.md): every open
                                                        # commit/best_case opp + checklist columns;
                                                        # no brief, never records a run; composes
                                                        # with --owner/--team/--region
python -m app.server                                    # read-only dashboard (127.0.0.1:5100)
pytest -q                                               # full test suite
```

Point `--db`, `--config`, `--quotas`, and `--out-dir` at your files, or set
`PIPELINE_HYGIENE_DB` / `PIPELINE_HYGIENE_CONFIG` / `PIPELINE_HYGIENE_QUOTAS` /
`PIPELINE_HYGIENE_OUT` (ingest, brief, and the dashboard all honor the store
and config vars; brief also honors the quotas and out-dir vars). To keep an
evaluation trial fully isolated, set `PIPELINE_HYGIENE_DB` and
`PIPELINE_HYGIENE_OUT` to a scratch location so neither the store nor the
written briefs touch your production copies. The brief prints `reading
snapshot store <path>` to stderr so a scheduled run can't silently brief the
wrong database.

## Persona pass

`.claude/skills/persona-pass` runs a usability simulation: it role-plays each
user persona in [docs/personas.md](docs/personas.md) (frontline manager,
RevOps analyst, VP, flagged AE) against the real product to surface usability
and product gaps that unit tests miss. Run it before a release or after any
change to the brief, digests, dashboard, or CLI.

## Reproducible runs (clock determinism)

Every function that evaluates time takes an explicit `as_of: date`;
`date.today()` appears only as the default for a CLI `--as-of` flag, never
inside the engine. Because nothing reads the wall clock mid-computation, a run
is fully reproducible: the same snapshots evaluated at the same `as_of` always
produce the same flags, scores, and brief. That is what makes the golden-file
test and a defensible audit trail possible.

## How correctness is verified (epistemics)

Two independent oracles keep the engine honest, so a passing suite means more
than "the code agrees with itself":

- **Per-rule unit tests** pin each rule's exact boundaries — at the threshold,
  one step past, empty/null — so no rule can silently drift.
- **The seed-manifest integration test** runs the whole engine over a generated
  org and checks its output against the manifest. The manifest's expected
  violations are built field-by-field by the generator, *never* by running the
  rules engine, so the two are checked against each other rather than against a
  shared source — the comparison is non-circular.

## Decisions

The key design decisions; finer implementation rationale lives in `SPEC.md`,
the code, and git history.

- **Read-only, coaching not comp.** The agent never writes to source data or
  contacts sellers. Owner/team scoreboards, forecast-integrity patterns, and
  the commit-accuracy ledger all carry a "coaching signal, not a comp input"
  disclaimer, sort alphabetically/chronologically (never worst-first — a
  leaderboard is one sort from a comp weapon), and suppress a percentage until
  `min_opps_for_owner_score` items have resolved (a bare "100%" on n=1 is the
  number that gets screenshotted).
- **Deterministic engine.** Every rule is a pure `(row, config, as_of)`
  function with a versioned threshold in `config.yaml`; no model, no
  randomness. `date.today()` lives only in CLI defaults (see Reproducible
  runs), and the golden brief (`tests/golden/desk_brief_golden.md`) is compared
  byte-for-byte — to regenerate, delete it and run the test once (it recreates
  the file and fails asking for review).
- **One coverage basis everywhere.** `scoring.required_coverage_multiple` is
  the single source of truth: coverage is open pipeline / (remaining quota ×
  required multiple), and `low_coverage` fires exactly when that ratio is under
  1.00x — so the shown ratio and the flag can never contradict each other. The
  basis string carries the exact fraction (`trailing win rate 20/32 closed won
  (62.5%) -> required multiple 1.60x`) so a reader can reproduce the number to
  the dollar; a rounded multiple alone broke a persona sim's napkin check by
  ~$285K.
- **Since-last-run is diff-based.** Each full brief run records its per-opp rule
  sets in the `runs` table; the next run diffs against it. Violations that
  vanish because a deal closed are reported under "Closed", never "Cleared";
  newly appearing opps are listed separately and their violations aren't counted
  as new flags. Flag streaks count consecutive *runs*, and re-running on the
  same snapshot replaces that run (idempotent) so streaks never inflate.
- **Filtered and scrub outputs never record a run.** `--owner`/`--team`/
  `--region` briefs and `--commit-scrub` write suffixed files and are never
  written to the `runs` table (a partial open-opp map would corrupt streaks and
  since-last-run for later full briefs).
- **Private digests, not published rankings.** `--digests` writes one private
  coaching digest per owner (`out/digests/<as_of>/<owner_slug>.md`) with only
  that owner's data — coaching evidence favors private weekly digests; published
  rankings raise attrition.
- **Quotas/metadata come from the `--quotas` JSON, not `config.yaml`.** Seed
  never mutates `config.yaml`; it writes quotas and owner `{team, region}`
  metadata into `data/seed_manifest.json`, which callers merge at run time.
  Coverage, teams, and regions are blank without it.
- **History-only push stats.** H11 ("lost deal control") derives `push_count`,
  `cumulative_extension_days`, and `max_push_days` from consecutive stored
  snapshots where close_date moved *later* — never from a CSV column. With
  fewer than 2 snapshots there are zero observed transitions, so H11 is simply
  silent (no `insufficient_history` state).
- **Validation is strict and persisted.** Missing required *columns* are fatal
  (nonzero exit); invalid *rows* are rejected into a ValidationReport. Mixed
  currency in one file is fatal; a uniform-but-unexpected currency is a warning.
  The per-snapshot report is stored as `validation_json` on the `snapshots`
  table so the brief and dashboard surface it without re-validating.
- **New config keys are optional and back-compatible.** Every key added after
  the frozen `tests/config_test.yaml` is schema-checked only when present with
  defaults applied at point of use (e.g. `display_currency_symbol`,
  `pipeline_gen_weekly_target`, `staleness_escalation`), so the frozen test
  config and existing deployments stay byte-identical.
- **Fiscal quarters** are labeled `FY<year>-Q<n>`, the fiscal year named for the
  calendar year it ends in (with `fiscal_year_start_month: 7`, 2026-08 is
  FY2027-Q1).

## Handoff

Next session candidates (recorded, deliberately NOT built) — reviewed this
session against the code; all remain unimplemented:

- Aging thresholds derived from the org's own per-stage medians (1.5-2x
  median), replacing the static `aging_norm_days` in `config.yaml`.
- Org-specific backtesting: a flagged-vs-outcome table from the org's own
  stored history — turns vendor benchmark stats into auditable org
  evidence.
- Dashboard explainability panel: rule + threshold + triggering snapshot
  values per flag (the anti-black-box wedge).
- Slack/email push delivery of the brief and digests (top 3-5 cap, weekly,
  digest not firehose — alert fatigue kills adoption).
- Cross-CRM connector via `stage_map` (original spec handoff option).
- Funnel / stage-transition analytics (deferred across earlier sessions):
  blocked on the simulator producing no mid-funnel stage transitions, so
  there is nothing yet to measure conversion against. Unblock the seed data
  first.

Start the next session by reading `README.md`, `SPEC.md`,
`data/seed_manifest.json`, and `data/delta_manifest.json`.
