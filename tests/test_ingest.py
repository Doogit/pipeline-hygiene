"""Ingest validation + snapshot store tests (tests/config_test.yaml only)."""
import csv
import random
from datetime import date
from pathlib import Path

import pytest

from src.ingest import IngestError, MixedCurrencyError, main as ingest_main, validate_csv
from src.seed import write_csv
from src.seed.org import build_org
from src.seed.pathologies import CSV_COLUMNS, generate_snapshot
from src.snapshots import SnapshotStore

AS_OF = date(2026, 8, 10)

GOOD_ROW = {
    "opp_id": "OPP-0001", "account": "Bluepine Corp", "opp_name": "Bluepine deal",
    "owner": "Avery Ashford", "stage": "qualify", "amount": "50000.00",
    "currency": "USD", "created_date": "2026-05-01", "close_date": "2026-09-30",
    "last_activity_date": "2026-08-01", "next_step": "Send proposal to procurement",
    "next_step_date": "2026-08-15", "forecast_category": "pipeline",
    "contact_count": "3", "product_line": "CorePlatform",
    "stage_entered_date": "2026-07-01", "close_date_changes": "0",
}


def _write_fixture(path, rows, columns=CSV_COLUMNS):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _row(**overrides):
    row = dict(GOOD_ROW)
    row.update(overrides)
    return row


def test_rejects_with_reasons(tmp_path, config):
    p = tmp_path / "opps_2026-08-10.csv"
    _write_fixture(p, [
        _row(),
        _row(opp_id="OPP-0002", stage="Negotiation/Review"),      # unknown stage
        _row(opp_id="OPP-0003", close_date="08/10/2026"),         # bad date
        _row(opp_id="OPP-0001"),                                  # duplicate
        _row(opp_id="OPP-0004", forecast_category="Pipeline"),    # bad enum case
        _row(opp_id="OPP-0005", amount="fifty grand"),            # bad amount
        _row(opp_id="OPP-0006", last_activity_date=""),           # empty required date
        _row(opp_id="OPP-0007", amount="NaN"),                    # non-finite amount
        _row(opp_id="OPP-0008", contact_count="-1"),              # impossible count
        _row(opp_id="OPP-0009", close_date_changes="-1"),         # impossible history
    ])
    rows, report = validate_csv(p, config)
    assert [r["opp_id"] for r in rows] == ["OPP-0001"]
    assert report.total_rows == 10 and report.accepted == 1 and report.rejected == 9
    reasons = {opp: reason for _, opp, reason in report.row_reasons}
    assert "unknown stage" in reasons["OPP-0002"]
    assert "bad date in close_date" in reasons["OPP-0003"]
    assert "duplicate opp_id" in reasons["OPP-0001"]
    assert "invalid forecast_category" in reasons["OPP-0004"]
    assert "non-numeric amount" in reasons["OPP-0005"]
    assert "bad date in last_activity_date" in reasons["OPP-0006"]
    assert "non-finite amount" in reasons["OPP-0007"]
    assert "negative contact_count" in reasons["OPP-0008"]
    assert "negative close_date_changes" in reasons["OPP-0009"]


def test_missing_required_column_is_fatal(tmp_path, config):
    p = tmp_path / "opps_2026-08-10.csv"
    columns = [c for c in CSV_COLUMNS if c != "forecast_category"]
    _write_fixture(p, [{k: v for k, v in GOOD_ROW.items() if k != "forecast_category"}],
                   columns=columns)
    with pytest.raises(IngestError, match="missing required columns"):
        validate_csv(p, config)


def test_mixed_currency_is_fatal_and_cli_exits_nonzero(tmp_path, config, capsys):
    p = tmp_path / "opps_2026-08-10.csv"
    _write_fixture(p, [_row(), _row(opp_id="OPP-0002", currency="EUR")])
    with pytest.raises(MixedCurrencyError, match="mixed currency"):
        validate_csv(p, config)
    test_config = Path(__file__).parent / "config_test.yaml"
    code = ingest_main([str(p), "--db", str(tmp_path / "t.db"),
                        "--config", str(test_config)])
    assert code != 0
    assert "mixed currency" in capsys.readouterr().err


