# Printer Realtime Bridge

A tiny FastAPI + aiohttp service that:

- Maintains a resilient WebSocket connection to your printer at  
  `wss://print.lo.f0rth.space/ws/printer/{PRINTER_ID}`.
- Exposes REST APIs (under `/api`) for the **latest status** and **latest camera frame**.
- Sends Telegram notifications to a **specific thread/topic**:
  - job start / file changes
  - state changes
  - progress in configurable steps (default 5%)
  - errors
  - **hourly photo snapshots while printing**
  - **final photo on completion**
  - **timelapse video on completion (≤ 50 MB, saved to disk and sent to Telegram)**
- Automatically **reconnects** on WebSocket errors, if **no `jpeg_image`** arrives for 60s, and also **forces a reconnect every 5 minutes** (configurable).
- If **no `printer_status`** is received for 30s, marks the printer state as **IDLE** (configurable via `STATUS_TIMEOUT_SECONDS`). This can cause a RUNNING → IDLE transition, which triggers completion notifications and timelapse logic.
- Optional **AI-based defect detection** via OpenRouter (Gemini 2.5 Flash)
- Saves frames to disk for active print jobs; retains last 7 days by default

## Endpoints (all prefixed with `/api`)

- `GET /api/` → health check (`OK`)
- `GET /api/printers` → list known printers `{ printer_id, model, is_online, has_status }`
- `GET /api/printer/{printer_id}` → latest JSON status for that printer
- `GET /api/printer/{printer_id}/image` → latest JPEG frame (binary) for that printer

> Back-compat: if you still rely on a fixed `PRINTER_ID`, the same paths work:
> - `GET /api/printer/A1M-1`
> - `GET /api/printer/A1M-1/image`

### Curl examples
```bash
curl -s http://localhost:8000/api/
curl -s http://localhost:8000/api/printer/A1M-1 | jq
curl -s -o frame.jpg http://localhost:8000/api/printer/A1M-1/image
````

## Environment Variables

| Name                     | Default                                              | Description                                                  |
| ------------------------ | ---------------------------------------------------- | ------------------------------------------------------------ |
| `PRINTER_ID`             | `A1M-1`                                              | Seed printer ID for backward compatibility.                  |
| `WS_URL`                 | `wss://print.lo.f0rth.space/ws/printer/{PRINTER_ID}` | Legacy single-printer URL, used only for startup logs.       |
| `PRINTERS_API_URL`       | `https://print.lo.f0rth.space/api/printers`          | Discovery endpoint returning printers list.                  |
| `PRINTERS_REFRESH_SECONDS` | `3600`                                            | How often to refresh printers list (hourly by default).      |
| `WS_URL_TEMPLATE`        | `wss://print.lo.f0rth.space/ws/printer/{printer_id}` | Template for per-printer WebSocket URL.                      |
| `PORT`                   | `8000`                                               | HTTP port for FastAPI.                                       |
| `TELEGRAM_BOT_TOKEN`     | —                                                    | **Required** to enable Telegram notifications.               |
| `TELEGRAM_CHAT_ID`       | —                                                    | **Required** chat ID (negative for supergroups).             |
| `TELEGRAM_THREAD_ID`     | —                                                    | Optional topic/thread ID within the chat.                    |
| `PROGRESS_STEP`          | `5`                                                  | Notify progress every N% (5, 10, ...).                       |
| `RECONNECT_MIN_SECONDS`  | `1`                                                  | Min backoff on reconnect.                                    |
| `RECONNECT_MAX_SECONDS`  | `30`                                                 | Max backoff on reconnect.                                    |
| `PHOTO_INTERVAL_SECONDS` | `3600`                                               | Hourly photo cadence during active prints.                   |
| `FORCED_RECONNECT_SECONDS` | `300`                                              | Force a reconnect periodically (set ≤ 0 to disable).         |
| `IMAGE_TIMEOUT_SECONDS`  | `60`                                                 | Force reconnect if no `jpeg_image` in N seconds.             |
| `WATCHDOG_TICK_SECONDS`  | `5`                                                  | How often the watchdog checks for stale images.              |
| `IMAGES_DIR`             | `images`                                             | Base directory to save camera frames for active jobs.        |
| `IMAGE_RETENTION_DAYS`   | `7`                                                  | How many days to retain saved images.                        |
| `RETENTION_CLEANUP_INTERVAL_SECONDS` | `3600`                                  | Cleanup cadence check (piggybacks hourly loop).              |
| `TIMELAPSE_MAX_BYTES`      | `52428800` (50 MB)                               | Maximum size for generated timelapse MP4.                    |
| `TIMELAPSE_TARGET_DURATION_SECONDS` | `45`                                     | Desired timelapse duration; FPS derived from frame count.    |
| `TIMELAPSE_MAX_WIDTH`      | `1280`                                           | Max output width; height auto-calculated.                    |
| `TIMELAPSE_FPS_CAP`        | `30`                                             | Max FPS cap when deriving from frame count.                  |
| `OPENROUTER_API_KEY`     | —                                                    | API key for OpenRouter. Enables AI checks when set.          |
| `OPENROUTER_BASE_URL`    | `https://openrouter.ai/api/v1`                      | Base URL for OpenRouter.                                     |
| `AI_MODEL`               | `google/gemini-2.5-flash`                           | Vision-capable model ID on OpenRouter.                       |
| `AI_CHECK_INTERVAL_SECONDS` | `3600`                                           | How often to run AI frame checks per job.                    |
| `AI_CONFIDENCE_THRESHOLD`| `0.7`                                                | Min confidence to alert on detected defect.                  |

## Telegram Setup

