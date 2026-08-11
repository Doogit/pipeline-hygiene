# pipeline-hygiene

A read-only sales pipeline inspection agent. This branch ingests opportunity
CSV snapshots into a SQLite snapshot store, runs deterministic hygiene rules
(H1-H10), and scores every opportunity, owner, and desk.

Operating principle: **agents inspect, people sell.** The agent never writes to
source data and never contacts sellers.

Design ethos: *a written rule is a suggestion; a gate is a control.* Every
hygiene rule is a deterministic, individually testable pure function with a
versioned threshold in `config.yaml` — the auditable complement to opaque
CRM-vendor deal-risk AI.

## Layout

```
src/            ingest.py, snapshots.py, rules.py, scoring.py, brief.py
src/seed/       org simulator: __main__.py, org.py, pathologies.py, series.py
app/            dashboard.py (read-only Streamlit dashboard)
data/           generated CSVs, seed_manifest.json, delta_manifest.json, pipeline.db
tests/          pytest + hypothesis; loads tests/config_test.yaml ONLY
out/            generated desk briefs (Task 5)
config.yaml     runtime thresholds, stage_map, rule weights
```

## Current Usage

```
python -m src.seed --rows 400 --as-of 2026-08-10        # single snapshot + ground-truth manifest
python -m src.seed --series 4 --as-of 2026-08-10        # weekly snapshots T0..T3 + delta manifest
python -m src.ingest data/opps_2026-08-10.csv           # validate + load into data/pipeline.db
python -m src.brief --as-of 2026-08-10 --quotas data/seed_manifest.json
                                                        # write out/desk_brief_2026-08-10.md
streamlit run app/dashboard.py                          # read-only dashboard
pytest -q                                               # full test suite
```

## Clock determinism

Every time-evaluating function takes an explicit `as_of: date`. `date.today()`
appears only in CLI entry points as the `--as-of` default.

## Epistemics

Handcrafted per-rule unit tests (exact boundaries: at threshold, one past,
empty/null) are the correctness oracle for the rules engine. The seed manifest
integration test proves engine↔generator consistency at scale — expected
violations in the manifest are constructed field-by-field by the generator,
never by running the rules engine, so the comparison is non-circular.

## Decisions

- The spec file in this repo is named `spec.md.md` (double extension as
  delivered); treated as SPEC.md.
- `tests/config_test.yaml` is content-identical to `config.yaml` plus the
  mandated frozen-header comment (a literal byte-identical copy could not carry
  the header).
- Seed does not mutate `config.yaml`. Generated per-owner quotas are written
  into `data/seed_manifest.json` under `"quotas"` (and owner metadata under
  `"owners"`); callers merge them into `config["quotas"]` at run time.
- Ingest selects a stage vocabulary via `--stage-map <name>` (default
  `default`) naming a key under `config.stage_map`.
- Single-snapshot seed writes `data/opps_<as-of>.csv`; ingest derives the
  snapshot date from the `opps_YYYY-MM-DD.csv` filename (override with
  `--snapshot-date`).
- Series mode: the **last** snapshot lands on `--as-of`, spaced 7 days
  (`--series 4` → as_of-21 … as_of). Series writes `data/snapshots/` and
  `data/delta_manifest.json`; single mode writes `data/seed_manifest.json`.
- Validation strictness: `created_date`, `close_date`, `last_activity_date`
  must be non-empty ISO dates (only `next_step_date` and optional columns may
  be empty per spec); `contact_count` must parse as an integer (row rejected
  otherwise); missing required *columns* are fatal (nonzero exit), invalid
  *rows* are rejected into the ValidationReport.
- Uniform-but-unexpected currency (e.g. all EUR when `expected_currency: USD`)
  is a ValidationReport warning; **mixed** currency is fatal (nonzero exit) per
  spec.
- Insufficient-history threshold ("fewer than 2 snapshots") is counted over
  snapshots stored at-or-before the evaluated snapshot date, store-wide.
- The per-snapshot ValidationReport is persisted as `validation_json` on the
  `snapshots` table (written at ingest; older dbs are migrated in place), which
  is how the brief and dashboard surface it without re-running validation.
- Fiscal quarters are labeled `FY<year>-Q<n>` where the fiscal year is named
  for the calendar year it ends in (with `fiscal_year_start_month: 7`,
  2026-08 falls in FY2027-Q1, Microsoft-style).
