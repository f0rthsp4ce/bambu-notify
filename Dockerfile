# syntax=docker/dockerfile:1

FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY <<EOF /app/requirements.txt
fastapi==0.115.0
uvicorn[standard]==0.30.6
aiohttp==3.10.5
EOF

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

EXPOSE 8000
CMD ["python", "app.py"]
