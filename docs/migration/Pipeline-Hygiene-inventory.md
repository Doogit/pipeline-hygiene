# Task 1 — Pipeline-Hygiene `st.*` inventory (authoritative section/element map)

Source: `app/dashboard.py` (851 lines). This inventory is the **authoritative source of
section/element grouping** that Task 3 replicates. Line numbers are against the current file on
`feat/fasthtml-ui-migration` (= `origin/main` @ `baec121`).

## Finding 1 (recorded per plan §4.1) — the page is ENTIRELY read-only

Grep evidence on `app/dashboard.py`:

| Probe | Count |
|---|---|
| `st.session_state` | **0** |
| `@st.cache_*` / `st.cache_*` | **0** |
| `st.data_editor` / `text_input` / `text_area` / `number_input` / `st.form` | **0** |

The only inputs are **read-only selectors** (1 file_uploader, 2 selectbox, 1 date_input, 4 multiselect)
and 1 download_button. **Nothing on the page is edited.** Consequence (plan §4.1): the template's
"editable fields must survive filter changes / confirm-before-regenerate dialog" requirement is
**NOT APPLICABLE**. No confirm-dialog is added (plan §6). ~118 genuine `st.*` UI call sites total
(the raw `st.` grep of 120 includes 2 false hits inside the string `"…manifest.json"`).

## Parity-golden design note (so Task 1 supports the non-circular gate, plan §10 Task 4)

Every row below is tagged **[V]** or **[S]**:

- **[V] value/string-bearing** — the text/number is computed from data. In the port these live in the
  **view model** (`src/pipeline_hygiene_view.py`) and are what Task 4 freezes from the **live Streamlit
  `AppTest` render** and diffs. Formatting (currency, `%`, `.1f`, `+/-`) happens once, in the view model.
- **[S] static label/chrome** — fixed text (tab names, column headers, section titles) that lives directly
  in the FastHTML page and is **not** parity-frozen.

This split is what keeps the golden non-circular: [V] cells come from the current Streamlit render, never
re-derived from the new code.

---

## A. Global page chrome

| # | Line | Element | Streamlit call | Purpose | Tag | Target equivalent (FastHTML + htmx/Tailwind) |
|---|---|---|---|---|---|---|
| 1 | 45 | Page config | `st.set_page_config(layout="wide")` | Wide layout, tab title `pipeline-hygiene` | S | `<title>` + page shell; Tailwind wide container (`max-w-screen-2xl mx-auto`). No htmx. |
| 2 | 46 | Page title | `st.title(...)` | "pipeline-hygiene — forecast-call prep" | S | `<h1>` |
| 3 | 47–48 | Read-only caption | `st.caption(...)` | "Read-only: agents inspect, people sell. Nothing on this page writes…" (§7 disclaimer) | V* | `<p class="text-sm text-slate-500">` — **PORT VERBATIM (§7)**. Static string but listed under §7; keep byte-exact. |
| 4 | 55 | Quotas-merged caption | `st.sidebar.caption(f"Quotas and team/region merged from `{QUOTAS_PATH}`.")` | Provenance of merged quotas | V | Sidebar `<p>`; value = the resolved path. |

## B. Sidebar — "Data source"

