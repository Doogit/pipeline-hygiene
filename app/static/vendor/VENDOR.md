# Vendored front-end assets (offline-first, zero-CDN)

All runtime assets are served locally from `app/static`; the page makes **no
external requests** and sends `Content-Security-Policy: default-src 'self';
style-src 'self' 'unsafe-inline'`. Record the exact upstream version and SHA-256
on every update, and re-verify (`sha256sum -c`).

## Runtime JS (served to the browser)

| Asset | Version | SHA-256 | Provenance |
|---|---|---|---|
| htmx.min.js | 2.0.10 | `71ea67185bfa8c98c39d31717c6fce5d852370fcdfd129db4543774d3145c0de` | `cdn.jsdelivr.net/npm/htmx.org@2.0.10/dist/htmx.min.js` |
| vega.min.js | 6.3.1 | `70bfdc84b15f11f3fb3469a24af03314b3222dece4f2c8615e542f183f8f775a` | `cdn.jsdelivr.net/npm/vega@6.3.1/build/vega.min.js` |
| vega-lite.min.js | 6.4.1 | `6d5035fdd429b4bc6f91f3754426c3f516f3dbd8e08b105cfc2496bed4ebd254` | `cdn.jsdelivr.net/npm/vega-lite@6.4.1/build/vega-lite.min.js` — **must match** Altair 6.1.0's emitted `$schema` v6.4.1 |
| vega-embed.min.js | 7.1.0 | `c36254270219eee58fb9b1d954decad954fb07bfc9ab780c5d4401bd445cd50c` | `cdn.jsdelivr.net/npm/vega-embed@7.1.0/build/vega-embed.min.js` |
| vega-interpreter.bundle.js | 2.3.1 (bundled) | `4cd272e83df53623826da36fbeab137c5789a9fd72ae40285b6372c7e0bf463e` | esbuild IIFE of `vega-interpreter@2.3.1` — see below |
| embed.js | (local) | `b91401736de4d42053f089e161d312c13b62ac8678b5e338aa6dba7cb580886d` | this repo |
| ui.js | (local) | `b8dea92a2f5492d0c6aa32a91d23572b2b713bb5ec81bcf47950fe7f306ae7a3` | this repo |

The CSP-safe **AST interpreter** is why Vega runs under a strict CSP with no
`unsafe-eval` (Vega otherwise compiles spec expressions via `new Function`). It
ships ESM-only with a bare `vega-util` import, so it is bundled to a
self-contained IIFE global (`window.vegaInterp`) at build time (Node only for
this one asset; the runtime stays Node-free):

```
npm install vega-interpreter@2.3.1 esbuild
echo "export { expressionInterpreter } from 'vega-interpreter';" > interp-entry.js
npx esbuild interp-entry.js --bundle --format=iife --global-name=vegaInterp \
    --minify --outfile=app/static/vendor/vega-interpreter.bundle.js
```

## Generated CSS

| Asset | SHA-256 | Built from |
|---|---|---|
| ../app.css | `b5ca30df1e1f992c505c5c1feeb29d4f7413ed89c97388b6ebdfc5346a6d4256` | `app/tailwind.css` via tailwindcss CLI v4.3.3 |

Rebuild offline with the standalone (no-Node) tailwindcss CLI — see
`scripts/build_css.sh`. The generated `app/static/app.css` is committed; the CLI
binary is **not** committed (112 MB) — fetch it per that script and verify:

| Tool | Version | SHA-256 | Provenance |
|---|---|---|---|
| tailwindcss CLI | v4.3.3 (`tailwindcss-windows-x64.exe`) | `e0e260ce048014e9268f6237ff18f8ccf02cef521cbd0ae04e82c2cdf7aa3955` | `github.com/tailwindlabs/tailwindcss/releases/download/v4.3.3/` |
