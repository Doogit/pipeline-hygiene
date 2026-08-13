"""Rule-to-draft generators (Monday Packet, PR B / R2).

Each fired hygiene rule that today only prints a coaching prompt becomes one or
more drafted work items — a proposed next step, a close-date correction plus a
timeline-validation email, a disqualification-review doc, a 1:1 agenda item, a
pipeline-generation task list. Every template string lives in config's `drafts:`
block (R2.6): editable without code, and NO LLM — that is PR C. The output is a
pure, deterministic function of (snapshot rows, config, as_of), so re-running a
build proposes byte-identical payloads and the idempotent ledger (PR A) records
no duplicates (acceptance #1).

`as_of` is threaded everywhere; date.today() never appears here.
"""
import json
from datetime import date, timedelta
from statistics import median

from .work_items import normalize_owner, packets_enabled

# R2.5 is owner-scoped, but work_items.opp_id is NOT NULL. Owner-level items use
# this sentinel opp_id (no real opp collides with it) so the ledger's per-opp
# keying and idempotency still hold. Documented, not a real opportunity.
OWNER_SCOPE_SOURCE = "low_coverage"


def owner_scope_id(owner):
    """Sentinel opp_id for an owner-scoped (non-opportunity) work item."""
    return f"<owner:{normalize_owner(owner)}>"


def _money(amount, config):
    symbol = config.get("display_currency_symbol") or "$"
    return "n/a" if amount is None else f"{symbol}{amount:,.0f}"


def _add_business_days(start, n):
    """start + n business days (weekends skipped). Deterministic, calendar-free."""
    d = start
    added = 0
    while added < n:
        d += timedelta(days=1)
        if d.weekday() < 5:
            added += 1
    return d


def _fallback_date(config, as_of):
    return _add_business_days(
        as_of, config["drafts"]["proposed_date_offset_business_days"])


# --- R2.1: missing/expired next step (H4) -> proposed next-step text ---

def next_step_draft(row, config, as_of):
    """Template branch only (R2.1): 'Confirm <stage-exit criterion> by <date>'.
    The note-linked branch depends on PR C. Criterion table is per open stage in
    config; the due date is the deterministic stage template (as_of + Nbd)."""
    drafts = config["drafts"]
    criterion = drafts["stage_exit"][row["stage"]]
    due = _fallback_date(config, as_of)
    text = drafts["next_step_template"].format(criterion=criterion,
                                               due_date=due.isoformat())
    return {"item_type": "next_step",
            "payload": {"draft_text": text, "due_date": due,
                        "stage": row["stage"], "criterion": criterion}}


# --- R2.2: stale / passed close date (H2, H3) -> proposed date + email ---

def proposed_close_date(config, as_of, intervals):
    """Proposed close date from the deal's own push history. With >= 2 observed
    pushes, it is as_of + the median push size (the deal's typical slip in days);
    with fewer than 2 it degrades to the stage template (as_of + Nbd, same math
    as H4). Both anchor at as_of so the proposal is always a future,
    deterministic date regardless of how stale the current close date is (a
    past H2 close date offers nothing to project from)."""
    if len(intervals) >= 2:
        return as_of + timedelta(days=int(round(median(intervals))))
    return _fallback_date(config, as_of)


def close_date_drafts(row, config, as_of, intervals):
    """A close-date field update plus a drafted timeline-validation email
    (R2.2). Both carry the same proposed date; the email's merge fields come
    from the opp row. Returns the two items in a stable order."""
    proposed = proposed_close_date(config, as_of, intervals)
    basis = (f"median of {len(intervals)} observed push(es)"
             if len(intervals) >= 2 else "stage template (no push history)")
    email = config["drafts"]["timeline_email"]
    merge = {
        "account": row["account"], "opp_name": row["opp_name"],
        "owner": row["owner"], "opp_id": row["opp_id"],
        "close_date": row["close_date"].isoformat()
        if row["close_date"] is not None else "n/a",
        "proposed_close_date": proposed.isoformat(),
    }
    return [
        {"item_type": "field_update",
         "payload": {"field": "close_date", "proposed_value": proposed,
                     "basis": basis}},
        {"item_type": "email_draft",
         "payload": {"subject": email["subject"].format(**merge),
                     "body": email["body"].format(**merge),
                     "merge_fields": merge}},
    ]


