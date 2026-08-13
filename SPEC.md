# Build: pipeline-hygiene — a read-only sales pipeline inspection agent (v2)

## Constraints
- New repo `pipeline-hygiene`. Create branch `feat/mvp-rules-engine` before any code.
- Vendor-neutral core: no MSX, Dataverse, Salesforce, or HubSpot dependencies. Ingestion is CSV against the schema below; CRM connectors are future sessions. A config-driven `stage_map` (not code) handles vocabulary differences between CRMs.
- The agent is **read-only** over pipeline data. It inspects and reports. It never writes to source data and never contacts sellers directly. Operating principle: **agents inspect, people sell.**
- Deterministic rules engine first. **No LLM calls in Tasks 1–6.** Task 7 is feature-flagged (`LLM_ENABLED=1`) and skippable.
- **Clock determinism:** every function that evaluates time takes an explicit `as_of: date` parameter. `date.today()` may appear only in CLI entry points as the default for `--as-of`. No exceptions — rules, scoring, seed, brief, dashboard all thread `as_of` through.
- **Test config isolation:** tests load `tests/config_test.yaml` (frozen copy of defaults), never the repo `config.yaml`. Editing runtime config must never break tests.
- Synthetic data only. No real customer, employer, or colleague names anywhere in the repo.
- Stack: Python 3.11+, SQLite, FastHTML + htmx (UI), pytest + hypothesis. No paid services for the MVP.
- If session context strains, stop cleanly after Task 3, commit, and use the Handoff section — Tasks 4–6 run fine in a fresh session.

## Objective
Working MVP that ingests opportunity CSV snapshots into a snapshot store, runs deterministic hygiene checks, scores every opportunity and owner, and produces: (a) an exceptions report, (b) a dated "desk brief" markdown digest with run-over-run deltas, (c) a read-only FastHTML dashboard. Done = every verification check passes against simulated data with a per-opportunity ground-truth manifest.

## Context
Sales desks lose deals to hygiene failures — stale opportunities, slipped close dates, vague next steps, forecast categories that contradict stage reality. Managers usually discover these at the forecast call, which is too late. This agent finds them continuously, before the call.

Design ethos: *a written rule is a suggestion; a gate is a control.* Every hygiene rule is a deterministic, individually testable function — not advice in a doc.

Positioning: CRM vendors now ship opaque AI deal-risk features. This tool is the deterministic, auditable complement — every flag traces to a versioned rule with a testable threshold. That is the differentiator; protect it.

Nothing exists yet. This session builds from zero.

## Data Model

### Required CSV columns

| column | type | notes |
|---|---|---|
| opp_id | TEXT | primary key within a snapshot |
| account | TEXT | synthetic company name |
| opp_name | TEXT | |
| owner | TEXT | seller name (synthetic) |
| stage | TEXT | source vocabulary; normalized via `stage_map` at ingest |
| amount | REAL | 0/null allowed (H8 catches it) |
| currency | TEXT | must be uniform per snapshot (see ingest validation) |
| created_date | DATE | |
| close_date | DATE | |
| last_activity_date | DATE | |
| next_step | TEXT | free text, may be empty |
| next_step_date | DATE | may be empty |
| forecast_category | TEXT | canonical enum: pipeline, best_case, commit, omitted (matches Dynamics/Salesforce defaults) |
| contact_count | INTEGER | multithreading proxy |
| product_line | TEXT | |

### Optional CSV columns (real CRM exports usually lack these)

| column | type | fallback when absent |
|---|---|---|
| stage_entered_date | DATE | derived from snapshot history: first stored snapshot date at which the current stage was observed |
| close_date_changes | INTEGER | derived from snapshot history: count of snapshot-to-snapshot transitions where close_date changed |

If a column is absent AND fewer than 2 snapshots exist, the dependent rules (H3, H6) return status `insufficient_history` for that opportunity — reported in the brief, never counted as a violation and never silently skipped.

### Canonical stages
`prospect, qualify, develop, propose, commit, closed_won, closed_lost`. Ingest maps source stage labels onto these via `config.stage_map`. Unknown labels are validation errors, not guesses.

**Open opportunity** = stage not in {closed_won, closed_lost} after mapping. All hygiene rules apply to open opportunities only. **Open pipeline** = sum of amount over open opportunities.

