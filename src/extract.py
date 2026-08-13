"""Capture-diff: notes -> proposed field updates (R3).

`capture_proposals(notes, opp_context, proposals, ...) -> proposals[]` validates
manually-entered field-update proposals for one opportunity. Each accepted
proposal becomes a work_items `field_update` (source=capture) via
WorkItemStore.upsert_item (R3.5).

============================ LOCAL-ONLY, NO EGRESS BY DESIGN ========================
This is a local-only tool. It makes NO network calls and has NO LLM/API path.
There is deliberately no hosted extractor, no `ANTHROPIC_API_KEY` (or any other
key) read, and no HTTP client anywhere in this module — nothing here can call out.
Capture is MANUAL ENTRY: a human reads the captured note and enters structured
proposals (field, proposed_value, confidence, evidence_quote); this module's job
is to VALIDATE and record them, never to generate them. (An earlier draft
considered a hosted Anthropic extractor; it was removed to keep the tool
local-only. `tests/test_extract.py::test_module_has_no_network_or_api_path`
guards against a network/API path being reintroduced.)
====================================================================================

SECURITY BOUNDARY (R3.3): the STRICT OUTPUT CONTRACT (`validate_proposal`) is the
gate for every proposal regardless of who typed it. It rejects, and logs (never
shows), any proposal that:
  - names a field outside the canonical schema (unknown field),
  - carries a proposed_value that fails to type-coerce to that field's type,
  - carries an evidence_quote not found VERBATIM (whitespace-normalized) in the
    submitted notes.
Even on the manual path this matters: it stops a mistyped field, a wrong value
type, or an evidence quote that isn't actually in the note from reaching the CRM
block.

EVIDENCE NORMALIZATION (explicit choice): WHITESPACE-ONLY. `_normalize` collapses
every run of Unicode whitespace to a single ASCII space and strips the ends. It
does NOT casefold and does NOT apply Unicode NFKC. Consequences, tested in
test_extract.py:
  - a case-variant evidence_quote (different letter case than the notes) is NOT
    found verbatim -> REJECTED.
  - a homoglyph evidence_quote (e.g. Cyrillic 'е' for Latin 'e') is NOT found
    verbatim -> REJECTED.
This is the strict posture: only exact-character evidence (modulo whitespace)
proves the entered quote came from the real note, which is exactly what an
anti-tamper gate should require.

ENTITY RESOLUTION (R3.4, resolves plan Q3): today's opportunities schema carries
only `contact_count` (an integer), never a per-opp contact LIST, so there is no
roster to fuzzy-match person shorthand (initials/first names) against. Per Q3 we
DEGRADE: with no contact list available, all person shorthand is shown as-is and
FLAGGED unresolved (`unresolved_entities` on each proposal). Wiring real entity
resolution is deferred until a contacts source exists.

as_of is threaded for logging/capture; this module has no CLI entry point, so it
never calls date.today().
"""
import re
from datetime import date

# Canonical, capture-updatable fields and their value types. A field_update
# proposal may only target one of these; anything else is an unknown field and
# is rejected. Kept intentionally NARROW — capture proposes the human-editable
# CRM fields, never derived/history columns (close_date_changes,
# stage_entered_date) or identity columns (opp_id, owner). Types map to the
# opportunities schema in snapshots.py.
FIELD_TYPES = {
    "stage": "str",
    "close_date": "date",
    "next_step": "str",
    "next_step_date": "date",
    "forecast_category": "str",
    "amount": "number",
    "contact_count": "int",
}

# The exact keys a raw proposal object may carry. Any extra key is an unknown
# field (schema violation) and rejects the whole proposal.
_ALLOWED_KEYS = {"field", "proposed_value", "confidence", "evidence_quote"}

_WS = re.compile(r"\s+")


def _normalize(text):
    """Whitespace-only normalization (see module docstring). Collapse every run
    of whitespace to a single space and strip. No casefold, no NFKC."""
    return _WS.sub(" ", (text or "")).strip()


def _coerce(value, kind):
    """Type-coerce a proposed_value to a field's declared type, or raise
    ValueError. Returns a JSON/DB-friendly value (dates as ISO strings so the
    payload matches the field_update expiry comparison in work_items._coerce)."""
    if kind == "str":
        if not isinstance(value, str):
            raise ValueError("expected string")
        return value
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("expected integer")
        return value
    if kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("expected number")
        return value
    if kind == "date":
        if not isinstance(value, str):
            raise ValueError("expected ISO date string")
        return date.fromisoformat(value).isoformat()  # raises on bad format
    raise ValueError(f"unknown kind {kind!r}")


