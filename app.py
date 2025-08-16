import os
import asyncio
import base64
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any
from pathlib import Path

import aiohttp

try:
    # Prefer async client to avoid blocking the event loop
    from openai import AsyncOpenAI  # type: ignore
except Exception:  # pragma: no cover
    AsyncOpenAI = None  # type: ignore
from fastapi import FastAPI, APIRouter, Response, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
import uvicorn

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
PRINTER_ID = os.getenv("PRINTER_ID", "A1M-1")
DEFAULT_WS_URL = f"wss://print.lo.f0rth.space/ws/printer/{PRINTER_ID}"
WS_URL = os.getenv("WS_URL", DEFAULT_WS_URL)

PORT = int(os.getenv("PORT", "8000"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")  # e.g. "-100123456789"
TELEGRAM_THREAD_ID = os.getenv("TELEGRAM_THREAD_ID")  # optional

PROGRESS_STEP = int(os.getenv("PROGRESS_STEP", "5"))
RECONNECT_MIN_SECONDS = float(os.getenv("RECONNECT_MIN_SECONDS", "1"))
RECONNECT_MAX_SECONDS = float(os.getenv("RECONNECT_MAX_SECONDS", "30"))
PHOTO_INTERVAL_SECONDS = int(os.getenv("PHOTO_INTERVAL_SECONDS", "3600"))

# AI / OpenRouter configuration
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
AI_MODEL = os.getenv("AI_MODEL", "google/gemini-2.5-flash")
AI_CHECK_INTERVAL_SECONDS = int(os.getenv("AI_CHECK_INTERVAL_SECONDS", "3600"))
AI_CONFIDENCE_THRESHOLD = float(os.getenv("AI_CONFIDENCE_THRESHOLD", "0.7"))

# Image archival configuration
IMAGES_DIR = os.getenv("IMAGES_DIR", "images")
IMAGE_RETENTION_DAYS = int(os.getenv("IMAGE_RETENTION_DAYS", "7"))
RETENTION_CLEANUP_INTERVAL_SECONDS = int(
    os.getenv("RETENTION_CLEANUP_INTERVAL_SECONDS", "3600")
)

# Watchdog: force reconnect if no jpeg_image within this window
IMAGE_TIMEOUT_SECONDS = int(os.getenv("IMAGE_TIMEOUT_SECONDS", "60"))
WATCHDOG_TICK_SECONDS = int(os.getenv("WATCHDOG_TICK_SECONDS", "5"))

FINISHED_STATES = {
    "FINISH",
    "FINISHED",
    "IDLE",
    "DONE",
    "COMPLETED",
    "CANCELED",
    "CANCELLED",
    "FAIL",
    "FAILED",
}

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("printer-ws")


# -----------------------------------------------------------------------------
# App State
# -----------------------------------------------------------------------------
class AppState:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.latest_status: Optional[Dict[str, Any]] = None
        self.latest_status_ts: Optional[datetime] = None

        self.latest_image_bytes: Optional[bytes] = None
        self.latest_image_ts: Optional[datetime] = None
        self.image_seq: int = 0  # increments on every jpeg_image received

        # Notification tracking
        self.prev_status: Optional[Dict[str, Any]] = None
        self.last_notified_percent: Optional[int] = None
        self.last_notified_state: Optional[str] = None
        self.last_notified_task: Optional[str] = None

        # Hourly photo sending
        self.last_photo_sent_at: Optional[datetime] = None
        self.last_photo_job: Optional[str] = None

        # AI checks
        self.last_ai_check_at: Optional[datetime] = None
        self.last_ai_job: Optional[str] = None
        self.ai_client: Optional[Any] = None

        # Lifecycle
        self.run_event = asyncio.Event()
        self.run_event.set()

        # Shared HTTP session
        self.http_session: Optional[aiohttp.ClientSession] = None

        # Filesystem
        self.images_base_path: Path = Path(IMAGES_DIR)
        self.last_retention_cleanup_at: Optional[datetime] = None


state = AppState()

app = FastAPI(title="Printer Realtime Bridge", version="1.3.0")
router = APIRouter(prefix="/api")


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def safe_get(d: Optional[Dict[str, Any]], *path, default=None):
    cur = d or {}
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


async def ensure_http_session() -> aiohttp.ClientSession:
    if state.http_session is None or state.http_session.closed:
        timeout = aiohttp.ClientTimeout(total=None, connect=30, sock_read=None)
        state.http_session = aiohttp.ClientSession(timeout=timeout)
    return state.http_session


def is_finished_state(s: Optional[str]) -> bool:
    if not s:
        return False
    return s.upper() in FINISHED_STATES


def is_print_active(status: Optional[Dict[str, Any]]) -> bool:
    if not status:
        return False
    gcode_state = (safe_get(status, "gcode_state") or "").upper()
    task = safe_get(status, "subtask_name")
    percent = safe_get(status, "mc_percent")
    if is_finished_state(gcode_state):
        return False
    if gcode_state in {"RUNNING", "RESUME", "PRINTING", "BUSY"}:
        return True
    try:
        if task and percent is not None and int(percent) < 100:
            return True
    except Exception:
        pass
    return False


def ensure_images_dir(subdir: Optional[str] = None) -> Path:
    base = state.images_base_path
    try:
        base.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logger.warning("Failed to create base images dir %s: %s", base, e)
    if subdir:
        path = base / subdir
        try:
            path.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.warning("Failed to create subdir %s: %s", path, e)
        return path
    return base


def build_image_filename(job_name: Optional[str], ts: datetime, seq: int) -> str:
    safe_job = (job_name or "unknown").replace("/", "_").replace("\\", "_")
    return f"{ts.strftime('%Y%m%dT%H%M%S')}_{seq:06d}_{safe_job}.jpg"


async def save_image_if_active_job(image_bytes: bytes) -> None:
    if not image_bytes:
        return
    async with state.lock:
        status = state.latest_status
        ts = state.latest_image_ts or now_utc()
        seq = state.image_seq
    if not is_print_active(status):
        return
    job = safe_get(status, "subtask_name")
    target_dir = ensure_images_dir(job or None)
    fname = build_image_filename(job, ts, seq)
    try:
        # Write atomically
        tmp_path = target_dir / (fname + ".tmp")
        final_path = target_dir / fname
        with open(tmp_path, "wb") as f:
            f.write(image_bytes)
        os.replace(tmp_path, final_path)
    except Exception as e:
        logger.warning("Failed to save image %s: %s", fname, e)


# -----------------------------------------------------------------------------
# Telegram Notifications
# -----------------------------------------------------------------------------
def telegram_is_enabled() -> bool:
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


async def telegram_send(text: str) -> None:
    if not telegram_is_enabled():
        return
    try:
        session = await ensure_http_session()
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload: Dict[str, Any] = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "disable_web_page_preview": True,
        }
        if TELEGRAM_THREAD_ID:
            payload["message_thread_id"] = int(TELEGRAM_THREAD_ID)

        async with session.post(url, json=payload) as resp:
            if resp.status != 200:
                body = await resp.text()
                logger.warning("Telegram send failed: %s %s", resp.status, body)
    except Exception as e:
        logger.exception("Error sending Telegram message: %s", e)


async def telegram_send_photo(image_bytes: bytes, caption: str = "") -> None:
    if not telegram_is_enabled() or not image_bytes:
        return
    try:
        session = await ensure_http_session()
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        form = aiohttp.FormData()
        form.add_field("chat_id", TELEGRAM_CHAT_ID)
        if TELEGRAM_THREAD_ID:
            form.add_field("message_thread_id", str(int(TELEGRAM_THREAD_ID)))
        if caption:
            form.add_field("caption", caption)
        form.add_field(
            "photo", image_bytes, filename="frame.jpg", content_type="image/jpeg"
        )
        async with session.post(url, data=form) as resp:
            if resp.status != 200:
                body = await resp.text()
                logger.warning("Telegram photo failed: %s %s", resp.status, body)
    except Exception as e:
        logger.exception("Error sending Telegram photo: %s", e)


def format_time_remaining(minutes_val: Optional[int]) -> str:
    if minutes_val is None:
        return "n/a"
    try:
        m = int(minutes_val)
        if m < 0:
            return "n/a"
        hrs = m // 60
        mins = m % 60
        if hrs > 0:
            return f"{hrs}h {mins}m"
        return f"{mins}m"
    except Exception:
        return "n/a"


def summarize_status_for_notification(status: Dict[str, Any]) -> str:
    percent = safe_get(status, "mc_percent")
    layer = safe_get(status, "layer_num")
    total_layer = safe_get(status, "total_layer_num")
    gcode_state = safe_get(status, "gcode_state")
    subtask_name = safe_get(status, "subtask_name")
    remaining = safe_get(status, "mc_remaining_time")

    bits = []
    if subtask_name:
        bits.append(f"File: {subtask_name}")
    if percent is not None:
        bits.append(f"Progress: {percent}%")
    if layer is not None and total_layer is not None and total_layer not in (0, "0"):
        bits.append(f"Layer: {layer}/{total_layer}")
    if remaining is not None:
        bits.append(f"ETA: {format_time_remaining(remaining)}")
    if gcode_state:
        bits.append(f"State: {gcode_state}")
    return " | ".join(bits) if bits else "Update received."


# -----------------------------------------------------------------------------
# AI (OpenRouter via openai library)
# -----------------------------------------------------------------------------
def ai_is_enabled() -> bool:
    return bool(OPENROUTER_API_KEY and AsyncOpenAI is not None)


async def ensure_ai_client():
    if not ai_is_enabled():
        return None
    if state.ai_client is None:
        try:
            state.ai_client = AsyncOpenAI(
                base_url=OPENROUTER_BASE_URL,
                api_key=OPENROUTER_API_KEY,
            )
        except Exception as e:
            logger.exception("Failed to initialize AI client: %s", e)
            state.ai_client = None
    return state.ai_client


def _image_to_data_uri(image_bytes: bytes) -> str:
    b64 = base64.b64encode(image_bytes).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


async def analyze_image_with_ai(image_bytes: bytes) -> Optional[Dict[str, Any]]:
    """Analyze image for visible failures/defects using the configured model.

    Returns: {"has_defect": bool, "confidence": float [0..1], "summary": str} or None.
    """
    client = await ensure_ai_client()
    if client is None:
        return None
    try:
        system_prompt = (
            "You are an expert 3D printing monitor. Analyze the photo for visible print failures "
            "or defects (e.g., spaghetti, layer shifts, adhesion issues, severe stringing, nozzle crash). "
            'Respond ONLY with strict JSON: {"has_defect": boolean, "confidence": number between 0 and 1, "summary": short string}.'
        )
        user_text = "Check for any visible failure or defect in this print frame. If likely failing, set has_defect=true."
        data_uri = _image_to_data_uri(image_bytes)
        resp = await client.chat.completions.create(
            model=AI_MODEL,
            temperature=0.1,
            max_tokens=300,
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_text},
                        {"type": "image_url", "image_url": {"url": data_uri}},
                    ],
                },
            ],
        )
        content = (resp.choices[0].message.content or "").strip()
        try:
            parsed = json.loads(content)
        except Exception:
            lowered = content.lower()
            has_defect = any(
                kw in lowered
                for kw in [
                    "defect",
                    "failure",
                    "spaghetti",
                    "layer shift",
                    "crash",
                    "detached",
                    "warping",
                    "stringing",
                ]
            )
            parsed = {
                "has_defect": has_defect,
                "confidence": 0.5,
                "summary": content[:300],
            }
        verdict = bool(parsed.get("has_defect", False))
        try:
            conf = float(parsed.get("confidence", 0))
        except Exception:
            conf = 0.0
        summary = str(parsed.get("summary") or "")
        return {"has_defect": verdict, "confidence": conf, "summary": summary}
    except Exception as e:
        logger.exception("AI analysis failed: %s", e)
        return None