| # | Line | Element | Streamlit call | Purpose | Tag | Target equivalent |
|---|---|---|---|---|---|---|
| 5 | 60 | Sidebar header | `st.sidebar.header("Data source")` | Section header | S | `<h2>` in `<aside>` |
| 6 | 61–63 | CSV uploader | `st.sidebar.file_uploader("Upload a snapshot CSV (validated, evaluated in memory, never stored)", type="csv")` | Optional CSV upload path | V (label §7/§9) | `<input type="file" accept=".csv">` inside sidebar `<form>`; **`hx-post`** multipart to upload route → swaps a status region near the uploader (see §Interaction). **hx-indicator** spinner. Label help text ported. **NB §9:** reconcile "never stored" vs current on-disk `NamedTemporaryFile` (open decision). |
| 7 | 64–66 | Stage-map selector | `st.sidebar.selectbox("Stage vocabulary (stage_map)", …, index=default)` | Choose stage vocabulary | V | `<select name="stage_map">`; default = `"default"`. `hx-get` re-renders content on change; carried on every partial via `hx-include`. |
| 8 | 108–110 | Snapshot selector | `st.sidebar.selectbox("Snapshot", dates, index=last, format_func=isoformat)` | Pick stored snapshot (default latest) | V | `<select name="snapshot">`; default = last date. Change → `hx-get` re-render; `hx-include`d everywhere. |
| 9 | 121 | as_of date | `st.sidebar.date_input("Evaluate as of", value=snapshot_date)` | Evaluate-as-of date; default = snapshot date | V | `<input type="date" name="as_of">`, default = snapshot date (NOT `date.today()`). Change → `hx-get` re-render. |
| 10 | 95–96 | Upload single-snapshot note | `st.sidebar.caption("Uploaded snapshot {d} — single snapshot, so H3/H6 may report insufficient history.")` | Degradation note (§7) | V | Sidebar `<p>`; **PORT VERBATIM (§7)**. |

## C. Sidebar — "Filters"

| # | Line | Element | Streamlit call | Purpose | Tag | Target equivalent |
|---|---|---|---|---|---|---|
| 11 | 210 | Filters header | `st.sidebar.header("Filters")` | Section header | S | `<h2>` |
| 12 | 213 | Owner filter | `st.sidebar.multiselect("Owner", owners_all)` | Multi owner filter (~60 names) | V | Multiselect widget — **open decision** (`<select multiple>` vs searchable checkbox list). `hx-include`d; **hx-trigger** = open decision (change vs Apply). |
| 13 | 217 | Team filter | `st.sidebar.multiselect("Team", teams_all)` *(only if teams exist)* | Multi team filter; expands to rosters | V | Same widget family; degrades away when no `owner_meta`. Team picks union with owners (logic ported to server `_matches`). |
| 14 | 218 | Stage filter | `st.sidebar.multiselect("Stage", stages_all)` | Multi stage filter | V | multiselect |
| 15 | 219 | Severity filter | `st.sidebar.multiselect("Severity", ["high","medium","low"])` | Multi severity filter | V | multiselect (fixed 3 options) |
| 16 | 220–226 | Filter scope caption | `st.sidebar.caption(...)` | Explains which tables filters touch + severity semantics (§7 provenance) | V* | Sidebar `<p>`; **PORT VERBATIM** wording. |

**Filter transport (plan §10):** the sidebar is one `<form>`; every content-partial `hx-get` uses
`hx-include` on that form (or query params) so drill-downs/tables are filtered identically to each other;
the server re-applies `_matches` (lines 233–240) unchanged. Snapshot/as_of/stage_map ride along too.

## D. Headline metrics band (desk-wide; NOT filtered)

