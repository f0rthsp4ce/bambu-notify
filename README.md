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
- Automatically **reconnects** on WebSocket errors **and** if **no `jpeg_image`** arrives for 60s.
- Optional **AI-based defect detection** via OpenRouter (Gemini 2.5 Flash)

## Endpoints (all prefixed with `/api`)

- `GET /api/` → health check (`OK`)
- `GET /api/printer/{PRINTER_ID}` → latest JSON status
- `GET /api/printer/{PRINTER_ID}/image` → latest JPEG frame (binary)

> By default `PRINTER_ID=A1M-1`, so the concrete paths are:
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
| `PRINTER_ID`             | `A1M-1`                                              | Printer ID used in the WebSocket URL and the fixed API path. |
| `WS_URL`                 | `wss://print.lo.f0rth.space/ws/printer/{PRINTER_ID}` | Override WebSocket URL if needed.                            |
| `PORT`                   | `8000`                                               | HTTP port for FastAPI.                                       |
| `TELEGRAM_BOT_TOKEN`     | —                                                    | **Required** to enable Telegram notifications.               |
| `TELEGRAM_CHAT_ID`       | —                                                    | **Required** chat ID (negative for supergroups).             |
| `TELEGRAM_THREAD_ID`     | —                                                    | Optional topic/thread ID within the chat.                    |
| `PROGRESS_STEP`          | `5`                                                  | Notify progress every N% (5, 10, ...).                       |
| `RECONNECT_MIN_SECONDS`  | `1`                                                  | Min backoff on reconnect.                                    |
| `RECONNECT_MAX_SECONDS`  | `30`                                                 | Max backoff on reconnect.                                    |
| `PHOTO_INTERVAL_SECONDS` | `3600`                                               | Hourly photo cadence during active prints.                   |
| `IMAGE_TIMEOUT_SECONDS`  | `60`                                                 | Force reconnect if no `jpeg_image` in N seconds.             |
| `WATCHDOG_TICK_SECONDS`  | `5`                                                  | How often the watchdog checks for stale images.              |
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

* **Resilient WebSocket:** Exponential backoff (bounded), automatic reconnect on network issues.
* **Image Watchdog:** If no `jpeg_image` is received for `IMAGE_TIMEOUT_SECONDS` (default 60s), the socket is closed intentionally to force a reconnect.
* **Hourly Photos:** While a print is active, the service posts a snapshot at `PHOTO_INTERVAL_SECONDS`. The timer resets on new jobs.
* **Final Photo:** When the job reaches a finished state (e.g., `FINISH`, `IDLE`, `DONE`), a last snapshot is posted (if available).
* **Progress Pacing:** Notifications at `PROGRESS_STEP`% increments; also on state transitions and errors.
* **AI Defect Detection (optional):** On an hourly cadence per job, the latest frame is analyzed by the configured OpenRouter model; if a defect is detected above threshold, a text alert and evidence photo are posted.

## Notes & Tips

* The API stores only the **latest** status and frame in memory; it does not persist history.
* Ensure your bot has permission to post photos in the destination chat/thread.
* Use a reverse proxy (nginx/traefik) for TLS/ingress in production.
* Health check: `GET /api/` returns `OK`. You can add readiness checks to also verify `latest_status_ts` recency if desired.

## License

MIT