# --- R2.3: 3+ pushes (H3 / disqualify_review_pushes) -> disqualification doc ---

def disqualification_doc(row, config, as_of, committed_quarter, intervals):
    """Markdown disqualification-review clinic doc (R2.3): the push history, a
    drift sparkline of the individual pushes, the quarter the deal was
    originally committed for (from the commit ledger), and the two review
    questions from config. Deterministic — golden-tested."""
    push_count = row.get("push_count") or 0
    cumulative = row.get("cumulative_extension_days") or 0
    max_push = row.get("max_push_days") or 0
    sparkline = " ".join(f"+{d}d" for d in intervals) or "n/a"
    committed = committed_quarter or "not previously committed"
    questions = config["drafts"]["disqualification"]["questions"]
    lines = [
        f"# Disqualification review — {row['account']} ({row['opp_id']})",
        "",
        f"Owner: {row['owner']} · Stage: {row['stage']} · "
        f"Amount: {_money(row['amount'], config)}",
        f"Originally committed for: {committed}",
        "",
        "## Push history",
        f"- Close date pushed later {push_count} time(s), {cumulative}d later "
        f"cumulatively (largest single push {max_push}d).",
        f"- Drift per push: {sparkline}",
        "",
        "## Review questions",
        f"1. {questions[0]}",
        f"2. {questions[1]}",
    ]
    markdown = "\n".join(lines) + "\n"
    return {"item_type": "clinic_doc",
            "payload": {"markdown": markdown, "committed_quarter": committed_quarter,
                        "push_count": push_count, "drift": list(intervals)}}


# --- R2.4: risky commit -> 1:1 agenda item ---

def agenda_item_draft(entry, rank, config):
    """1:1 agenda item straight from a brief.risky_commits entry (R2.4): the
    coaching prompt, dollar rank, and the risky rules already computed there."""
    row = entry["row"]
    return {"item_type": "agenda_item",
            "payload": {"title": f"{row['account']} ({row['opp_id']})",
                        "prompt": entry["prompt"],
                        "risky_rules": list(entry["risky_rules"]),
                        "dollar_rank": rank,
                        "amount": row["amount"],
                        "forecast_category": row["forecast_category"]}}


# --- R2.5: low coverage (owner) -> pipeline-generation task-list stub ---

def coverage_task_draft(owner, config):
    """Pipeline-generation task list for an owner under coverage (R2.5). Play
    names come from config; no external trigger data (explicitly v1-scoped)."""
    drafts = config["drafts"]
    plays = list(drafts["coverage_plays"])
    header = drafts["coverage_task_template"].format(owner=owner)
    markdown = header + "\n" + "\n".join(f"- [ ] {p}" for p in plays) + "\n"
    return {"item_type": "clinic_doc",
            "payload": {"markdown": markdown, "owner": owner, "plays": plays}}


# --- router ---

def generate(rule_id, row, result, config, as_of, *, context=None):
    """Route one fired rule to zero or more draft work items, each a
    {item_type, payload} dict ready for WorkItemStore.upsert_item. Pure: the
    same (rule_id, row, config, as_of, context) always yields the same output.

    `context` carries per-snapshot derived inputs a single row cannot provide —
    {'intervals': {opp_id: [push_days]}, 'committed_quarters': {opp_id: q}}.
    Absent -> the close-date median degrades to the template and the
    disqualification doc reports no committed quarter (still deterministic).
    """
    context = context or {}
    intervals = (context.get("intervals") or {}).get(row["opp_id"], [])
    if rule_id == "H4":
        return [next_step_draft(row, config, as_of)]
    if rule_id in ("H2", "H3"):
        items = close_date_drafts(row, config, as_of, intervals)
        if rule_id == "H3" and (row.get("push_count") or 0) \
                >= config["disqualify_review_pushes"]:
            quarter = (context.get("committed_quarters") or {}).get(row["opp_id"])
            items.append(disqualification_doc(row, config, as_of, quarter,
                                              intervals))
        return items
    return []


