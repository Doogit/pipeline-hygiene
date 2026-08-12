#!/usr/bin/env pwsh
# One-shot deploy of the pipeline-hygiene dashboard (demo data) to Azure
# App Service for Containers.
#
# The image is built INSIDE Azure with `az acr build`, so you do NOT need Docker
# installed locally — only the Azure CLI.
#
# Prerequisites:
#   1. Azure CLI installed            (https://aka.ms/azcli)
#   2. az login                       (authenticated session)
#   3. az account set -s "<sub>"      (correct subscription selected)
#
# What it serves: SYNTHETIC seed data with NO authentication. That is fine for a
# demo (no PII), but before pointing it at real pipeline data, gate it behind
# Microsoft sign-in by setting ENTRA_CLIENT_ID + ENTRA_TENANT_ID before running
# (step 8 turns on Easy Auth) — see "Add Entra sign-in" in deploy/README.md.
#
# Override any name via environment variable, e.g.  $env:LOCATION = "westus2".

$ErrorActionPreference = 'Stop'

# A per-run suffix keeps the globally-unique names (web app host, ACR) free of
# collisions. Pass APP_NAME / ACR_NAME to pin them across re-runs instead.
$rand = Get-Random -Minimum 10000 -Maximum 99999

$Location      = if ($env:LOCATION)       { $env:LOCATION }       else { 'eastus' }
$ResourceGroup = if ($env:RESOURCE_GROUP) { $env:RESOURCE_GROUP } else { 'rg-pipeline-hygiene' }
$AppName       = if ($env:APP_NAME)       { $env:APP_NAME }       else { "pipeline-hygiene-$rand" }
$AcrName       = if ($env:ACR_NAME)       { $env:ACR_NAME }       else { "pipehygacr$rand" }
$PlanName      = "plan-$AppName"
$Image         = "pipeline-hygiene:latest"
$Port          = 8000

# Repo root is the Docker build context (this script lives in deploy/).
$RepoRoot = (Resolve-Path "$PSScriptRoot/..").Path

Write-Host "Deploying '$AppName' to resource group '$ResourceGroup' in '$Location'..."

# 1. Resource group
az group create -n $ResourceGroup -l $Location -o none

# 2. Container registry + remote build (no local Docker needed)
az acr create -g $ResourceGroup -n $AcrName --sku Basic -o none
# App Service managed-identity pulls require the registry to accept ARM
# audience tokens; otherwise startup can fail with an ACR UNAUTHORIZED pull.
az acr config authentication-as-arm update -r $AcrName --status enabled -o none
Write-Host "Building image in Azure (az acr build)..."
az acr build -r $AcrName -t $Image "$RepoRoot" -o none

# 3. Linux App Service plan. B1 keeps the container Always On so the first
#    visitor doesn't hit a cold WebSocket. F1 (free) works but has no Always On.
az appservice plan create -g $ResourceGroup -n $PlanName --is-linux --sku B1 -o none

# 4. Web app pointing at the freshly built image
$LoginServer = az acr show -n $AcrName --query loginServer -o tsv
az webapp create -g $ResourceGroup -p $PlanName -n $AppName `
    --container-image-name "$LoginServer/$Image" -o none

# 5. Pull from ACR via the web app's managed identity (no admin creds stored)
az webapp identity assign -g $ResourceGroup -n $AppName -o none
$PrincipalId = az webapp identity show -g $ResourceGroup -n $AppName --query principalId -o tsv
$AcrId       = az acr show -n $AcrName --query id -o tsv
az role assignment create --assignee-object-id $PrincipalId `
    --assignee-principal-type ServicePrincipal --role AcrPull --scope $AcrId -o none
az webapp config set -g $ResourceGroup -n $AppName `
    --generic-configurations '{"acrUseManagedIdentityCreds": true}' -o none

# 6. Tell App Service the container port, enable WebSockets (Streamlit needs the
#    socket), and keep the app warm.
az webapp config appsettings set -g $ResourceGroup -n $AppName --settings WEBSITES_PORT=$Port -o none
az webapp config set -g $ResourceGroup -n $AppName --web-sockets-enabled true --always-on true -o none

# 7. Restart so the pull uses the identity + role granted above.
az webapp restart -g $ResourceGroup -n $AppName -o none

# 8. Authentication boundary. When ENTRA_CLIENT_ID + ENTRA_TENANT_ID are set,
#    turn on App Service "Easy Auth" so every request requires a Microsoft
#    corporate sign-in (the same commands documented in deploy/README.md, run
#    for you). When they are NOT set, the app is deployed OPEN to anyone with
#    the URL — fine for the synthetic demo data, unsafe for real pipeline data.
if ($env:ENTRA_CLIENT_ID -and $env:ENTRA_TENANT_ID) {
    Write-Host "Enabling Microsoft (Entra) sign-in..."
    # az is a native command, so a non-zero exit does NOT throw under
    # $ErrorActionPreference='Stop' -- check $LASTEXITCODE after each call. The
    # app is already live (created + restarted above), so a SILENT auth failure
    # would leave it open while we claim success; fail loud and closed instead.
    az webapp auth microsoft update -g $ResourceGroup -n $AppName `
        --client-id $env:ENTRA_CLIENT_ID `
        --issuer "https://login.microsoftonline.com/$($env:ENTRA_TENANT_ID)/v2.0" -o none
    if ($LASTEXITCODE -eq 0) {
        az webapp auth update -g $ResourceGroup -n $AppName `
            --enabled true --action RequireAuthentication `
            --redirect-provider azureactivedirectory -o none
    }
    if ($LASTEXITCODE -eq 0) {
        Write-Host "Entra sign-in enabled: unauthenticated visitors are bounced to Microsoft login."
    } else {
        Write-Warning "Entra sign-in setup FAILED (az exit $LASTEXITCODE): app '$AppName' is LIVE and OPEN."
        Write-Warning "Verify with 'az webapp auth show -g $ResourceGroup -n $AppName' and re-run once fixed,"
        Write-Warning "or take it down now with 'az webapp stop -g $ResourceGroup -n $AppName'."
    }
} else {
    Write-Warning "Deployed OPEN with NO authentication -- anyone with the URL can view the dashboard."
    Write-Warning "This is acceptable ONLY for the SYNTHETIC demo data baked into the image."
    Write-Warning "Before serving real pipeline data, set `$env:ENTRA_CLIENT_ID and"
    Write-Warning "`$env:ENTRA_TENANT_ID and re-run, or follow 'Add Entra sign-in' in deploy/README.md."
}

$AppHost = az webapp show -g $ResourceGroup -n $AppName --query defaultHostName -o tsv
Write-Host ""
Write-Host "Deployed: https://$AppHost"
Write-Host "First load can take ~30-60s while the container starts and warms up."
Write-Host "To tear it all down:  az group delete -n $ResourceGroup --yes --no-wait"