def test_uniform_unexpected_currency_warns(tmp_path, config):
    p = tmp_path / "opps_2026-08-10.csv"
    _write_fixture(p, [_row(currency="EUR"), _row(opp_id="OPP-0002", currency="EUR")])
    rows, report = validate_csv(p, config)
    assert len(rows) == 2
    assert any("differs from expected_currency" in w for w in report.warnings)


def test_stage_map_vocabulary(tmp_path, config):
    p = tmp_path / "opps_2026-08-10.csv"
    _write_fixture(p, [_row(stage="Develop"), _row(opp_id="OPP-0002", stage="Won")])
    rows, report = validate_csv(p, config, stage_map_name="dynamics_default")
    assert [r["stage"] for r in rows] == ["develop", "closed_won"]
    assert report.rejected == 0


def test_seed_csv_round_trips_clean(tmp_path, config):
    rng = random.Random(42)
    org = build_org(rng)
    rows, _ = generate_snapshot(org, 300, AS_OF, config, rng)
    p = tmp_path / f"opps_{AS_OF.isoformat()}.csv"
    write_csv(p, rows)
    accepted, report = validate_csv(p, config)
    assert report.rejected == 0 and report.accepted == 300
    assert accepted[0]["close_date"] is None or isinstance(accepted[0]["close_date"], date)


def test_store_roundtrip_and_replace(tmp_path, config):
    p = tmp_path / "opps_2026-08-10.csv"
    _write_fixture(p, [_row(), _row(opp_id="OPP-0002")])
    store = SnapshotStore(":memory:", config)
    report = store.ingest_csv(p, AS_OF)
    assert report.accepted == 2
    store.ingest_csv(p, AS_OF)  # re-ingest same date replaces, not duplicates
    rows = store.load_rows(AS_OF)
    assert [r["opp_id"] for r in rows] == ["OPP-0001", "OPP-0002"]
    assert rows[0]["close_date"] == date(2026, 9, 30)
    assert rows[0]["amount"] == 50000.0


def test_derivations_from_history(tmp_path, config):
    """No optional columns; 3 snapshots. close_date_changes counts transitions,
    stage_entered_date is the earliest contiguous date of the current stage."""
    store = SnapshotStore(":memory:", config)
    cols = [c for c in CSV_COLUMNS if c not in ("stage_entered_date", "close_date_changes")]
    base = {k: v for k, v in GOOD_ROW.items() if k in cols}
    weeks = [
        (date(2026, 7, 27), dict(base, stage="qualify", close_date="2026-09-30")),
        (date(2026, 8, 3), dict(base, stage="develop", close_date="2026-10-15")),
        (date(2026, 8, 10), dict(base, stage="develop", close_date="2026-10-30")),
    ]
    for snap_date, row in weeks:
        p = tmp_path / f"opps_{snap_date.isoformat()}.csv"
        _write_fixture(p, [row], columns=cols)
        store.ingest_csv(p, snap_date)
    rows = store.rows_with_history(date(2026, 8, 10))
    assert rows[0]["close_date_changes"] == 2          # 9/30->10/15, 10/15->10/30
    assert rows[0]["stage_entered_date"] == date(2026, 8, 3)  # develop run start


def test_single_snapshot_without_optional_columns_stays_none(tmp_path, config):
    store = SnapshotStore(":memory:", config)
    cols = [c for c in CSV_COLUMNS if c not in ("stage_entered_date", "close_date_changes")]
    p = tmp_path / "opps_2026-08-10.csv"
    _write_fixture(p, [{k: v for k, v in GOOD_ROW.items() if k in cols}], columns=cols)
    store.ingest_csv(p, AS_OF)
    rows = store.rows_with_history(AS_OF)
    assert rows[0]["close_date_changes"] is None
    assert rows[0]["stage_entered_date"] is None


def test_source_columns_take_precedence(tmp_path, config):
    store = SnapshotStore(":memory:", config)
    for snap_date, close in [(date(2026, 8, 3), "2026-09-30"), (date(2026, 8, 10), "2026-10-15")]:
        p = tmp_path / f"opps_{snap_date.isoformat()}.csv"
        _write_fixture(p, [_row(close_date=close)])  # carries explicit 0 / 2026-07-01
        store.ingest_csv(p, snap_date)
    rows = store.rows_with_history(date(2026, 8, 10))
    assert rows[0]["close_date_changes"] == 0                  # source, not derived 1
    assert rows[0]["stage_entered_date"] == date(2026, 7, 1)   # source value
