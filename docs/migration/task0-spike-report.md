# Task 0 — FastHTML + Tailwind offline spike: results

Status: **PASS — chart option A (vendored Vega) is confirmed offline-viable.** No STOP condition
hit. One design decision is surfaced for the gate (CSP posture for charts — see §5).
Date: 2026-08-12 · Branch: `feat/fasthtml-ui-migration` · Spike ran on Windows 11, Python 3.14.3, Node 24.14.0.

The spike is not committed to the repo (it lives in the session scratchpad). This report records the
**evidence, the exact pins, and the SHA-256s** so Task 3 can vendor deterministically. Nothing here
touches `src/`, `tests/`, the DDL, or the SQLite file.

## 1. Verdict against the Task 0 gate (§10)

| Gate criterion | Result |
|---|---|
| Install `python-fasthtml`; render hello-world with `default_hdrs=False` + local htmx (Pico dropped) | ✅ FastHTML 0.14.11; `FastHTML(default_hdrs=False, htmx=False, hdrs=(local Link/Script...))` renders with **no CDN tags** |
| Build a stylesheet with the standalone `tailwindcss` CLI (no Node) and serve it locally | ✅ tailwindcss v4.3.3 standalone `.exe` generated `app.css` (8.9 KB) in 123 ms, offline, no Node/npm |
| Vendored Vega chart renders the Flow hover offline | ✅ Altair `to_dict()` spec (schema `v6.4.1`) rendered by vendored vega/vega-lite/vega-embed; **hover interaction fires** (point opacity flips on `mouseover`) |
| `grep` served HTML for external origins → zero | ✅ **Zero external requests** (headless network capture) and zero external origin *fetches*. One inert string remains — the Vega-Lite `$schema` id (see §4) |
| Pin FastHTML + tailwindcss CLI versions | ✅ see §2 |
| Record Streamlit cold-start baseline | ✅ see §6 |

**No fallback triggered.** Zero-CDN config works and offline Vega works, so chart option A stands
(option B/C not needed). The only thing needing a user call is the **CSP posture** (§5) — both viable
options keep option A and full offline.

## 2. Pinned versions

| Component | Version | How pinned |
|---|---|---|
| `python-fasthtml` | **0.14.11** | `requirements.txt` (Task 3) |
| `tailwindcss` standalone CLI | **v4.3.3** (`tailwindcss-windows-x64.exe`) | vendored binary / documented (not pip) |
| htmx | **2.0.10** | vendored `.js` |
| vega | **6.3.1** | vendored `.js` |
| vega-lite | **6.4.1** | vendored `.js` — **must match** Altair 6.1.0's emitted schema `v6.4.1` |
| vega-embed | **7.1.0** | vendored `.js` |
| vega-interpreter | **2.3.1** | bundled to IIFE via esbuild (build-time Node only) — see §5 |

Altair (installed **6.1.0**) emits `$schema: .../vega-lite/v6.4.1.json` and targets vega 6 / vega-embed 7.
The vendored vega-lite is pinned to that exact schema so `chart.to_dict()` old-vs-new stays byte-diffable (§8 of the plan).

## 3. Vendored-asset SHA-256 (supply chain)

| Asset | Bytes | SHA-256 | Upstream |
|---|---|---|---|
| htmx.min.js | 51,238 | `71ea67185bfa8c98c39d31717c6fce5d852370fcdfd129db4543774d3145c0de` | `cdn.jsdelivr.net/npm/htmx.org@2.0.10/dist/htmx.min.js` |
| vega.min.js | 515,801 | `70bfdc84b15f11f3fb3469a24af03314b3222dece4f2c8615e542f183f8f775a` | `.../npm/vega@6.3.1/build/vega.min.js` |
| vega-lite.min.js | 249,075 | `6d5035fdd429b4bc6f91f3754426c3f516f3dbd8e08b105cfc2496bed4ebd254` | `.../npm/vega-lite@6.4.1/build/vega-lite.min.js` |
| vega-embed.min.js | 60,077 | `c36254270219eee58fb9b1d954decad954fb07bfc9ab780c5d4401bd445cd50c` | `.../npm/vega-embed@7.1.0/build/vega-embed.min.js` |
| vega-interpreter.bundle.js | 6,686 | `4cd272e83df53623826da36fbeab137c5789a9fd72ae40285b6372c7e0bf463e` | esbuild IIFE of `vega-interpreter@2.3.1` (see §5) |
| tailwindcss.exe (CLI) | 112,503,296 | `e0e260ce048014e9268f6237ff18f8ccf02cef521cbd0ae04e82c2cdf7aa3955` | `github.com/tailwindlabs/tailwindcss/releases/download/v4.3.3/tailwindcss-windows-x64.exe` |

