# Migration Plan: Pipeline-Hygiene dashboard, Streamlit → FastHTML + htmx + Tailwind

Status: DRAFT — revised after ce-doc-review (7 personas) + external research; Tailwind adopted post-review for cross-tool consistency (§13) · Date: 2026-08-12

## 0. Motivation — why this migration, and why now

**Problem:** Streamlit is too visually constrained. Three iterations of trying to
make the dashboard look like a modern web page have stalled against Streamlit's
styling ceiling (fixed component chrome, limited layout control, theme keys
instead of real CSS). The product goal is to **visually improve the tool**.

**Why this port is step 1 (and deliberately changes nothing visible yet):** you
cannot safely redesign on top of a UI framework that fights you. This migration
moves the presentation layer to a stack we fully control in HTML/CSS
(FastHTML + htmx + Tailwind), while proving — via a parity gate — that not a
single number, string, or degradation path regressed in the move. Once the page
renders from
plain HTML we own, the visual redesign (**step 2, a separate effort**) has room
to happen. So:

- **Success of THIS step = zero regression + full styling control unlocked.**
  "Nothing looks different yet" is the intended outcome of the port, not a sign
  it was pointless — it is what makes the redesign that follows low-risk.
- **The visual redesign is explicitly out of scope here** and tracked as the
  next phase. This plan does not restyle anything; it relocates the same output
  onto a controllable substrate.

**Why FastHTML specifically (recorded as a given constraint, not an open pick):**
the target was set by the initiating request. It fits the goal because every
benefit we need — server-rendered request/response (kills Streamlit's rerun
workarounds), htmx-native partial swaps, minimal dependencies, and offline
vendoring — accrues to it, and it hands us raw HTML/CSS for step 2. Plain
Flask/FastAPI + Jinja + htmx would also satisfy the port; FastHTML is chosen for
its htmx-native ergonomics. Staying on Streamlit is rejected precisely because it
is the constraint we are trying to escape. If FastHTML proves unsuitable during
the Task 0 spike (§10), fall back to FastAPI + Jinja + htmx — the view-model and
parity work below are framework-agnostic and survive that swap.

**CSS layer — Tailwind, adopted now for long-term consistency.** Styling is
authored with Tailwind, built offline via the standalone `tailwindcss` CLI (no
Node) into a committed generated stylesheet — chosen now, not deferred, so this
tool shares one design vocabulary with GridSignals and the other tools as the app
grows, and so step 2's redesign builds on the same substrate rather than ripping
out hand-rolled CSS later. **Adopting Tailwind is not the same as redesigning:**
step 1's job for it is only to re-clothe the *same* content to the current look;
the visual redesign remains step 2. Accepted cost: a build step (CLI scans class
names → regenerates the stylesheet on class changes and in CI). The parity gate
(values/strings/`.md`/chart-specs — §5, §8) is unaffected, since it does not
diff pixels.

## 1. Objective & done-definition

Port the Pipeline-Hygiene UI from `app/dashboard.py` (Streamlit) to a
FastHTML + htmx page at `app/server.py`, preserving **every number, every
user-visible string, and every degradation path**. Presentation-layer port, not
a rewrite; not a restyle (§0).

Done =
- The parity harness shows **zero diff** between the new page's view model and a
  golden **frozen from the live Streamlit render** (§10 Task 4), for every
  fixture; and the exported desk-brief `.md` is **byte-identical** old vs new.
- Chart parity is verified at the **spec level** (`chart.to_dict()` old vs new),
  not left implicit — see §8.
- A manual visual/interaction acceptance pass (charts, hover, drill-down, tabs,
  offline) matches the still-live Streamlit page before Streamlit is deleted.
- Streamlit-specific workarounds are **deleted**, not reimplemented.
- Offline-first and read-only postures are intact and **enforced**, not merely
  asserted (§9).

## 2. Placeholder resolution (this repo has no `core/`)

The template ships with `{{TBD}}` slots and assumes a `core/` package. This
repo's layout is `src/` (logic) + `app/` (UI). Mapping:

