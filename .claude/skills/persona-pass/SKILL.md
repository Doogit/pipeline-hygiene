---
name: persona-pass
description: Run a persona-driven usability simulation of pipeline-hygiene. Role-play each user persona in docs/personas.md against the real product end-to-end (seed a series, ingest, brief, digests, dashboard) to surface usability and product gaps that unit tests and spec review miss. Use before a release, after a feature that changes user-facing output (brief, digest, dashboard, CLI), or when the user asks to "run the persona pass", "simulate usage", or "find UX gaps".
---

# Persona pass

A pre-release usability gate. Unit tests prove the engine is correct; the
persona pass proves the *product* is usable — by making agents finish a real
person's job with the tool's real outputs and logging where they get stuck.

This method found five defects the spec review and the test suite missed
(ingest exiting 0 on total rejection, the dashboard diffing against itself,
non-idempotent run recording, comp-weaponizable pattern labels, a coverage
flag that fired on 97% of owners). Treat it as a standing check, not a one-off.

## When to run

- Before a release or PR that changes any user-facing output: the desk brief,
  the coaching digests, the dashboard, or a CLI's arguments/messages.
- When the user asks to "run the persona pass", "simulate usage as <role>",
  "find UX gaps", or "would a manager/VP/AE actually be able to use this".
- After adding a new output surface, run it against every persona; after a
  narrow change, run only the personas whose primary artifact it touches.

## How to run

1. **Read `docs/personas.md`.** Each persona names a role, a concrete moment
   with a time budget, jobs-to-be-done phrased as questions the tool must
   answer, and an explicit "how they are disappointed." Pick the personas
   whose primary artifact your change touches (all of them for a broad change).

2. **Launch one agent per persona, in parallel, each in its own worktree.**
   Use `subagent_type: "general-purpose"` with `isolation: "worktree"` so the
   simulations cannot collide on `data/` or `out/`. Give each agent:
   - the full persona (paste it — the agent does not share your context),
   - the instruction to work read-only on the product: run anything, but do
     not modify `src/` (config.yaml edits are allowed for personas whose job
     is configuration, e.g. Marcus),
   - the concrete task: set up realistic data via the **documented** flow
     (`python -m src.seed --series 4` or per README → ingest all snapshots →
     `python -m src.brief` per snapshot so since-last-run deltas exist →
     `--digests`), then role-play the persona's jobs against the generated
     artifacts and the dashboard (drive the FastHTML page by booting
     `python -m app.server` on a per-persona port and fetching its routes over
     HTTP, or read the pure view model in `src/pipeline_hygiene_view.py`
     directly — it is what the page renders),
   - the required return shape (below).

3. **Make each agent return a self-contained report:**
   - **(a) friction log** — each entry: what the persona tried, what happened,
     severity (blocker / major / minor). Prefer verbatim quotes from the
     actual generated output over paraphrase.
   - **(b) per-job verdict** — served / partial / unserved, with evidence.
   - **(c) top 5 improvements ranked for this persona**, each with a one-line
     implementation sketch honest to the deterministic / read-only /
     "agents inspect, people sell" ethos. Reject any idea that needs CRM
     write-back or activity capture — the tool is read-only over CSV snapshots.
   - **(d) 2–3 things the tool does surprisingly well** for this persona.

4. **Synthesize across personas.** The strong signal is *convergence*: a defect
   independently reported by several personas, or a gap that both a persona sim
   and external research surface from opposite directions. Rank findings by
   convergence and by how directly they block a job. Separate P0 correctness /
   trust bugs (small diffs, ship now) from larger structural features
   (team rollups, new ledgers — their own PRs).

## Cleanup

Persona agents leave worktrees and branches behind. After synthesizing:

```
git worktree list                       # find agent-* worktrees
git worktree remove --force <path>      # per leftover worktree
git worktree prune
git branch -D <leftover-branches>
```

Their generated `data/`, `out/`, and `*.db` artifacts live inside the removed
worktrees, so removing the worktree cleans them up. Never let a persona sim
write into the primary checkout's `data/`.

## Honest to the ethos

The personas exist to defend the product's premise, not to bolt on features.
When a persona wants something the tool deliberately does not do — contact a
seller, write to the CRM, score with opaque AI — that is a finding about
*framing and documentation*, not a feature request. Record it as such.
