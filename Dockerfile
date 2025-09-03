FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

# (optional but recommended) fonts for Pillow PNG export + certs
RUN apt-get update && apt-get install -y --no-install-recommends \
      fonts-dejavu-core ca-certificates && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py endpoints.yaml ./
RUN mkdir -p /app/data

# IMPORTANT: use sh -c so ${PORT} expands (JSON form doesn't expand env vars)
CMD ["sh", "-c", "uvicorn main:APP --host 0.0.0.0 --port ${PORT}"]