| # | Line | Element | Streamlit call | Purpose | Tag | Target equivalent |
|---|---|---|---|---|---|---|
| 17 | 153 | Metric row container | `st.columns(5, gap="small", vertical_alignment="center")` | 5-up metric layout | S | CSS grid `grid grid-cols-5 gap-2` |
| 18 | 154 | Open opps | `cols[0].metric("Open opps", desk.n_open, border=True)` | Count | V | Bordered metric card; value `desk.n_open`. |
| 19 | 155–162 | Desk score + delta | `cols[1].metric("Desk score (weighted mean)", "n/a"/f"{…:.1f}", delta=±.1f)` | Score + since-last-run delta | V | Card; `"n/a"` empty state; delta `+/-.1f` with up/down color. |
| 20 | 163–165 | Healthy % | `cols[2].metric(f"Healthy (score >= {threshold})", "n/a"/f"{…:.1f}%")` | % healthy | V | Card; label interpolates config threshold. |
| 21 | 166–170 | Open pipeline + delta | `cols[3].metric("Open pipeline", f"{CUR}{…:,.0f}", delta=±,.0f)` | Money + delta | V | Card; currency-formatted. |
| 22 | 171–178 | At-risk $ + inverse delta | `cols[4].metric("At-risk dollars", …, delta_color="inverse", help=…)` | Money; **inverse** delta color; help tooltip | V | Card; **inverse** delta coloring (down = good); help → title/popover. Help text ported. |
| 23 | 186–188 | Violations line | `st.markdown(":red[{h} high] · :orange[{m} medium] · :blue[{l} low]" + insufficient_note)` | Severity counts + insufficient-history note (§7) | V | `<p>` with colored `<span>`s (Okabe-Ito palette, lines 42–43); `:red[]/:orange[]/:blue[]` markup → spans. Insufficient-history suffix **PORT VERBATIM**. |
| 24 | 191–206 | Severity mix bar | `st.altair_chart(alt.Chart(mix)…mark_bar, width="stretch")` | Compact stacked severity bar | V(chart) | Vega chart #1 (see §Charts). Renders only if any counts. |

## E. Tabs container

| # | Line | Element | Streamlit call | Purpose | Tag | Target equivalent |
|---|---|---|---|---|---|---|
| 25 | 316–319 | Tab bar | `st.tabs(["Forecast call","Slippage","Trajectory","Flow","Owners","Teams","Appendix"])` | 7 tabs, order fixed | S | Client-side tabs — **open decision** (CSS-hidden-toggle vs htmx-swap; URL fragment deep-link?). Tab labels static. Preserve order exactly. |

### E1. Tab "Forecast call" (landing) — lines 323–362

| # | Line | Streamlit call | Purpose | Tag | Target |
|---|---|---|---|---|---|
| 26 | 324 | `st.subheader("Risky commits")` | Section title | S | `<h3>` |
| 27 | 328–329 | `st.caption("No commit/best_case opp carries a risk flag ({rules}) …")` | Empty state (filtered) | V | `<p>`; rules list interpolated; **PORT VERBATIM** shape. |
| 28 | 334–336 | `st.markdown("**{n}** commit/best_case opps carry a risk flag{filtered_note} — **{CUR}{total}** … Coaching prompts, not gotchas.")` | Summary + coaching disclaimer (§7) | V | `<p>`; `filtered_note` conditional; disclaimer **PORT VERBATIM**. |
| 29 | 346–352 | `st.dataframe(risky_df, column_config={amount:_MONEY, score:_SCORE(ProgressColumn), flags:small, ask the seller:large})` | Risky-commit table | V(table) | `<table>` from view-model rows; money format; **score → CSS ProgressColumn bar** (§Charts); column widths. Static ordering (dollar-ranked) unless sort is opted in (open decision). |
| 30 | 353–354 | `st.caption("Since last run: no previous run recorded.")` | Empty delta state | V | `<p>` **PORT VERBATIM**. |
| 31 | 358–362 | `st.caption("Since last run (snapshot {d}): {new} new flags, {cleared} cleared, {closed} closed, {added} added, {removed} removed.")` | Since-last-run delta | V | `<p>`; all counts from view model. |

### E2. Tab "Slippage" — lines 366–420

