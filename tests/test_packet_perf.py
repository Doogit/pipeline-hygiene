"""Non-functional gate (Monday Packet R6.1): a 400-opp packet build stays well
under 10s. There is no LLM/network in the build (local-only tool), so this
measures the deterministic path only: evaluate rules over 400 opps, generate the
draft work items, upsert them, and assemble one owner packet.

The 10s budget is generous headroom; the assertion guards against an accidental
O(n^2) regression in the build, not micro-performance.
"""
import random
import time
from datetime import date

from src.brief import merge_quota_payload
from src.drafts import build_work_items
from src.seed import write_csv
from src.seed.org import build_org
from src.seed.series import generate_series
from src.snapshots import SnapshotStore
from src.work_items import WorkItemStore
from src import packet

AS_OF = date(2026, 8, 10)


def test_400_opp_packet_build_under_10s(tmp_path, config, monkeypatch):
    monkeypatch.setenv("PIPELINE_HYGIENE_PACKETS", "1")
    rng = random.Random(42)
    org = build_org(rng)
    dates, snapshots, _ = generate_series(org, 400, 2, AS_OF, config, rng)
    cfg = merge_quota_payload(config, {
        "quotas": {s.name: s.quota for s in org.sellers},
        "owners": {s.name: {"team": s.team, "region": s.region}
                   for s in org.sellers}})
    store = SnapshotStore(":memory:", cfg)
    wi = None
    try:
        for d, rows in snapshots:
            path = tmp_path / f"opps_{d.isoformat()}.csv"
            write_csv(path, rows)
            assert store.ingest_csv(path, d).rejected == 0
        latest = dates[-1]

        # Measure only the build (seeding/ingest above is test setup, not the gate).
        wi = WorkItemStore(":memory:", cfg)
        start = time.monotonic()
        result = build_work_items(store, wi, latest, cfg, latest)
        counts = {}
        for it in wi.items(open_only=True):
            counts[it["owner_normalized"]] = \
                counts.get(it["owner_normalized"], 0) + 1
        if counts:  # build the packet for the busiest owner
            busiest = max(counts, key=counts.get)
            packet.build_owner_packet(wi, busiest, cfg, latest,
                                      snapshot_store=store,
                                      snapshot_date=latest)
        elapsed = time.monotonic() - start
    finally:
        if wi is not None:
            wi.close()
        store.close()

    assert result["upserted"] > 0, "expected the 400-opp snapshot to fire rules"
    assert elapsed < 10.0, \
        f"400-opp packet build took {elapsed:.1f}s (budget 10s, R6.1)"
