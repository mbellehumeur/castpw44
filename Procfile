web: gunicorn -w 1 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000 app:app --timeout 600 --access-logfile - --error-logfile -
