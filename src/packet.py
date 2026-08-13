"""Packet assembly + export (Monday Packet, PR D / R4).

`build_owner_packet(...)` turns one owner's OPEN work items (proposed / accepted
/ edited) from the PR-A ledger into a `PacketModel`: five dollar-ranked sections
(field updates + one consolidated paste-ready CRM block, next steps, drafted
emails, review docs, and a one-line hygiene-score delta), plus a verbatim
drafts-only header and a computed minutes-to-clear estimate. `render_markdown`
and `render_html` are pure serialisers of that model.

Pure functions only — NO app/ import. `as_of` is threaded through every
time-evaluating call; date.today() never appears here. No-op safe when packets
are disabled (the caller only builds work items behind the flag; with no open
items the packet is an explicit empty state).

Relationship to the existing `--digests` surface (resolves the PR-D P1 product
question, since README lives outside this PR's file-ownership scope):

    The Monday Packet COEXISTS with the private per-owner coaching digest
    (`brief.write_digests` -> out/digests/<as_of>/<owner>.md); it does NOT
    replace it. They are different artifacts for different jobs:

      - The digest is a READ-ONLY coaching mirror: the seller's own row from
        the shared desk brief plus their flagged deals, "never a scorecard,
        never a comp input". It tells the seller WHAT is wrong and WHY.
      - The packet is an ACTION queue of drafted, reviewable artifacts (field
        updates, next steps, emails, review docs) the seller pastes/sends after
        review. It tells the seller WHAT TO DO about it, and is the surface the
        accept/dismiss lifecycle (PR E) operates on.

    The digest explains; the packet drafts the fix. A seller who reads their
    digest and wants to act opens their packet. Neither is a second competing
    copy of the other — the packet carries no rankings or peer figures, and the
    digest carries no drafted artifacts or CRM block.
"""
from dataclasses import dataclass, field
from datetime import date

from src.pipeline_hygiene_view import _money
from src.work_items import normalize_owner

# Verbatim drafts-only header (R4.4). <label> is the snapshot label; every other
# character is fixed. Do not edit without updating the golden packet + tests.
HEADER_TEMPLATE = ("Drafts only — review before use. Generated from snapshot "
                   "{label}. Nothing was sent or written on your behalf.")

# Section order (R4.1). Field updates first (they carry the paste-ready CRM
# block), then next steps, emails, review docs, and the score delta.
_SECTION_TITLES = {
    "field_updates": "Field updates",
    "next_steps": "Next steps",
    "emails": "Emails (drafts)",
    "review_docs": "Review docs",
    "score_delta": "Hygiene score",
}
_SECTION_ORDER = ["field_updates", "next_steps", "emails", "review_docs",
                  "score_delta"]

# Which item_type feeds which section.
_TYPE_SECTION = {
    "field_update": "field_updates",
    "next_step": "next_steps",
    "email_draft": "emails",
    "clinic_doc": "review_docs",
    "agenda_item": "review_docs",
}


@dataclass
class PacketItem:
    """One drafted work item positioned in the packet, with the amount it was
    dollar-ranked by and whether it is accepted (drives CRM-block inclusion)."""
    work_item_id: int
    opp_id: str
    item_type: str
    status: str
    payload: dict
    amount: float

    @property
    def accepted(self):
        return self.status == "accepted"


@dataclass
class PacketModel:
    owner: str                       # display owner name
    owner_normalized: str
    snapshot_label: str
    header: str                      # verbatim drafts-only line (R4.4)
    sections: dict                   # section key -> list[PacketItem]
    crm_block: str                   # consolidated paste-ready block (accepted only)
    score_delta_line: str            # one-line hygiene-score delta (R4.1 §5)
    minutes_to_clear: int            # computed estimate (R4.5)
    item_count: int
    generated_as_of: date = field(default=None)


# --- assembly -------------------------------------------------------------

def _amount_by_opp(snapshot_store, snapshot_date):
    if snapshot_store is None or snapshot_date is None:
        return {}
    return {r["opp_id"]: (r["amount"] or 0.0)
            for r in snapshot_store.load_rows(snapshot_date)}


def _item_amount(item, payload, amount_by_opp):
    # agenda items carry their own amount; everything else keys on the opp's
    # snapshot amount (missing -> 0.0, so it ranks last, deterministically).
    if item["item_type"] == "agenda_item" and payload.get("amount") is not None:
        return payload["amount"] or 0.0
    return amount_by_opp.get(item["opp_id"], 0.0)


def _minutes_to_clear(items, config):
    """item_count x per-type minute constants (R4.5). The estimate is COMPUTED
    and printed as-is — no fixed "15-minute" promise a large packet would
    falsify (resolves the P2 slogan risk)."""
    per_type = config["drafts"]["packet"]["minutes_per_item"]
    default = config["drafts"]["packet"].get("default_minutes_per_item", 1)
    total = sum(per_type.get(it.item_type, default) for it in items)
    return int(total)


