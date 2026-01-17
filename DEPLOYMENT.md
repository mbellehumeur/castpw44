# Azure App Service Deployment Guide for Cast Hub

## Prerequisites
- Azure CLI installed: `az --version`
- Azure account with an active subscription
- App Service named "cast-hub" already created
- For viewer component: FHIR Cast extension repository available locally

## Quick Start - Deploy with Viewer

### On Windows (PowerShell):
```powershell
# Prepare deployment package with viewer
.\scripts\prepare-deployment.ps1

# Deploy to Azure
az webapp deployment source config-zip --resource-group <your-resource-group> --name cast-hub --src cast-hub.zip
```

### On macOS/Linux (Bash):
```bash
# Prepare deployment package with viewer
bash scripts/prepare-deployment.sh ~/path/to/fhircast-extension/Viewers/platform/app/dist

# Deploy to Azure
az webapp deployment source config-zip --resource-group <your-resource-group> --name cast-hub --src cast-hub.zip
```

## Deployment Steps

### 1. Login to Azure
```bash
az login
```

### 2. Configure App Service Settings

Set the startup command (Azure will use port 8000 by default):
```bash
az webapp config set --resource-group <your-resource-group> --name cast-hub --startup-file "gunicorn -w 1 -k uvicorn.workers.UvicornWorker app:app --bind=0.0.0.0:8000"
```

Or if you want to use the PORT environment variable that Azure sets:
```bash
az webapp config set --resource-group <your-resource-group> --name cast-hub --startup-file "gunicorn -w 1 -k uvicorn.workers.UvicornWorker app:app --bind=0.0.0.0:\$PORT"
```

**Note:** This app uses a single worker (`-w 1`) because it relies on in-memory state (subscriptions, WebSocket connections, etc.) that must be shared. Multiple workers would create separate instances and cause state synchronization issues.

Enable build during ZIP deploy so dependencies from requirements.txt are installed on the server:
```bash
az webapp config appsettings set \
  --resource-group <your-resource-group> \
  --name cast-hub \
  --settings SCM_DO_BUILD_DURING_DEPLOYMENT=true
```

### 3. Configure Python Runtime
```bash
az webapp config set --resource-group <your-resource-group> --name cast-hub --linux-fx-version "PYTHON|3.11"
```

### 4. Enable App Service Logs (Optional but Recommended)
```bash
az webapp log config --resource-group <your-resource-group> --name cast-hub --application-logging filesystem --level information
```

### 5. Deploy the Application

**Option A: Deploy via ZIP with Viewer (RECOMMENDED)**

This is the easiest way to include the FHIR Cast viewer component:

**Windows (PowerShell):**
```powershell
# Navigate to the cast directory
cd c:\Users\marti\src\cast

# Run the deployment preparation script
.\scripts\prepare-deployment.ps1 -SourceViewerPath "C:\Users\marti\fhircast-extension\Viewers\platform\app\dist"

# Deploy the created ZIP file
az webapp deployment source config-zip --resource-group <your-resource-group> --name cast-hub --src cast-hub.zip
```

**macOS/Linux (Bash):**
```bash
# Navigate to the cast directory
cd ~/path/to/cast

# Run the deployment preparation script
bash scripts/prepare-deployment.sh ~/path/to/fhircast-extension/Viewers/platform/app/dist

# Deploy the created ZIP file
az webapp deployment source config-zip --resource-group <your-resource-group> --name cast-hub --src cast-hub.zip
```

**Option B: Deploy from Local Git**
```bash
# Configure local git deployment
az webapp deployment source config-local-git --resource-group <your-resource-group> --name cast-hub

# Add the Azure remote (use the URL from the previous command)
git remote add azure <deployment-url>

# Prepare viewer files locally first
.\scripts\prepare-deployment.ps1  # Windows
bash scripts/prepare-deployment.sh ~/path/to/viewers  # macOS/Linux

# Deploy
git add .
git commit -m "Deploy Cast Hub with Viewer to Azure"
git push azure main
```

**Option C: Deploy from GitHub**
```bash
az webapp deployment source config --resource-group <your-resource-group> --name cast-hub --repo-url <your-github-repo-url> --branch main --manual-integration

# Note: For GitHub deployment, ensure viewer directory is included in the repo
```

### 6. Verify Deployment

Check the app URL:
```bash
az webapp show --resource-group <your-resource-group> --name cast-hub --query defaultHostName --output tsv
```

Your app will be available at: `https://cast-hub.azurewebsites.net`

**Verify the viewer is working:**
```bash
# Main API
curl https://cast-hub.azurewebsites.net/api/hub/status

# Admin panel
curl https://cast-hub.azurewebsites.net/api/hub/admin

# Viewer application
curl https://cast-hub.azurewebsites.net/viewer/
```