# --- packet build entry point ---

def build_work_items(store, wi_store, snapshot_date, config, as_of, *,
                     by="system"):
    """Evaluate a stored snapshot, route every violation (plus the risky-commit
    and low-coverage rollups) through the generators, upsert each item into the
    ledger keyed by its source rule, then expire items resolved in the newest
    snapshot. Idempotent: re-running on the same snapshot upserts identical
    payloads and adds no new open items (acceptance #1). No-op when packets are
    disabled (flag-off invariance). Returns {upserted, expired, skipped}."""
    if not packets_enabled():
        return {"upserted": 0, "expired": 0, "skipped": True}

    from . import brief
    data = brief.build(store, snapshot_date, as_of, config)
    rows_by_id = {r["opp_id"]: r for r in data["rows"]}
    context = {
        "intervals": store.close_date_intervals(snapshot_date),
        "committed_quarters": {e.opp_id: e.committed_quarter
                               for e in (data["ledger"] or [])},
    }
    snapshot_id = snapshot_date.isoformat()
    upserted = 0

    def push(opp_id, owner, source, item):
        wi_store.upsert_item(opp_id=opp_id, owner=owner, source=source,
                             item_type=item["item_type"], payload=item["payload"],
                             as_of=as_of, snapshot_id=snapshot_id, by=by)

    # per-violation routing: source is the rule so expiry re-evaluates it
    for opp_id, opp_result in data["results"].items():
        row = rows_by_id[opp_id]
        fired = {v.rule_id for v in opp_result.violations}
        for v in opp_result.violations:
            # H3 emits the same close-date drafts as H2 plus, when applicable,
            # the disqualification doc. Keep one open close-date/email pair so
            # expiry remains tied to the strongest still-fired close-date rule.
            if v.rule_id == "H2" and "H3" in fired:
                continue
            for item in generate(v.rule_id, row, opp_result, config, as_of,
                                 context=context):
                push(opp_id, row["owner"], v.rule_id, item)
                upserted += 1

    # R2.4: risky commits -> agenda items (source = the dominant risky rule)
    for rank, entry in enumerate(data["risky_commits"], start=1):
        row = entry["row"]
        push(row["opp_id"], row["owner"], entry["dominant"],
             agenda_item_draft(entry, rank, config))
        upserted += 1

    # R2.5: low-coverage owners -> pipeline-generation task list (owner-scoped)
    for stats in data["owners"].values():
        if stats.coverage_flagged:
            push(owner_scope_id(stats.owner), stats.owner, OWNER_SCOPE_SOURCE,
                 coverage_task_draft(stats.owner, config))
            upserted += 1

    expired_low_coverage = _expire_resolved_low_coverage(wi_store, data["owners"],
                                                         as_of, by)
    expired = wi_store.expire_resolved(data["rows"], config, as_of, by=by)
    return {"upserted": upserted, "expired": expired + expired_low_coverage,
            "skipped": False}


def _expire_resolved_low_coverage(wi_store, owner_stats, as_of, by):
    """Expire owner-scoped low-coverage tasks once the owner is no longer
    flagged. WorkItemStore's generic rule expiry cannot evaluate this synthetic
    source because it is produced from owner rollups, not a per-opp rule."""
    expired = 0
    still_flagged = {stats.owner for stats in owner_stats.values()
                     if stats.coverage_flagged}
    for item in wi_store.items(open_only=True):
        if item["source"] != OWNER_SCOPE_SOURCE:
            continue
        payload = json.loads(item["payload_json"])
        owner = payload.get("owner")
        if owner not in still_flagged:
            wi_store.transition(item["id"], "expired", at=as_of, by=by,
                                reason="resolved_in_source")
            expired += 1
    return expired
