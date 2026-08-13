# pipeline-hygiene

A local-only sales pipeline inspection app. This tool ingests CRM opportunity
snapshots (CSV exports) into a SQLite snapshot store, runs deterministic hygiene rules
(H1-H11), and provides opportunity, owner, and desk insights.

The tool is intended to be run in a local environment (e.g. Azure server / workspace) to protect sensitive data. After install in the local environment, all data stays local.   

## Demo

The read-only dashboard cycling through its seven tabs. This GIF is generated
from the stills in `docs/screenshots/` by `scripts/build_demo_reel.py`, so it
stays in sync with them (CI rebuilds it whenever the screenshots change).

![Demo reel cycling through the dashboard tabs](docs/demo-reel.gif)

## Security & secure-by-design

This tool handles sales-pipeline data, so it is built to keep that data local, inert, and hard to exfiltrate. The security posture is a design property, not a bolt-on:

- **Read-only over the data.** The tool never writes back to a CRM, never contacts sellers, and never mutates source files or the snapshot store. Viewing, filtering, drilling down, and downloading a brief all record nothing — there is no write path to abuse.
- **Local-only by default.** `python -m app.server` binds `127.0.0.1`. Binding to a non-local host is refused unless you explicitly opt in with `PIPELINE_HYGIENE_ALLOW_NONLOCAL_HOST=1`, so the private data is never served to the network by accident.
- **Strict Content-Security-Policy on every response.** `default-src 'self'; style-src 'self' 'unsafe-inline'`. Scripts are same-origin only — no inline JavaScript and no `eval`; only inline *styles* are permitted.
- **Zero-CDN, fully offline, integrity-pinned assets.** htmx, vega/vega-lite/vega-embed, the CSP-safe AST interpreter, and the Tailwind stylesheet are all vendored under `app/static` and served locally — no third-party requests, no CDN supply-chain exposure, no external tracking. Each asset's upstream version and SHA-256 are recorded in `app/static/vendor/VENDOR.md` and re-verified on update (`sha256sum -c`).
- **CSP-safe chart rendering.** Charts render through the Vega AST interpreter specifically so the app needs no `unsafe-eval` — the strict CSP above holds with no exceptions carved out for charting.
- **Uploaded CSVs never persist.** An uploaded snapshot is validated and held in a transient in-memory session keyed by an opaque `secrets`-generated token, then pruned on expiry. Nothing from an upload is written to disk or into the snapshot store.
- **Auth at the platform edge, not hand-rolled.** The app ships no bespoke authentication. It serves synthetic demo data openly; for real data you place sign-in *in front* of it (e.g. Azure App Service "Easy Auth" / Entra `RequireAuthentication`), a clean boundary with no auth code to get wrong. The deploy guide explicitly warns not to point it at real pipeline data until sign-in is enabled.
- **Small, auditable surface.** A single read-only FastHTML + htmx process with minimal dependencies and no database writes keeps the attack surface small and the behavior easy to reason about.

This is a demo-grade tool, not a hardened multi-tenant service: there is no built-in RBAC, audit logging, or activity capture. The design goal is that the default posture is safe (local, read-only, offline) and that using it with real data is a deliberate, gated step.

## Bring your own CSV

Export data from your CRM and upload the file to the dashboard. The tool does the rest.

If you want to generate test / seed data, use the following commands.

```
python -m src.ingest your_export.csv --stage-map your_map
python -m src.brief --as-of 2026-08-10 --quotas your_quotas.json --digests
```

## Features
## Dashboard (FastHTML)

```
python -m app.server        # then open http://127.0.0.1:5100
```

The dashboard is a read-only page: it never changes your data, and viewing or
downloading a brief records nothing. It runs locally on `127.0.0.1` by default
and works fully offline — no CDNs, external calls, or fonts pulled from the
internet — in a light Microsoft-style theme.

Every tab shares the same header: desk score, open pipeline, and at-risk dollars
with week-over-week arrows (at-risk turns red as it rises), the violation counts,
and a severity-mix bar. The sidebar chooses the data source (a stored snapshot or
a CSV upload), sets the `as_of` evaluation date, and filters by owner, team,
stage, or severity — the headline metrics and team/region rollups always stay
desk-wide.

### Forecast call (landing tab)

Built to stand alone for a Monday meeting: the risky commits table — every
open commit/best_case deal carrying a risk flag, dollar-ranked, each with
the deterministic coaching prompt of its dominant rule — plus the
since-last-run summary line. Sidebar filters apply here too, so a
frontline manager can run her 9:00 call on just her team's risky commits.

![Forecast call tab](docs/screenshots/tab-forecast-call.png)

### Slippage

Push analytics from snapshot history: how often each deal's close date has
slipped and by how much, the H11 badges, a disqualification-review marker at
3+ pushes, and a small close-date drift sparkline per deal.