async def ai_check_and_alert_if_needed() -> None:
    """Run defect analysis on the latest frame on a per-job hourly cadence."""
    async with state.lock:
        status = state.latest_status
        img = state.latest_image_bytes
        job = safe_get(status, "subtask_name")
        last_checked = state.last_ai_check_at
        last_job = state.last_ai_job

    if not ai_is_enabled() or not status or not img:
        return
    if not is_print_active(status):
        return

    if job != last_job:
        async with state.lock:
            state.last_ai_job = job
            state.last_ai_check_at = None
        last_checked = None

    due = (last_checked is None) or (
        (now_utc() - last_checked) >= timedelta(seconds=AI_CHECK_INTERVAL_SECONDS)
    )
    if not due:
        return

    result = await analyze_image_with_ai(img)
    async with state.lock:
        state.last_ai_check_at = now_utc()

    if not result:
        return

    has_defect = bool(result.get("has_defect", False))
    confidence = float(result.get("confidence", 0.0))
    summary = str(result.get("summary") or "")

    if has_defect and confidence >= AI_CONFIDENCE_THRESHOLD:
        caption = (
            f"🚨 Possible print failure detected @cofob\n"
            f"Confidence: {confidence:.2f}\n"
            f"{summarize_status_for_notification(status)}\n"
            f"Finding: {summary[:400]}\n"
            f"Time: {now_utc().isoformat()}"
        )
        await telegram_send(caption)
        await telegram_send_photo(img, "🔎 Evidence frame")


