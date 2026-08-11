"""CSV validation + ingest.

Validation runs before anything else. Invalid rows are rejected into a
ValidationReport with per-row reasons; missing required columns and mixed
currency are fatal (IngestError -> CLI exits nonzero). A hygiene tool that
silently mis-parses produces false violations and dies of distrust.
"""
import argparse
import csv
import math
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

REQUIRED_COLUMNS = [
    "opp_id", "account", "opp_name", "owner", "stage", "amount", "currency",
    "created_date", "close_date", "last_activity_date", "next_step",
    "next_step_date", "forecast_category", "contact_count", "product_line",
]
OPTIONAL_COLUMNS = ["stage_entered_date", "close_date_changes"]
FORECAST_ENUM = {"pipeline", "best_case", "commit", "omitted"}
CANONICAL_STAGES = {"prospect", "qualify", "develop", "propose", "commit",
                    "closed_won", "closed_lost"}

# Required-date strictness: created/close/last_activity must be non-empty ISO
# dates; only next_step_date and the optional columns may be empty (spec data
# model notes emptiness only for those).
REQUIRED_DATE_FIELDS = ("created_date", "close_date", "last_activity_date")


class IngestError(Exception):
    """Fatal ingest problem: missing columns, unreadable file, mixed currency."""


class AllRowsRejectedError(IngestError):
    """Fatal non-empty snapshot whose rows were all rejected."""

    def __init__(self, snapshot_date, report):
        self.snapshot_date = snapshot_date
        self.report = report
        top = "; ".join(f"{reason} ({n})" for reason, n
                        in list(report.reason_counts().items())[:5])
        super().__init__(
            f"all {report.total_rows} rows rejected - nothing stored for "
            f"{snapshot_date.isoformat()}. Top reasons: {top}")


class MixedCurrencyError(IngestError):
    pass


@dataclass
class ValidationReport:
    source_file: str
    total_rows: int = 0
    accepted: int = 0
    rejected: int = 0
    row_reasons: list = field(default_factory=list)   # (row_number, opp_id, reason)
    warnings: list = field(default_factory=list)

    def reject(self, row_number, opp_id, reason):
        self.rejected += 1
        self.row_reasons.append((row_number, opp_id, reason))

    def reason_counts(self):
        counts = {}
        for _, _, reason in self.row_reasons:
            key = reason.split(":")[0]
            counts[key] = counts.get(key, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))

    def to_dict(self):
        return {"source_file": self.source_file, "total_rows": self.total_rows,
                "accepted": self.accepted, "rejected": self.rejected,
                "reason_counts": self.reason_counts(),
                "row_reasons": [list(r) for r in self.row_reasons],
                "warnings": list(self.warnings)}


def rejection_detail_lines(report, limit=10):
    for row_number, opp_id, reason in report.row_reasons[:limit]:
        yield f"    line {row_number} {opp_id or '(no opp_id)'}: {reason}"
    if report.rejected > limit:
        yield f"    ... and {report.rejected - limit} more"


def load_config(path="config.yaml"):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _parse_date(value, allow_empty):
    value = (value or "").strip()
    if not value:
        if allow_empty:
            return None
        raise ValueError("empty date")
    return date.fromisoformat(value)


def _parse_row(raw, stage_map):
    """Parse + validate one raw CSV dict. Returns typed row; raises ValueError."""
    row = {}
    opp_id = (raw.get("opp_id") or "").strip()
    if not opp_id:
        raise ValueError("missing opp_id")
    row["opp_id"] = opp_id
    for col in ("account", "opp_name", "owner", "product_line", "next_step"):
        row[col] = (raw.get(col) or "").strip()

    source_stage = (raw.get("stage") or "").strip()
    if source_stage not in stage_map:
        raise ValueError(f"unknown stage: {source_stage!r} not in stage_map")
    row["stage"] = stage_map[source_stage]
    if row["stage"] not in CANONICAL_STAGES:
        raise ValueError(f"stage_map maps {source_stage!r} to non-canonical "
                         f"{row['stage']!r}")

    amount_raw = (raw.get("amount") or "").strip()
    if amount_raw == "":
        row["amount"] = None
    else:
        try:
            row["amount"] = float(amount_raw)
        except ValueError:
            raise ValueError(f"non-numeric amount: {amount_raw!r}") from None
        if not math.isfinite(row["amount"]):
            raise ValueError(f"non-finite amount: {amount_raw!r}")

    row["currency"] = (raw.get("currency") or "").strip()
    if not row["currency"]:
        raise ValueError("empty currency")

    for col in ("created_date", "close_date", "last_activity_date", "next_step_date"):
        try:
            row[col] = _parse_date(raw.get(col), allow_empty=col not in REQUIRED_DATE_FIELDS)
        except ValueError:
            raise ValueError(f"bad date in {col}: {(raw.get(col) or '')!r}") from None

    forecast = (raw.get("forecast_category") or "").strip()
    if forecast not in FORECAST_ENUM:
        raise ValueError(f"invalid forecast_category: {forecast!r}")
    row["forecast_category"] = forecast

    contact_raw = (raw.get("contact_count") or "").strip()
    try:
        row["contact_count"] = int(contact_raw)
    except ValueError:
        raise ValueError(f"non-integer contact_count: {contact_raw!r}") from None
    if row["contact_count"] < 0:
        raise ValueError(f"negative contact_count: {contact_raw!r}")

    try:
        row["stage_entered_date"] = _parse_date(raw.get("stage_entered_date"), allow_empty=True)
    except ValueError:
        raise ValueError(f"bad date in stage_entered_date: {raw.get('stage_entered_date')!r}") from None
    changes_raw = (raw.get("close_date_changes") or "").strip()
    if changes_raw == "":
        row["close_date_changes"] = None
    else:
        try:
            row["close_date_changes"] = int(changes_raw)
        except ValueError:
            raise ValueError(f"non-integer close_date_changes: {changes_raw!r}") from None
        if row["close_date_changes"] < 0:
            raise ValueError(f"negative close_date_changes: {changes_raw!r}")
    return row