# person shorthand: bare initials ("JD", "R.P.") or a lone capitalized first
# name. Used only to FLAG unresolved entities (R3.4 degrade), never to rewrite.
_SHORTHAND = re.compile(r"\b(?:[A-Z]\.?){2,}\b|\b[A-Z][a-z]+\b")


def _flag_entities(evidence_quote):
    """R3.4 degrade: with no contact roster, return person shorthand tokens in
    the evidence so the UI can show them as-is and flag them unresolved."""
    return sorted(set(_SHORTHAND.findall(evidence_quote or "")))


class ProposalRejected(ValueError):
    """A raw proposal failed the strict output contract. Carries a machine
    `reason` for the append-only rejection log. NEVER surfaced to the user."""

    def __init__(self, reason, detail=None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def validate_proposal(raw, notes):
    """The strict output contract (R3.3) — the security gate for every proposal.

    Returns a clean proposal dict on success; raises ProposalRejected (with a
    log-safe reason) on any violation. Order matters: schema/type rejection runs
    and can reject BEFORE the evidence-presence check ever passes, so a note that
    quotes itself cannot smuggle an out-of-schema field through.
    """
    if not isinstance(raw, dict):
        raise ProposalRejected("not_an_object")
    extra = set(raw) - _ALLOWED_KEYS
    if extra:
        raise ProposalRejected("unknown_field", ",".join(sorted(extra)))
    field = raw.get("field")
    if field not in FIELD_TYPES:
        raise ProposalRejected("unknown_field", str(field))
    if "proposed_value" not in raw:
        raise ProposalRejected("type_failure", "missing proposed_value")
    try:
        value = _coerce(raw["proposed_value"], FIELD_TYPES[field])
    except (ValueError, TypeError) as exc:
        raise ProposalRejected("type_failure", f"{field}: {exc}")
    confidence = raw.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) \
            or not 0.0 <= float(confidence) <= 1.0:
        raise ProposalRejected("type_failure", "confidence not in [0,1]")
    evidence = raw.get("evidence_quote")
    if not isinstance(evidence, str) or not evidence.strip():
        raise ProposalRejected("type_failure", "evidence_quote missing")
    if _normalize(evidence) not in _normalize(notes):
        raise ProposalRejected("evidence_not_verbatim")
    return {
        "field": field,
        "proposed_value": value,
        "confidence": float(confidence),
        "evidence_quote": evidence,
        "unresolved_entities": _flag_entities(evidence),
    }


def capture_proposals(notes, opp_context, proposals, *, as_of=None,
                      notes_store=None, note_id=None):
    """Validate manually-entered field-update proposals for one opp (R3).

    LOCAL-ONLY: no network, no LLM, no API. A human reads the captured note and
    enters structured proposals; this runs each through the provider-independent
    strict contract (validate_proposal), logs rejections to the rejection table
    (never shown) when a `notes_store` is supplied, and returns only the
    proposals that passed — each with `unresolved_entities` flagged (R3.4).

    `proposals` is the list of raw entered dicts; an empty/absent list returns [].
    `as_of` is threaded for the rejection-log timestamp; never calls date.today().
    """
    accepted = []
    for raw in proposals or ():
        try:
            accepted.append(validate_proposal(raw, notes))
        except ProposalRejected as rej:
            if notes_store is not None:
                if as_of is None:
                    raise ValueError(
                        "as_of is required when logging capture rejections"
                    ) from rej
                notes_store.log_rejection(
                    at=as_of, reason=rej.reason,
                    opp_id=opp_context.get("opp_id"), note_id=note_id,
                    detail=rej.detail)
            # never re-raised, never shown to the user
    return accepted


def proposal_to_work_item(proposal):
    """Shape a validated proposal into the WorkItemStore.upsert_item payload for
    an item_type=field_update, source=capture item (R3.5). The caller supplies
    opp_id/owner/as_of and passes source='capture'."""
    return {
        "field": proposal["field"],
        "proposed_value": proposal["proposed_value"],
        "confidence": proposal["confidence"],
        "evidence_quote": proposal["evidence_quote"],
        "unresolved_entities": proposal["unresolved_entities"],
    }