# -----------------------------------------------------------------------------
# Image retention cleanup
# -----------------------------------------------------------------------------
async def delete_old_images() -> None:
    """Delete images older than IMAGE_RETENTION_DAYS from IMAGES_DIR."""
    try:
        base = ensure_images_dir()
        cutoff = now_utc() - timedelta(days=IMAGE_RETENTION_DAYS)
        # Iterate through all subdirectories (job names) and files
        for sub in base.iterdir():
            try:
                if sub.is_file():
                    # Unexpected files directly under base
                    mtime = datetime.fromtimestamp(sub.stat().st_mtime, tz=timezone.utc)
                    if mtime < cutoff:
                        sub.unlink(missing_ok=True)
                    continue
                if not sub.is_dir():
                    continue
                for file in sub.iterdir():
                    if not file.is_file():
                        continue
                    mtime = datetime.fromtimestamp(
                        file.stat().st_mtime, tz=timezone.utc
                    )
                    if mtime < cutoff:
                        file.unlink(missing_ok=True)
                # Remove empty job folders
                try:
                    next(sub.iterdir())
                except StopIteration:
                    sub.rmdir()
            except Exception as e:
                logger.debug("Retention scan skip %s: %s", sub, e)
    except Exception as e:
        logger.debug("Retention cleanup error: %s", e)