| # | Line | Streamlit call | Purpose | Tag | Target |
|---|---|---|---|---|---|
| 32 | 367 | `st.subheader("Slipping pipeline")` | Title | S | `<h3>` |
| 33 | 368–369 | `st.caption("Close-date pushes observed in stored history; movement per se is not risk.")` | Provenance (§7) | V | `<p>` PORT VERBATIM |
| 34 | 370–374 | `st.popover("How H11 fires")` + `st.caption(H11 thresholds)` | Explainer popover | V | **Native Popover API** (plan §10) — button + `popover` div; body interpolates config thresholds. |
| 35 | 377–378 | `st.caption("No push history available (rows evaluated outside the snapshot store).")` | Degradation (§7) | V | `<p>` PORT VERBATIM |
| 36 | 384–385 | `st.caption("No close-date pushes observed in stored history (matching the filters).")` | Empty state | V | `<p>` PORT VERBATIM |
| 37 | 389–390 | `st.markdown("**{CUR}{total}** slipping across **{n}** distinct opps.")` | Summary | V | `<p>` |
| 38 | 414–420 | `st.dataframe(slip_records, column_config={amount:_MONEY, "close-date drift":LineChartColumn})` | Slippage table + **sparkline** | V(table) | `<table>`; **"close-date drift" → inline SVG sparkline** (§Charts, §8 — no Altair spec). "review close plan" text cell ported. |

### E3. Tab "Trajectory" — lines 424–490

| # | Line | Streamlit call | Purpose | Tag | Target |
|---|---|---|---|---|---|
| 39 | 425 | `st.subheader("Trajectory")` | Title | S | `<h3>` |
| 40 | 428–429 | `st.caption("Trajectory charts need at least 2 stored snapshots; showing nothing rather than a one-point trend.")` | Empty state (§7) | V | `<p>` PORT VERBATIM |
| 41 | 444–458 | `st.altair_chart(coverage open vs required line, width="stretch")` | Coverage line chart | V(chart) | Vega chart #2 |
| 42 | 459–461 | `st.caption(f"Coverage basis: {basis}")` | Provenance (§7) | V | `<p>`; basis string from view model |
| 43 | 469–480 | `st.altair_chart(desk score trend line)` | Desk score trend chart | V(chart) | Vega chart #3 |
| 44 | 481–482 | `st.caption("Desk score trend appears after 2+ stored snapshots.")` | Empty state (§7) | V | `<p>` PORT VERBATIM |
| 45 | 484 | `st.divider()` | Rule | S | `<hr>` |
| 46 | 485 | `st.subheader("Commit accuracy by committed-for quarter")` | Title | S | `<h3>` |
| 47 | 486–489 | `st.caption("Committed-for quarter = fiscal quarter … stays counted against the quarter it was promised for.")` | Provenance (§7) | V | `<p>` PORT VERBATIM |
| 48 | 490 | `_ledger_section(ledger, …quarter)` → `st.popover` + `st.caption`/`st.dataframe` | Commit-accuracy ledger (quarter) | V(table) | See **Ledger component** below. |

### E4. Tab "Flow" — lines 494–599

| # | Line | Streamlit call | Purpose | Tag | Target |
|---|---|---|---|---|---|
| 49 | 495 | `st.subheader("Pipeline flow")` | Title | S | `<h3>` |
| 50 | 497–498 | `st.caption("Flow needs at least 2 stored snapshots; showing nothing rather than a one-point bridge.")` | Empty state (§7) | V | `<p>` PORT VERBATIM |
| 51 | 501–502 | `st.markdown("**Waterfall {prev} → {cur}**")` | Waterfall heading | V | `<p>`/`<h4>` |
| 52 | 518–533 | `st.altair_chart(waterfall bars)` | Waterfall chart | V(chart) | Vega chart #4 |
| 53 | 535–539 | `st.caption("Reconciles exactly: … pushed later — an annotation, never a bucket.")` | Reconciliation caption (§7) | V | `<p>` PORT VERBATIM (bridge-reconcile promise; use display-reconciled ints line 511) |
| 54 | 562–565 | `st.altair_chart(pacing bars + optional target rule)` | Generation pacing chart | V(chart) | Vega chart #5 (target line via `mark_rule`) |
| 55 | 566–567 | `st.caption("Mean {CUR}{…}/week over {n} snapshot pair(s).{pace_note}")` | Pacing note + target/no-target note (§7) | V | `<p>`; `pace_note` conditional PORT VERBATIM |
| 56 | 581–599 | `st.altair_chart(created vs closed, **flow_hover** selection_point)` | **Flow hover chart** | V(chart) | Vega chart #6 — **client-side hover** (`selection_point`, `empty=True`). Task 0 confirmed offline via vendored vega-embed + AST interpreter. |

