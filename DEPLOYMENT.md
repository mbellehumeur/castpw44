# Azure App Service Deployment Guide for Cast Hub

## Prerequisites
- Azure CLI installed: `az --version`
- Azure account with an active subscription
- App Service named "cast-hub" already created

## Deployment Steps

### 1. Login to Azure
```bash
az login
```

### 2. Configure App Service Settings

Set the startup command (Azure will use port 8000 by default):
```bash
az webapp config set --resource-group <your-resource-group> --name cast-hub --startup-file "gunicorn -w 4 -k uvicorn.workers.UvicornWorker app:app --bind=0.0.0.0:8000"
```

Or if you want to use the PORT environment variable that Azure sets:
```bash
az webapp config set --resource-group <your-resource-group> --name cast-hub --startup-file "gunicorn -w 4 -k uvicorn.workers.UvicornWorker app:app --bind=0.0.0.0:\$PORT"
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

**Option A: Deploy from Local Git**
```bash
# Configure local git deployment
az webapp deployment source config-local-git --resource-group <your-resource-group> --name cast-hub

# Add the Azure remote (use the URL from the previous command)
git remote add azure <deployment-url>

# Deploy
git add .
git commit -m "Deploy Cast Hub to Azure"
git push azure main
```

**Option B: Deploy via ZIP**
```bash
# Create a ZIP file of your application
zip -r cast-hub.zip . -x "*.git*" -x "__pycache__/*" -x "*.pyc"

# Deploy the ZIP
az webapp deployment source config-zip --resource-group <your-resource-group> --name cast-hub --src cast-hub.zip
```

**Option C: Deploy from GitHub**
```bash
az webapp deployment source config --resource-group <your-resource-group> --name cast-hub --repo-url <your-github-repo-url> --branch main --manual-integration
```

### 6. Verify Deployment

Check the app URL:
```bash
az webapp show --resource-group <your-resource-group> --name cast-hub --query defaultHostName --output tsv
```

Your app will be available at: `https://cast-hub.azurewebsites.net`

View logs:
```bash
az webapp log tail --resource-group <your-resource-group> --name cast-hub
```

### 7. Configure CORS (if needed)
```bash
az webapp cors add --resource-group <your-resource-group> --name cast-hub --allowed-origins "*"
```

## Important Notes

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

Save this as `deploy.sh`:
```bash
#!/bin/bash
RESOURCE_GROUP="your-resource-group"
APP_NAME="cast-hub"

echo "Deploying Cast Hub to Azure App Service..."

# Package the app
zip -r cast-hub.zip . -x "*.git*" -x "__pycache__/*" -x "*.pyc" -x "deploy.sh"

# Deploy
az webapp deployment source config-zip \
  --resource-group $RESOURCE_GROUP \
  --name $APP_NAME \
  --src cast-hub.zip

# Clean up
rm cast-hub.zip

echo "Deployment complete!"
echo "App URL: https://${APP_NAME}.azurewebsites.net"
```

Make it executable: `chmod +x deploy.sh`
Run it: `./deploy.sh`
