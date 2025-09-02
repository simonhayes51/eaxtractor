FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py endpoints.yaml ./
CMD sh -c 'uvicorn main:APP --host 0.0.0.0 --port ${PORT:-8000}'