### E5. Tab "Owners" — lines 603–692

| # | Line | Streamlit call | Purpose | Tag | Target |
|---|---|---|---|---|---|
| 57 | 604 | `st.subheader("Owner scoreboard")` | Title | S | `<h3>` |
| 58 | 624–632 | `st.dataframe(owner_records, on_select="rerun", selection_mode="single-row", key="owner_scoreboard", column_config={mean/median:_SCORE, pipeline/gap:_MONEY, coverage:%.2fx})` | **Owner table w/ row selection** | V(table) | `<table>`; each row `hx-get` drill-down (see §Interaction — 3 states, `aria-selected` survives swap). score→ProgressColumn bar; coverage `%.2fx`. |
| 59 | 638–639 | `st.markdown("**{owner} — open flagged opps** ({n}, read-only)")` | Drill-down heading | V | Swapped partial heading (focus target). |
| 60 | 641–646 | `st.dataframe(detail, column_config={amount:_MONEY, score:_SCORE, detail:large})` | Drill-down table (selected-with-results) | V(table) | `<table>` in swapped partial. |
| 61 | 648 | `st.caption("{owner} has no open opps with violations.")` | Drill-down empty (selected-empty) | V | `<p>` PORT VERBATIM |
| 62 | 650–651 | `st.caption("Select an owner row to drill into their open flagged opps.")` | Drill-down unselected | V | `<p>` PORT VERBATIM (default state) |
| 63 | 652–657 | `st.caption("small_n = fewer than {n} … Basis: {coverage_basis}.")` | Provenance/legend (§7) | V | `<p>` PORT VERBATIM |
| 64 | 658–660 | `st.caption(brief.desk_coverage_note(...))` | Desk coverage note (§7) | V | `<p>` (conditional) |
| 65 | 662 | `st.divider()` | Rule | S | `<hr>` |
| 66 | 663 | `st.subheader("Forecast integrity patterns")` | Title | S | `<h3>` |
| 67 | 664 | `st.caption("Coaching signal, not a comp input.")` | Coaching disclaimer (§7) | V | `<p>` PORT VERBATIM |
| 68 | 666 | `st.caption("Unavailable outside the snapshot store.")` | Degradation (§7) | V | `<p>` PORT VERBATIM |
| 69 | 671 | `st.caption("No overcall/undercall patterns flagged.")` | Empty state | V | `<p>` PORT VERBATIM |
| 70 | 672–675 | `st.markdown("- :orange[Overcall pattern] {owner} — {share} of {n} ever-commit opps later pushed or lost")` | Overcall bullets | V | `<li>` w/ colored span |
| 71 | 676–681 | `st.markdown("- :blue[Undercall pattern] {owner} — open pipeline {om} omitted, {far} far-out (n={n})")` | Undercall bullets | V | `<li>` w/ colored span |
| 72 | 684–685 | `st.caption("Suppressed as small_n: {n} owners …")` | Suppression note (§7) | V | `<p>` |
| 73 | 687 | `st.divider()` | Rule | S | `<hr>` |
| 74 | 688 | `st.subheader("Commit accuracy")` | Title | S | `<h3>` |
| 75 | 692 | `_ledger_section(owner_entries, …owner)` | Owner ledger (filtered) | V(table) | **Ledger component** |

### E6. Tab "Teams" — lines 696–776