async def send_hourly_photo_if_needed() -> None:
    """Send a photo if active job hasn't had one in PHOTO_INTERVAL_SECONDS."""
    async with state.lock:
        status = state.latest_status
        img = state.latest_image_bytes
        job = safe_get(status, "subtask_name")
        last_sent = state.last_photo_sent_at

    if not status or not img:
        return
    if not is_print_active(status):
        return

    # Reset per-job timer
    if job != state.last_photo_job:
        async with state.lock:
            state.last_photo_job = job
            state.last_photo_sent_at = None
        last_sent = None

    due = (last_sent is None) or (
        (now_utc() - last_sent) >= timedelta(seconds=PHOTO_INTERVAL_SECONDS)
    )
    if not due:
        return

    caption = f"🖼️ Hourly snapshot\n{summarize_status_for_notification(status)}\nTime: {now_utc().isoformat()}"
    await telegram_send_photo(img, caption)
    async with state.lock:
        state.last_photo_sent_at = now_utc()


# -----------------------------------------------------------------------------
# WebSocket watchdog (no jpeg_image -> reconnect)
# -----------------------------------------------------------------------------
async def image_watchdog(
    ws: aiohttp.ClientWebSocketResponse, connected_at: datetime
) -> None:
    while state.run_event.is_set() and not ws.closed:
        await asyncio.sleep(max(1, WATCHDOG_TICK_SECONDS))
        async with state.lock:
            last_img_ts = state.latest_image_ts
        base_ts = last_img_ts or connected_at
        if (now_utc() - base_ts).total_seconds() > IMAGE_TIMEOUT_SECONDS:
            logger.warning(
                "No jpeg_image in %ds; forcing reconnect.", IMAGE_TIMEOUT_SECONDS
            )
            try:
                await ws.close(code=aiohttp.WSCloseCode.OK, message=b"image-timeout")
            except Exception:
                pass
            break


