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
python -m src.brief --as-of 2026-08-10 --quotas data/seed_manifest.json --digests
                                                        # ...plus out/digests/<as_of>/<owner>.md
python -m src.brief --as-of 2026-08-10 --quotas data/seed_manifest.json --team "Team EMEA-1"
                                                        # filtered brief for one team (repeatable
                                                        # --team/--owner/--region, case-insensitive;
                                                        # writes a suffixed file, never records a run)
streamlit run app/dashboard.py                          # read-only dashboard
pytest -q                                               # full test suite
```

## Bring your own CSV

The simulator above is a demo. To run against a real CRM export, produce a CSV
with these **required** columns (extra columns are ignored):

`opp_id, account, opp_name, owner, stage, amount, currency, created_date,
close_date, last_activity_date, next_step, next_step_date, forecast_category,
contact_count, product_line`

- Optional history columns (`stage_entered_date`, `close_date_changes`) are
  derived from stored snapshots when absent; with a single snapshot the
  dependent rules (H3, H6) report `insufficient_history` rather than firing.
- `forecast_category` must be one of `pipeline, best_case, commit, omitted`.
  Dates are ISO (`YYYY-MM-DD`). Amount may be blank (H8 catches it).
- `stage` is your CRM's own vocabulary; map it to the canonical stages
  (`prospect, qualify, develop, propose, commit, closed_won, closed_lost`) by
  adding a block under `stage_map:` in `config.yaml` and passing its name:
  `python -m src.ingest your_export.csv --stage-map your_map`. An unknown stage
  is rejected per-row with the offending label named — it is never guessed.
- Set `expected_currency` in `config.yaml`; a uniform but unexpected currency
  warns, mixed currency in one file is fatal.

Ingest is safe to schedule: it exits nonzero (and stores nothing) if every row
is rejected, prints the rejected rows with reasons, and names the snapshot date
from the filename (`opps_YYYY-MM-DD.csv`) or `--snapshot-date`.

```
python -m src.ingest your_export.csv --stage-map your_map
python -m src.brief --as-of 2026-08-10 --quotas your_quotas.json --digests
```

## Persona pass

`.claude/skills/persona-pass` runs a usability simulation: it role-plays each
user persona in [docs/personas.md](docs/personas.md) (frontline manager,
RevOps analyst, VP, flagged AE) against the real product to surface usability
and product gaps that unit tests miss. Run it before a release or after any
change to the brief, digests, dashboard, or CLI.

## Dashboard (Streamlit)

```
streamlit run app/dashboard.py
```

The dashboard is a read-only Streamlit app whose tabs mirror the brief
structure — it never writes to the store or to source data, and viewing or
downloading a brief from it does not record a run. Every view shares the
same headline: an `st.metric` row with week-over-week delta arrows wired to
since-last-run (desk score, open pipeline, at-risk dollars — at-risk uses
`delta_color="inverse"` so rising risk reads red), the violation counts as
colored text, and the severity mix as a compact stacked bar. The sidebar
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
3+ pushes, and a per-opp close-date drift sparkline
(`st.column_config.LineChartColumn`).

![Slippage tab](docs/screenshots/tab-slippage.png)

### Trajectory

Altair charts across stored snapshots: coverage (open vs required pipeline
at 1 / trailing win rate, with the basis printed underneath),
created-vs-closed weekly flow bars, and the desk score trend from recorded
brief runs. Needs at least 2 stored snapshots; with fewer it degrades to a
clear caption instead of a one-point trend. Below the charts: commit
accuracy by committed-for quarter (the fiscal quarter of the close date
when the deal was first called commit — immune to later pushes, so a
slipped commit stays counted against the quarter it was promised for).

![Trajectory tab](docs/screenshots/tab-trajectory.png)

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
first so ordering itself carries the signal, plus commit accuracy by team
and region. Team/region membership comes from the `owners` block of the
`--quotas`/`PIPELINE_HYGIENE_QUOTAS` JSON (the seed manifest already
carries it); without that metadata the tab degrades to a clear caption.

![Teams tab](docs/screenshots/tab-teams.png)

### Appendix

The full exception list — the only place it appears; drill-down, never the
headline — with streak annotations ("flagged N runs") and per-opp score
history sparklines, plus the validation report and the download-brief
button.

![Appendix tab](docs/screenshots/tab-appendix.png)

Implementation notes:

- Data source: the latest stored snapshot from `data/pipeline.db` by
  default (snapshot selectable in the sidebar); an uploaded CSV runs the
  same ingest validation and is evaluated entirely in memory. Sidebar
  owner/stage/severity filters apply to the Slippage, Owners, and Appendix
  tables.
- Tables use `st.column_config` throughout: `ProgressColumn` for 0-100
  scores, `NumberColumn` `$%d`-style formatting for amounts,
  `LineChartColumn` sparklines for close-date drift and score history,
  plain text badges for rules (Okabe-Ito colorblind-safe severity palette;
  colored text, no emoji).
- Charts are Streamlit built-ins + the Altair that ships with Streamlit —
  no extra dependencies.
- Runtime paths can be overridden with `PIPELINE_HYGIENE_CONFIG`,
  `PIPELINE_HYGIENE_DB`, and `PIPELINE_HYGIENE_QUOTAS` (used by the
  `streamlit.testing.v1.AppTest` suite in `tests/test_dashboard.py` to run
  the app against isolated fixtures).
- Source screenshots live in `docs/screenshots/` (captured from the app
  running headless on 127.0.0.1 against the committed series data).

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
  2026-08 falls in FY2027-Q1).
- The brief CLI takes `--quotas <json>` (e.g. `data/seed_manifest.json`) and
  merges its `"quotas"` mapping into `config["quotas"]` at run time, per the
  seed-does-not-mutate-config decision above. When the same file carries an
  `"owners"` block (`{owner: {team, region, ...}}`), it also feeds
  `config["owner_meta"]` for the team/region rollups — one file, one flag,
  no schema change. For your own data:
  `{"quotas": {"Ada Lovelace": 900000}, "owners": {"Ada Lovelace":
  {"team": "Team North", "region": "EMEA"}}}`.
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
  run; quotas and team/region metadata auto-merge from
  `data/seed_manifest.json` when present. Runtime
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
- Owner and team/region coverage share the desk headline's basis
  (`scoring.required_coverage_multiple` is the single source of truth): the
  shown ratio is open pipeline / (remaining quota x required multiple) and
  `low_coverage` fires exactly when it is under 1.00x — the ratio and the
  flag can never contradict each other, and the definition + basis line
  renders adjacent to every table that carries a Coverage column. (The
  persona pass caught the first cut showing raw pipeline/quota beside a
  flag computed on the remaining-quota basis: "Coverage 1.18" next to
  `low_coverage` read as a broken flag.)
- Team/region rollups: the `--quotas` JSON's `owners` block (owner ->
  {team, region}, already in the seed manifest) feeds `owner_meta`; Teams
  and Regions tables render in the brief appendix and the dashboard Teams
  tab, worst coverage first, with roster-summed quota so group coverage
  stays comparable to per-owner coverage. Owners with a quota but no open
  opps still count toward their group's required pipeline. No metadata ->
  a clear "not configured" note, never a guess.
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

- Commit accuracy (forecast ledger): of opps ever forecast `commit` in
  stored history (`src/patterns.commit_ledger`), one mutually exclusive
  outcome to date — won / lost / pushed (still open with a later
  close-date move after the first commit snapshot) / still open — so
  shares sum to 100%. The committed-for quarter anchors to the close date
  at the FIRST commit snapshot, so a later push can never move a
  commitment between quarters. Rolled up by owner, team, and quarter in
  the brief appendix and the dashboard Owners/Teams/Trajectory tabs;
  tables sort alphabetically/chronologically, deliberately NOT worst-first
  (an accuracy leaderboard is one sort away from a comp weapon), and
  always carry the coaching-signal disclaimer. Recomputed
  deterministically from the store on every run — nothing persisted,
  nothing to drift. "Won/resolved" counts closed opps only, shown as a
  won/closed fraction; the percentage renders only once
  `min_opps_for_owner_score`+ commits have resolved (both persona sims
  independently hit a bare "100%" on n=1 — exactly the number that gets
  screenshot into a leaderboard). Each private digest carries its owner's
  own ledger row ("Of your N ever-commit deals ...") so a flagged seller
  sees the same numbers, and nothing more, that the brief shows.
- Coverage basis strings carry the exact fraction ("trailing win rate
  20/32 closed won (62.5%) -> required multiple 32/20 = 1.60x") so a
  reader can reproduce required pipeline to the dollar; a rounded
  multiple alone broke the napkin check by ~$285K in the persona sim
  (Dana's "math she has to trust rather than see").
- Filtered brief: `--owner`/`--team`/`--region` (repeatable, combinable)
  restrict the whole brief to a selection; team/region membership comes
  from the `--quotas` `owners` block. Matching is case-insensitive and
  the "Team " prefix is optional (`--team na-east-1` finds
  `Team NA-East-1`); unknown names are errors listing the known values,
  never guesses. Every rollup and dollar figure recomputes over the
  selection (headline says "Selection score"), EXCEPT the required
  coverage multiple, which stays desk-wide
  (one coverage basis everywhere: a team's filtered coverage equals its
  row in the unfiltered Teams table). Filtered briefs are marked
  "FILTERED BRIEF" under the title, are never recorded in the `runs`
  table (a partial open-opp map would corrupt flag streaks and
  since-last-run for later full briefs), and write to
  `desk_brief_<as_of>_<filter-slug>.md` so the canonical brief survives.
  The previous run's desk score is dropped from a filtered "since last
  run" (it was desk-wide, not comparable), and opps that left the
  snapshot cannot be owner-attributed, so a filtered "removed" list is
  always empty. `--digests` under a filter writes digests for the
  selection only (content identical to unfiltered digests).
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
