# Deploy to Azure App Service

Runs the read-only dashboard as a container on **Azure App Service for
Containers**, serving the committed synthetic demo data. The image is built
inside Azure, so **no local Docker is required** — only the Azure CLI.

## One command

```powershell
az login
az account set -s "<your-subscription>"
./deploy/azure-deploy.ps1
```

The script creates a resource group, an Azure Container Registry, builds the
image with `az acr build`, provisions a Linux App Service plan + web app, wires
up managed-identity pull, enables ACR ARM-audience token auth, WebSockets, and
the container port, and prints the URL. First load takes ~30-60s while the
container warms.

Override defaults with environment variables before running:

```powershell
$env:LOCATION = "westus2"; $env:APP_NAME = "contoso-pipeline-hygiene"
./deploy/azure-deploy.ps1
```

Tear everything down with `az group delete -n rg-pipeline-hygiene --yes`.

## What it serves, and the auth boundary

By default the app is **open** and shows **synthetic** pipeline data (no PII) —
appropriate for a demo you can hand someone a link to. It is *not* appropriate
for real pipeline data until you put sign-in in front of it.

### Add Entra sign-in (before using real data)

App Service "Easy Auth" gates the whole app behind Microsoft corporate sign-in
with no application code. The deploy script wires it up automatically when you
set two environment variables before running — otherwise it deploys **open**
and prints a warning telling you so:

```powershell
$env:ENTRA_CLIENT_ID = "<entra-app-client-id>"
$env:ENTRA_TENANT_ID = "<tenant-id>"
./deploy/azure-deploy.ps1        # deploys AND gates behind Microsoft sign-in
```

Every request then requires a valid corporate login; unauthenticated visitors
are bounced to the Microsoft sign-in page.

To turn Easy Auth on manually against an app that already exists (equivalent to
what step 8 of the script runs):

```powershell
# Creates an Entra app registration and turns on RequireAuthentication.
az webapp auth microsoft update -g rg-pipeline-hygiene -n <app-name> `
    --client-id "<entra-app-client-id>" `
    --issuer "https://login.microsoftonline.com/<tenant-id>/v2.0"
az webapp auth update -g rg-pipeline-hygiene -n <app-name> `
    --enabled true --action RequireAuthentication --redirect-provider azureactivedirectory
```

## Notes / limitations

- **Untested against a live subscription from this repo checkout.** The ingest
  step baked into the image is verified; the `az` deploy commands assume a
  recent Azure CLI (the `--container-image-name` flag and
  `acrUseManagedIdentityCreds`).
- **State is ephemeral and demo-only.** The SQLite store is baked into the image
  at build time. To ingest new data you rebuild the image. For a real multi-user
  deployment with uploads you would move the store to Azure Files (SQLite) or
  Azure Database for PostgreSQL — deliberately out of scope for demo mode.
- **Single container, always-on B1 plan.** For a rarely-used demo you can switch
  the plan SKU to `F1` (free, but no Always On → cold starts).