def _crm_line(item):
    """One accepted field_update rendered for the paste-ready CRM block."""
    p = item.payload
    value = p.get("proposed_value")
    if isinstance(value, date):
        value = value.isoformat()
    return f"{item.opp_id} · {p.get('field')} = {value}"


def _crm_block(field_items):
    """Consolidated paste-ready CRM block from ACCEPTED field updates only
    (R4.1 §1; the accept/dismiss E2E is PR E — here filtered by fixture status).
    Empty accepted set -> an explicit placeholder, never a stray blank block."""
    accepted = [it for it in field_items if it.accepted]
    if not accepted:
        return "(no accepted field updates yet)"
    return "\n".join(_crm_line(it) for it in accepted)


def _score_delta_line(snapshot_store, snapshot_date, owner, config, as_of):
    """One-line hygiene-score delta since the previous stored snapshot (R4.1
    §5). Snapshot-anchored and cache-free — derived from existing snapshots, no
    packet_runs table (Q4). Degrades to a plain 'no prior snapshot' note."""
    if snapshot_store is None or snapshot_date is None:
        return "Hygiene score delta unavailable (no snapshot store)."
    prior = [d for d in snapshot_store.snapshot_dates() if d < snapshot_date]
    current = _owner_mean_score(snapshot_store, snapshot_date, owner, config,
                                as_of)
    if current is None:
        return "Hygiene score: n/a (no open opps to score)."
    if not prior:
        return f"Hygiene score: {current:.1f} (no prior snapshot to compare)."
    prev_date = prior[-1]
    previous = _owner_mean_score(snapshot_store, prev_date, owner, config,
                                 prev_date)
    if previous is None:
        return (f"Hygiene score: {current:.1f} "
                f"(no comparable score at {prev_date.isoformat()}).")
    delta = current - previous
    return (f"Hygiene score: {current:.1f} ({delta:+.1f} since "
            f"{prev_date.isoformat()}).")


def _owner_mean_score(snapshot_store, snapshot_date, owner, config, as_of):
    from src import brief
    data = brief.build(snapshot_store, snapshot_date, as_of, config,
                       owner_filter={owner})
    stats = data["owners"].get(owner)
    return None if stats is None else stats.mean_score


def _resolve_owner_name(snapshot_store, snapshot_date, owner_normalized):
    """Best display name for a normalized owner: from the snapshot rows if the
    store is present, else the normalized key itself."""
    if snapshot_store is not None and snapshot_date is not None:
        for name in snapshot_store.owners(snapshot_date):
            if normalize_owner(name) == owner_normalized:
                return name
    return owner_normalized


def build_owner_packet(wi_store, owner_normalized, config, as_of, *,
                       snapshot_store=None, snapshot_date=None):
    """Assemble one owner's packet from their OPEN work items (R4.1).

    `wi_store` is the PR-A `WorkItemStore` (the source of drafted items).
    `snapshot_store`/`snapshot_date` (the `SnapshotStore` + evaluated date) are
    optional but supply the dollar-ranking amounts and the hygiene-score delta;
    without them amounts degrade to 0.0 (stable order) and the delta degrades to
    a note. Pure: same ledger + snapshot + as_of -> same PacketModel.
    """
    import json

    owner_normalized = normalize_owner(owner_normalized)
    amount_by_opp = _amount_by_opp(snapshot_store, snapshot_date)

    packet_items = []
    for row in wi_store.items(open_only=True, owner=owner_normalized):
        payload = json.loads(row["payload_json"])
        packet_items.append(PacketItem(
            work_item_id=row["id"], opp_id=row["opp_id"],
            item_type=row["item_type"], status=row["status"], payload=payload,
            amount=_item_amount(row, payload, amount_by_opp)))

    # Dollar-rank WITHIN each section: descending amount, opp_id then id as the
    # deterministic tie-break.
    sections = {key: [] for key in _SECTION_ORDER}
    for it in packet_items:
        sections[_TYPE_SECTION[it.item_type]].append(it)
    for key in ("field_updates", "next_steps", "emails", "review_docs"):
        sections[key].sort(key=lambda it: (-it.amount, it.opp_id,
                                           it.work_item_id))

    owner_name = _resolve_owner_name(snapshot_store, snapshot_date,
                                     owner_normalized)
    label = snapshot_date.isoformat() if snapshot_date is not None else "n/a"
    return PacketModel(
        owner=owner_name,
        owner_normalized=owner_normalized,
        snapshot_label=label,
        header=HEADER_TEMPLATE.format(label=label),
        sections=sections,
        crm_block=_crm_block(sections["field_updates"]),
        score_delta_line=_score_delta_line(snapshot_store, snapshot_date,
                                           owner_name, config, as_of),
        minutes_to_clear=_minutes_to_clear(packet_items, config),
        item_count=len(packet_items),
        generated_as_of=as_of)


# --- export (R4.2): pure PacketModel -> .md / .html -----------------------