| # | Line | Streamlit call | Purpose | Tag | Target |
|---|---|---|---|---|---|
| 76 | 697 | `st.subheader("Teams and regions")` | Title | S | `<h3>` |
| 77 | 699–701 | `st.caption("No team/region metadata configured. Point PIPELINE_HYGIENE_QUOTAS …")` | Degradation (§7) | V | `<p>` PORT VERBATIM |
| 78 | 703–706 | `st.caption("Coverage = open pipeline vs required … Basis: {coverage_basis}.")` | Provenance (§7) | V | `<p>` PORT VERBATIM |
| 79 | 732 | `st.markdown("**{Teams/Regions}**")` (×2) | Sub-labels | S | `<h4>` |
| 80 | 733–734 | `st.dataframe(_group_df(groups), column_config=_GROUP_COLS)` (×2: Teams, Regions) | Team & Region rollup tables | V(table) | `<table>` ×2; money/score/coverage formats |
| 81 | 735–738 | `st.caption(brief.desk_coverage_note(groups, unit))` (×2) | Coverage notes (§7) | V | `<p>` (conditional) |
| 82 | 741 | `st.divider()` | Rule | S | `<hr>` |
| 83 | 742 | `st.subheader("Coverage trend by team / region")` | Title | S | `<h3>` |
| 84 | 743–746 | `st.caption("Coverage per stored snapshot, each on its own win-rate basis; 1.00x is the bar …")` | Provenance (§7) | V | `<p>` PORT VERBATIM |
| 85 | 761–763 | `st.altair_chart((line + bar) per dim)` (×2: team, region) | Coverage trend charts | V(chart) | Vega chart #7 (×2 dims; `mark_rule` 1.00x reference line) |
| 86 | 765 | `st.divider()` | Rule | S | `<hr>` |
| 87 | 766 | `st.subheader("Commit accuracy by team and region")` | Title | S | `<h3>` |
| 88 | 768–770 | `_ledger_section(ledger, …team)` | Team ledger | V(table) | **Ledger component** |
| 89 | 772–776 | `st.dataframe(_ledger_df(ledger, …region))` | Region ledger table | V(table) | `<table>` |

### E7. Tab "Appendix" — lines 780–850

| # | Line | Streamlit call | Purpose | Tag | Target |
|---|---|---|---|---|---|
| 90 | 781 | `st.subheader("Exceptions (full list)")` | Title | S | `<h3>` |
| 91 | 811–814 | `st.caption("{n} open opps with violations match the filters. Rule legend: …")` | Count + rule legend (§7) | V | `<p>`; legend from `RULE_LABELS` |
| 92 | 816–823 | `st.dataframe(exceptions, column_config={amount:_MONEY, score:_SCORE, "score history":LineChartColumn(y_min=0,y_max=100), detail:large})` | Full exception table + **sparkline** | V(table) | Wide `<table>` — **needs horizontal overflow** (`overflow-x:auto`, plan §10). **"score history" → inline SVG sparkline** (§Charts, §8). |
| 93 | 825 | `st.expander("Validation report")` | Collapsible validation block | S+V | **`<details>`** (plan §6) wrapping the rows below |
| 94 | 829–830 | `st.warning(brief.mismatch_summary(mismatch))` | quota↔owner mismatch warning (§7) | V | Warning `<div>` (aria) — **PORT VERBATIM** summary |
| 95 | 832 | `st.write("No validation report stored for this snapshot.")` | Empty state | V | `<p>` PORT VERBATIM |
| 96 | 834–837 | `st.write("Source `{file}`: accepted {a}/{t} rows, rejected {r}.")` | Validation summary | V | `<p>` |
| 97 | 838–839 | `st.warning(warning)` (loop) | Per-warning validation messages (§7) | V | Warning `<div>`s |
| 98 | 840–843 | `st.dataframe(row_reasons, columns=[row,opp_id,reason])` | Rejected-row reasons table (§7) | V(table) | `<table>` |
| 99 | 845–850 | `st.download_button("Download desk brief (markdown)", brief.render(data,config), file_name=f"desk_brief_{as_of}.md", help="… does not record a run.")` | **Brief download** | V | `<a>`/form → **native file response**, byte-identical `.md` (parity §10). Help "does not record a run" rendered as visible caption near button (plan §10). |

## Repeated component: commit-accuracy **Ledger** (`_ledger_section`, lines 273–285)

