FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py .
# Use uvicorn directly, binding to Railway's PORT
CMD ["sh", "-c", "uvicorn server:mcp.sse_app --host 0.0.0.0 --port ${PORT:-8080}"]