def _item_lines_md(item, config):
    """The markdown body for one packet item, by type."""
    p = item.payload
    if item.item_type == "field_update":
        value = p.get("proposed_value")
        if isinstance(value, date):
            value = value.isoformat()
        basis = f" — {p['basis']}" if p.get("basis") else ""
        return [f"- **{item.opp_id}** set `{p.get('field')}` = "
                f"`{value}`{basis} ({_money(item.amount, config)})"]
    if item.item_type == "next_step":
        return [f"- **{item.opp_id}** {p.get('draft_text')} "
                f"({_money(item.amount, config)})"]
    if item.item_type == "email_draft":
        lines = [f"- **{item.opp_id}** DRAFT — {p.get('subject')} "
                 f"({_money(item.amount, config)})", ""]
        lines += [f"  > {ln}" for ln in (p.get("body") or "").splitlines()]
        return lines
    if item.item_type == "agenda_item":
        return [f"- **{item.opp_id}** {p.get('title')}: {p.get('prompt')} "
                f"({_money(item.amount, config)})"]
    # clinic_doc: embed the pre-rendered markdown body verbatim, indented.
    body = (p.get("markdown") or "").rstrip("\n")
    return [f"- **{item.opp_id}** review doc ({_money(item.amount, config)}):",
            ""] + [f"  {ln}" if ln else "" for ln in body.splitlines()]


def render_markdown(model, config):
    """Render a PacketModel to markdown (R4.2). Pure; deterministic; golden."""
    lines = [f"# Monday Packet — {model.owner}", "",
             f"> {model.header}", "",
             f"_Estimated {model.minutes_to_clear} minute(s) to clear "
             f"{model.item_count} item(s)._", ""]
    for key in _SECTION_ORDER:
        lines.append(f"## {_SECTION_TITLES[key]}")
        lines.append("")
        if key == "score_delta":
            lines.append(model.score_delta_line)
            lines.append("")
            continue
        items = model.sections[key]
        if not items:
            lines.append("_None._")
            lines.append("")
        else:
            for it in items:
                lines.extend(_item_lines_md(it, config))
            lines.append("")
        if key == "field_updates":
            lines.append("### Paste-ready CRM block (accepted only)")
            lines.append("")
            lines.append("```")
            lines.append(model.crm_block)
            lines.append("```")
            lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def _esc(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _item_html(item, config):
    p = item.payload
    money = _esc(_money(item.amount, config))
    if item.item_type == "field_update":
        value = p.get("proposed_value")
        if isinstance(value, date):
            value = value.isoformat()
        basis = f" — {_esc(p['basis'])}" if p.get("basis") else ""
        return (f"<li><strong>{_esc(item.opp_id)}</strong> set "
                f"<code>{_esc(p.get('field'))}</code> = "
                f"<code>{_esc(value)}</code>{basis} ({money})</li>")
    if item.item_type == "next_step":
        return (f"<li><strong>{_esc(item.opp_id)}</strong> "
                f"{_esc(p.get('draft_text'))} ({money})</li>")
    if item.item_type == "email_draft":
        return (f"<li><strong>{_esc(item.opp_id)}</strong> "
                f"<span class=\"draft\">DRAFT</span> — "
                f"{_esc(p.get('subject'))} ({money})"
                f"<pre>{_esc(p.get('body') or '')}</pre></li>")
    if item.item_type == "agenda_item":
        return (f"<li><strong>{_esc(item.opp_id)}</strong> "
                f"{_esc(p.get('title'))}: {_esc(p.get('prompt'))} ({money})</li>")
    return (f"<li><strong>{_esc(item.opp_id)}</strong> review doc ({money})"
            f"<pre>{_esc(p.get('markdown') or '')}</pre></li>")


def render_html(model, config):
    """Render a PacketModel to a standalone HTML fragment (R4.2). The verbatim
    header and the computed minutes-to-clear both appear (acceptance #4)."""
    parts = [f"<h1>Monday Packet — {_esc(model.owner)}</h1>",
             f"<p class=\"header\">{_esc(model.header)}</p>",
             f"<p class=\"clear-estimate\">Estimated "
             f"{model.minutes_to_clear} minute(s) to clear "
             f"{model.item_count} item(s).</p>"]
    for key in _SECTION_ORDER:
        parts.append(f"<h2>{_esc(_SECTION_TITLES[key])}</h2>")
        if key == "score_delta":
            parts.append(f"<p>{_esc(model.score_delta_line)}</p>")
            continue
        items = model.sections[key]
        if not items:
            parts.append("<p><em>None.</em></p>")
        else:
            parts.append("<ul>")
            parts.extend(_item_html(it, config) for it in items)
            parts.append("</ul>")
        if key == "field_updates":
            parts.append("<h3>Paste-ready CRM block (accepted only)</h3>")
            parts.append(f"<pre class=\"crm\">{_esc(model.crm_block)}</pre>")
    return "\n".join(parts) + "\n"
