param(
    [Parameter(Mandatory = $true)][string]$ResourceGroup,
    [Parameter(Mandatory = $true)][string]$AppName,
    [string]$PythonVersion = "3.11",
    [switch]$UsePortEnv = $true,
    [switch]$EnableWebSockets = $true,
    [string]$HealthCheckPath = "",
    [switch]$TailLogs
)

$ErrorActionPreference = 'Stop'

function Write-Step($msg) {
    Write-Host "[step] $msg" -ForegroundColor Cyan
}

function Write-Done($msg) {
    Write-Host "[done] $msg" -ForegroundColor Green
}

Write-Step "Configuring Azure App Service '$AppName' in resource group '$ResourceGroup'"

# Determine startup command
if ($UsePortEnv) {
    # Use `$PORT` so App Service provides the port at runtime
    $startup = 'gunicorn -w 1 -k uvicorn.workers.UvicornWorker app:app --bind=0.0.0.0:`$PORT'
} else {
    $startup = 'gunicorn -w 1 -k uvicorn.workers.UvicornWorker app:app --bind=0.0.0.0:8000'
}

Write-Step "Setting Python runtime version to PYTHON|$PythonVersion"
az webapp config set `
  --resource-group $ResourceGroup `
  --name $AppName `
  --linux-fx-version "PYTHON|$PythonVersion" | Out-Null

Write-Step "Setting startup command: $startup"
az webapp config set `
  --resource-group $ResourceGroup `
  --name $AppName `
  --startup-file $startup | Out-Null

if ($EnableWebSockets) {
    Write-Step "Enabling WebSockets"
    az webapp config set `
      --resource-group $ResourceGroup `
      --name $AppName `
      --web-sockets-enabled true | Out-Null
}

Write-Step "Enabling remote build so requirements are installed (SCM_DO_BUILD_DURING_DEPLOYMENT=true)"
az webapp config appsettings set `
  --resource-group $ResourceGroup `
  --name $AppName `
  --settings SCM_DO_BUILD_DURING_DEPLOYMENT=true | Out-Null

if ($HealthCheckPath -and $HealthCheckPath.Trim() -ne "") {
    Write-Step "Setting health check path to '$HealthCheckPath'"
    az webapp config set `
      --resource-group $ResourceGroup `
      --name $AppName `
      --health-check-path "$HealthCheckPath" | Out-Null
}

Write-Step "Restarting app"
az webapp restart `
  --resource-group $ResourceGroup `
  --name $AppName | Out-Null

Write-Done "Configuration applied. Checking logs next${if($TailLogs){' (tailing)'}else{''}}"

if ($TailLogs) {
    az webapp log tail `
      --resource-group $ResourceGroup `
      --name $AppName
} else {
    Write-Host "Tip: View logs with: az webapp log tail --resource-group $ResourceGroup --name $AppName" -ForegroundColor Yellow
}