- The brief CLI takes `--quotas <json>` (e.g. `data/seed_manifest.json`) and
  merges its `"quotas"` mapping into `config["quotas"]` at run time, per the
  seed-does-not-mutate-config decision above.
- Since-last-run semantics mirror the delta manifest: violations that vanish
  because a deal closed are reported under "Closed", never under "Cleared";
  newly appearing opps are listed under "Opps added" and their violations are
  not counted as new violations on tracked opps. Each brief run records its
  per-opp rule sets in the `runs` table; the next run diffs against that.
- The golden brief (`tests/golden/desk_brief_golden.md`) is compared exactly;
  `.gitattributes` disables CRLF conversion for it. To regenerate: delete it
  and run the test once (it recreates the file and fails asking for review).
- Dashboard: `as_of` defaults to the selected snapshot's date (no
  `date.today()` outside CLI defaults); an uploaded CSV runs the same ingest
  validation but is evaluated in memory and never written to the store; the
  download-brief button renders from the displayed data and does not record a
  run; quotas auto-merge from `data/seed_manifest.json` when present. Runtime
  paths can be overridden with `PIPELINE_HYGIENE_CONFIG`,
  `PIPELINE_HYGIENE_DB`, and `PIPELINE_HYGIENE_QUOTAS` for isolated tests or
  alternate deployments.
- In series evolution, fields of unscripted rows are shifted week-over-week
  (activity/next-step dates +7) so their expected violation sets are invariant
  by construction; only scripted deltas change expectations, including
  bookkept couplings (a close-date push increments `close_date_changes` and
  may introduce H3 — recorded in the delta manifest).
- H11 ("lost deal control") fires on `max_push_days >= push_alarm_days` OR
  `cumulative_extension_days >= cumulative_push_alarm_days`, always high
  severity (weight 20). Push derivations (`push_count`,
  `cumulative_extension_days`, `max_push_days`) are history-only — computed
  from consecutive stored-snapshot pairs where close_date moved LATER
  (pull-ins excluded), never from a CSV column. Evidence framing: Gong
  (n=13,439) shows won deals update close dates MORE than lost, so frequent
  small updates must not trip anything; only serial/large later-drift does.
- H11 has no `insufficient_history` state: with fewer than 2 snapshots there
  are zero observed transitions, so push stats are genuinely 0 (unlike
  H3/H6, no source column can ever supply them) and the rule is simply
  silent. Rows evaluated outside the store (property tests, in-memory
  uploads) carry no push keys and are also silent. A single-seed manifest
  therefore provably contains no H11 (asserted in tests).
- Series mode scripts one BIG push per week (>= `push_alarm_days`, the only
  H11 source, recorded under `introduced`); regular pushes are sized below
  the single-push alarm and budgeted per-opp below the cumulative alarm so
  they can never introduce H11 uncontrolled.
- The brief's "Slipping pipeline" section lists open opps with >= 1 observed
  push, dollar-ranked with distinct-opp totals, and marks
  `push_count >= disqualify_review_pushes` with "recommend disqualification
  review".
- Brief page 1 (forecast-call prep) runs: Headline (+Validation), Risky
  commits, Trajectory, Since last run (summary counts), Slipping pipeline
  (kept on page 1 after the four mandated sections — slippage is the
  research's #1 ranked signal). Everything else sits under "## Appendix"
  with headings demoted to `###` (heading text preserved verbatim so
  existing section assertions still match); the top-10 exceptions table is
  preserved as-is there, and the FULL exception list lives as the
  dashboard's appendix drill-down.
- Risky commits = open opps with forecast commit/best_case carrying any of
  H1/H2/H4/H5/H7/H11, dollar-ranked, capped at 10 with the total always
  stated. Each gets the fixed coaching prompt of its dominant rule
  (deterministic: worst severity, then heaviest rule weight, then lowest
  rule number) — questions to ask the seller, never gotchas.
- Trajectory: created-vs-closed flow comes from the since-last-run delta
  (added vs closed opps, won/lost split, counts + dollars). Coverage uses
  required multiple = 1 / trailing win rate over stored closed outcomes (an
  opp counts as an outcome when its last stored row is closed_won/lost);
  with fewer than `min_closed_for_win_rate` outcomes (or zero wins) it
  falls back to `coverage_ratio_min`. The basis used is always printed.
  Remaining quota = sum of configured quotas minus closed-won dollars whose
  close_date falls in the current fiscal quarter of as_of.
- Forecast-integrity patterns (src/patterns.py) — a coaching signal, never a
  comp input (the disclaimer renders wherever shown). Overcall = share of an
  owner's ever-commit opps subsequently pushed (later close-date move in a
  snapshot pair after the first commit snapshot) or closed_lost. Undercall =
  share of observed wins never commit/best_case before the winning snapshot
  (only wins seen open in an earlier snapshot count — an outcome with no
  pre-win history is uninformative), OR the conjunction of omitted-dollar
  share AND far-out-dollar share of open pipeline (conjunction because
  omitted share alone is dominated by single-big-deal noise at per-owner n).
  Metrics report their n; below `min_opps_for_owner_score` they are
  suppressed as small_n, never flagged.