### config.yaml
```yaml
staleness_days: {commit: 7, propose: 14, develop: 21, qualify: 30, prospect: 45}
aging_norm_days: {prospect: 30, qualify: 30, develop: 45, propose: 30, commit: 21}
big_deal_threshold: 100000
close_date_horizon_days: 270
fiscal_year_start_month: 7        # July fiscal-year start; configurable per org
expected_currency: USD
coverage_ratio_min: 3.0
quotas: {}                        # owner -> quarterly quota; seed fills this
rule_weights: {H1: 15, H2: 20, H3: 10, H4: 20, H5: 25, H6: 10, H7: 10, H8: 5, H9: 5, H10: 10}
healthy_score_threshold: 80
min_opps_for_owner_score: 5       # below this, owner score carries a small-n flag
next_step_quality:
  min_chars: 15
  filler_phrases: ["follow up", "touch base", "check in", "circle back", "tbd", "pending"]
  action_verbs: ["send", "schedule", "present", "review", "confirm", "deliver", "meet", "call", "demo", "negotiate", "sign", "propose"]
stage_map:
  default: {prospect: prospect, qualify: qualify, develop: develop, propose: propose, commit: commit, closed_won: closed_won, closed_lost: closed_lost}
  # example alternate vocabulary a Dynamics/MSX export might carry:
  dynamics_default: {Qualify: qualify, Develop: develop, Propose: propose, Close: commit, Won: closed_won, Lost: closed_lost}
```

## Hygiene Rules (implemented in Task 4)

Shared predicate in `src/rules.py`, used by H4 and H5 so neither duplicates the other:
```python
def has_valid_next_step(row, as_of) -> bool:
    # next_step non-empty AND next_step_date present AND next_step_date >= as_of
```

Each rule = a pure function `(row, config, as_of) -> Violation | None | InsufficientHistory`. Violation = (rule_id, severity, detail). IDs are stable — tests reference them. Rules never call other rules.

| ID | Rule | Trigger | Severity |
|---|---|---|---|
| H1 | Stale by stage | days(as_of − last_activity_date) > staleness_days[stage] | high if stage in commit/propose, else medium |
| H2 | Close date in past | close_date < as_of | high |
| H3 | Serial slippage | close_date_changes ≥ 2 | high if ≥3 changes OR stage = commit, else medium |
| H4 | Missing/expired next step | NOT has_valid_next_step(row, as_of) | high |
| H5 | Forecast mismatch | forecast_category = commit AND (stage in prospect/qualify/develop OR NOT has_valid_next_step(row, as_of)) | high |
| H6 | Aging in stage | days(as_of − stage_entered_date) > aging_norm_days[stage] | medium |
| H7 | Single-threaded big deal | contact_count < 2 AND amount ≥ big_deal_threshold | medium |
| H8 | Amount hygiene | amount null or ≤ 0 | low |
| H9 | Vague next step | has_valid_next_step is true, BUT text fails quality heuristic: len < min_chars OR lowercased text contains a filler_phrase OR contains no action_verb | low |
| H10 | Parked close date | close_date > as_of + close_date_horizon_days | medium if forecast_category in (commit, best_case), else low |

### Scoring
- Opp score: start 100, deduct rule_weights per violation, floor 0.
- Owner: mean AND median of opp scores, n(open opps), violation count, open pipeline $, coverage ratio (open pipeline / quota) with flag when < coverage_ratio_min. Owners with n < min_opps_for_owner_score carry a `small_n` flag everywhere they're displayed.
- Desk: amount-weighted mean, unweighted median, and % of open opps with score ≥ healthy_score_threshold. Report all three — a single big clean deal must not be able to mask a rotten tail.
- **At-risk dollars** = sum of amount over **distinct** open opps having ≥1 high-severity violation. Never sum per violation.

## Task 1: Scaffold + config
- Layout: `src/` (`ingest.py`, `snapshots.py`, `rules.py`, `scoring.py`, `brief.py`), `src/seed/` (`__main__.py`, `org.py`, `pathologies.py`, `series.py`), `app/server.py`, `app/render.py`, `app/static/`, `data/`, `tests/`, `out/`, `config.yaml`, `tests/config_test.yaml`, `README.md`.
- `config.yaml` per the block above; `tests/config_test.yaml` is a byte-identical frozen copy with a header comment: "Frozen for tests. Do not edit to make tests pass."