| Template slot | Resolved value | Note |
|---|---|---|
| `{{TOOL_NAME}}` | Pipeline-Hygiene | |
| `{{TARGET_UI}}` | FastHTML + htmx + Tailwind | given constraint (§0); Tailwind built offline via standalone CLI |
| `{{OLD_PAGE}}` | `app/dashboard.py` | 850 lines, 121 `st.*` call sites |
| `{{NEW_PAGE}}` | `app/server.py` | new FastHTML entry point |
| `{{CORE_MODULES}}` | `src/` | brief, ingest, patterns, rules, scoring, snapshots, seed |
| `{{TOOL_NAME_SNAKE}}` | `pipeline_hygiene` | → `src/pipeline_hygiene_view.py`, `tests/test_pipeline_hygiene_view.py` |
| "core store / schema / SQLite — do not touch" | `src/snapshots.py` DDL + the `.db` file | schema is inline in `snapshots.py`; no separate `schema.py` |
| `{{BRANCH}}` | `feat/fasthtml-ui-migration` | worktree `../pipeline-hygiene-fh`, off `origin/main`, PR base `main` |

## 3. Pre-flight results (actual vs expected)

| Check | Expected | Actual |
|---|---|---|
| `git status --porcelain` | empty | ✅ empty |
| `pytest tests/` | all pass | ✅ 131 passed (46s) |
| `grep -rn "import streamlit" src/` | no matches | ✅ none — logic is clean of UI |
| `st.*` call-site count in old page | record | 121 |
| **Streamlit cold-start render time** | record as baseline | ⬜ TODO before Task 3 — the §12 target is "no worse than this", not an absolute (the page recomputes multi-snapshot series on load; an unanchored number could be trivially met or impossible) |

Baseline is green; safe to port on top of it.

## 4. Findings that shape the plan

1. **The page is entirely read-only.** `grep` confirms `0` `st.session_state`,
   `0` `@st.cache_*`, `0` `st.data_editor`/`text_input`/`text_area`. There is
   nothing the user edits. **Consequence:** the template's "editable fields must
   survive filter changes / confirm-before-regenerate dialog" requirement is
   **not applicable**. Record this explicitly in the Task 1 inventory.

2. **`src/brief.py` is already the aggregation layer.** `brief.build_from_rows(...)`
   returns a `data` dict; `brief.render(data, config)` produces the downloadable
   markdown — both pure and shared. The dashboard adds a **second, UI-only** layer:
   currency/percent formatting, DataFrame shaping, conditional captions, empty
   states, chart specs. The Task 2 view model captures exactly this second layer.
   Because that layer lives **only in `dashboard.py` today**, the parity baseline
   must come from the live Streamlit render, not a re-derivation (§10 Task 4).

3. **Charts are the main technical risk.** Seven tab-level charts (Altair →
   Vega-Lite), one with a client-side hover interaction (Flow "created vs closed"
   via `alt.selection_point`), **plus** in-cell mini-charts the tab-level count
   misses: two `LineChartColumn` sparklines (Slippage "close-date drift" line 418;
   Appendix "score history" line 820) and `ProgressColumn` score bars. These
   carry user-visible information (colorblind-safe palette, target line, coverage
   bar, hover). See §8 for the rendering decision — the one design choice needing
   sign-off.

4. **Streamlit-coupled control flow is small and specific:** 5 `st.stop()`
   early-exits, 1 `on_select="rerun"` drill-down, hidden-tab `width="stretch"`
   sizing comments. These delete cleanly (§6) — but their htmx replacements need
   an explicit interaction contract (§10 Task 3), which the first draft omitted.

## 5. The parity boundary — `src/pipeline_hygiene_view.py`

Pure functions, **no UI imports**. Same inputs the page takes (the `data` dict
from `brief.build_from_rows`, plus `config`, filter selections, store-derived
extras) → plain dataclasses/dicts holding **every value and every user-visible
string**, including:

- All formatted values (currency, `%`, `.1f` scores, `+/-` deltas) formatted
  **once, here**.
