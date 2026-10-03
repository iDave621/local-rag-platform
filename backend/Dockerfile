FROM python:3.12-slim-bookworm@sha256:54c85f3c47607a77f32adec749d3c81d1348bf25833671f512b26a9b6d778cb3
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    ANONYMIZED_TELEMETRY=False OTEL_SDK_DISABLED=true \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 \
    LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false
WORKDIR /app
# Executed only on the connected preparation machine. No package installation at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl openssl tesseract-ocr tesseract-ocr-eng tesseract-ocr-heb \
    libreoffice-writer fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
COPY backend/requirements.lock.txt ./requirements.lock.txt
RUN pip install --no-cache-dir -r requirements.lock.txt && pip check
COPY backend/app ./app
LABEL org.opencontainers.image.version="2.2.0"