## Task 2: Org simulator seed (`python -m src.seed`)
Not a random-row generator — an org simulator with ground truth.

### 2A: Organization
- 3-level hierarchy: 2 segments (enterprise, mid-market) → 4 regions → 8–12 teams. 60 sellers with quotas by segment. Deal amounts log-normal per segment. Close dates cluster toward fiscal quarter ends (hockey stick). 200–1,000 opportunities (`--rows`, default 400). All dates relative to `--as-of` (default today), which is written into the manifest as `generated_as_of`.
- Seller personas drive field patterns: **clean operator**, **sandbagger** (far-out close dates, omitted-heavy), **happy-ears** (commit early, slips), **ghost** (stale everything).

### 2B: Pathology injectors + manifest (the critical part)
- Each injector plants a specific violation set on specific rows: zombie pipeline, serial-slippage cohort, quarter-end date pileup, single-threaded whales, forecast/stage contradictions, bulk quarter-start creation, vague next steps, parked dates.
- **Planted ≠ detected unless controlled.** For every generated row, the generator explicitly sets clean values for every rule-relevant field NOT in that row's target violation set. A row planted for H2 must not accidentally trip H4.
- Manifest `data/seed_manifest.json`: `{"generated_as_of": "...", "expected": {"OPP-0001": ["H2"], "OPP-0002": ["H1","H4","H5"], ...}}` plus aggregate per-rule counts computed from `expected`. Expected sets are constructed field-by-field, never by running the rules engine (that would be circular).
- Some rows fully clean (score 100); some stack 3+ violations.
- Epistemics note for README: handcrafted per-rule unit tests are the correctness oracle; the manifest integration test proves engine↔generator consistency at scale.

### 2C: Longitudinal series
- `python -m src.seed --series 4` emits weekly snapshots T0…T3 (`data/snapshots/opps_YYYY-MM-DD.csv`) with a controlled delta script: clear X violations, introduce Y, close Z deals, push N close dates. Writes `data/delta_manifest.json` recording exact expected deltas between consecutive snapshots.
- Same seed + same `--as-of` → byte-identical output (seeded RNG; assert in tests via file hash).

## Task 3: Ingest, validation, snapshot store
### 3A: Validation (`src/ingest.py`)
Runs before anything else. Checks: required columns present; stage labels resolve through `stage_map` (unknown → reject row); forecast_category in enum; dates parse ISO; amount numeric or empty; duplicate opp_id within snapshot → reject later duplicates. Output: accepted rows + `ValidationReport` (counts + per-row reasons), persisted with the run and surfaced in brief + dashboard. **Mixed currency in one snapshot: exit nonzero with a clear error.** A hygiene tool that silently mis-parses produces false violations and dies of distrust.

### 3B: Snapshot store (`src/snapshots.py`, SQLite `data/pipeline.db`)
- Tables: `snapshots` (snapshot_date, source_file, row_count), `opportunities` (full row per snapshot, keyed snapshot_date + opp_id), `runs` (run summaries for since-last-run deltas).
- Derivations when optional columns absent: `close_date_changes` = count of consecutive-snapshot pairs where close_date differs; `stage_entered_date` = earliest contiguous snapshot date at which the current stage was observed. Source columns, when present, take precedence.
- This store is the architecture that lets future CRM connectors work with plain exports — real exports don't carry field history.

## Task 4: Rules engine + scoring
- One pure function per rule in `src/rules.py`, shared predicate as specced, thresholds from config. `src/scoring.py` implements the scoring block above, including distinct-opp at-risk dollars and small-n flags.
- Tests (all against `tests/config_test.yaml`):
  - Per-rule boundary units: exactly at threshold, one day past, empty/null fields. These are the oracle.
  - Manifest consistency: engine violations over seed CSV at `generated_as_of` == manifest `expected`, compared per-opp; print side-by-side per-rule counts.
  - Property tests (hypothesis): (1) arbitrary well-typed rows never raise; (2) monotonicity — degrading any single field (older activity date, more slips, emptier next step) never increases an opp score.
  - Insufficient-history: single snapshot, no optional columns → H3/H6 report `insufficient_history`, zero violations.

