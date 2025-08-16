# syntax=docker/dockerfile:1

FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt

RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app.py /app/app.py

ENV PRINTER_ID=A1M-1
ENV WS_URL=""
ENV PORT=8000
ENV PROGRESS_STEP=5
ENV RECONNECT_MIN_SECONDS=1
ENV RECONNECT_MAX_SECONDS=30
ENV PHOTO_INTERVAL_SECONDS=3600
ENV IMAGE_TIMEOUT_SECONDS=60
ENV WATCHDOG_TICK_SECONDS=5
ENV OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
ENV AI_MODEL=google/gemini-2.5-flash
ENV AI_CHECK_INTERVAL_SECONDS=3600
ENV AI_CONFIDENCE_THRESHOLD=0.7
ENV IMAGES_DIR=images
ENV IMAGE_RETENTION_DAYS=7
ENV RETENTION_CLEANUP_INTERVAL_SECONDS=3600
ENV TIMELAPSE_MAX_BYTES=52428800
ENV TIMELAPSE_TARGET_DURATION_SECONDS=45
ENV TIMELAPSE_MAX_WIDTH=1280
ENV TIMELAPSE_FPS_CAP=30

EXPOSE 8000
CMD ["python", "app.py"]
