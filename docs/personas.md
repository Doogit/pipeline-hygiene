# User personas for the persona pass

These are the people who *use* pipeline-hygiene (distinct from the seller
personas the seed simulator plants *inside* the data — clean operator,
sandbagger, happy-ears, ghost). Each one has a job to finish with the tool's
outputs. The persona pass (`.claude/skills/persona-pass`) role-plays each of
them against the real product to surface usability and product gaps that unit
tests and spec review miss.

Keep these grounded and specific: a persona is only useful if it has a
concrete job, a time budget, and a way to be disappointed.

---

## Dana — frontline sales manager

- **Context:** 8 direct reports, enterprise segment. It is Monday 8:30am; the
  forecast call is at 9:00. Competent with sales tools, not a programmer — she
  runs documented commands, she does not read source.
- **Jobs to be done:**
  1. Decide which commit/best_case deals to challenge on the call.
  2. Know the one question to ask each rep about each risky deal.
  3. Give her VP a defensible number.
- **How she is disappointed:** whole-desk views when she manages 8 people;
  opp IDs instead of account names she can say out loud; math she has to
  trust rather than see; anything that makes her look wrong in front of the VP.
- **Primary artifact:** the desk brief (page 1) and the per-owner digests.

## Marcus — RevOps analyst

- **Context:** 200-person company, evaluating this open-source tool against
  buying a commercial suite. Fluent in CSV/YAML/CLI, not a Python developer.
  His CRM export is HubSpot-flavored: non-canonical stage names, EUR, blank
  amounts, extra columns the tool does not know, no history columns.
- **Jobs to be done:**
  1. Get his own export ingested and briefed.
  2. Configure quotas/thresholds for his org.
  3. Answer his boss on cost of operation, auditability, extensibility, and
     what commercial capabilities are simply absent.
- **How he is disappointed:** simulator-only docs with no column contract;
  errors that hide in SQLite; silent failure that a cron would call success;
  a stage_map he has to reverse-engineer.
- **Primary artifact:** the README, the ingest CLI, config.yaml.

## Priya — second-line sales VP

- **Context:** over 4 regions / ~60 sellers. Thinks in quarters, coverage, and
  trends — not individual deals. Monthly business review is coming.
- **Jobs to be done:**
  1. Which regions/teams are behind on coverage or hygiene.
  2. Is the desk trending better or worse over the last month.
  3. Forecast credibility — how often do commits actually close.
  4. Where to spend coaching budget.
- **How she is disappointed:** owner-or-desk only, no team/region rollups;
  single-point metrics where she needs trends; named "sandbagging" labels she
  cannot use without turning a coaching tool into a comp weapon; any metric
  that reads as "everyone is failing" and so says nothing.
- **Primary artifact:** the dashboard trajectory/owners tabs, the brief
  headline.

## Jordan — the flagged account executive

- **Context:** an AE whose manager just forwarded their private coaching
  digest. Skeptical, slightly defensive, busy; pattern-matches this to "another
  surveillance tool."
- **Jobs to be done:**
  1. Understand *why exactly* a deal was flagged — specific and checkable, not
     vibes.
  2. Judge whether it is fair for their stage/segment, and see the rule.
  3. Know what to do this week to clear the flags.
  4. Confirm their data is private from peers.
- **How they are disappointed:** bare rule tokens ("H6") with no value or
  threshold; a fixed field they cleared still showing next week; loaded labels
  reaching the room; comparative shame in a desk-wide view.
- **Primary artifact:** their own coaching digest; secondarily whatever of the
  desk brief / dashboard is visible to them.

---

## Adding a persona

A good persona has: a role and scale, a concrete moment with a time budget, a
short list of jobs to be done phrased as questions the tool must answer, and an
explicit "how they are disappointed." Add it here and the persona pass will
pick it up.
