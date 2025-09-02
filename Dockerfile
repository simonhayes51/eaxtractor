FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py endpoints.yaml ./
# optional: keep snapshots across restarts if Railway persists the volume
VOLUME ["/app/data"]
ENV PORT=8000
CMD ["uvicorn", "main:APP", "--host", "0.0.0.0", "--port", "8000"]