- Every conditional note and empty state, verbatim (§7 lists them).
- Table rows as lists of plain dicts (today's `pd.DataFrame` inputs).
- Chart payloads as `{data, vega_lite_spec}` so the numbers **and the encoding
  spec** are diffable even though the client render mechanism may differ (§8).

No user-visible string literals live in the FastHTML page except static labels
(tab names, column headers, section titles).

Tests: `tests/test_pipeline_hygiene_view.py` covers happy path, missing-optional
degradation (no `owner_meta` → Teams note; no push history → slippage note),
empty/insufficient data (`<2` snapshots → trajectory & flow empty states), and
each conditional note.

## 6. Streamlit workarounds to DELETE (not port)

| Workaround | Requirement it served | How the target serves it natively |
|---|---|---|
| 5× `st.stop()` early-exits | Abort the top-to-bottom script (no store / empty store / rejected upload) | Route returns the error/empty partial and returns — normal control flow |
| `on_select="rerun"` owner drill-down | Re-run the whole script to show the selected owner's opps | `hx-get` returns just the drill-down partial (contract in §10 Task 3) |
| Hidden-tab `width="stretch"` sizing + comments | Force Altair to size to a tab container starting hidden | Pure CSS width; the Streamlit sizing quirk does not exist |
| `st.tabs(...)` rerun-preserved tab state | Keep the active tab across reruns | Client-side tabs (§10 Task 3 decides mechanism) |
| `st.columns` / `st.popover` / `st.expander` primitives | Streamlit-specific layout | Semantic HTML: native **Popover API** for popovers, `<details>` for expanders, CSS grid for columns (§10 Task 3) |

**None reimplemented.** No confirm-dialog is added — there are no edits to
protect (Finding 1).

## 7. Requirements that only LOOK like workarounds — PORT VERBATIM

Product requirements, ported string-for-string into the view model:

- Read-only disclaimers: page caption "Read-only: agents inspect, people sell …";
  download help "does not record a run"; "single snapshot, so H3/H6 may report
  insufficient history".
- Degradation notes: every "Unavailable outside the snapshot store", "No push
  history available", the insufficient-history list, "showing nothing rather than
  a one-point trend/bridge", "Desk score trend appears after 2+ stored snapshots".
- Provenance labels: "Coverage basis: …", "Basis: …", "committed-for quarter =
  …", "snapshot-anchored", the waterfall "Reconciles exactly …" caption.
- Coaching disclaimers: "Coaching signal, not a comp input", "Coaching prompts,
  not gotchas".
- Unmatched/mismatch callouts: `quota_owner_mismatch` → `mismatch_summary`
  warning; upload rejection reasons table; validation warnings.

## 8. Chart rendering — DECISION (default A; confirmed offline-viable by research)

The parity gate covers view-model values + exported `.md` + **chart specs**, not
pixel-diffs. Options:

- **A (recommended): emit Vega-Lite specs, render with vendored `vega`/
  `vega-lite`/`vega-embed` served locally.** Research confirms this works fully
  offline — download the three libs, reference them with local `<script>` tags;
  Altair's `chart.to_dict()` yields the same Vega-Lite spec the page produces
  today, so visuals **and** the Flow hover are preserved and the spec is
  byte-diffable old vs new. Bonus for step 2 (§0): charts become data+spec we
  style in CSS, not Streamlit-locked widgets.
- **B: server-side static SVG** (`vl-convert-python`/matplotlib). Offline, no
  client JS, but **loses the Flow hover** and adds a heavy build dep.
- **C: hand-rolled SVG/CSS bars** for the simple charts, dropping the rich ones.

**Chart-parity contract (fixes the "objective promises every degradation path,
but §8 dropped chart parity" gap):**
- Under **A**, assert `chart.to_dict()` equality (old Altair spec vs new) in the
  parity test — visual + interaction fidelity follows from spec identity.
- If **B or C** is chosen, each lost visual/interaction (Flow hover, exact color
  mapping, target-line) becomes an **explicit allowlist entry** in the parity
  test with a one-line justification — never an implicit loss.

**In-cell mini-charts** (Finding 3), which have **no Altair spec to reuse**:
render the two `LineChartColumn` sparklines as inline SVG sparklines and the
`ProgressColumn` score bars as a CSS bar. List all three in the Task 1 inventory
and the parity allowlist.

## 9. Offline-first + read-only, enforced not asserted

- **Vendor `htmx.min.js` locally** under `app/static/`; **drop Pico** — Tailwind
  is the styling layer, so Pico's default stylesheet is redundant. Research
  confirms FastHTML's `fast_app()` **does load htmx/Pico from a CDN by default** —
  so use `fast_app(default_hdrs=False, hdrs=(...local Script/Link...))` (and
  `htmx=False` if serving htmx yourself) and a local static mount. This is
  verified in the Task 0 spike (§10), not assumed.
- **Tailwind, built offline:** generate the stylesheet with the standalone
  `tailwindcss` CLI (a self-contained per-platform binary — **no Node/npm**),
  configured to scan the FastHTML templates/components for class names, and
  **commit the generated CSS** under `app/static/`. Reference it with a local
  `<link>` — never the Play CDN (banned by offline-first). The CLI binary and the
  generated CSS are both vendored; the build step runs in CI (or the committed CSS
  is regenerated) whenever classes change. Verified in the Task 0 spike (§10).
- **Vendored-JS/CSS supply chain:** record the exact upstream version **and a
  SHA-256** for each vendored asset (htmx, vega, vega-lite, vega-embed, the
  `tailwindcss` CLI binary, and the generated Tailwind stylesheet) at commit time,
  and add a checksum-verify step to the vendoring/update process. ~1 MB of Vega JS
  is the cost of option A; weigh against B/C at sign-off (§13).
- **CSP:** send an actual restrictive header (`Content-Security-Policy:
  default-src 'self'`) from the FastHTML app and verify it — do not rely on a
  `grep` of the HTML alone. (Either enforce the header or downgrade the claim
  from "CSP-safe" to "no external origins"; the plan chooses to enforce.)
- **Bind address:** the server **must bind `127.0.0.1`** explicitly; any
  non-localhost bind requires a deliberate opt-in flag. (uvicorn defaults to
  localhost, but a net-new ASGI listener replacing Streamlit must not be left to
  a default — verify in §12.)
- **Read-only + upload:** no writes outside the snapshot store; no outbound
  calls; viewing/downloading records nothing. The upload endpoint must set a
  **size limit** and **guarantee temp-file cleanup on exception/crash**. Note:
  the current code writes the upload to a `NamedTemporaryFile` on disk (lines
  75-93) despite copy saying "evaluated in memory, never stored" — reconcile this
  when it becomes a real HTTP endpoint (correct the copy or the implementation;
  flagged as a decision in §13).

## 10. Task-by-task plan & gates

**Task 0 — FastHTML + Tailwind offline spike (NEW gate, before Task 3).** Install
`python-fasthtml`; render a hello-world with `default_hdrs=False` + local htmx
(Pico dropped); run the standalone `tailwindcss` CLI to generate a stylesheet from a
sample template and serve it locally; `grep` the served HTML for external origins
→ must be zero; confirm a vendored Vega chart renders the Flow hover offline.
**Pin the confirmed FastHTML version and the `tailwindcss` CLI version.** If
zero-CDN config or offline Vega fails, option A falls back to B and the chart code
changes — so this is settled by evidence **before** the page is built. Also
record the Streamlit cold-start baseline (§3) here.

**Task 1 — Inventory** → `docs/migration/Pipeline-Hygiene-inventory.md`. One row
per `st.*` call site (Element / Streamlit call / Purpose / State / Target
equivalent — the "Target equivalent" column names the htmx/loading/focus
treatment per element), plus the two lists: (a) workarounds to delete (§6),
(b) requirements to preserve (§7). Record Finding 1 (no editable state). This
inventory is the **authoritative source of section/element grouping** Task 3
replicates. **GATE: stop and show the inventory before Task 2.**

**Task 2 — View model** → `src/pipeline_hygiene_view.py` + tests. Pure, no UI
imports, all formatting here (§5).

**Task 3 — FastHTML page** → `app/server.py`, rendering strictly from the view
model. Preserve the interaction contract: same sidebar selectors (upload,
stage_map, snapshot, as_of, filters), same defaults (latest snapshot, as_of =
snapshot date), same tab order, section grouping **per the Task 1 inventory**.
Download → native file response, byte-identical.

*Interaction states & htmx contract (was under-specified — design-lens):*
- **Owner drill-down** has three states that must all render: unselected
  ("Select an owner row to drill into …"), selected-with-results, and
  selected-empty ("{owner} has no open opps with violations"). The selected row
  carries a persisted `aria-selected` + CSS indicator that **survives the htmx
  swap** (Streamlit auto-highlights; hand-built tables do not).
- **Loading feedback:** every `hx-get`/`hx-post` shows an `hx-indicator`
  (spinner or disabled control) so a click/filter change is never silent.
- **Focus + screen-reader:** swapped targets use `aria-live="polite"` (with
  `aria-busy` during load) and move focus to the swapped content's heading. This
  is the primary **new** a11y risk (Streamlit full-page-reran; htmx does not).
- **Filter-state transport:** every htmx partial request carries the **full
  active filter set** (via `hx-include` on the sidebar form, or query params) so
  a swapped-in drill-down is filtered identically to the tables around it; the
  server re-applies `_matches` the same way. The drill-down `hx-get` also carries
  snapshot_date / as_of / stage_map. **Uploaded-CSV drill-down** (upload is never
  persisted) either needs a server-side temp-session or is explicitly scoped out.
- **Tables:** decide static (row order from the view model) vs client-side
  sortable headers, and state which tables keep sorting; specify horizontal
  overflow for the wide Appendix table. Streamlit's `st.dataframe` gave
  click-to-sort/resize for free — losing it silently changes triage.
- **Tabs:** decide CSS-hidden-toggle (all data loaded upfront) vs htmx-swap
  (lazy per tab, needs loading state); state whether the active tab is reflected
  in the URL fragment for deep-linking.
- **Popovers:** use the native **HTML Popover API** (Baseline since Apr 2025 —
  click, light-dismiss, Escape, zero JS) for "How commit accuracy is computed" /
  "How H11 fires", **not** `<details>` (which reflows inline and lacks
  light-dismiss).
- **Multiselects:** decide the widget (searchable checkbox list vs
  `<select multiple>`, given ~60 owner names) and the `hx-trigger` (on-change vs
  an explicit Apply button).
- **Upload failure:** error partial swaps into a dedicated status region near the
  uploader (rest of page untouched); the `AllRowsRejectedError` row-reasons table
  renders in that same target.
- **Download help** ("does not record a run") renders as a visible caption/title
  near the button, not dropped.

*Accessibility floor (capped — no full WCAG audit this port):* real `<label>`s,
keyboard-reachable controls, no color-only signaling (keep text severity labels),
**plus** the `aria-live`/focus handling for htmx swaps above. Explicitly out of
scope: broader ARIA-role/screen-reader audit tooling.

*Styling:* author the page with Tailwind utility classes, built offline (§9).
Step 1 reproduces the **current look** (zero visual change) — Tailwind is the CSS
tool, not the redesign (that is step 2). The pixel styling is not parity-gated
(§8), so this is free to be authored in Tailwind.

Update `requirements.txt` (add FastHTML + pinned Task 0 version; comment
load-bearing pins; **do not remove Streamlit yet**). Add the pinned `tailwindcss`
CLI version and the build command to the repo (a Makefile target or CI step);
requirements.txt is pip-only, so the CLI binary is documented/vendored separately.

**Task 4 — Parity gate** → `tests/test_pipeline_hygiene_parity.py`.
**The golden must be frozen from the live Streamlit render, not re-derived**
(fixes the P0 circularity: `dashboard.py` has no view model today, so a golden
"from the Streamlit view model" would equal the new code by construction and hide
a formatting/caption regression). Concretely:
- **Before Task 2**, run `dashboard.py` under Streamlit `AppTest` against each
  fixture and freeze its actual displayed strings/values (`.value` per element,
  `at.dataframe[i].value`, captions/markdown text) as immutable committed
  goldens.
- Task 4 diffs the new view model against **those frozen goldens**.
- The `.md` download parity is not circular (both UIs call the shared pure
  `brief.render`) and is asserted byte-for-byte.
- **Fixtures:** full data, minimal-required-fields-only, empty — **and at least
  one multi-snapshot (≥2) fixture** so the trajectory/flow/chart paths are
  actually paritied instead of collapsing to their empty-state notes.
- Any intentional diff is an explicit allowlist entry with a one-line
  justification; **empty allowlist is the expected outcome** (chart B/C losses,
  if chosen, are the only anticipated entries — §8). If parity fails: stop and
  report the diff; do not edit the golden to match the new page.

**Task 4.5 — Manual visual/interaction acceptance (NEW, before Task 5).** The
parity gate cannot see chart visuals, the Flow hover, layout, tab/drill-down UX,
or offline render. Exercise all seven charts + sparklines, Flow hover, owner
drill-down (all three states), tab order, filters, and offline render
**side-by-side against the still-live Streamlit page**, and sign off. (Or keep
`dashboard.py` behind a flag for one release instead of deleting in the same PR.)

**Task 5 — Retire Streamlit** (only after Task 4 passes clean **and** Task 4.5 is
signed off): delete `app/dashboard.py` and unreferenced Streamlit helpers; remove
`streamlit` and Streamlit-only deps (`altair` iff nothing else imports it —
verify with `grep -rn "altair\|streamlit" --include=*.py .`; note the view model
may still build Vega-Lite specs as plain dicts without importing `streamlit`);
update README run instructions; keep the parity test as the ongoing regression
baseline.

## 11. Constraints honored

- Logic in `src/` is not changed. If a `src/` function needs a new signature to
  serve the UI, add an **additive wrapper** — never edit the existing one. (Note:
  `dashboard.py` calls one private `store._history`; the view model becomes its
  caller — a read, not a `src/` edit.)
- No writes outside the snapshot store; no outbound calls; view/download records
  nothing.
- `src/snapshots.py` DDL, the SQLite file, and existing `tests/` covering logic
  are untouched.
- Streamlit page stays live until Task 4 passes **and** Task 4.5 is signed off.

## 12. Verification checklist (command per item)

- Core untouched: `git diff --stat feat/fasthtml-ui-migration -- src/` → only
  additive files, no modified lines in existing `src/` modules.
- Full suite: `pytest tests/` → all pass, pre-existing tests unmodified.
- Parity: `pytest tests/test_pipeline_hygiene_parity.py -v` → pass, allowlist
  empty (or only the signed-off chart entries).
- Cold start: launch app, load page → **no worse than the Streamlit baseline**
  recorded in §3 (paste both numbers).
- Traceability: pick any $/count figure → matches a direct `sqlite3` query on the
  store (paste both).
- Degradation: run against minimal-required-fields profile → renders, optional
  features skipped with the visible note intact, no traceback.
- Offline: disable network, restart, exercise full flow → works; `grep -i
  "cdn\|http"` on rendered HTML → no external origins; **and** confirm the CSP
  response header is present. The Tailwind stylesheet is a committed generated
  file served locally (no Play CDN), and rebuilds offline via the pinned CLI.
- Bind: confirm the server listens on `127.0.0.1` only (e.g. `netstat`/curl the
  external interface → refused).
- Export: download artifact old vs new before Task 5 → `diff` → identical.
- Visual/interaction acceptance (Task 4.5): signed-off side-by-side pass.
- No Streamlit residue (post-Task 5): `grep -rn "streamlit" --include=*.py
  --include=*.txt --include=*.md .` → none outside changelog/history.

## 13. Open decisions & risks

Resolved by research (§0/§8/§9): FastHTML *can* run fully offline; Vega vendoring
+ spec reuse is viable; AppTest can freeze the parity baseline; Popover API is
Baseline; uvicorn binds localhost by default.

**UI-stack decision (settled):** step 1 uses **FastHTML + htmx + Tailwind**.
Tailwind is adopted **now, not deferred**, for long-term consistency: as the app
grows it shares one design vocabulary with GridSignals and the other tools, and
step 2's redesign builds on the same substrate instead of ripping out hand-rolled
CSS. It is built offline with the standalone `tailwindcss` CLI (no Node/npm) into
a committed generated stylesheet (§9), keeping the offline-first constraint intact
without a JS-ecosystem toolchain. FastAPI vs FastHTML is a non-issue — same
Starlette/ASGI base — so the sibling agent's "FastAPI + htmx + Tailwind" for
GridSignals aligns with this stack. **Accepted cost:** a build step (CLI scans
classes → regenerates CSS, run in CI or committed) and one more pinned/checksummed
vendored artifact. This does not touch the parity gate (values/strings/`.md`/
chart-specs — §5, §8), and step 1 still targets zero visual change (§0); the
redesign that Tailwind enables is step 2.

Remaining sign-offs:

1. **Chart option A vs B/C** — A preserves hover + spec-diff parity and suits the
   step-2 redesign, at ~1 MB vendored JS + a self-owned charting-JS maintenance
   surface (security patches, upgrades). Confirm A, or pick B/C and accept the
   allowlisted visual losses. **Decide before Task 3** (gated by Task 0).
2. **Upload endpoint copy vs behavior** — fix the on-disk temp-file to match
   "evaluated in memory, never stored", or correct the copy. Behavior change must
   stay within the byte-identical-output constraint.
3. **Table sorting** — keep client-side sort on which tables (if any), or go
   fully static ordered by the view model.
4. **Tabs** — CSS-hidden-toggle vs htmx-swap, and URL deep-linking or not.
5. FastHTML maturity risk — Task 0 is the early kill-switch; FastAPI + Jinja +
   htmx is the framework-agnostic fallback (§0).

## 14. Handoff

Reusable output for later work: the view-model pattern
(`src/pipeline_hygiene_view.py` structure), the **Tailwind config / design tokens**
(the intended shared design vocabulary across GridSignals + Pipeline-Hygiene + the
other tools — keep it a portable `tailwind.config` rather than one-off inline
styles), and this decision log. **Do not** treat a bespoke "shared FastHTML
component framework" as an expected Task 3 deliverable — cross-tool consistency
rides on the shared Tailwind config, not a hand-built component library; this is a
single-page port with no second consumer yet, so if a layout helper falls out
naturally, note it post-hoc. Any "reusable migration pattern" framing is gated
behind **this port proving out** (parity green + Task 4.5 signed off + step-2
redesign actually enabled) before it is propagated to other tools.

## 15. Review & research provenance

Revised after a 7-persona `ce-doc-review` (coherence, feasibility, adversarial,
product-lens, design-lens, scope-guardian, security-lens). Key integrations:
§0 motivation (product-lens P0/P1); §10 Task 4 non-circular golden + §10 Task 0
spike + Task 4.5 acceptance (feasibility/adversarial P0/P1); §10 interaction
contract + capped a11y (design-lens ×10); §9 CSP/bind/pinning/upload (security-lens
×4); §8 chart-parity contract + sparklines; §14 de-scoped shared framework
(scope-guardian); §7↔§8 cross-refs fixed (coherence).

External research (Aug 2026):
- FastHTML default CDN + local override: [DeepWiki: HTMX Integration](https://deepwiki.com/AnswerDotAI/fasthtml/5-htmx-integration), [FastHTML docs](https://www.fastht.ml/docs/tutorials/by_example.html)
- Vega/Vega-Lite/vega-embed offline: [Vega-Lite embed usage](https://vega.github.io/vega-lite/usage/embed.html), [vega-embed](https://github.com/vega/vega-embed)
- Streamlit AppTest value extraction: [st.testing.v1.AppTest](https://docs.streamlit.io/develop/api-reference/app-testing/st.testing.v1.apptest), [cheat sheet](https://docs.streamlit.io/develop/concepts/app-testing/cheat-sheet)
- htmx a11y (hx-indicator/aria-live/focus): [Wagtail: htmx accessibility gaps](https://wagtail.org/blog/htmx-accessibility-gaps-data-and-recommendations/), [MDN aria-live](https://developer.mozilla.org/en-US/docs/Web/Accessibility/ARIA/Reference/Attributes/aria-live)
- Popover API Baseline: [web.dev: Popover Baseline](https://web.dev/blog/popover-baseline), [MDN Popover API](https://developer.mozilla.org/en-US/docs/Web/API/Popover_API)
- uvicorn bind default: [Uvicorn settings](https://uvicorn.dev/settings/)