1. Create a bot via `@BotFather` → get the token.
2. Add the bot to your group/supergroup and grant permission to post.
3. Obtain `TELEGRAM_CHAT_ID`:

   * Use a helper bot like `@RawDataBot` or an API call from a quick script.
4. Optional: For forum-style topics, set `TELEGRAM_THREAD_ID` to the topic ID.

## Run Locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install fastapi==0.115.0 "uvicorn[standard]==0.30.6" aiohttp==3.10.5
pip install openai==1.99.9

export TELEGRAM_BOT_TOKEN=123456:ABCDEF...
export TELEGRAM_CHAT_ID=-1001234567890
# export TELEGRAM_THREAD_ID=42           # optional
# Dynamic printers (optional overrides)
# export PRINTERS_API_URL=https://print.lo.f0rth.space/api/printers
# export PRINTERS_REFRESH_SECONDS=3600
# export WS_URL_TEMPLATE=wss://print.lo.f0rth.space/ws/printer/{printer_id}
export OPENROUTER_API_KEY=sk-or-...
# export OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
# export AI_MODEL=google/gemini-2.5-flash
# export AI_CHECK_INTERVAL_SECONDS=3600
# export AI_CONFIDENCE_THRESHOLD=0.6

python app.py
# API at: http://localhost:8000/api/...
```

## Run via Docker

```bash
docker build -t printer-bridge:latest .
docker run --rm -p 8000:8000 \
  -e TELEGRAM_BOT_TOKEN=$TELEGRAM_BOT_TOKEN \
  -e TELEGRAM_CHAT_ID=$TELEGRAM_CHAT_ID \
  -e TELEGRAM_THREAD_ID=$TELEGRAM_THREAD_ID \
  -e OPENROUTER_API_KEY=$OPENROUTER_API_KEY \
  -e PRINTER_ID=A1M-1 \
  printer-bridge:latest
```

## Behavior Details

* **Resilient WebSocket:** Exponential backoff (bounded), automatic reconnect per-printer on network issues.
* **Image Watchdog:** If no `jpeg_image` is received for `IMAGE_TIMEOUT_SECONDS` (default 60s), the socket is closed intentionally to force a reconnect.
* **Hourly Photos:** While a print is active, the service posts a snapshot at `PHOTO_INTERVAL_SECONDS` per printer. The timer resets on new jobs.
* **Final Photo:** When the job reaches a finished state (e.g., `FINISH`, `IDLE`, `DONE`), a last snapshot is posted (if available).
* **Timelapse:** On completion, frames saved under `images/<printer_id>/<job_name>/` are compiled with ffmpeg into an MP4 and saved alongside the frames. The video is kept under `TIMELAPSE_MAX_BYTES` via adaptive width/CRF/bitrate and is sent to Telegram.
* **Progress Pacing:** Notifications at `PROGRESS_STEP`% increments; also on state transitions and errors.
* **AI Defect Detection (optional):** On an hourly cadence per job (per printer), the latest frame is analyzed by the configured OpenRouter model; if a defect is detected above threshold, a text alert and evidence photo are posted.

## Manual timelapse from saved snapshots

If you want to build a timelapse yourself from the saved JPEG frames on disk (without waiting for the automatic one), you can use ffmpeg directly.

- **Where frames are saved**: `IMAGES_DIR` (default `images`), organized as `images/<printer_id>/<job_name>/`.
- **Filename pattern**: `YYYYMMDDThhmmss_XXXXXX_<job>.jpg` (lexicographically sorted by time).

### Quick start (macOS)

```bash
# 1) Install ffmpeg (once)
brew install ffmpeg

# 2) Go to the job's frames directory
cd "images/<printer_id>/<job_name>"

# 3) Build a 30 FPS timelapse (scaled to max width 1280)
ffmpeg -hide_banner -loglevel error \
  -pattern_type glob -framerate 30 -i '*.jpg' \
  -vf "scale='min(iw,1280)':'-2',format=yuv420p,setsar=1" \
  -c:v libx264 -crf 23 -pix_fmt yuv420p -movflags +faststart \
  timelapse.mp4
```

### Target a specific duration

Pick a desired duration (in seconds). FPS will be derived as ceil(frames / duration).

```bash
cd "images/<printer_id>/<job_name>"
FRAMES=$(ls -1 *.jpg 2>/dev/null | wc -l | tr -d ' ')
DURATION=45  # seconds (adjust)
FPS=$(( (FRAMES + DURATION - 1) / DURATION ))
[ "$FPS" -lt 1 ] && FPS=1

ffmpeg -hide_banner -loglevel error \
  -pattern_type glob -framerate "$FPS" -i '*.jpg' \
  -vf "scale='min(iw,1280)':'-2',format=yuv420p,setsar=1" \
  -c:v libx264 -crf 23 -pix_fmt yuv420p -movflags +faststart \
  timelapse.mp4
```

Notes:
- The lexicographic order of filenames matches capture time, so globbing `'*.jpg'` produces the correct sequence.
- If your frames are huge, increase CRF (e.g., 28) to shrink the file, or reduce the max width (e.g., `min(iw,640)`).
- The service auto-cleans frames older than `IMAGE_RETENTION_DAYS` (default 7 days).

## Notes & Tips

* The API stores only the **latest** status and frame in memory per printer; it does not persist history.
* Printers are discovered from `PRINTERS_API_URL` and refreshed every `PRINTERS_REFRESH_SECONDS`. Only printers with `is_online=true` spawn WebSocket connections.
* Ensure your bot has permission to post photos in the destination chat/thread.
* Use a reverse proxy (nginx/traefik) for TLS/ingress in production.
* Health check: `GET /api/` returns `OK`. You can add readiness checks to also verify `latest_status_ts` recency if desired.

## License

MIT
