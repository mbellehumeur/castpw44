#!/bin/bash
# Bash script to prepare deployment package for Azure App Service

SOURCE_VIEWER_PATH="${1:-C:/Users/marti/fhircast-extension/Viewers/platform/app/dist}"

TARGET_DIR="./viewer"
APP_DIR="."
OUTPUT_ZIP="cast-hub.zip"

echo "Cast Hub Deployment Preparation"
echo "================================"

# Check if source viewer directory exists
if [ ! -d "$SOURCE_VIEWER_PATH" ]; then
    echo "ERROR: Source viewer path not found: $SOURCE_VIEWER_PATH"
    exit 1
fi

# Create target directory if it doesn't exist
mkdir -p "$TARGET_DIR"
echo "Copying viewer dist files from: $SOURCE_VIEWER_PATH"

# Copy the dist directory
cp -r "$SOURCE_VIEWER_PATH"/* "$TARGET_DIR/" || { echo "Copy failed"; exit 1; }
echo "Viewer dist files copied successfully"

# Create deployment ZIP
echo "Creating deployment package: $OUTPUT_ZIP"
rm -f "$OUTPUT_ZIP"

# Create ZIP with all necessary files
zip -r "$OUTPUT_ZIP" \
    app.py \
    requirements.txt \
    startup.sh \
    web.config \
    Resources \
    viewer \
    -x "*.git*" "__pycache__/*" "*.pyc" "*.egg-info/*" \
    2>&1 | grep -v "^  adding:\|^updating:" || true

ZIPSIZE=$(du -h "$OUTPUT_ZIP" | cut -f1)

echo ""
echo "Deployment package ready: $OUTPUT_ZIP"
echo "Package size: $ZIPSIZE"
echo ""
echo "To deploy to Azure, run:"
echo "az webapp deployment source config-zip --resource-group <your-resource-group> --name cast-hub --src $OUTPUT_ZIP"
echo ""
