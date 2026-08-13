"""Contract tests for the Azure/Docker packaging (see Dockerfile, deploy/).

These run in the normal suite (no Docker needed) so drift between the repo and
what the image build assumes fails fast and locally, not on a cloud build. They
assert the *satisfiability* of the Dockerfile's declarations, not its wording.
"""

import importlib
import hashlib
import json
import re
from pathlib import Path

from src.ingest import load_config

REPO = Path(__file__).resolve().parent.parent
DOCKERFILE = REPO / "Dockerfile"
PACKAGE_WORKFLOW = REPO / ".github" / "workflows" / "package.yml"


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


def test_default_config_used_by_docker_build_loads():
    # The Dockerfile invokes src.ingest without --config, so default config.yaml
    # must stay valid for the image build to work.
    load_config(REPO / "config.yaml")


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
    # WEBSITES_PORT (deploy script) must match the port the app binds
    # (Dockerfile), or App Service routes to a dead port.
    df = _dockerfile_text()
    port = re.search(r"ENV PORT=(\d+)", df)
    assert port, "Dockerfile no longer sets ENV PORT"
    deploy = (REPO / "deploy" / "azure-deploy.ps1").read_text(encoding="utf-8")
    assert re.search(rf"\$Port\s*=\s*{port.group(1)}\b", deploy), (
        f"deploy/azure-deploy.ps1 $Port must equal Dockerfile PORT ({port.group(1)})"
    )


def test_deploy_enables_acr_arm_audience_auth_for_managed_identity_pull():
    deploy = (REPO / "deploy" / "azure-deploy.ps1").read_text(encoding="utf-8")
    assert "acrUseManagedIdentityCreds" in deploy
    assert re.search(
        r"az\s+acr\s+config\s+authentication-as-arm\s+update\b[^\n]*"
        r"--status\s+enabled",
        deploy,
    ), "managed-identity ACR pulls need authentication-as-arm enabled"


def test_dashboard_entrypoint_present():
    # The FastHTML server is the image entrypoint (Streamlit retired, Task 5).
    assert (REPO / "app" / "server.py").exists(), "server entrypoint moved — update Dockerfile CMD"
    cmd = re.search(r'CMD\b.*', _dockerfile_text())
    assert cmd and "app.server" in cmd.group(0), \
        "Dockerfile CMD must launch the FastHTML app (python -m app.server)"
    assert cmd and "streamlit" not in cmd.group(0), \
        "Dockerfile CMD still references retired Streamlit"


def test_package_workflow_probes_fasthtml_health_endpoint():
    workflow = PACKAGE_WORKFLOW.read_text(encoding="utf-8")
    assert "/healthz" in workflow, \
        "container smoke test must probe the FastHTML health endpoint"
    assert "/_stcore/health" not in workflow, \
        "container smoke test still probes retired Streamlit health endpoint"


def test_local_vendor_manifest_hashes_match_files():
    manifest = (REPO / "app" / "static" / "vendor" / "VENDOR.md").read_text(
        encoding="utf-8"
    )
    rows = re.findall(r"\| ([^|]+\.js) \| \(local\) \| `([0-9a-f]{64})` \|", manifest)
    assert rows, "VENDOR.md must record hashes for local runtime JS"
    for asset, expected in rows:
        path = REPO / "app" / "static" / "vendor" / asset
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        assert actual == expected, f"{asset} SHA-256 in VENDOR.md is stale"


def test_deploy_does_not_enable_streamlit_websockets():
    deploy = (REPO / "deploy" / "azure-deploy.ps1").read_text(encoding="utf-8")
    assert not re.search(r"--web-sockets-enabled\s+true\b", deploy), (
        "FastHTML dashboard is plain HTTP; deploy should not enable retired "
        "Streamlit WebSockets"
    )


def test_packaging_modules_importable():
    # Modules the Dockerfile invokes with `python -m` must import cleanly.
    for mod in ("src.ingest", "src.snapshots"):
        importlib.import_module(mod)