View logs:
```bash
az webapp log tail --resource-group <your-resource-group> --name cast-hub
```

### 7. Configure CORS (if needed)
```bash
az webapp cors add --resource-group <your-resource-group> --name cast-hub --allowed-origins "*"
```

## Important Notes

### Viewer Component Deployment
The Cast Hub now includes the FHIR Cast Viewers platform. The application will:
1. Look for viewer files in `./viewer/` directory relative to the app
2. Fall back to checking the fhircast-extension repository location (for development)
3. Serve the viewer at the `/viewer` route with proper SPA routing

**To include the viewer in your deployment:**
- Use the `prepare-deployment.ps1` (Windows) or `prepare-deployment.sh` (Linux/macOS) scripts
- These scripts automatically copy the viewer dist directory into the deployment package
- The viewer is optional - the hub will work without it but `/viewer` route won't be available

### Port Configuration
Azure App Service automatically sets the `PORT` environment variable (typically 8000). The app is already configured to read from command-line arguments, but for Azure, we use gunicorn with uvicorn workers which will bind to the correct port.

### WebSocket Support
Azure App Service supports WebSockets, but you need to enable it:
```bash
az webapp config set --resource-group <your-resource-group> --name cast-hub --web-sockets-enabled true
```

### Environment Variables
Set any custom environment variables:
```bash
az webapp config appsettings set --resource-group <your-resource-group> --name cast-hub --settings KEY=VALUE
```

### Health Check
Consider adding a health check endpoint (already available at `/`):
```bash
az webapp config set --resource-group <your-resource-group> --name cast-hub --health-check-path "/"
```

## Troubleshooting

### View Application Logs
```bash
az webapp log tail --resource-group <your-resource-group> --name cast-hub
```

### ModuleNotFoundError: No module named 'uvicorn'
- Ensure `requirements.txt` is at the app root (it is in this repo).
- Make sure build-on-deploy is enabled for ZIP deploys so Oryx installs dependencies:
  ```bash
  az webapp config appsettings set \
    --resource-group <your-resource-group> \
    --name cast-hub \
    --settings SCM_DO_BUILD_DURING_DEPLOYMENT=true
  ```
- Restart the app and recheck logs to confirm pip installs ran:
  ```bash
  az webapp restart --resource-group <your-resource-group> --name cast-hub
  az webapp log tail --resource-group <your-resource-group> --name cast-hub
  ```
- If deploying to Windows App Service by mistake, switch to Linux (recommended for FastAPI/uvicorn) or ensure a proper Windows-specific setup that installs packages.

### SSH into Container
```bash
az webapp ssh --resource-group <your-resource-group> --name cast-hub
```

### Restart App Service
```bash
az webapp restart --resource-group <your-resource-group> --name cast-hub
```

### Check Deployment Status
```bash
az webapp deployment list-publishing-credentials --resource-group <your-resource-group> --name cast-hub
```

## Quick Deployment Script

### Windows PowerShell (`deploy.ps1`):
```powershell
param(
    [string]$ResourceGroup = "your-resource-group",
    [string]$AppName = "cast-hub",
    [string]$ViewerPath = "C:\Users\marti\fhircast-extension\Viewers\platform\app\dist"
)

Write-Host "Deploying Cast Hub to Azure App Service..." -ForegroundColor Green

# Prepare deployment package with viewer
.\scripts\prepare-deployment.ps1 -SourceViewerPath $ViewerPath

# Deploy
az webapp deployment source config-zip `
  --resource-group $ResourceGroup `
  --name $AppName `
  --src cast-hub.zip

Write-Host "Deployment complete!" -ForegroundColor Green
Write-Host "App URL: https://${AppName}.azurewebsites.net" -ForegroundColor Cyan
Write-Host "Viewer URL: https://${AppName}.azurewebsites.net/viewer/" -ForegroundColor Cyan
```

### macOS/Linux Bash (`deploy.sh`):
```bash
#!/bin/bash

RESOURCE_GROUP="${1:-your-resource-group}"
APP_NAME="${2:-cast-hub}"
VIEWER_PATH="${3:-~/path/to/fhircast-extension/Viewers/platform/app/dist}"

echo "Deploying Cast Hub to Azure App Service..."

# Prepare deployment package with viewer
bash scripts/prepare-deployment.sh "$VIEWER_PATH"

# Deploy
az webapp deployment source config-zip \
  --resource-group $RESOURCE_GROUP \
  --name $APP_NAME \
  --src cast-hub.zip

echo "Deployment complete!"
echo "App URL: https://${APP_NAME}.azurewebsites.net"
echo "Viewer URL: https://${APP_NAME}.azurewebsites.net/viewer/"
```

Make bash script executable: `chmod +x deploy.sh`
Run it: `./deploy.sh <resource-group> <app-name> <viewer-path>`
