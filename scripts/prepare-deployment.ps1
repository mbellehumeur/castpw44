# PowerShell script to prepare deployment package for Azure App Service
# This copies the viewer dist directory and creates a deployment package

param(
    [string]$SourceViewerPath = "C:\Users\marti\fhircast-extension\Viewers\platform\app\dist",
    [string]$TargetDir = ".\viewer",
    [string]$AppDir = ".",
    [string]$OutputZip = "cast-hub.zip"
)

Write-Host "Cast Hub Deployment Preparation" -ForegroundColor Green
Write-Host "================================" -ForegroundColor Green

# Check if source viewer directory exists
if (-not (Test-Path $SourceViewerPath)) {
    Write-Host "ERROR: Source viewer path not found: $SourceViewerPath" -ForegroundColor Red
    exit 1
}

# Create target directory if it doesn't exist
if (-not (Test-Path $TargetDir)) {
    New-Item -ItemType Directory -Path $TargetDir | Out-Null
    Write-Host "Created directory: $TargetDir" -ForegroundColor Green
}

# Copy the dist directory
Write-Host "Copying viewer dist files from: $SourceViewerPath" -ForegroundColor Cyan
Copy-Item -Path "$SourceViewerPath\*" -Destination $TargetDir -Recurse -Force
Write-Host "Viewer dist files copied successfully" -ForegroundColor Green

# Create deployment ZIP
Write-Host "Creating deployment package: $OutputZip" -ForegroundColor Cyan
if (Test-Path $OutputZip) {
    Remove-Item $OutputZip -Force
}

# Create ZIP with all necessary files
$filesToZip = @(
    "app.py",
    "requirements.txt",
    "startup.sh",
    "web.config",
    "Resources",
    "viewer"
)

# Use 7-Zip if available, otherwise use PowerShell
try {
    $sevenZipPath = (Get-Command 7z -ErrorAction SilentlyContinue).Source
    if ($sevenZipPath) {
        Write-Host "Using 7-Zip for compression..." -ForegroundColor Yellow
        & 7z a -r $OutputZip $filesToZip 2>$null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "Created ZIP using 7-Zip" -ForegroundColor Green
        } else {
            throw "7-Zip failed"
        }
    } else {
        throw "7-Zip not found"
    }
} catch {
    Write-Host "Creating ZIP using PowerShell (built-in)..." -ForegroundColor Yellow
    Compress-Archive -Path $filesToZip -DestinationPath $OutputZip -Force
    Write-Host "Created ZIP using PowerShell" -ForegroundColor Green
}

Write-Host ""
Write-Host "Deployment package ready: $OutputZip" -ForegroundColor Green
Write-Host "Package size: $('{0:N2}' -f ((Get-Item $OutputZip).Length / 1MB)) MB" -ForegroundColor Cyan
Write-Host ""
Write-Host "To deploy to Azure, run:" -ForegroundColor Cyan
Write-Host "az webapp deployment source config-zip --resource-group <your-resource-group> --name cast-hub --src $OutputZip" -ForegroundColor Yellow
Write-Host ""
