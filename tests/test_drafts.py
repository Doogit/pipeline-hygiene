"""PR B — rule-to-draft generators (R2).

Every generator is a pure function of (row, config, as_of) plus per-snapshot
derived inputs, so identical inputs yield identical payloads and the idempotent
ledger records no duplicates on re-run (acceptance #1). Tests load
tests/config_test.yaml via the shared `config` fixture; as_of is threaded, never
date.today().
"""
import random
from datetime import date, timedelta
from pathlib import Path

import pytest

from src import drafts
from src.drafts import (agenda_item_draft, build_work_items, close_date_drafts,
                        coverage_task_draft, disqualification_doc, generate,
                        next_step_draft, owner_scope_id, proposed_close_date)
from src.brief import merge_quota_payload, risky_commits
from src.rules import evaluate_snapshot
from src.seed import write_csv
from src.seed.org import build_org
from src.seed.series import generate_series
from src.snapshots import SnapshotStore
from src.work_items import WorkItemStore

AS_OF = date(2026, 8, 10)
GOLDEN = Path(__file__).parent / "golden" / "disqualification_doc_golden.md"


def _row(opp_id="OPP-1", **over):
    row = {
        "opp_id": opp_id, "account": "Ironlabs Corp", "opp_name": "Platform Expansion",
        "owner": "Rowan Pemberton", "stage": "propose", "amount": 120_000.0,
        "currency": "USD", "created_date": date(2026, 1, 5),
        "close_date": date(2026, 9, 30), "last_activity_date": AS_OF,
        "next_step": "Send updated proposal to procurement",
        "next_step_date": date(2026, 10, 1), "forecast_category": "commit",
        "contact_count": 3, "product_line": "CorePlatform",
        "stage_entered_date": date(2026, 8, 1), "close_date_changes": 0,
    }
    row.update(over)
    return row


# --- R2.1: next step (H4) ---

def test_next_step_draft_is_deterministic_stage_template(config):
    a = next_step_draft(_row(stage="develop"), config, AS_OF)
    b = next_step_draft(_row(stage="develop"), config, AS_OF)
    assert a == b
    # 10 business days after Mon 2026-08-10 is Mon 2026-08-24
    assert a["payload"]["due_date"] == date(2026, 8, 24)
    assert a["item_type"] == "next_step"
    assert a["payload"]["draft_text"] == (
        "Confirm the economic buyer has agreed the evaluation criteria "
        "by 2026-08-24")


def test_next_step_criterion_varies_by_stage(config):
    develop = next_step_draft(_row(stage="develop"), config, AS_OF)
    commit = next_step_draft(_row(stage="commit"), config, AS_OF)
    assert develop["payload"]["criterion"] != commit["payload"]["criterion"]


# --- R2.2: close date + email (H2/H3) ---

def test_proposed_close_date_median_of_pushes(config):
    # median of [10, 20, 30] = 20 -> as_of + 20 calendar days
    assert proposed_close_date(config, AS_OF, [10, 20, 30]) == \
        AS_OF + timedelta(days=20)


def test_proposed_close_date_fallback_under_two_pushes(config):
    # <2 pushes -> stage template (as_of + 10 business days)
    assert proposed_close_date(config, AS_OF, [30]) == date(2026, 8, 24)
    assert proposed_close_date(config, AS_OF, []) == date(2026, 8, 24)


def test_close_date_drafts_field_update_and_email(config):
    items = close_date_drafts(_row(), config, AS_OF, [10, 20])
    assert [i["item_type"] for i in items] == ["field_update", "email_draft"]
    fu = items[0]["payload"]
    assert fu["field"] == "close_date"
    assert fu["proposed_value"] == AS_OF + timedelta(days=15)


def test_email_merge_fields_all_substituted(config):
    items = close_date_drafts(_row(), config, AS_OF, [10, 20])
    email = items[1]["payload"]
    assert "{" not in email["subject"] and "}" not in email["subject"]
    assert "{" not in email["body"] and "}" not in email["body"]
    assert email["subject"] == "Confirming the timeline for Ironlabs Corp"
    assert "Platform Expansion" in email["body"]      # opp_name merge
    assert "2026-09-30" in email["body"]              # current close_date
    assert "2026-08-25" in email["body"]              # proposed (as_of + 15d)
    assert email["body"].rstrip().endswith("Rowan Pemberton")  # owner merge


# --- R2.3: disqualification doc (H3 / disqualify_review_pushes) ---

def test_disqualification_doc_golden(config):
    row = _row(opp_id="OPP-DQ", account="Meridian Freight", stage="commit",
               amount=250_000.0, close_date_changes=3,
               push_count=3, cumulative_extension_days=62, max_push_days=28)
    item = disqualification_doc(row, config, AS_OF, "FY2027-Q1", [12, 22, 28])
    assert item["item_type"] == "clinic_doc"
    generated = item["payload"]["markdown"]
    if not GOLDEN.exists():
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(generated, encoding="utf-8", newline="\n")
        pytest.fail(f"golden file created at {GOLDEN}; review and commit it")
    assert generated == GOLDEN.read_text(encoding="utf-8")