Runtime JS total ≈ **0.86 MB** (htmx + vega + vega-lite + vega-embed + interpreter). The 112 MB
tailwind CLI is a **build-time** tool — only the generated CSS ships; **do not commit the binary to git**
(fetch-by-URL + verify SHA-256 in a `scripts/` helper, or keep it out of tree). The `vega-interpreter.bundle.js`
SHA is build-output; regenerate with the command in §5 and re-verify.

## 4. The one remaining "external" string is inert

The served HTML contains exactly one external URL literal:
`https://vega.github.io/schema/vega-lite/v6.4.1.json`. This is the Vega-Lite **`$schema` identifier**
inside the chart spec — vega-embed **never dereferences it** (headless capture shows zero requests to
that host), and it is **required to be present** because the parity gate (§8) diffs `chart.to_dict()`,
which includes `$schema`. Interpret the plan's "zero external origins" as **zero fetched origins** — met.
The CSP header (§5) would in any case block an accidental fetch.

## 5. KEY FINDING — strict CSP vs Vega, and the resolution

`Content-Security-Policy: default-src 'self'` (as written in plan §9) **breaks Vega rendering** in three
escalating ways, each surfaced and resolved in the spike:

1. **Inline `<script>` that calls `vegaEmbed(...)` is blocked.** → Fix: move the embed call to an
   **external** `/static/embed.js`; pass the spec as a **non-executable** `<script type="application/json">`
   data block (not governed by `script-src`) that the external script reads. No inline JS anywhere.
2. **vega-embed applies inline element styles** (blocked by `default-src`). → Fix: relax **styles only** —
   `style-src 'self' 'unsafe-inline'`. `script-src` stays strict `'self'`.
3. **Vega compiles spec expressions with `new Function()` (needs `'unsafe-eval'`).** This is the load-bearing
   one — a **negative control** proved it: with strict CSP and *no* interpreter, the chart does **not** render
   and throws the `unsafe-eval` violation. → Fix: use Vega's **CSP-safe AST interpreter** (`vega-interpreter`),
   passed to vega-embed as `{ ast: true, expr: vegaInterp.expressionInterpreter }`, so no `new Function`/eval runs.

`vega-interpreter` ships **ESM-only** with a bare `vega-util` import, so it can't be a plain `<script>` and
can't resolve in-browser without an import map. Resolution used in the spike: **bundle it to a self-contained
IIFE global** (`window.vegaInterp`) with esbuild (Node, build-time only; output is 6.5 KB vendored JS, runtime
stays Node-free):

```
npm install vega-interpreter@2.3.1 esbuild
echo "export { expressionInterpreter } from 'vega-interpreter';" > interp-entry.js
npx esbuild interp-entry.js --bundle --format=iife --global-name=vegaInterp --minify \
    --outfile=app/static/vendor/vega-interpreter.bundle.js
```

**Confirmed working CSP** (chart renders, hover fires, console clean, zero external, no eval):
```
Content-Security-Policy: default-src 'self'; style-src 'self' 'unsafe-inline'
```

### DECISION for the gate (chart CSP posture — both keep option A + full offline)

