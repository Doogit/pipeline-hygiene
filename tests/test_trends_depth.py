"""Session 7 WS3: snapshot-anchored desk-score trend, mass-delta collapse,
gap-to-cover, desk-relative coverage note, in-quarter coverage split.
"""
import random
from datetime import date, timedelta

from src import brief
from src.scoring import fiscal_quarter_end
from src.seed import write_csv
from src.seed.org import build_org
from src.seed.series import generate_series
from src.snapshots import SnapshotStore

AS_OF = date(2026, 8, 10)


def test_fiscal_quarter_end():
    assert fiscal_quarter_end(date(2026, 8, 10), 7) == date(2026, 9, 30)
    assert fiscal_quarter_end(date(2026, 9, 30), 7) == date(2026, 9, 30)
    assert fiscal_quarter_end(date(2026, 10, 1), 7) == date(2026, 12, 31)
    assert fiscal_quarter_end(date(2026, 6, 30), 7) == date(2026, 6, 30)
    assert fiscal_quarter_end(date(2026, 1, 15), 1) == date(2026, 3, 31)
    assert fiscal_quarter_end(date(2026, 12, 31), 1) == date(2026, 12, 31)


def _series_store(tmp_path, config, rows=150):
    rng = random.Random(42)
    org = build_org(rng)
    dates, snapshots, _ = generate_series(org, rows, 4, AS_OF, config, rng)
    cfg = dict(config)
    cfg["quotas"] = {s.name: s.quota for s in org.sellers}
    cfg["owner_meta"] = {s.name: {"team": s.team, "region": s.region}
                         for s in org.sellers}
    store = SnapshotStore(":memory:", cfg)
    for d, rows_ in snapshots:
        p = tmp_path / f"opps_{d.isoformat()}.csv"
        write_csv(p, rows_)
        assert store.ingest_csv(p, d).rejected == 0
    return store, cfg, dates


def test_desk_score_series_snapshot_anchored(tmp_path, config):
    store, cfg, dates = _series_store(tmp_path, config)
    series = brief.desk_score_series(store, cfg, dates[-1])
    assert [d for d, _ in series] == dates
    assert all(0 <= s <= 100 for _, s in series)
    # anchored to the evaluated snapshot: an earlier up_to sees a prefix
    prefix = brief.desk_score_series(store, cfg, dates[-2])
    assert prefix == series[:-1]
    # deterministic, cache-free
    assert brief.desk_score_series(store, cfg, dates[-1]) == series
    # last-n window
    assert brief.desk_score_series(store, cfg, dates[-1], n=2) == series[-2:]


def test_headline_trend_line_renders(tmp_path, config):
    store, cfg, dates = _series_store(tmp_path, config)
    data = brief.build(store, dates[-1], dates[-1], cfg)
    markdown = brief.render(data, cfg)
    scores = " ".join(f"{s:.1f}" for _, s in data["score_trend"])
    assert (f"- Desk score, last {len(dates)} snapshots: {scores} "
            f"(snapshot-anchored, oldest first)") in markdown
    # a filtered brief drops the desk-wide trend
    owner = store.owners(dates[-1])[0]
    filtered = brief.build(store, dates[-1], dates[-1], cfg,
                           owner_filter={owner}, filter_label=f"owner {owner}")
    assert filtered["score_trend"] is None
    assert "- Desk score, last" not in brief.render(filtered, cfg)


def test_mass_delta_collapses_per_rule(config):
    cfg = dict(config, delta_detail_max_per_rule=10)
    opps = {f"OPP-{i:04d}": ["H6"] for i in range(25)}
    opps["OPP-KEEP"] = ["H1"]
    data = {"since_last_run": {
        "prev_snapshot_date": "2026-08-03", "prev_as_of": "2026-08-03",
        "new_violations": opps, "cleared_violations": {},
        "closed": {}, "added": [], "removed": [],
        "score_change": {"prev": None, "current": None},
    }, "rows": []}
    lines = []
    brief._since_last_run_detail(lines, data, cfg)
    text = "\n".join(lines)
    assert "- H6: 25 opps (detail collapsed, > 10 per rule)" in text
    assert "OPP-KEEP: H1" in text
    assert "OPP-0003" not in text          # collapsed rule loses per-opp lines
    # under the threshold nothing collapses
    lines = []
    brief._since_last_run_detail(lines, data,
                                 dict(config, delta_detail_max_per_rule=30))
    text = "\n".join(lines)
    assert "detail collapsed" not in text
    assert "OPP-0003" in text


def test_gap_to_cover_rendered(tmp_path, config):
    store, cfg, dates = _series_store(tmp_path, config)
    data = brief.build(store, dates[-1], dates[-1], cfg)
    markdown = brief.render(data, cfg)
    assert "| Gap to cover |" in markdown
    stats = next(iter(data["owners"].values()))
    if stats.required_pipeline is not None:
        gap = max(stats.required_pipeline - stats.open_pipeline, 0.0)
        assert f"| {stats.coverage_ratio:.2f} | ${gap:,.0f} |" in markdown
    # gap is 0 exactly when the coverage flag is off (same basis)
    for s in data["owners"].values():
        if s.required_pipeline is None:
            continue
        gap = max(s.required_pipeline - s.open_pipeline, 0.0)
        assert (gap > 0) == s.coverage_flagged


def test_desk_relative_coverage_note(tmp_path, config):
    store, cfg, dates = _series_store(tmp_path, config)
    data = brief.build(store, dates[-1], dates[-1], cfg)
    owners = data["owners"]
    with_cov = [s for s in owners.values() if s.coverage_ratio is not None]
    flagged = [s for s in with_cov if s.coverage_flagged]
    share = len(flagged) / len(with_cov)
    note = brief.desk_coverage_note(owners, cfg)
    if share > 0.75:
        assert note is not None
        assert f"{len(flagged)} of {len(with_cov)}" in note
        assert note in brief.render(data, cfg)
    # a threshold above the actual share suppresses the note; flags stay
    assert brief.desk_coverage_note(
        owners, dict(cfg, low_coverage_desk_note_share=1.0)) is None


def test_in_quarter_coverage_split(tmp_path, config):
    store, cfg, dates = _series_store(tmp_path, config)
    data = brief.build(store, dates[-1], dates[-1], cfg)
    coverage = data["trajectory"]["coverage"]
    open_pipeline = data["trajectory"]["open_pipeline"]
    assert coverage["in_quarter_open"] + coverage["later_open"] == \
        open_pipeline
    q_end = fiscal_quarter_end(dates[-1], cfg["fiscal_year_start_month"])
    expected = sum(r["amount"] or 0.0 for r in data["rows"]
                   if r["stage"] not in ("closed_won", "closed_lost")
                   and r["close_date"] is not None
                   and r["close_date"] <= q_end)
    assert coverage["in_quarter_open"] == expected
    markdown = brief.render(data, cfg)
    assert (f"- Of open pipeline: ${coverage['in_quarter_open']:,.0f} "
            f"closes in {coverage['quarter']} or earlier") in markdown