def test_disqualification_doc_handles_no_commit_quarter(config):
    row = _row(push_count=3, cumulative_extension_days=40, max_push_days=20)
    item = disqualification_doc(row, config, AS_OF, None, [20, 20])
    assert "not previously committed" in item["payload"]["markdown"]


# --- R2.4: risky commit agenda item ---

def test_agenda_item_from_risky_commit_entry(config):
    rows = [_row("OPP-A", forecast_category="commit",
                 close_date=date(2026, 8, 1), amount=200_000.0)]  # H2 in past
    results = evaluate_snapshot(rows, config, AS_OF)
    entry = risky_commits(rows, results, config)[0]
    item = agenda_item_draft(entry, 1, config)
    assert item["item_type"] == "agenda_item"
    assert item["payload"]["dollar_rank"] == 1
    assert item["payload"]["prompt"] == entry["prompt"]
    assert item["payload"]["risky_rules"] == entry["risky_rules"]


# --- R2.5: low-coverage owner task list ---

def test_coverage_task_draft_uses_config_plays(config):
    item = coverage_task_draft("Rowan Pemberton", config)
    assert item["item_type"] == "clinic_doc"
    md = item["payload"]["markdown"]
    assert "Rowan Pemberton" in md
    for play in config["drafts"]["coverage_plays"]:
        assert f"- [ ] {play}" in md


# --- router ---

def test_generate_routes_h4_to_next_step(config):
    items = generate("H4", _row(), None, config, AS_OF)
    assert [i["item_type"] for i in items] == ["next_step"]


def test_generate_h2_emits_field_update_and_email(config):
    items = generate("H2", _row(), None, config, AS_OF,
                     context={"intervals": {"OPP-1": [10, 20]}})
    assert [i["item_type"] for i in items] == ["field_update", "email_draft"]


def test_generate_h3_adds_disqualification_doc_over_threshold(config):
    row = _row(push_count=3)
    over = generate("H3", row, None, config, AS_OF,
                    context={"intervals": {"OPP-1": [10, 20, 30]},
                             "committed_quarters": {"OPP-1": "FY2027-Q1"}})
    assert [i["item_type"] for i in over] == \
        ["field_update", "email_draft", "clinic_doc"]
    # under the push threshold: no doc
    under = generate("H3", _row(push_count=1), None, config, AS_OF)
    assert [i["item_type"] for i in under] == ["field_update", "email_draft"]


# --- packet build entry point ---

def _seed_store(tmp_path, config):
    rng = random.Random(42)
    org = build_org(rng)
    dates, snapshots, _ = generate_series(org, 150, 4, AS_OF, config, rng)
    cfg = merge_quota_payload(config, {
        "quotas": {s.name: s.quota for s in org.sellers},
        "owners": {s.name: {"team": s.team, "region": s.region}
                   for s in org.sellers}})
    store = SnapshotStore(":memory:", cfg)
    for d, rows in snapshots:
        path = tmp_path / f"opps_{d.isoformat()}.csv"
        write_csv(path, rows)
        assert store.ingest_csv(path, d).rejected == 0
    return store, cfg


def test_build_covers_every_fired_rule_and_is_idempotent(tmp_path, config,
                                                         monkeypatch):
    monkeypatch.setenv("PIPELINE_HYGIENE_PACKETS", "1")
    store, cfg = _seed_store(tmp_path, config)
    wi = WorkItemStore(":memory:", cfg)

    first = build_work_items(store, wi, AS_OF, cfg, AS_OF)
    assert first["skipped"] is False and first["upserted"] > 0

    # every open opp with an H4 violation has an open next_step item
    rows = store.rows_with_history(AS_OF)
    results = evaluate_snapshot(rows, cfg, AS_OF)
    open_items = wi.items(open_only=True)
    next_step_opps = {i["opp_id"] for i in open_items
                      if i["item_type"] == "next_step"}
    h4_opps = {oid for oid, r in results.items()
               if any(v.rule_id == "H4" for v in r.violations)}
    assert h4_opps and h4_opps <= next_step_opps

    # re-run on the same snapshot adds no new open items (acceptance #1)
    before = len(open_items)
    build_work_items(store, wi, AS_OF, cfg, AS_OF)
    assert len(wi.items(open_only=True)) == before
    wi.close()


def test_build_is_noop_when_flag_off(tmp_path, config, monkeypatch):
    monkeypatch.setenv("PIPELINE_HYGIENE_PACKETS", "0")
    store, cfg = _seed_store(tmp_path, config)
    wi = WorkItemStore(":memory:", cfg)
    result = build_work_items(store, wi, AS_OF, cfg, AS_OF)
    assert result == {"upserted": 0, "expired": 0, "skipped": True}
    assert wi.items() == []
    wi.close()


def test_owner_scope_id_is_sentinel():
    assert owner_scope_id("Rowan Pemberton") == "<owner:rowan pemberton>"
