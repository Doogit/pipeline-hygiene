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

## Handoff

Next session: start by reading `README.md`, `data/seed_manifest.json`, and
`data/delta_manifest.json`.