def validate_csv(csv_path, config, stage_map_name="default"):
    """Validate a snapshot CSV. Returns (accepted_rows, ValidationReport).

    Raises IngestError for fatal problems (missing required columns, unknown
    stage_map name, mixed currency across accepted rows).
    """
    csv_path = Path(csv_path)
    stage_maps = config.get("stage_map", {})
    if stage_map_name not in stage_maps:
        raise IngestError(f"unknown stage_map {stage_map_name!r}; "
                          f"available: {sorted(stage_maps)}")
    stage_map = stage_maps[stage_map_name]
    report = ValidationReport(source_file=str(csv_path))

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames or []
        missing = [c for c in REQUIRED_COLUMNS if c not in header]
        if missing:
            raise IngestError(f"missing required columns: {missing}")

        accepted, seen_ids = [], set()
        for row_number, raw in enumerate(reader, start=2):  # 1 = header line
            report.total_rows += 1
            opp_id = (raw.get("opp_id") or "").strip()
            try:
                row = _parse_row(raw, stage_map)
            except ValueError as exc:
                report.reject(row_number, opp_id, str(exc))
                continue
            if row["opp_id"] in seen_ids:
                report.reject(row_number, row["opp_id"],
                              f"duplicate opp_id: {row['opp_id']} (later duplicate rejected)")
                continue
            seen_ids.add(row["opp_id"])
            accepted.append(row)

    report.accepted = len(accepted)
    currencies = sorted({r["currency"] for r in accepted})
    if len(currencies) > 1:
        raise MixedCurrencyError(
            f"mixed currency in one snapshot: {currencies}. Amounts are not "
            f"comparable; split the export by currency and re-ingest.")
    expected_ccy = config.get("expected_currency")
    if currencies and expected_ccy and currencies[0] != expected_ccy:
        report.warnings.append(
            f"uniform currency {currencies[0]} differs from expected_currency "
            f"{expected_ccy}")
    return accepted, report


def snapshot_date_from_filename(csv_path):
    m = re.search(r"(\d{4}-\d{2}-\d{2})", Path(csv_path).name)
    return date.fromisoformat(m.group(1)) if m else None


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m src.ingest",
                                description="Validate and load a snapshot CSV")
    p.add_argument("csv_path")
    p.add_argument("--db", default="data/pipeline.db")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--stage-map", default="default")
    p.add_argument("--snapshot-date", type=date.fromisoformat, default=None,
                   help="defaults to the YYYY-MM-DD in the filename")
    args = p.parse_args(argv)

    snapshot_date = args.snapshot_date or snapshot_date_from_filename(args.csv_path)
    if snapshot_date is None:
        print("error: no --snapshot-date given and none found in filename",
              file=sys.stderr)
        return 2
    try:
        config = load_config(args.config)
        from .snapshots import SnapshotStore
        store = SnapshotStore(args.db, config)
        report = store.ingest_csv(args.csv_path, snapshot_date,
                                  stage_map_name=args.stage_map)
    except AllRowsRejectedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        for line in rejection_detail_lines(exc.report):
            print(line, file=sys.stderr)
        return 1
    except IngestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"snapshot {snapshot_date}: accepted {report.accepted}/"
          f"{report.total_rows} rows, rejected {report.rejected}")
    for warning in report.warnings:
        print(f"warning: {warning}")
    if report.rejected:
        for count_reason, n in report.reason_counts().items():
            print(f"  rejected {n}: {count_reason}")
        # Per-row detail so an operator can fix the offending deals without
        # opening the store; the aggregate counts alone are not actionable.
        for line in rejection_detail_lines(report):
            print(line)
    return 0


if __name__ == "__main__":
    # Run the packaged main, not this module-as-__main__: otherwise IngestError
    # raised through src.snapshots (which imports src.ingest) is a different
    # class object than the __main__ one, and main's except would miss it.
    from src.ingest import main as _main
    sys.exit(_main())