Used 4× (quarter #48, owner #75, team #88; region variant #89). Each instance:
- `st.popover("How commit accuracy is computed")` + `st.caption(LEDGER_CAPTION)` → **Native Popover API**;
  `LEDGER_CAPTION` (lines 247–254, incl. "Coaching signal, not a comp input.") **PORT VERBATIM**.
- 3 states: `None` → `st.caption("Unavailable outside the snapshot store.")`; empty →
  `st.caption("No opp in stored history has ever been forecast commit.")`; else `st.dataframe(_ledger_df…)`.
  All three **PORT VERBATIM**.

---

## Charts & in-cell mini-charts (feeds plan §8 + parity allowlist)

**7 tab-level Altair charts** (each `chart.to_dict()` is spec-diffable old-vs-new under option A):

| Chart | Line | Type | Interaction / special encoding |
|---|---|---|---|
| C1 Severity mix | 191–206 | stacked bar | Okabe-Ito severity palette; height 40 |
| C2 Coverage open vs required | 444–458 | multi-line | 2-series palette; `~s` axis format |
| C3 Desk score trend | 469–480 | line | y domain [0,100] |
| C4 Open-pipeline waterfall | 518–533 | bar | level/in/out palette; display-reconciled ints |
| C5 Generation pacing | 546–565 | bar (+ optional `mark_rule` target) | dashed target line when configured |
| C6 Created vs closed | 581–599 | grouped bar | **client-side hover** `selection_point(empty=True)` → opacity 1.0/0.35 (the Flow hover; Task 0 proven offline) |
| C7 Coverage trend by team/region | 754–763 | line + `mark_rule` 1.00x | ×2 (team, region); reference rule line |

**In-cell mini-charts — NO Altair spec to reuse (plan §8 → parity allowlist entries):**

| Mini | Line | Streamlit | Target |
|---|---|---|---|
| M1 Slippage "close-date drift" | 418–419 | `LineChartColumn` | inline **SVG sparkline** |
| M2 Appendix "score history" | 820–821 | `LineChartColumn(y_min=0,y_max=100)` | inline **SVG sparkline** (fixed 0–100 domain) |
| M3 Score bars (`_SCORE_COL`) | 244–245 | `ProgressColumn(0–100)` | **CSS bar** — appears in tables #29, #58, #60, #92 |

These 3 have no spec to diff → each is an explicit **parity allowlist entry** with justification (plan §8).

---

## List A — Streamlit workarounds to DELETE, not port (plan §6)

| Workaround | Where (lines) | Requirement it served | Native replacement |
|---|---|---|---|
| **5× `st.stop()`** | 74, 88, 91, 102, 107 | Abort top-to-bottom script (bad upload date, all-rows-rejected, other IngestError, no store, empty store) | Route returns the error/empty partial and **returns** — normal control flow. |
| **`on_select="rerun"` owner drill-down** | 626 (+ selection read 633) | Re-run whole script to show selected owner's opps | Row **`hx-get`** returns just the drill-down partial (contract §Interaction). |
| **Hidden-tab `width="stretch"` sizing + comments** | 194–195, 206, 458, 480, 533, 565, 599, 763 (+ table widths) | Force Altair to size to a tab container starting hidden | Pure **CSS width**; the Streamlit hidden-tab sizing quirk does not exist. |
| **`st.tabs(...)` rerun-preserved tab state** | 316–319 | Keep active tab across reruns | Client-side tabs (mechanism = open decision). |
| **`st.columns` / `st.popover` / `st.expander` primitives** | 153; 276, 370; 825 | Streamlit-specific layout | Semantic HTML: **CSS grid** (columns), **native Popover API** (popover), **`<details>`** (expander). |

None reimplemented. No confirm-dialog added (Finding 1 — nothing to protect).

## List B — requirements that only LOOK like workarounds — PORT VERBATIM (plan §7)

Ported string-for-string into the view model. Grouped as plan §7:

- **Read-only disclaimers:** page caption "Read-only: agents inspect, people sell…" (47–48); uploader
  "validated, evaluated in memory, never stored" (61–63); download help "does not record a run" (850);
  "single snapshot, so H3/H6 may report insufficient history" (95–96).
- **Degradation notes:** "Unavailable outside the snapshot store." (280, 666); "No push history available…"
  (377–378); insufficient-history list (181–188); "showing nothing rather than a one-point trend"
  (428–429) / "…one-point bridge" (497–498); "Desk score trend appears after 2+ stored snapshots." (482);
  "No opp in stored history has ever been forecast commit." (282); "No team/region metadata configured…"
  (699–701).
- **Provenance labels:** "Coverage basis: {basis}" (461); "Basis: {coverage_basis}." (657, 706);
  "committed-for quarter = …" (486–489); "snapshot-anchored" (479 chart title); waterfall "Reconciles
  exactly …" (535–539); "Coverage per stored snapshot, each on its own win-rate basis; 1.00x is the bar"
  (743–746).
- **Coaching disclaimers:** "Coaching signal, not a comp input." (254 in LEDGER_CAPTION, 664); "Coaching
  prompts, not gotchas." (336).
- **Unmatched/mismatch callouts:** `quota_owner_mismatch` → `mismatch_summary` warning (827–830); upload
  rejection reasons table (84–87, 840–843); validation warnings (838–839).

---

## Interaction contract summary (feeds plan §10 Task 3 — design only, not built this session)

- **Owner drill-down (3 states):** unselected "Select an owner row to drill into…" (default); selected-with-
  results (`{owner} — open flagged opps ({n}, read-only)` + table); selected-empty "{owner} has no open opps
  with violations". Selected row carries persisted `aria-selected` + CSS indicator **surviving the htmx swap**.
- **Loading feedback:** every `hx-get`/`hx-post` shows an `hx-indicator`.
- **Focus + SR:** swapped targets `aria-live="polite"` + `aria-busy` during load; move focus to the swapped
  content's heading (primary new a11y risk — Streamlit full-page-reran, htmx does not).
- **Filter transport:** every partial carries the full active filter set (`hx-include` on the sidebar form)
  + snapshot/as_of/stage_map; server re-applies `_matches` identically. **Uploaded-CSV drill-down** needs a
  server-side temp-session or is explicitly scoped out (open decision — upload is never persisted).
- **Upload failure:** error partial swaps into a dedicated status region near the uploader (rest of page
  untouched); `AllRowsRejectedError` row-reasons table renders in that same target.
- **Wide Appendix table:** `overflow-x:auto`.

## Open decisions to surface at the HARD GATE (owned by user; Task 0 already settled the chart-stack)

1. **Chart CSP posture** (from Task 0): A-strict (bundle vega-interpreter, no `unsafe-eval`) vs A-loose
   (`script-src 'self' 'unsafe-eval'`, no Node). *Recommend A-strict.* — see `task0-spike-report.md §5`.
2. **Upload endpoint copy vs behavior** (§9/§13): current code writes upload to on-disk `NamedTemporaryFile`
   (75–93) despite "evaluated in memory, never stored" — fix impl or copy. Must stay byte-identical output.
3. **Table sorting:** keep client-side sort on which tables (Streamlit `st.dataframe` gave it free), or go
   fully static ordered by the view model. Affects triage on Risky/Slippage/Owners/Appendix.
4. **Tabs mechanism:** CSS-hidden-toggle (all data upfront) vs htmx-swap (lazy per tab); URL-fragment
   deep-link or not.
5. **Owner/Team multiselect widget** (~60 names): `<select multiple>` vs searchable checkbox list; and
   `hx-trigger` on-change vs an explicit Apply button.

---

**GATE:** Task 0 (spike, `task0-spike-report.md`) + Task 1 (this inventory) are complete. **Stopping here
for review before Task 2 (view model).** The view model is not extracted and the page is not built this session.