- **Option A-strict (recommended, spike-proven):** bundle `vega-interpreter` (one Node build-time step,
  one extra vendored+checksummed 6.5 KB asset) → CSP has **no `unsafe-eval`**, scripts are `'self'`-only.
  Strongest security posture; matches plan §9's intent. Cost: a second build tool (Node) exists solely to
  produce that one bundle (Tailwind's CLI is already no-Node; this is the only Node touchpoint).
- **Option A-loose:** skip the interpreter, set `script-src 'self' 'unsafe-eval'`. No Node at all, no
  interpreter asset. Cost: `'unsafe-eval'` weakens the CSP (all scripts are still `'self'`/no-inline, and the
  app is read-only localhost, so practical risk is low — but it is a real relaxation of the plan's stated posture).

Either way `style-src` needs `'unsafe-inline'` for Vega's inline styles (low risk). Recommend **A-strict**
unless you want zero Node in the toolchain, in which case **A-loose** is acceptable given the read-only/localhost posture.

## 6. Streamlit cold-start baseline (anchors plan §3 / §12)

Measured against a 4-snapshot store (weekly `opps_2026-07-20..08-10`, 400–409 rows each — the multi-snapshot
path that recomputes trajectory/flow series, i.e. the heavy case).

| What it measures | Value |
|---|---|
| Streamlit **script compute / data render** (`AppTest.run()`), **cold** first run (import-dominated) | **≈ 4.35 s** |
| Streamlit script compute, **warm** subsequent run | **≈ 0.55 s** |
| Streamlit HTTP **shell** (server answers `/` with the SPA shell) | ≈ 0.36 s |

**Caveat that makes this a fair anchor:** Streamlit's HTTP `200` (≈0.36 s) is only the SPA *shell* — the data
renders over a websocket **after** that, so the "time until the numbers appear" is the **script compute
(~4.35 s cold / ~0.55 s warm)**. FastHTML server-renders the data *into* the HTML, so its comparable cold
number is server boot (spike measured **~0.26 s**) **+** the view-model compute (~0.55 s-class once warm).
The §12 target — "FastHTML cold start no worse than the Streamlit baseline" — is therefore **~4.35 s cold**,
which server-rendering should beat comfortably. Re-measure the real FastHTML page in Task 3/§12 and paste both.

## 7. Other spike findings that shape Task 3

- **`fasthtml.serve()` defaults to `host="0.0.0.0"`, not localhost.** The plan §9 bind requirement is a real
  action item — the new listener **must pass `host="127.0.0.1"` explicitly** (validated in the spike). Do not
  rely on a uvicorn default; FastHTML's wrapper overrides it to 0.0.0.0.
- **CSP is applied via Starlette `BaseHTTPMiddleware`** (`app.add_middleware(...)`); FastHTML has no
  `@app.middleware` decorator (it does have `add_middleware`). Header verified present on the response.
- **`width:"container"` charts** measured width 0 under headless (container width resolved before layout). A
  real browser sizes them correctly; for Task 3, ensure the chart container has a resolved width (Tailwind
  width utility on the parent) and re-check in Task 4.5.
- **Env hygiene note:** installing `python-fasthtml` globally bumped `starlette` to 1.6.0, which trips
  pre-existing pins in unrelated global tools (`serena-agent`, `theharvester`). The repo suite is unaffected
  (Streamlit uses tornado, not starlette) — **re-ran `pytest tests/` → 138 passed** after all installs. A
  dedicated venv is the clean long-term home for the FastHTML deps (Task 3 adds them to `requirements.txt`).

## 8. Reference: the working wiring (for Task 3)

- Headers: local `Link`(app.css) + `Script`(htmx) + `Script`(vega, vega-lite, **vega-interpreter.bundle**, vega-embed).
- Body per chart: `Div(id="flow")` + `<script type="application/json" id="flow-spec">{spec}</script>` +
  external `Script(src="/static/vendor/embed.js")`.
- `embed.js`: for each `script[type=application/json][id$=-spec]`, `vegaEmbed('#'+base, JSON.parse(text),
  { actions:false, ast:true, expr: window.vegaInterp.expressionInterpreter })`.
- CSP middleware sets `default-src 'self'; style-src 'self' 'unsafe-inline'` (A-strict) — or add
  `script-src 'self' 'unsafe-eval'` and drop the interpreter (A-loose).
- Serve with `serve(host="127.0.0.1", ...)`.
