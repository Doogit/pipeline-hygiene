"""Org simulator seed: deterministic synthetic pipeline data with ground truth.

Same seed + same --as-of => byte-identical output (seeded RNG, LF line
endings, sorted JSON keys).
"""
import csv
import json

from .pathologies import CSV_COLUMNS


def _fmt(col, value):
    if value is None:
        return ""
    if col == "amount":
        return f"{value:.2f}"
    if col in ("created_date", "close_date", "last_activity_date",
               "next_step_date", "stage_entered_date"):
        return value.isoformat()
    return str(value)


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(CSV_COLUMNS)
        for row in rows:
            writer.writerow([_fmt(c, row[c]) for c in CSV_COLUMNS])


def write_json(path, obj):
    with open(path, "w", newline="", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
        f.write("\n")
