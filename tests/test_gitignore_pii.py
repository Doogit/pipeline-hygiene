"""The data/ gitignore is the PII control: it must default-deny real CRM exports
(which carry customer names/amounts/contacts) while keeping the synthetic demo
snapshots tracked so the container image still builds. This locks the whitelist
against a future edit that loosens it (e.g. swapping the exact filenames for a
!data/**/opps_*.csv wildcard, which would silently re-admit real dated exports).
"""
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _ignored(relpath):
    # git check-ignore -q exits 0 when the path IS ignored, 1 when it is not.
    return subprocess.run(
        ["git", "check-ignore", "-q", relpath], cwd=REPO
    ).returncode == 0


# Paths a user following the README would plausibly create with REAL data.
REAL_EXPORTS = [
    "your_export.csv",                       # README --init example, repo root
    "opps.csv",                              # bare root drop
    "data/opps_2099-01-01.csv",              # next real weekly snapshot
    "data/snapshots/opps_2099-01-01.csv",    # real export in the series dir
    "data/exports/crm.csv",                  # nested subdir
    "data/pipeline.xlsx",                    # spreadsheet export
    "data/customers.db",                     # a real SQLite store
]

# The synthetic demo files that ship with the repo and are baked into the image.
DEMO_SNAPSHOTS = [
    "data/opps_2026-08-10.csv",
    "data/snapshots/opps_2026-07-20.csv",
    "data/snapshots/opps_2026-07-27.csv",
    "data/snapshots/opps_2026-08-03.csv",
    "data/snapshots/opps_2026-08-10.csv",
]


def test_real_exports_are_ignored():
    for p in REAL_EXPORTS:
        assert _ignored(p), f"{p} would be committed -- PII leak risk"


def test_demo_snapshots_stay_tracked():
    for p in DEMO_SNAPSHOTS:
        assert not _ignored(p), f"{p} is ignored -- container build / demo data would break"