![Slippage tab](docs/screenshots/tab-slippage.png)

### Trajectory

Charts across stored snapshots: coverage (open vs required pipeline, with its
basis printed underneath) and the desk-score trend. Needs at least 2 snapshots;
with fewer it shows a short caption instead of a misleading one-point trend.
Below the charts, commit accuracy by the quarter each deal was first called
commit — so a later slip stays counted against the quarter it was promised for.

![Trajectory tab](docs/screenshots/tab-trajectory.png)

### Flow

Where the pipeline dollars went between the last two snapshots, and whether
enough new pipeline is being created. An open-pipeline waterfall (beginning +
created + increased − decreased − won − lost − removed = ending) that reconciles
exactly, pipeline-generation pacing per week against an optional target line, and
created-vs-closed bars per week. The same waterfall appears as a table on brief
page 1.

![Flow tab](docs/screenshots/tab-flow.png)

### Owners

Owner scoreboard (scores, pipeline dollars, coverage, small-n and low-coverage
flags), the forecast-integrity patterns (over- and under-calling), and the
per-owner commit-accuracy ledger — all marked "coaching signal, not a comp
input." Coverage uses the same basis as the desk headline, and `low_coverage`
means exactly under 1.00x, so the ratio and the flag can never disagree.

![Owners tab](docs/screenshots/tab-owners.png)

### Teams

Team and region roll-ups over the same open opps — owners, pipeline, quota,
coverage, violations, at-risk dollars — sorted worst-coverage first, so the order
itself carries the signal. When most groups trip the flag, each table notes it as
a desk-wide condition rather than "every team failing." Below the tables, a
per-team and per-region **coverage trend** across snapshots, plus commit accuracy
by team and region. Team/region membership comes from the quotas JSON; without
it, the tab shows a caption instead.

![Teams tab](docs/screenshots/tab-teams.png)

### Appendix

The full exception list — the only place it appears; drill-down, never the
headline — with streak annotations ("flagged N runs") and per-opp score
history sparklines, plus the validation report and the download-brief
button.

![Appendix tab](docs/screenshots/tab-appendix.png)

## Install & run

Two ways: local Python, or Docker (no Python needed).

### Option A — Docker (no local Python)

You still need the repo (Docker builds the image *from* it — the app code and demo data are copied in), but you don't install Python or any dependencies on your host:

```
bash
git clone https://github.com/Doogit/pipeline-hygiene.git
cd pipeline-hygiene

docker build -t pipeline-hygiene .
docker run -p 8000:8000 pipeline-hygiene   # open http://127.0.0.1:8000
```

### Option B — Local (Python 3.10+)

```
bash
git clone https://github.com/Doogit/pipeline-hygiene.git
cd pipeline-hygiene

# optional but recommended: isolate deps
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

That installs the runtime (pyyaml, pandas, python-fasthtml, altair). No services, no API keys, no LLM calls, and no Node/CSS build step — the Tailwind stylesheet ships prebuilt in `app/static`.

The dashboard reads from a local SQLite store that isn't committed, so load the bundled demo snapshots once before launching:

```bash
# bash / macOS / Linux
for f in data/snapshots/opps_*.csv; do python -m src.ingest "$f"; done
```

```powershell
# Windows PowerShell
Get-ChildItem data/snapshots/opps_*.csv | ForEach-Object { python -m src.ingest $_.FullName }
```

Then run it:

```bash
python -m app.server        # open http://127.0.0.1:5100
```

Ingesting the full four-snapshot series (not just one) is what makes the Trajectory, Slippage, and Flow tabs populate. To use real data instead, ingest your own CRM export — see [Bring your own CSV](#bring-your-own-csv).



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

## Implementation notes:

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

Recently implemented or unblocked:

- Org-specific backtesting: `python -m src.backtest` reports each rule's
  flagged opportunities joined to final observed outcomes from stored history.
- Seed series stage progression: `--progress-per-week` can now create
  mid-funnel movement for later conversion and dwell analytics.

Next session candidates (recorded, deliberately NOT built) — reviewed this
session against the code; all remain unimplemented:

- Aging thresholds derived from the org's own per-stage medians (1.5-2x
  median), replacing the static `aging_norm_days` in `config.yaml`.
- Dashboard explainability panel: rule + threshold + triggering snapshot
  values per flag (the anti-black-box wedge).
- Slack/email push delivery of the brief and digests (top 3-5 cap, weekly,
  digest not firehose — alert fatigue kills adoption).
- Cross-CRM connector via `stage_map` (original spec handoff option).
- Funnel / stage-transition analytics (deferred across earlier sessions):
  seed series can now opt into mid-funnel stage progression with
  `--progress-per-week`, so the next step is deriving conversion and dwell
  analytics from stored snapshots.

Start the next session by reading `README.md`, `SPEC.md`,
`data/seed_manifest.json`, and `data/delta_manifest.json`.
