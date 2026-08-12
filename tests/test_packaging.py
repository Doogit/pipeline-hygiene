"""Contract tests for the Azure/Docker packaging (see Dockerfile, deploy/).

These run in the normal suite (no Docker needed) so drift between the repo and
what the image build assumes fails fast and locally, not on a cloud build. They
assert the *satisfiability* of the Dockerfile's declarations, not its wording.
"""

import importlib
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOCKERFILE = REPO / "Dockerfile"


def _dockerfile_text() -> str:
    assert DOCKERFILE.exists(), "Dockerfile missing — Azure packaging cannot build"
    return DOCKERFILE.read_text(encoding="utf-8")


def _repo_path_from_image_path(image_path: str) -> Path:
    # The image lays the repo down at /app; map an /app/... path back to the repo.
    return REPO / image_path.replace("/app/", "", 1)


def test_demo_snapshots_present_as_a_series():
    # The Dockerfile globs data/snapshots/opps_*.csv and ingests each at build;
    # a series (>=2) is what populates the Trajectory / Slippage / Flow tabs.
    snaps = sorted((REPO / "data" / "snapshots").glob("opps_*.csv"))
    assert len(snaps) >= 2, f"need >=2 demo snapshots to bake, found {len(snaps)}"


def test_quotas_manifest_referenced_by_dockerfile_is_valid():
    # Guards the SILENT failure mode: if PIPELINE_HYGIENE_QUOTAS points at a
    # manifest that is missing or lacks quotas/owners, coverage and the Teams
    # tab render blank with no error at build or boot.
    m = re.search(r"PIPELINE_HYGIENE_QUOTAS=(\S+)", _dockerfile_text())
    assert m, "Dockerfile no longer sets PIPELINE_HYGIENE_QUOTAS"
    manifest = _repo_path_from_image_path(m.group(1))
    assert manifest.exists(), f"quotas manifest {manifest.name} referenced by Dockerfile is missing"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data.get("quotas"), f"{manifest.name} has no quotas block"
    assert data.get("owners"), f"{manifest.name} has no owners block (team/region rollups need it)"


def test_dockerfile_and_deploy_agree_on_port():
    # WEBSITES_PORT (deploy script) must match the port Streamlit binds
    # (Dockerfile), or App Service routes to a dead port.
    df = _dockerfile_text()
    port = re.search(r"ENV PORT=(\d+)", df)
    assert port, "Dockerfile no longer sets ENV PORT"
    deploy = (REPO / "deploy" / "azure-deploy.ps1").read_text(encoding="utf-8")
    assert re.search(rf"\$Port\s*=\s*{port.group(1)}\b", deploy), (
        f"deploy/azure-deploy.ps1 $Port must equal Dockerfile PORT ({port.group(1)})"
    )


def test_dashboard_entrypoint_present():
    assert (REPO / "app" / "dashboard.py").exists(), "dashboard entrypoint moved — update Dockerfile CMD"


def test_packaging_modules_importable():
    # Modules the Dockerfile invokes with `python -m` must import cleanly.
    for mod in ("src.ingest", "src.snapshots"):
        importlib.import_module(mod)