- The persona-recovery test needed persona-DRIVEN series evolution: a
  uniformly random push/close schedule carries zero per-persona signal, so
  no series length could separate happy_ears from clean operators. The
  simulator now preferentially pushes (and skews to closed_lost) the
  commit-forecast deals of happy-ears sellers — plant is field-level
  behavior, detection is statistics over snapshots, and the detector never
  sees persona labels, so plant-vs-detect stays non-circular. Coupling
  bookkeeping (H3/H11, delta manifest) is unchanged.
- Flag streaks count consecutive RUNS (from the `runs` table), not
  snapshots: the current evaluation plus immediately preceding recorded
  runs carrying the same (opp, rule); a cleared run resets the streak.
  Re-running the brief on the same snapshot therefore extends streaks —
  runs are the accountability cadence. Annotated as "flagged N runs" (from
  N >= 2) in the brief exceptions table and the dashboard.
- `python -m src.brief --digests` writes one PRIVATE coaching digest per
  owner with open opps to `out/digests/<as_of>/<owner_slug>.md`: top 3-5
  dollar-weighted risks, that owner's week-over-week new/cleared/closed,
  longest-unresolved flags (streaks), and exactly ONE suggested coaching
  focus (deterministic: rule with most dollars at risk, ties to the lowest
  rule number). No other owner's data, no rankings, small_n carried over —
  coaching evidence favors private weekly digests; published rankings
  raise attrition.

- Dashboard tabs mirror the brief structure (Forecast call landing,
  Slippage, Trajectory, Owners, Appendix) rather than inventing a second
  information architecture; each tab is designed to fit one screen. Charts
  are Streamlit built-ins + bundled Altair only, with explicit pixel sizes
  and right-side legends: container-sized charts collapse when rendered
  inside an initially hidden tab, and `alt.Legend(orient="bottom")`
  collapses the plot area under Streamlit's Vega theme (both verified
  empirically). Severity palette is Okabe-Ito (colorblind-safe) everywhere;
  colored text chips (:red[] etc.) instead of emoji.
- Task 12 disclosure: `tests/test_dashboard.py` (added during PR #2 review;
  not one of the original 50) asserted the pre-redesign layout literally
  (exactly 5 metrics, exactly 2 dataframes), which cannot coexist with the
  mandated tabbed redesign. It was rewritten to assert the same semantics
  (store loads, owner filter narrows the exceptions table, zero
  exceptions) plus per-tab element presence and graceful single-snapshot
  degradation. No other pre-existing test was modified beyond goldens and
  additive config keys.
- Dashboard screenshots in `docs/screenshots/` were captured with the
  locally installed Playwright driving the headless app on 127.0.0.1 —
  a verification tool only, not a project dependency.

## Handoff

Next session candidates (recorded, deliberately NOT built this session):

- Aging thresholds derived from the org's own per-stage medians (1.5-2x
  median), replacing static `aging_norm_days`.
- Org-specific backtesting: a flagged-vs-outcome table from the org's own
  stored history — turns vendor benchmark stats into auditable org
  evidence.
- Dashboard explainability panel: rule + threshold + triggering snapshot
  values per flag (the anti-black-box wedge).
- Slack/email push delivery of the brief and digests (top 3-5 cap, weekly,
  digest not firehose — alert fatigue kills adoption).
- Cross-CRM connector via `stage_map` (original spec handoff option).

Start the next session by reading `README.md`, `data/seed_manifest.json`,
and `data/delta_manifest.json`.
