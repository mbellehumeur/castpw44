# Cast Hub (FastAPI + Uvicorn). Target: Amazon ECS + ALB.
FROM python:3.12-slim-bookworm

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY Resources ./Resources
COPY sceneview ./sceneview

EXPOSE 2017

CMD ["python", "app.py", "--host", "0.0.0.0", "--port", "2017"]