# -----------------------------------------------------------------------------
# WebSocket loop with auto-reconnect
# -----------------------------------------------------------------------------
async def websocket_loop() -> None:
    backoff = RECONNECT_MIN_SECONDS
    while state.run_event.is_set():
        try:
            session = await ensure_http_session()
            logger.info("Connecting to WebSocket: %s", WS_URL)
            async with session.ws_connect(WS_URL, heartbeat=30) as ws:
                logger.info("WebSocket connected.")
                backoff = RECONNECT_MIN_SECONDS

                connected_at = now_utc()
                watchdog_task = asyncio.create_task(image_watchdog(ws, connected_at))

                try:
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                payload = json.loads(msg.data)
                            except json.JSONDecodeError:
                                logger.warning(
                                    "Skipping non-JSON frame of length %d",
                                    len(msg.data),
                                )
                                continue

                            msg_type = payload.get("type")
                            if msg_type == "printer_status":
                                data = payload.get("data")
                                if isinstance(data, dict):
                                    async with state.lock:
                                        state.latest_status = data
                                        state.latest_status_ts = now_utc()
                                    try:
                                        await maybe_notify_on_update(data)
                                    except Exception as e:
                                        logger.exception("Notify error: %s", e)

                            elif msg_type == "jpeg_image":
                                b64 = payload.get("image")
                                if isinstance(b64, str):
                                    try:
                                        decoded = base64.b64decode(b64, validate=False)
                                        async with state.lock:
                                            state.latest_image_bytes = decoded
                                            state.latest_image_ts = now_utc()
                                            state.image_seq += 1
                                        # Persist only if a print job is active
                                        try:
                                            await save_image_if_active_job(decoded)
                                        except Exception as e:
                                            logger.debug(
                                                "Image save skipped/failed: %s", e
                                            )
                                    except Exception:
                                        logger.warning(
                                            "Failed to decode JPEG image (base64)."
                                        )
                            else:
                                logger.debug("Unknown message type: %s", msg_type)

                        elif msg.type == aiohttp.WSMsgType.BINARY:
                            logger.debug(
                                "Received binary WS frame (%d bytes).", len(msg.data)
                            )

                        elif msg.type in (
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.CLOSING,
                            aiohttp.WSMsgType.CLOSED,
                        ):
                            logger.warning("WebSocket closed by server.")
                            break

                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            logger.error("WebSocket error: %s", ws.exception())
                            break
                finally:
                    watchdog_task.cancel()
                    try:
                        await watchdog_task
                    except asyncio.CancelledError:
                        pass

        except asyncio.CancelledError:
            logger.info("WebSocket loop cancelled.")
            break
        except Exception as e:
            logger.warning("WebSocket connection failed: %s", e)

        if not state.run_event.is_set():
            break
        sleep_for = min(backoff, RECONNECT_MAX_SECONDS)
        logger.info("Reconnecting in %.1f seconds...", sleep_for)
        await asyncio.sleep(sleep_for)
        backoff = min(backoff * 2, RECONNECT_MAX_SECONDS)

    logger.info("WebSocket loop exited.")


