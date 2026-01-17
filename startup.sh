#!/bin/bash
# Azure App Service startup script for Cast Hub

# Activate virtual environment if it exists
if [ -d "antenv" ]; then
    source antenv/bin/activate
fi

# Start the application with gunicorn
exec gunicorn -w 1 -k uvicorn.workers.UvicornWorker app:app \
    --bind=0.0.0.0:8000 \
    --timeout 600 \
    --access-logfile - \
    --error-logfile - \
    --log-level info