## Task 5: Desk brief (`python -m src.brief --as-of ...`)
Writes `out/desk_brief_YYYY-MM-DD.md`:
- Headline: desk amount-weighted mean, median, % opps ≥ healthy threshold; at-risk dollars (distinct); violation counts by severity; validation summary (rows accepted/rejected + top reasons).
- Fiscal segmentation: at-risk dollars and H5 list grouped by fiscal quarter of close_date (from `fiscal_year_start_month`).
- Top 10 exceptions ranked severity then amount, rule badges + one-line detail.
- Per-owner table: mean, median, n (+small_n flag), open pipeline $, coverage ratio + flag, violation count.
- Forecast-integrity section: every H5 individually.
- Since last run: from `runs` + snapshot store — new violations, cleared violations, score change, opps closed/added.
- Golden-file snapshot test: fixed seed + fixed `as_of` → brief matches `tests/golden/desk_brief_golden.md` exactly.

## Task 6: FastHTML dashboard
- `python -m app.server`: loads latest snapshot from store by default; CSV upload runs the same ingest validation. Filters: owner, stage, severity (an opp appears under each severity it carries; filter semantics documented in-app). Exception table with rule badges; owner scoreboard with small-n flags; desk headline; validation report view; download-brief button.
- Strictly read-only: no edit affordances of any kind.

## Task 7 (feature-flagged — skip unless LLM_ENABLED=1): LLM layer
- Exactly two functions in `src/llm.py`:
  1. Next-step quality grade — refinement over H9's heuristic, one-line reason.
  2. Draft nudge per exception, addressed to the manager to review and send themselves. Never sent automatically; always labeled DRAFT.
- Provider via `ANTHROPIC_API_KEY`; degrade gracefully to "LLM disabled" if unset.

## Pre-flight Checks
- [ ] `python --version` → 3.11+. If lower: stop and report.
- [ ] `git status` clean on a fresh repo; branch `feat/mvp-rules-engine` created.
- [ ] `pip install -r requirements.txt` succeeds. If not: stop and report.

## Verification Checklist
- [ ] All tests pass: `pytest -q` → 0 failures (boundary, manifest consistency, property, insufficient-history, golden brief, ingest validation).
- [ ] Determinism: run `python -m src.seed --rows 400 --as-of 2026-08-10` twice → `sha256sum` of CSV + manifest identical across runs.
- [ ] Manifest consistency output: integration test prints per-rule engine counts vs manifest side by side → identical.
- [ ] Series + deltas: `python -m src.seed --series 4` then ingest all snapshots and run brief per snapshot → "since last run" section of each brief matches `data/delta_manifest.json` exactly.
- [ ] Validation: fixture with unknown stage, bad date, duplicate opp_id → rows rejected with reasons in ValidationReport; fixture with mixed currency → ingest exits nonzero with clear message.
- [ ] Insufficient history: single snapshot without optional columns → brief shows H3/H6 as insufficient_history, not violations.
- [ ] Brief generates: `python -m src.seed && python -m src.brief` → `out/desk_brief_*.md` contains "Desk score", validation summary, fiscal-quarter section, per-owner table, forecast-integrity section.
- [ ] Perf: `python -m src.seed --rows 50000` then full ingest + rules + brief completes < 60s on this machine (report actual).
- [ ] Dashboard: `python -m app.server` loads store data; owner/stage/severity filters change the table; no write/edit controls anywhere.
- [ ] No real-world data: `grep -ri` the repo for anything that isn't a synthetic name → nothing found.

## Final Output
Return: file tree, test output, determinism hashes, one generated desk brief pasted inline, dashboard screenshot, branch name + commits, anything unverified and why, recommended next step.

## Handoff to Next Session
Next session options (pick one): a CRM connector mapping a real export (Salesforce report CSV or a Dynamics/MSX-style export exercising `stage_map`) onto this schema via the snapshot store, or the public write-up ("Agents inspect, people sell: building a read-only pipeline inspector") for the blog.
Start the next session by reading `README.md`, `data/seed_manifest.json`, and `data/delta_manifest.json`.