# -----------------------------------------------------------------------------
# Notification logic
# -----------------------------------------------------------------------------
async def maybe_notify_on_update(new_status: Dict[str, Any]) -> None:
    prev = state.prev_status or {}
    new_state = (safe_get(new_status, "gcode_state") or "").upper()
    old_state = (safe_get(prev, "gcode_state") or "").upper()
    new_task = safe_get(new_status, "subtask_name")
    new_percent = safe_get(new_status, "mc_percent")
    print_error = safe_get(new_status, "print_error")
    remaining = safe_get(new_status, "mc_remaining_time")

    # Task change (new file or restarted job)
    if new_task and new_task != state.last_notified_task:
        msg = f"🖨️ Print job: {new_task}"
        if new_state:
            msg += f"\nState: {new_state}"
        if isinstance(new_percent, int):
            msg += f"\nProgress: {new_percent}%"
        await telegram_send(msg)
        state.last_notified_task = new_task
        state.last_notified_percent = None
        state.last_photo_job = new_task
        state.last_photo_sent_at = None

    # State change
    if new_state and new_state != state.last_notified_state:
        await telegram_send(
            f"🔄 State changed: {old_state or 'unknown'} → {new_state}\n{summarize_status_for_notification(new_status)}"
        )
        state.last_notified_state = new_state

        # If job just finished/idle, summary + final photo
        if is_finished_state(new_state):
            await telegram_send(
                f"✅ Print completed.\n{summarize_status_for_notification(new_status)}"
            )
            img = state.latest_image_bytes
            if img:
                caption = f"🏁 Final photo\n{summarize_status_for_notification(new_status)}\nTime: {now_utc().isoformat()}"
                await telegram_send_photo(img, caption)
            state.last_photo_sent_at = None

    # Error
    try:
        err_int = int(print_error) if print_error is not None else 0
    except Exception:
        err_int = 0
    if err_int:
        await telegram_send(
            f"❗ Printer reported error code: {err_int}\n{summarize_status_for_notification(new_status)}"
        )

    # Progress step notification
    if isinstance(new_percent, int):
        threshold = state.last_notified_percent
        should_notify = False
        current_step = (int(new_percent) // PROGRESS_STEP) * PROGRESS_STEP
        if threshold is None:
            if new_percent == 0 or current_step > 0:
                should_notify = True
        elif new_percent >= threshold + PROGRESS_STEP:
            should_notify = True

        if should_notify:
            msg = f"⏳ Progress: {current_step if current_step > 0 else int(new_percent)}%"
            if remaining is not None:
                msg += f" • ETA {format_time_remaining(remaining)}"
            layer = safe_get(new_status, "layer_num")
            total_layer = safe_get(new_status, "total_layer_num")
            if layer is not None and total_layer not in (None, 0, "0"):
                msg += f" • Layer {layer}/{total_layer}"
            await telegram_send(msg)
            state.last_notified_percent = (
                current_step if current_step > 0 else int(new_percent)
            )

    state.prev_status = new_status.copy()


# -----------------------------------------------------------------------------
# Background hourly photo loop
# -----------------------------------------------------------------------------
async def photo_loop() -> None:
    """Lightweight ticker to evaluate hourly photo sending."""
    tick_seconds = max(
        20, min(120, PHOTO_INTERVAL_SECONDS // 90 or 30)
    )  # 20–120s ticks
    while state.run_event.is_set():
        try:
            if telegram_is_enabled():
                await send_hourly_photo_if_needed()
            # Run AI check on a similar lightweight cadence
            if ai_is_enabled():
                await ai_check_and_alert_if_needed()
            # Periodic retention cleanup (no more often than configured)
            now = now_utc()
            last_cleanup = state.last_retention_cleanup_at
            if (
                last_cleanup is None
                or (now - last_cleanup).total_seconds()
                >= RETENTION_CLEANUP_INTERVAL_SECONDS
            ):
                await delete_old_images()
                async with state.lock:
                    state.last_retention_cleanup_at = now
        except Exception as e:
            logger.exception("Hourly photo loop error: %s", e)
        await asyncio.sleep(tick_seconds)


# -----------------------------------------------------------------------------
# API Routes (all under /api prefix)
# -----------------------------------------------------------------------------
@router.get("/api/", response_class=PlainTextResponse)
async def root():
    return "OK"


@router.get(f"/api/api/printer/{PRINTER_ID}")
async def get_printer_status():
    async with state.lock:
        if state.latest_status is None:
            raise HTTPException(status_code=404, detail="No status received yet.")
        payload = {
            "printer_id": PRINTER_ID,
            "received_at": state.latest_status_ts.isoformat()
            if state.latest_status_ts
            else None,
            "status": state.latest_status,
        }
    return JSONResponse(payload)


@router.get(f"/api/printer/{PRINTER_ID}/image")
async def get_printer_image():
    async with state.lock:
        if state.latest_image_bytes is None:
            raise HTTPException(status_code=404, detail="No image received yet.")
        img = state.latest_image_bytes
        ts = state.latest_image_ts.isoformat() if state.latest_image_ts else None

    headers = {
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "X-Image-Timestamp": ts or "",
    }
    return Response(content=img, media_type="image/jpeg", headers=headers)


# Optional dynamic routes (serve only the configured printer)
@router.get("/api/printer/{any_printer_id}")
async def get_status_dynamic(any_printer_id: str):
    if any_printer_id != PRINTER_ID:
        raise HTTPException(
            status_code=404,
            detail=f"Unsupported printer id. This instance serves only '{PRINTER_ID}'.",
        )
    return await get_printer_status()


@router.get("/api/printer/{any_printer_id}/image")
async def get_image_dynamic(any_printer_id: str):
    if any_printer_id != PRINTER_ID:
        raise HTTPException(
            status_code=404,
            detail=f"Unsupported printer id. This instance serves only '{PRINTER_ID}'.",
        )
    return await get_printer_image()


app.include_router(router)

# -----------------------------------------------------------------------------
# Lifecycle
# -----------------------------------------------------------------------------
_ws_task: Optional[asyncio.Task] = None
_photo_task: Optional[asyncio.Task] = None


@app.on_event("startup")
async def on_startup():
    logger.info(
        "Starting app. PRINTER_ID=%s | WS_URL=%s | PHOTO_INTERVAL_SECONDS=%s | IMAGE_TIMEOUT_SECONDS=%s",
        PRINTER_ID,
        WS_URL,
        PHOTO_INTERVAL_SECONDS,
        IMAGE_TIMEOUT_SECONDS,
    )
    global _ws_task, _photo_task
    _ws_task = asyncio.create_task(websocket_loop())
    _photo_task = asyncio.create_task(photo_loop())
    if ai_is_enabled():
        # Warm up AI client early to pay the init cost upfront
        try:
            await ensure_ai_client()
            logger.info(
                "AI client initialized. Model=%s BaseURL=%s",
                AI_MODEL,
                OPENROUTER_BASE_URL,
            )
        except Exception:
            logger.warning("AI client initialization skipped or failed.")


@app.on_event("shutdown")
async def on_shutdown():
    logger.info("Shutting down...")
    state.run_event.clear()
    global _ws_task, _photo_task
    for task in (_ws_task, _photo_task):
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    _ws_task = None
    _photo_task = None
    if state.http_session and not state.http_session.closed:
        await state.http_session.close()
    # No explicit close needed for AsyncOpenAI


# -----------------------------------------------------------------------------
# Entrypoint
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=PORT, reload=False)
