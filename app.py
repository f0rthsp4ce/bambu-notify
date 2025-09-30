import os
import asyncio
import base64
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, Set, List, Tuple
from pathlib import Path
import shutil
import io

import aiohttp
from PIL import Image

try:
    # Prefer async client to avoid blocking the event loop
    from openai import AsyncOpenAI  # type: ignore
except Exception:  # pragma: no cover
    AsyncOpenAI = None  # type: ignore
from fastapi import FastAPI, APIRouter, Response, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
import uvicorn
from prometheus_client import (
    REGISTRY,
    generate_latest,
    CONTENT_TYPE_LATEST,
    Gauge,
    CollectorRegistry,
)  # type: ignore[import-not-found]
from prometheus_client.core import GaugeMetricFamily  # type: ignore[import-not-found]

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
PRINTER_ID = os.getenv("PRINTER_ID", "A1M-1")
# Back-compat single-printer URL (used only for initial seeding). Dynamic mode uses WS_URL_TEMPLATE.
DEFAULT_WS_URL = f"wss://print.lo.f0rth.space/ws/printer/{PRINTER_ID}"
WS_URL = os.getenv("WS_URL", DEFAULT_WS_URL)

# Dynamic printers discovery
PRINTERS_API_URL = os.getenv(
    "PRINTERS_API_URL", "https://print.lo.f0rth.space/api/printers"
)
PRINTERS_REFRESH_SECONDS = int(os.getenv("PRINTERS_REFRESH_SECONDS", "3600"))
WS_URL_TEMPLATE = os.getenv(
    "WS_URL_TEMPLATE", "wss://print.lo.f0rth.space/ws/printer/{printer_id}"
)

# Optional HTTP status fallback/seed (helps populate metrics before WS delivers)
HTTP_STATUS_URL_TEMPLATE = os.getenv(
    "HTTP_STATUS_URL_TEMPLATE", "https://print.lo.f0rth.space/api/printer/{printer_id}"
)
HTTP_STATUS_SEED_ON_STARTUP = os.getenv("HTTP_STATUS_SEED_ON_STARTUP", "1") not in {
    "0",
    "false",
    "False",
}
HTTP_STATUS_SEED_INTERVAL_SECONDS = int(
    os.getenv("HTTP_STATUS_SEED_INTERVAL_SECONDS", "10")
)

PORT = int(os.getenv("PORT", "8000"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")  # e.g. "-100123456789"
TELEGRAM_THREAD_ID = os.getenv("TELEGRAM_THREAD_ID")  # optional

PROGRESS_STEP = int(os.getenv("PROGRESS_STEP", "5"))
RECONNECT_MIN_SECONDS = float(os.getenv("RECONNECT_MIN_SECONDS", "1"))
RECONNECT_MAX_SECONDS = float(os.getenv("RECONNECT_MAX_SECONDS", "30"))
PHOTO_INTERVAL_SECONDS = int(os.getenv("PHOTO_INTERVAL_SECONDS", "3600"))
FORCED_RECONNECT_SECONDS = int(os.getenv("FORCED_RECONNECT_SECONDS", "300"))

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

# Timelapse configuration
TIMELAPSE_MAX_BYTES = int(os.getenv("TIMELAPSE_MAX_BYTES", str(50 * 1024 * 1024)))
TIMELAPSE_TARGET_DURATION_SECONDS = int(
    os.getenv("TIMELAPSE_TARGET_DURATION_SECONDS", "45")
)
TIMELAPSE_MAX_WIDTH = int(os.getenv("TIMELAPSE_MAX_WIDTH", "1280"))
TIMELAPSE_FPS_CAP = int(os.getenv("TIMELAPSE_FPS_CAP", "30"))

# Watchdog: force reconnect if no jpeg_image within this window
IMAGE_TIMEOUT_SECONDS = int(os.getenv("IMAGE_TIMEOUT_SECONDS", "60"))
WATCHDOG_TICK_SECONDS = int(os.getenv("WATCHDOG_TICK_SECONDS", "5"))
# Status watchdog: mark status UNKNOWN if no printer_status within this window
STATUS_TIMEOUT_SECONDS = int(os.getenv("STATUS_TIMEOUT_SECONDS", "30"))

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

IGNORE_TRANSITIONS = {
    "FINISH",
    "FINISHED",
    "IDLE",
}

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
# Ensure our logger shows debug messages
logger = logging.getLogger("printer-ws")
logger.setLevel(logging.DEBUG)


# -----------------------------------------------------------------------------
# App State
# -----------------------------------------------------------------------------
class PrinterState:
    def __init__(self, printer_id: str, model: Optional[str] = None) -> None:
        self.printer_id = printer_id
        self.model = model
        self.is_online: bool = False
        # If true, we will always maintain a WS connection regardless of discovery status.
        self.always_connect: bool = False

        self.lock = asyncio.Lock()
        self.latest_status: Optional[Dict[str, Any]] = None
        self.latest_status_ts: Optional[datetime] = None

        self.latest_image_bytes: Optional[bytes] = None
        self.latest_image_ts: Optional[datetime] = None
        self.image_seq: int = 0

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

        # Timelapse guard per printer
        self.completed_timelapse_jobs: Set[str] = set()
        self.current_job_name: Optional[str] = None
        self.current_job_started_at: Optional[datetime] = None


class AppState:
    def __init__(self) -> None:
        # Global lock only for high-level structures
        self.lock = asyncio.Lock()

        # Dynamic printers
        self.printers: Dict[str, PrinterState] = {}
        self.ws_tasks: Dict[str, asyncio.Task] = {}

        # AI client (shared)
        self.ai_client: Optional[Any] = None

        # Lifecycle
        self.run_event = asyncio.Event()
        self.run_event.set()

        # Shared HTTP session
        self.http_session: Optional[aiohttp.ClientSession] = None

        # Filesystem
        self.images_base_path: Path = Path(IMAGES_DIR)
        self.last_retention_cleanup_at: Optional[datetime] = None

        # Seed a default printer immediately so metrics are not empty prior to discovery loop
        if PRINTER_ID:
            try:
                self.printers[PRINTER_ID] = PrinterState(PRINTER_ID)
                self.printers[PRINTER_ID].is_online = True
                self.printers[PRINTER_ID].always_connect = True
            except Exception:
                pass


state = AppState()

app = FastAPI(title="Printer Realtime Bridge", version="1.4.0")
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


def parse_iso8601(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        # Replace trailing Z with explicit UTC offset to support older Python formats
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


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


async def _start_ws_task_if_needed(pstate: PrinterState) -> None:
    async with state.lock:
        task = state.ws_tasks.get(pstate.printer_id)
        if task and not task.done():
            return
        # Start new task
        t = asyncio.create_task(websocket_loop_for_printer(pstate))
        state.ws_tasks[pstate.printer_id] = t


async def _stop_ws_task_if_running(printer_id: str) -> None:
    async with state.lock:
        task = state.ws_tasks.get(printer_id)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    async with state.lock:
        # Clean up if still present
        task2 = state.ws_tasks.get(printer_id)
        if task2 is task:
            state.ws_tasks.pop(printer_id, None)


async def printers_discovery_loop() -> None:
    """Fetch list of printers periodically and reconcile running WS tasks."""
    # Initial seeding for backward-compat: ensure env PRINTER_ID exists
    if PRINTER_ID:
        async with state.lock:
            if PRINTER_ID not in state.printers:
                state.printers[PRINTER_ID] = PrinterState(PRINTER_ID)
                state.printers[PRINTER_ID].is_online = True
                state.printers[PRINTER_ID].always_connect = True

    refresh = max(60, PRINTERS_REFRESH_SECONDS)
    while state.run_event.is_set():
        try:
            # Start/ensure WS tasks for known printers first, so a slow discovery fetch cannot block WS
            async with state.lock:
                printers_snapshot = list(state.printers.values())
            for p in printers_snapshot:
                if p.is_online or p.always_connect:
                    await _start_ws_task_if_needed(p)
                else:
                    await _stop_ws_task_if_running(p.printer_id)

            session = await ensure_http_session()
            fetched: List[Dict[str, Any]] = []
            try:
                # Use a short per-request timeout so we don't stall the loop
                req_timeout = aiohttp.ClientTimeout(total=5, connect=3)
                async with session.get(PRINTERS_API_URL, timeout=req_timeout) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if isinstance(data, list):
                            fetched = data
                        else:
                            logger.warning(
                                "Unexpected printers API payload type: %s", type(data)
                            )
                    else:
                        body = await resp.text()
                        logger.warning(
                            "Printers API error %s: %s", resp.status, body[:300]
                        )
            except Exception as e:
                logger.warning("Failed to fetch printers list: %s", e)

            # Build a set of seen printer ids
            seen: Set[str] = set()
            for item in fetched:
                try:
                    pid = str(item.get("name") or item.get("printer_id") or "").strip()
                    if not pid:
                        continue
                    model = item.get("model")
                    is_online = bool(item.get("is_online", False))
                except Exception:
                    continue
                seen.add(pid)

                async with state.lock:
                    p = state.printers.get(pid)
                    if p is None:
                        p = PrinterState(pid, model=str(model) if model else None)
                        state.printers[pid] = p
                    else:
                        if model and not p.model:
                            p.model = str(model)
                    p.is_online = is_online
                    BAMBU_PRINTER_UP.labels(printer_id=pid).set(1 if is_online else 0)

            # Mark missing printers offline (but keep them for history)
            async with state.lock:
                current_ids = list(state.printers.keys())
            for pid in current_ids:
                if fetched and pid not in seen:
                    async with state.lock:
                        p = state.printers.get(pid)
                        if p is not None:
                            p.is_online = False
                            BAMBU_PRINTER_UP.labels(printer_id=pid).set(0)

            # Reconcile WS tasks
            async with state.lock:
                printers_snapshot = list(state.printers.values())
            for p in printers_snapshot:
                if p.is_online or p.always_connect:
                    await _start_ws_task_if_needed(p)
                else:
                    await _stop_ws_task_if_running(p.printer_id)

        except Exception as e:
            logger.exception("Discovery loop error: %s", e)

        await asyncio.sleep(refresh)


async def http_status_seed_loop() -> None:
    """Periodically seed latest_status via HTTP API to populate metrics early.

    This complements the WebSocket stream and is controlled by
    HTTP_STATUS_SEED_ON_STARTUP and HTTP_STATUS_SEED_INTERVAL_SECONDS.
    """
    if not HTTP_STATUS_SEED_ON_STARTUP:
        logger.info("HTTP status seeding is DISABLED")
        return
    interval = max(2, HTTP_STATUS_SEED_INTERVAL_SECONDS)
    logger.info("HTTP status seeding loop starting with %ds interval", interval)

    # Do an immediate first seed on startup, then continue on interval
    first_run = True
    while state.run_event.is_set():
        if not first_run:
            await asyncio.sleep(interval)
        first_run = False
        try:
            session = await ensure_http_session()
            async with state.lock:
                ids = list(state.printers.keys())
            logger.debug("HTTP status seeding for %d printers: %s", len(ids), ids)
            for pid in ids:
                url = HTTP_STATUS_URL_TEMPLATE.format(printer_id=pid)
                logger.info("HTTP status seed for %s: requesting %s", pid, url)
                try:
                    req_timeout = aiohttp.ClientTimeout(total=5, connect=3)
                    async with session.get(url, timeout=req_timeout) as resp:
                        if resp.status != 200:
                            logger.debug(
                                "HTTP status seed for %s: HTTP %d", pid, resp.status
                            )
                            continue
                        data = await resp.json()
                        if not isinstance(data, dict):
                            continue
                        status = data.get("status")
                        if not isinstance(status, dict):
                            continue
                        ts = data.get("received_at")
                        parsed_ts = parse_iso8601(ts) if isinstance(ts, str) else None
                        if parsed_ts is None:
                            parsed_ts = now_utc()
                        p = state.printers.get(pid)
                        if p is None:
                            logger.warning(
                                "HTTP status seed for %s: printer not found in state!",
                                pid,
                            )
                            continue
                        async with p.lock:
                            p.latest_status = status
                            p.latest_status_ts = parsed_ts
                            try:
                                # Update metrics directly here
                                BAMBU_LAST_STATUS_TIMESTAMP.labels(printer_id=pid).set(
                                    parsed_ts.timestamp()
                                )
                                BAMBU_PRINT_ACTIVE.labels(printer_id=pid).set(
                                    1 if is_print_active(status) else 0
                                )

                                percent = _maybe_parse_number(status.get("mc_percent"))
                                if percent is not None:
                                    BAMBU_PROGRESS_PERCENT.labels(printer_id=pid).set(
                                        percent
                                    )

                                rem_time = _maybe_parse_number(
                                    status.get("mc_remaining_time")
                                )
                                if rem_time is not None:
                                    BAMBU_REMAINING_MINUTES.labels(printer_id=pid).set(
                                        rem_time
                                    )

                                nozzle_temp = _maybe_parse_number(
                                    status.get("nozzle_temper")
                                )
                                if nozzle_temp is not None:
                                    BAMBU_NOZZLE_TEMP.labels(printer_id=pid).set(
                                        nozzle_temp
                                    )

                                nozzle_target = _maybe_parse_number(
                                    status.get("nozzle_target_temper")
                                )
                                if nozzle_target is not None:
                                    BAMBU_NOZZLE_TARGET_TEMP.labels(printer_id=pid).set(
                                        nozzle_target
                                    )

                                bed_temp = _maybe_parse_number(status.get("bed_temper"))
                                if bed_temp is not None:
                                    BAMBU_BED_TEMP.labels(printer_id=pid).set(bed_temp)

                                bed_target = _maybe_parse_number(
                                    status.get("bed_target_temper")
                                )
                                if bed_target is not None:
                                    BAMBU_BED_TARGET_TEMP.labels(printer_id=pid).set(
                                        bed_target
                                    )

                                chamber_temp = _maybe_parse_number(
                                    status.get("chamber_temper")
                                )
                                if chamber_temp is not None:
                                    BAMBU_CHAMBER_TEMP.labels(printer_id=pid).set(
                                        chamber_temp
                                    )

                                speed = _maybe_parse_number(status.get("spd_mag"))
                                if speed is not None:
                                    BAMBU_PRINT_SPEED.labels(printer_id=pid).set(speed)

                                fan_cool = _maybe_parse_number(
                                    status.get("cooling_fan_speed")
                                )
                                if fan_cool is not None:
                                    BAMBU_COOLING_FAN_SPEED.labels(printer_id=pid).set(
                                        fan_cool
                                    )

                                fan_hb = _maybe_parse_number(
                                    status.get("heatbreak_fan_speed")
                                )
                                if fan_hb is not None:
                                    BAMBU_HEATBREAK_FAN_SPEED.labels(
                                        printer_id=pid
                                    ).set(fan_hb)

                                error_code = _maybe_parse_number(
                                    status.get("print_error")
                                )
                                if error_code is not None:
                                    BAMBU_PRINT_ERROR.labels(printer_id=pid).set(
                                        error_code
                                    )

                                wifi = _maybe_parse_number(status.get("wifi_signal"))
                                if wifi is not None:
                                    BAMBU_WIFI_SIGNAL.labels(printer_id=pid).set(wifi)

                            except Exception as e:
                                logger.warning(
                                    "[%s] Failed to update metrics from HTTP seed: %s",
                                    pid,
                                    e,
                                )
                        logger.info(
                            "HTTP status seed for %s: SUCCESS - updated printer_obj=%s with %d status fields at %s",
                            pid,
                            id(p),  # Memory address to match with metrics collection
                            len(status),
                            parsed_ts.isoformat() if parsed_ts else "unknown",
                        )
                        # Log some key values for verification
                        logger.info(
                            "HTTP status seed for %s: mc_percent=%s, gcode_state=%s, nozzle_temp=%s",
                            pid,
                            status.get("mc_percent"),
                            status.get("gcode_state"),
                            status.get("nozzle_temper"),
                        )
                except Exception as e:
                    logger.warning("HTTP status seed for %s failed: %s", pid, e)
        except Exception as e:
            logger.debug("HTTP status seed loop error: %s", e)


def ensure_images_dir(
    subdir: Optional[str] = None, pstate: Optional[PrinterState] = None
) -> Path:
    # Base path for images. For multi-printer, nest under printer_id.
    base = state.images_base_path
    if pstate is not None:
        base = base / pstate.printer_id
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


def embed_print_metadata(image_bytes: bytes, status: Optional[Dict[str, Any]]) -> bytes:
    """Embed print metadata into JPEG EXIF data.

    Args:
        image_bytes: Original JPEG image bytes
        status: Current printer status containing print information

    Returns:
        Modified JPEG bytes with embedded metadata
    """
    if not status:
        return image_bytes

    try:
        # Open image from bytes
        img = Image.open(io.BytesIO(image_bytes))

        # Extract metadata from status
        print_name = safe_get(status, "subtask_name") or "Unknown"
        progress = safe_get(status, "mc_percent")
        current_layer = safe_get(status, "layer_num")
        total_layers = safe_get(status, "total_layer_num")
        gcode_state = safe_get(status, "gcode_state") or "Unknown"
        remaining_time = safe_get(status, "mc_remaining_time")

        # Create metadata strings
        progress_str = f"{progress}%" if progress is not None else "Unknown"
        layer_str = (
            f"{current_layer}/{total_layers}"
            if current_layer is not None and total_layers is not None
            else f"{current_layer}"
            if current_layer is not None
            else "Unknown"
        )

        # Build comprehensive description
        description_parts = [
            f"Print: {print_name}",
            f"Progress: {progress_str}",
            f"Layer: {layer_str}",
            f"State: {gcode_state}",
        ]

        if remaining_time is not None:
            eta_str = format_time_remaining(remaining_time)
            description_parts.append(f"ETA: {eta_str}")

        description = " | ".join(description_parts)

        # Get existing EXIF data or create new
        exif_dict = img.getexif()

        # Add custom metadata to EXIF
        # Use standard EXIF tags where possible
        exif_dict[0x010E] = description  # ImageDescription
        exif_dict[0x010F] = "Bambu Notify"  # Make
        exif_dict[0x0110] = "3D Printer Monitor"  # Model
        exif_dict[0x0131] = "Bambu Notify v1.4.0"  # Software
        exif_dict[0x9003] = datetime.now().strftime(
            "%Y:%m:%d %H:%M:%S"
        )  # DateTimeOriginal

        # Add custom tags for specific print data
        exif_dict[0x9286] = f"PrintName: {print_name}"  # UserComment (print name)

        # Save image with new EXIF data
        output = io.BytesIO()
        img.save(output, format="JPEG", exif=exif_dict, quality=95)
        return output.getvalue()

    except Exception as e:
        logger.warning("Failed to embed metadata in image: %s", e)
        # Return original image if metadata embedding fails
        return image_bytes


async def save_image_if_active_job(pstate: PrinterState, image_bytes: bytes) -> None:
    if not image_bytes:
        return
    async with pstate.lock:
        status = pstate.latest_status
        ts = pstate.latest_image_ts or now_utc()
        seq = pstate.image_seq
    if not is_print_active(status):
        return
    job = safe_get(status, "subtask_name")
    target_dir = ensure_images_dir(job or None, pstate)
    fname = build_image_filename(job, ts, seq)
    try:
        # Embed print metadata into the image
        enhanced_image_bytes = embed_print_metadata(image_bytes, status)

        # Write atomically
        tmp_path = target_dir / (fname + ".tmp")
        final_path = target_dir / fname
        with open(tmp_path, "wb") as f:
            f.write(enhanced_image_bytes)
        os.replace(tmp_path, final_path)

        # Log successful metadata embedding
        if enhanced_image_bytes != image_bytes:
            logger.info(
                "Saved image %s with embedded metadata (print: %s, progress: %s%%, layer: %s)",
                fname,
                safe_get(status, "subtask_name"),
                safe_get(status, "mc_percent"),
                safe_get(status, "layer_num"),
            )
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


async def telegram_send_video_file(file_path: Path, caption: str = "") -> None:
    if not telegram_is_enabled() or not file_path.exists():
        return
    try:
        session = await ensure_http_session()
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendVideo"
        form = aiohttp.FormData()
        form.add_field("chat_id", TELEGRAM_CHAT_ID)
        if TELEGRAM_THREAD_ID:
            form.add_field("message_thread_id", str(int(TELEGRAM_THREAD_ID)))
        if caption:
            form.add_field("caption", caption)
        form.add_field("supports_streaming", "true")
        with open(file_path, "rb") as f:
            form.add_field(
                "video",
                f,
                filename=file_path.name,
                content_type="video/mp4",
            )
            async with session.post(url, data=form) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning("Telegram video failed: %s %s", resp.status, body)
    except Exception as e:
        logger.exception("Error sending Telegram video: %s", e)


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
# Timelapse utilities
# -----------------------------------------------------------------------------
def _parse_ts_from_filename(name: str) -> Optional[datetime]:
    # Expected: YYYYMMDDThhmmss_XXXXXX_job.jpg
    try:
        base = name.split("_")[0]
        return datetime.strptime(base, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _collect_job_images(
    pstate: PrinterState, job_name: str, started_at: Optional[datetime]
) -> List[Path]:
    job_dir = ensure_images_dir(job_name or None, pstate)
    if not job_dir.exists() or not job_dir.is_dir():
        return []
    files = [p for p in job_dir.iterdir() if p.is_file() and p.suffix.lower() == ".jpg"]
    if not files:
        return []
    # Filter by started_at if available
    if started_at is not None:
        filtered: List[Tuple[Path, float]] = []
        for p in files:
            ts = _parse_ts_from_filename(p.name)
            if ts is None:
                mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
                if mtime >= started_at:
                    filtered.append((p, p.stat().st_mtime))
            else:
                if ts >= started_at:
                    filtered.append((p, ts.timestamp()))
        files_sorted = [fp for fp, _ in sorted(filtered, key=lambda x: x[1])]
    else:
        files_sorted = sorted(files, key=lambda p: p.name)
    return files_sorted


async def _run_ffmpeg_build_timelapse(
    frames_dir: Path,
    out_path: Path,
    speedup_factor: int = 30,
) -> bool:
    """Build timelapse using glob pattern and frame selection for speedup."""
    cmd: List[str] = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-framerate",
        "30",
        "-pattern_type",
        "glob",
        "-i",
        str(frames_dir / "*.jpg"),
        "-vf",
        f"select='not(mod(n,{speedup_factor}))',setpts=N/30/TB",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-r",
        "30",
        str(out_path),
    ]

    try:
        proc = await asyncio.create_subprocess_exec(*cmd)
        await proc.communicate()
        return proc.returncode == 0 and out_path.exists()
    except Exception as e:
        logger.exception("ffmpeg execution failed: %s", e)
        return False


async def build_timelapse(
    pstate: PrinterState, job_name: Optional[str], started_at: Optional[datetime]
) -> Optional[Path]:
    if not job_name:
        return None
    if shutil.which("ffmpeg") is None:
        logger.warning("ffmpeg not found; skipping timelapse build.")
        return None
    images = _collect_job_images(pstate, job_name, started_at)
    if len(images) < 2:
        logger.info(
            "Not enough frames for timelapse (%d) for job %s", len(images), job_name
        )
        return None

    job_dir = ensure_images_dir(job_name, pstate)
    out_name = f"timelapse_30x_{now_utc().strftime('%Y%m%dT%H%M%S')}.mp4"
    out_path = job_dir / out_name

    # Build timelapse with 30x speedup directly from the job directory
    ok = await _run_ffmpeg_build_timelapse(job_dir, out_path, speedup_factor=30)

    if not ok:
        logger.warning("Failed to create timelapse for job %s", job_name)
        return None

    # Check if file was created and is within size limit
    try:
        size = out_path.stat().st_size
        if size > TIMELAPSE_MAX_BYTES:
            logger.warning(
                "Timelapse for job %s is too large (%d bytes > %d limit), but keeping it",
                job_name,
                size,
                TIMELAPSE_MAX_BYTES,
            )
        logger.info(
            "Created timelapse for job %s: %s (%d bytes)", job_name, out_path.name, size
        )
        return out_path
    except Exception as e:
        logger.warning(
            "Failed to check timelapse file size for job %s: %s", job_name, e
        )
        return out_path if out_path.exists() else None


async def build_and_send_timelapse_if_available(
    pstate: PrinterState, status: Dict[str, Any]
) -> None:
    job = safe_get(status, "subtask_name")
    async with pstate.lock:
        started_at = pstate.current_job_started_at
        key = f"{job}@{started_at.isoformat()}" if job and started_at else None
        if key and key in pstate.completed_timelapse_jobs:
            return
    try:
        out_path = await build_timelapse(pstate, job, started_at)
        if out_path and out_path.exists():
            caption = f"🎞️ Timelapse\n{summarize_status_for_notification(status)}\nTime: {now_utc().isoformat()}"
            await telegram_send_video_file(out_path, caption)
            async with pstate.lock:
                if key:
                    pstate.completed_timelapse_jobs.add(key)
    except Exception as e:
        logger.exception("Timelapse build/send failed: %s", e)


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
            "If the image is blurred or out-of-focus due to the camera, do NOT treat that as a print failure or defect. "
            "Only flag defects that are clearly visible on the printed part itself. "
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


async def ai_check_and_alert_if_needed(pstate: PrinterState) -> None:
    """Run defect analysis on the latest frame on a per-job hourly cadence."""
    async with pstate.lock:
        status = pstate.latest_status
        img = pstate.latest_image_bytes
        job = safe_get(status, "subtask_name")
        last_checked = pstate.last_ai_check_at
        last_job = pstate.last_ai_job

    if not ai_is_enabled() or not status or not img:
        return
    if not is_print_active(status):
        return

    if job != last_job:
        async with pstate.lock:
            pstate.last_ai_job = job
            pstate.last_ai_check_at = None
        last_checked = None

    due = (last_checked is None) or (
        (now_utc() - last_checked) >= timedelta(seconds=AI_CHECK_INTERVAL_SECONDS)
    )
    if not due:
        return

    result = await analyze_image_with_ai(img)
    async with pstate.lock:
        pstate.last_ai_check_at = now_utc()

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
        # Iterate printers -> jobs
        base_root = state.images_base_path
        base_root.mkdir(parents=True, exist_ok=True)
        cutoff = now_utc() - timedelta(days=IMAGE_RETENTION_DAYS)
        for printer_dir in base_root.iterdir():
            try:
                if not printer_dir.is_dir():
                    continue
                for sub in printer_dir.iterdir():
                    try:
                        if sub.is_file():
                            mtime = datetime.fromtimestamp(
                                sub.stat().st_mtime, tz=timezone.utc
                            )
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
                # Remove empty printer folders
                try:
                    next(printer_dir.iterdir())
                except StopIteration:
                    printer_dir.rmdir()
            except Exception as e:
                logger.debug("Retention scan skip %s: %s", printer_dir, e)
    except Exception as e:
        logger.debug("Retention cleanup error: %s", e)


async def send_hourly_photo_if_needed(pstate: PrinterState) -> None:
    """Send a photo if active job hasn't had one in PHOTO_INTERVAL_SECONDS."""
    async with pstate.lock:
        status = pstate.latest_status
        img = pstate.latest_image_bytes
        job = safe_get(status, "subtask_name")
        last_sent = pstate.last_photo_sent_at

    if not status or not img:
        return
    if not is_print_active(status):
        return

    # Reset per-job timer
    if job != pstate.last_photo_job:
        async with pstate.lock:
            pstate.last_photo_job = job
            pstate.last_photo_sent_at = None
        last_sent = None

    due = (last_sent is None) or (
        (now_utc() - last_sent) >= timedelta(seconds=PHOTO_INTERVAL_SECONDS)
    )
    if not due:
        return

    caption = f"🖼️ Hourly snapshot\n{summarize_status_for_notification(status)}\nTime: {now_utc().isoformat()}"
    await telegram_send_photo(img, caption)
    async with pstate.lock:
        pstate.last_photo_sent_at = now_utc()


# -----------------------------------------------------------------------------
# WebSocket watchdog (no jpeg_image -> reconnect)
# -----------------------------------------------------------------------------
async def image_watchdog(
    pstate: PrinterState, ws: aiohttp.ClientWebSocketResponse, connected_at: datetime
) -> None:
    while state.run_event.is_set() and not ws.closed:
        await asyncio.sleep(max(1, WATCHDOG_TICK_SECONDS))
        async with pstate.lock:
            last_img_ts = pstate.latest_image_ts
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
# WebSocket watchdog (no printer_status -> mark IDLE)
# -----------------------------------------------------------------------------
async def status_watchdog(
    pstate: PrinterState, ws: aiohttp.ClientWebSocketResponse, connected_at: datetime
) -> None:
    while state.run_event.is_set() and not ws.closed:
        await asyncio.sleep(max(1, WATCHDOG_TICK_SECONDS))
        async with pstate.lock:
            last_status_ts = pstate.latest_status_ts
            current_status = pstate.latest_status
        base_ts = last_status_ts or connected_at
        if (now_utc() - base_ts).total_seconds() > STATUS_TIMEOUT_SECONDS:
            current_state = (safe_get(current_status, "gcode_state") or "").upper()
            if current_state != "IDLE":
                logger.warning(
                    "[%s] No printer_status in %ds; marking status IDLE.",
                    pstate.printer_id,
                    STATUS_TIMEOUT_SECONDS,
                )
                # Preserve last known job name if any, so completion/timelapse can fire
                last_job = (
                    safe_get(current_status, "subtask_name")
                    or pstate.current_job_name
                    or safe_get(pstate.prev_status, "subtask_name")
                )
                idle_status: Dict[str, Any] = {"gcode_state": "IDLE"}
                if last_job:
                    idle_status["subtask_name"] = last_job
                async with pstate.lock:
                    pstate.latest_status = idle_status
                    pstate.latest_status_ts = now_utc()
                try:
                    await maybe_notify_on_update(pstate, idle_status)
                except Exception as e:
                    logger.exception(
                        "[%s] Notify error (IDLE): %s", pstate.printer_id, e
                    )


# -----------------------------------------------------------------------------
# Periodic forced reconnect guard
# -----------------------------------------------------------------------------
async def periodic_reconnect_guard(
    pstate: PrinterState, ws: aiohttp.ClientWebSocketResponse
) -> None:
    if FORCED_RECONNECT_SECONDS <= 0:
        return
    try:
        await asyncio.sleep(max(1, FORCED_RECONNECT_SECONDS))
        if not ws.closed:
            logger.info(
                "[%s] Forcing periodic reconnect after %ds.",
                pstate.printer_id,
                FORCED_RECONNECT_SECONDS,
            )
            try:
                await ws.close(
                    code=aiohttp.WSCloseCode.OK, message=b"periodic-reconnect"
                )
            except Exception:
                pass
    except asyncio.CancelledError:
        pass


# -----------------------------------------------------------------------------
# WebSocket loop with auto-reconnect
# -----------------------------------------------------------------------------
async def websocket_loop_for_printer(pstate: PrinterState) -> None:
    backoff = RECONNECT_MIN_SECONDS
    while state.run_event.is_set():
        try:
            session = await ensure_http_session()
            ws_url = WS_URL_TEMPLATE.format(printer_id=pstate.printer_id)
            logger.info("[%s] Connecting to WebSocket: %s", pstate.printer_id, ws_url)
            async with session.ws_connect(ws_url, heartbeat=30) as ws:
                logger.info("[%s] WebSocket connected.", pstate.printer_id)
                backoff = RECONNECT_MIN_SECONDS

                connected_at = now_utc()
                watchdog_task = asyncio.create_task(
                    image_watchdog(pstate, ws, connected_at)
                )
                status_task = asyncio.create_task(
                    status_watchdog(pstate, ws, connected_at)
                )
                periodic_task = asyncio.create_task(
                    periodic_reconnect_guard(pstate, ws)
                )

                try:
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                payload = json.loads(msg.data)
                            except json.JSONDecodeError:
                                logger.warning(
                                    "[%s] Skipping non-JSON frame of length %d",
                                    pstate.printer_id,
                                    len(msg.data),
                                )
                                continue

                            msg_type = payload.get("type")
                            if msg_type == "printer_status":
                                data = payload.get("data")
                                if isinstance(data, dict):
                                    async with pstate.lock:
                                        pstate.latest_status = data
                                        pstate.latest_status_ts = now_utc()
                                    try:
                                        await maybe_notify_on_update(pstate, data)
                                    except Exception as e:
                                        logger.exception(
                                            "[%s] Notify error: %s",
                                            pstate.printer_id,
                                            e,
                                        )

                            elif msg_type == "jpeg_image":
                                b64 = payload.get("image")
                                if isinstance(b64, str):
                                    try:
                                        decoded = base64.b64decode(b64, validate=False)
                                        now = now_utc()
                                        async with pstate.lock:
                                            pstate.latest_image_bytes = decoded
                                            pstate.latest_image_ts = now
                                            pstate.image_seq += 1
                                            seq = pstate.image_seq
                                        try:
                                            BAMBU_LAST_IMAGE_TIMESTAMP.labels(
                                                printer_id=pstate.printer_id
                                            ).set(now.timestamp())
                                            BAMBU_IMAGE_SEQ.labels(
                                                printer_id=pstate.printer_id
                                            ).set(seq)
                                        except Exception as e:
                                            logger.warning(
                                                "[%s] Failed to update image metrics: %s",
                                                pstate.printer_id,
                                                e,
                                            )
                                        # Persist only if a print job is active
                                        try:
                                            await save_image_if_active_job(
                                                pstate, decoded
                                            )
                                        except Exception as e:
                                            logger.debug(
                                                "[%s] Image save skipped/failed: %s",
                                                pstate.printer_id,
                                                e,
                                            )
                                    except Exception:
                                        logger.warning(
                                            "[%s] Failed to decode JPEG image (base64).",
                                            pstate.printer_id,
                                        )
                            else:
                                logger.debug(
                                    "[%s] Unknown message type: %s",
                                    pstate.printer_id,
                                    msg_type,
                                )

                        elif msg.type == aiohttp.WSMsgType.BINARY:
                            logger.debug(
                                "[%s] Received binary WS frame (%d bytes).",
                                pstate.printer_id,
                                len(msg.data),
                            )

                        elif msg.type in (
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.CLOSING,
                            aiohttp.WSMsgType.CLOSED,
                        ):
                            logger.warning(
                                "[%s] WebSocket closed by server.", pstate.printer_id
                            )
                            break

                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            logger.error(
                                "[%s] WebSocket error: %s",
                                pstate.printer_id,
                                ws.exception(),
                            )
                            break
                finally:
                    watchdog_task.cancel()
                    try:
                        await watchdog_task
                    except asyncio.CancelledError:
                        pass
                    status_task.cancel()
                    try:
                        await status_task
                    except asyncio.CancelledError:
                        pass
                    periodic_task.cancel()
                    try:
                        await periodic_task
                    except asyncio.CancelledError:
                        pass

        except asyncio.CancelledError:
            logger.info("[%s] WebSocket loop cancelled.", pstate.printer_id)
            break
        except Exception as e:
            logger.warning("[%s] WebSocket connection failed: %s", pstate.printer_id, e)

        if not state.run_event.is_set():
            break
        sleep_for = min(backoff, RECONNECT_MAX_SECONDS)
        logger.info(
            "[%s] Reconnecting in %.1f seconds...", pstate.printer_id, sleep_for
        )
        await asyncio.sleep(sleep_for)
        backoff = min(backoff * 2, RECONNECT_MAX_SECONDS)

    logger.info("[%s] WebSocket loop exited.", pstate.printer_id)


# -----------------------------------------------------------------------------
# Prometheus Exporter
# -----------------------------------------------------------------------------
def _unregister_metric_if_exists(
    metric_name: str, registry: CollectorRegistry = REGISTRY
):
    if metric_name in registry._names_to_collectors:
        registry.unregister(registry._names_to_collectors[metric_name])


_unregister_metric_if_exists("bambu_printer_up")
BAMBU_PRINTER_UP = Gauge(
    "bambu_printer_up",
    "Printer online status from discovery (1=online,0=offline)",
    ["printer_id"],
)
_unregister_metric_if_exists("bambu_printer_is_active")
BAMBU_PRINT_ACTIVE = Gauge(
    "bambu_printer_is_active",
    "Whether a print appears active based on status",
    ["printer_id"],
)
_unregister_metric_if_exists("bambu_printer_last_status_timestamp_seconds")
BAMBU_LAST_STATUS_TIMESTAMP = Gauge(
    "bambu_printer_last_status_timestamp_seconds",
    "Unix timestamp of the last received status for this printer",
    ["printer_id"],
)
_unregister_metric_if_exists("bambu_printer_last_image_timestamp_seconds")
BAMBU_LAST_IMAGE_TIMESTAMP = Gauge(
    "bambu_printer_last_image_timestamp_seconds",
    "Unix timestamp of the last received image for this printer",
    ["printer_id"],
)
_unregister_metric_if_exists("bambu_printer_image_seq")
BAMBU_IMAGE_SEQ = Gauge(
    "bambu_printer_image_seq",
    "Monotonic counter of frames observed in the current process",
    ["printer_id"],
)
_unregister_metric_if_exists("bambu_progress_percent")
BAMBU_PROGRESS_PERCENT = Gauge(
    "bambu_progress_percent",
    "Current reported print progress percent (mc_percent)",
    ["printer_id"],
)
_unregister_metric_if_exists("bambu_remaining_time_minutes")
BAMBU_REMAINING_MINUTES = Gauge(
    "bambu_remaining_time_minutes",
    "Reported remaining time in minutes (mc_remaining_time)",
    ["printer_id"],
)
_unregister_metric_if_exists("bambu_layer_number")
BAMBU_LAYER = Gauge("bambu_layer_number", "Current layer number", ["printer_id"])
_unregister_metric_if_exists("bambu_total_layers")
BAMBU_TOTAL_LAYERS = Gauge(
    "bambu_total_layers", "Total layers in the print", ["printer_id"]
)
_unregister_metric_if_exists("bambu_nozzle_temperature_celsius")
BAMBU_NOZZLE_TEMP = Gauge(
    "bambu_nozzle_temperature_celsius",
    "Current nozzle temperature in Celsius",
    ["printer_id"],
)
_unregister_metric_if_exists("bambu_nozzle_target_temperature_celsius")
BAMBU_NOZZLE_TARGET_TEMP = Gauge(
    "bambu_nozzle_target_temperature_celsius",
    "Target nozzle temperature in Celsius",
    ["printer_id"],
)
_unregister_metric_if_exists("bambu_bed_temperature_celsius")
BAMBU_BED_TEMP = Gauge(
    "bambu_bed_temperature_celsius",
    "Current bed temperature in Celsius",
    ["printer_id"],
)
_unregister_metric_if_exists("bambu_bed_target_temperature_celsius")
BAMBU_BED_TARGET_TEMP = Gauge(
    "bambu_bed_target_temperature_celsius",
    "Target bed temperature in Celsius",
    ["printer_id"],
)
_unregister_metric_if_exists("bambu_chamber_temperature_celsius")
BAMBU_CHAMBER_TEMP = Gauge(
    "bambu_chamber_temperature_celsius",
    "Current chamber temperature in Celsius",
    ["printer_id"],
)
_unregister_metric_if_exists("bambu_print_speed_percent")
BAMBU_PRINT_SPEED = Gauge(
    "bambu_print_speed_percent", "Print speed percentage", ["printer_id"]
)
_unregister_metric_if_exists("bambu_cooling_fan_speed_percent")
BAMBU_COOLING_FAN_SPEED = Gauge(
    "bambu_cooling_fan_speed_percent", "Cooling fan speed percentage", ["printer_id"]
)
_unregister_metric_if_exists("bambu_heatbreak_fan_speed_rpm")
BAMBU_HEATBREAK_FAN_SPEED = Gauge(
    "bambu_heatbreak_fan_speed_rpm", "Heatbreak fan speed RPM", ["printer_id"]
)
_unregister_metric_if_exists("bambu_print_error_code")
BAMBU_PRINT_ERROR = Gauge(
    "bambu_print_error_code", "Print error code (0=no error)", ["printer_id"]
)
_unregister_metric_if_exists("bambu_wifi_signal_dbm")
BAMBU_WIFI_SIGNAL = Gauge(
    "bambu_wifi_signal_dbm", "WiFi signal strength in dBm", ["printer_id"]
)


def _to_epoch_seconds(dt: Optional[datetime]) -> float:
    if not dt:
        return 0.0
    return dt.timestamp()


def _maybe_parse_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except Exception:
            return None
    if isinstance(value, str):
        s = value.strip()
        # Try exact numeric first
        try:
            return float(s)
        except Exception:
            pass
        # Try to extract a leading numeric (e.g., "-59dBm")
        sign = 1.0
        if s.startswith("-"):
            sign = -1.0
            s = s[1:]
        num = []
        dot_seen = False
        for ch in s:
            if ch.isdigit():
                num.append(ch)
            elif ch == "." and not dot_seen:
                dot_seen = True
                num.append(ch)
            else:
                break
        if num and not (len(num) == 1 and num[0] == "."):
            try:
                return float("".join(num)) * sign
            except Exception:
                return None
    return None


def _walk_status(
    path: str, value: Any, add_number, add_bool, add_string, index: str = ""
) -> None:
    # path should not include a leading dot; represent full path like 'status.nozzle_temper'
    if isinstance(value, dict):
        for k in sorted(value.keys()):
            _walk_status(
                f"{path}.{k}" if path else k,
                value[k],
                add_number,
                add_bool,
                add_string,
                index="",
            )
        return
    if isinstance(value, list):
        for i, item in enumerate(value):
            _walk_status(
                f"{path}[{i}]", item, add_number, add_bool, add_string, index=str(i)
            )
        return

    # Leaf value
    if isinstance(value, bool):
        add_bool(path, 1.0 if value else 0.0, index)
        add_string(path, "true" if value else "false", index)
        return

    num = _maybe_parse_number(value)
    if num is not None:
        add_number(path, num, index)
        # Also expose string form to allow state queries (e.g., RUNNING)
        if isinstance(value, str):
            add_string(path, value, index)
        return

    # Non-numeric string or unknown type → expose as string info
    if isinstance(value, str):
        add_string(path, value, index)
    # Other types (None, etc.) are skipped


class PrinterMetricsCollector:
    def collect(self):  # type: ignore[override]
        # Per-printer gauges
        g_up = GaugeMetricFamily(
            "bambu_printer_up",
            "Printer online status from discovery (1=online,0=offline)",
            labels=["printer_id"],
        )
        g_has_status = GaugeMetricFamily(
            "bambu_printer_has_status",
            "Whether any status has been received for this printer",
            labels=["printer_id"],
        )
        g_has_image = GaugeMetricFamily(
            "bambu_printer_has_image",
            "Whether any image has been received for this printer",
            labels=["printer_id"],
        )
        g_active = GaugeMetricFamily(
            "bambu_printer_is_active",
            "Whether a print appears active based on status",
            labels=["printer_id"],
        )
        g_last_status_ts = GaugeMetricFamily(
            "bambu_printer_last_status_timestamp_seconds",
            "Unix timestamp of the last received status for this printer",
            labels=["printer_id"],
        )
        g_last_image_ts = GaugeMetricFamily(
            "bambu_printer_last_image_timestamp_seconds",
            "Unix timestamp of the last received image for this printer",
            labels=["printer_id"],
        )
        g_image_seq = GaugeMetricFamily(
            "bambu_printer_image_seq",
            "Monotonic counter of frames observed in the current process",
            labels=["printer_id"],
        )
        g_progress = GaugeMetricFamily(
            "bambu_progress_percent",
            "Current reported print progress percent (mc_percent)",
            labels=["printer_id"],
        )
        g_remaining = GaugeMetricFamily(
            "bambu_remaining_time_minutes",
            "Reported remaining time in minutes (mc_remaining_time)",
            labels=["printer_id"],
        )
        g_layer = GaugeMetricFamily(
            "bambu_layer_number",
            "Current layer number (layer_num)",
            labels=["printer_id"],
        )
        g_total_layer = GaugeMetricFamily(
            "bambu_total_layers",
            "Total layer number (total_layer_num)",
            labels=["printer_id"],
        )

        # Temperature metrics
        g_nozzle_temp = GaugeMetricFamily(
            "bambu_nozzle_temperature_celsius",
            "Current nozzle temperature in Celsius",
            labels=["printer_id"],
        )
        g_nozzle_target_temp = GaugeMetricFamily(
            "bambu_nozzle_target_temperature_celsius",
            "Target nozzle temperature in Celsius",
            labels=["printer_id"],
        )
        g_bed_temp = GaugeMetricFamily(
            "bambu_bed_temperature_celsius",
            "Current bed temperature in Celsius",
            labels=["printer_id"],
        )
        g_bed_target_temp = GaugeMetricFamily(
            "bambu_bed_target_temperature_celsius",
            "Target bed temperature in Celsius",
            labels=["printer_id"],
        )
        g_chamber_temp = GaugeMetricFamily(
            "bambu_chamber_temperature_celsius",
            "Current chamber temperature in Celsius",
            labels=["printer_id"],
        )

        # Speed and fan metrics
        g_print_speed = GaugeMetricFamily(
            "bambu_print_speed_percent",
            "Print speed percentage",
            labels=["printer_id"],
        )
        g_cooling_fan_speed = GaugeMetricFamily(
            "bambu_cooling_fan_speed",
            "Cooling fan speed",
            labels=["printer_id"],
        )
        g_heatbreak_fan_speed = GaugeMetricFamily(
            "bambu_heatbreak_fan_speed",
            "Heatbreak fan speed",
            labels=["printer_id"],
        )

        # Print state metrics
        g_print_error = GaugeMetricFamily(
            "bambu_print_error_code",
            "Print error code (0 = no error)",
            labels=["printer_id"],
        )
        g_wifi_signal = GaugeMetricFamily(
            "bambu_wifi_signal_dbm",
            "WiFi signal strength in dBm",
            labels=["printer_id"],
        )

        # Generic flattened families
        g_number = GaugeMetricFamily(
            "bambu_status_number",
            "All numeric values extracted from the latest status. Labels: path=indexed dotted path, index=list index if any.",
            labels=["printer_id", "path", "index"],
        )
        g_bool = GaugeMetricFamily(
            "bambu_status_bool",
            "All boolean values as 0/1 extracted from the latest status.",
            labels=["printer_id", "path", "index"],
        )
        g_string = GaugeMetricFamily(
            "bambu_status_string_info",
            "All string values from the latest status, as a constant 1 metric with the value in a label.",
            labels=["printer_id", "path", "index", "value"],
        )

        # Debug metrics
        g_metrics_extraction_count = GaugeMetricFamily(
            "bambu_metrics_extraction_count",
            "Count of metrics extracted per type for debugging",
            labels=["printer_id", "type"],
        )

        # Snapshot printers with proper data access - since this is sync,
        # we need to avoid the async locks and copy data atomically
        printers_data = []
        try:
            logger.debug(
                "Metrics collection: found %d printers in state", len(state.printers)
            )
            # Get printer references first
            printer_refs = list(state.printers.items())

            for printer_id, p in printer_refs:
                try:
                    # Read all attributes in one atomic operation to minimize race conditions
                    # Since these are just reading object attributes, they should be atomic
                    is_online = p.is_online
                    latest_status = p.latest_status
                    latest_status_ts = p.latest_status_ts
                    latest_image_bytes = p.latest_image_bytes
                    latest_image_ts = p.latest_image_ts
                    image_seq = p.image_seq

                    printer_data = {
                        "printer_id": printer_id,
                        "is_online": is_online,
                        "latest_status": latest_status,
                        "latest_status_ts": latest_status_ts,
                        "latest_image_bytes": latest_image_bytes,
                        "latest_image_ts": latest_image_ts,
                        "image_seq": image_seq,
                    }
                    printers_data.append(printer_data)

                    logger.info(
                        "Metrics collection for %s: printer_obj=%s, has_status=%s (%s), is_online=%s, status_ts=%s",
                        printer_id,
                        id(p),  # Memory address of printer object
                        latest_status is not None,
                        type(latest_status).__name__,  # Type of latest_status
                        is_online,
                        latest_status_ts.isoformat() if latest_status_ts else "None",
                    )
                    if latest_status:
                        logger.info(
                            "Metrics collection for %s: status keys = %s, status sample: mc_percent=%s",
                            printer_id,
                            list(latest_status.keys())[:10],
                            latest_status.get("mc_percent"),
                        )
                    else:
                        logger.warning(
                            "Metrics collection for %s: status is %s (should be dict with data!)",
                            printer_id,
                            latest_status,
                        )
                except Exception as e:
                    logger.warning(
                        f"Failed to capture data for printer {printer_id}: {e}"
                    )
                    continue
        except Exception as e:
            logger.warning("Failed to snapshot printer data for metrics: %s", e)
            printers_data = []

        for printer_data in printers_data:
            pid = printer_data["printer_id"]
            status = printer_data["latest_status"]
            last_status_ts = printer_data["latest_status_ts"]
            has_status = 1.0 if status is not None else 0.0
            has_image = 1.0 if printer_data["latest_image_bytes"] is not None else 0.0
            is_active = 1.0 if is_print_active(status) else 0.0

            g_up.add_metric([pid], 1.0 if printer_data["is_online"] else 0.0)
            g_has_status.add_metric([pid], has_status)
            g_has_image.add_metric([pid], has_image)
            g_active.add_metric([pid], is_active)
            g_last_status_ts.add_metric([pid], _to_epoch_seconds(last_status_ts))
            g_last_image_ts.add_metric(
                [pid], _to_epoch_seconds(printer_data["latest_image_ts"])
            )
            g_image_seq.add_metric([pid], float(printer_data["image_seq"]))

            # Derived convenience metrics
            if status is not None:
                percent = safe_get(status, "mc_percent")
                if percent is not None:
                    num = _maybe_parse_number(percent)
                    if num is not None:
                        g_progress.add_metric([pid], num)
                rem = safe_get(status, "mc_remaining_time")
                if rem is not None:
                    num = _maybe_parse_number(rem)
                    if num is not None:
                        g_remaining.add_metric([pid], num)
                layer_num = safe_get(status, "layer_num")
                if layer_num is not None:
                    num = _maybe_parse_number(layer_num)
                    if num is not None:
                        g_layer.add_metric([pid], num)
                total_layer_num = safe_get(status, "total_layer_num")
                if total_layer_num is not None:
                    num = _maybe_parse_number(total_layer_num)
                    if num is not None:
                        g_total_layer.add_metric([pid], num)

                # Temperature metrics
                nozzle_temp = safe_get(status, "nozzle_temper")
                if nozzle_temp is not None:
                    num = _maybe_parse_number(nozzle_temp)
                    if num is not None:
                        g_nozzle_temp.add_metric([pid], num)

                nozzle_target_temp = safe_get(status, "nozzle_target_temper")
                if nozzle_target_temp is not None:
                    num = _maybe_parse_number(nozzle_target_temp)
                    if num is not None:
                        g_nozzle_target_temp.add_metric([pid], num)

                bed_temp = safe_get(status, "bed_temper")
                if bed_temp is not None:
                    num = _maybe_parse_number(bed_temp)
                    if num is not None:
                        g_bed_temp.add_metric([pid], num)

                bed_target_temp = safe_get(status, "bed_target_temper")
                if bed_target_temp is not None:
                    num = _maybe_parse_number(bed_target_temp)
                    if num is not None:
                        g_bed_target_temp.add_metric([pid], num)

                chamber_temp = safe_get(status, "chamber_temper")
                if chamber_temp is not None:
                    num = _maybe_parse_number(chamber_temp)
                    if num is not None:
                        g_chamber_temp.add_metric([pid], num)

                # Speed and fan metrics
                print_speed = safe_get(status, "spd_mag")
                if print_speed is not None:
                    num = _maybe_parse_number(print_speed)
                    if num is not None:
                        g_print_speed.add_metric([pid], num)

                cooling_fan = safe_get(status, "cooling_fan_speed")
                if cooling_fan is not None:
                    num = _maybe_parse_number(cooling_fan)
                    if num is not None:
                        g_cooling_fan_speed.add_metric([pid], num)

                heatbreak_fan = safe_get(status, "heatbreak_fan_speed")
                if heatbreak_fan is not None:
                    num = _maybe_parse_number(heatbreak_fan)
                    if num is not None:
                        g_heatbreak_fan_speed.add_metric([pid], num)

                # Print state metrics
                print_error = safe_get(status, "print_error")
                if print_error is not None:
                    num = _maybe_parse_number(print_error)
                    if num is not None:
                        g_print_error.add_metric([pid], num)

                wifi_signal = safe_get(status, "wifi_signal")
                if wifi_signal is not None:
                    num = _maybe_parse_number(wifi_signal)
                    if num is not None:
                        g_wifi_signal.add_metric([pid], num)

                # Flatten all status fields
                num_count = 0
                bool_count = 0
                string_count = 0

                def add_num(path: str, val: float, index: str = "") -> None:
                    nonlocal num_count
                    num_count += 1
                    g_number.add_metric([pid, f"status.{path}", index or ""], val)

                def add_b(path: str, val: float, index: str = "") -> None:
                    nonlocal bool_count
                    bool_count += 1
                    g_bool.add_metric([pid, f"status.{path}", index or ""], val)

                def add_str(path: str, sval: str, index: str = "") -> None:
                    nonlocal string_count
                    string_count += 1
                    # Limit very long strings to avoid huge scrapes
                    s = sval if len(sval) <= 200 else (sval[:197] + "...")
                    g_string.add_metric([pid, f"status.{path}", index or "", s], 1.0)

                _walk_status("", status, add_num, add_b, add_str)
                logger.debug(
                    "Metrics extraction for %s: %d numbers, %d bools, %d strings",
                    pid,
                    num_count,
                    bool_count,
                    string_count,
                )

                # Add debug metrics
                g_metrics_extraction_count.add_metric(
                    [pid, "numbers"], float(num_count)
                )
                g_metrics_extraction_count.add_metric([pid, "bools"], float(bool_count))
                g_metrics_extraction_count.add_metric(
                    [pid, "strings"], float(string_count)
                )

        # Yield families
        yield g_up
        yield g_has_status
        yield g_has_image
        yield g_active
        yield g_last_status_ts
        yield g_last_image_ts
        yield g_image_seq
        yield g_progress
        yield g_remaining
        yield g_layer
        yield g_total_layer
        # Temperature metrics
        yield g_nozzle_temp
        yield g_nozzle_target_temp
        yield g_bed_temp
        yield g_bed_target_temp
        yield g_chamber_temp
        # Speed and fan metrics
        yield g_print_speed
        yield g_cooling_fan_speed
        yield g_heatbreak_fan_speed
        # Print state metrics
        yield g_print_error
        yield g_wifi_signal
        # Generic flattened metrics
        yield g_number
        yield g_bool
        yield g_string
        # Debug metrics
        yield g_metrics_extraction_count


# Expose /metrics for Prometheus scrapes
@app.get("/metrics")
async def metrics_endpoint():
    content = generate_latest(REGISTRY)
    return Response(content=content, media_type=CONTENT_TYPE_LATEST)


# -----------------------------------------------------------------------------
# Notification logic
# -----------------------------------------------------------------------------
async def maybe_notify_on_update(
    pstate: PrinterState, new_status: Dict[str, Any]
) -> None:
    pid = pstate.printer_id
    try:
        BAMBU_LAST_STATUS_TIMESTAMP.labels(printer_id=pid).set_to_current_time()
        BAMBU_PRINT_ACTIVE.labels(printer_id=pid).set(
            1 if is_print_active(new_status) else 0
        )

        percent = _maybe_parse_number(new_status.get("mc_percent"))
        if percent is not None:
            BAMBU_PROGRESS_PERCENT.labels(printer_id=pid).set(percent)

        rem_time = _maybe_parse_number(new_status.get("mc_remaining_time"))
        if rem_time is not None:
            BAMBU_REMAINING_MINUTES.labels(printer_id=pid).set(rem_time)

        layer = _maybe_parse_number(new_status.get("layer_num"))
        if layer is not None:
            BAMBU_LAYER.labels(printer_id=pid).set(layer)

        total_layers = _maybe_parse_number(new_status.get("total_layer_num"))
        if total_layers is not None:
            BAMBU_TOTAL_LAYERS.labels(printer_id=pid).set(total_layers)

        nozzle_temp = _maybe_parse_number(new_status.get("nozzle_temper"))
        if nozzle_temp is not None:
            BAMBU_NOZZLE_TEMP.labels(printer_id=pid).set(nozzle_temp)

        nozzle_target = _maybe_parse_number(new_status.get("nozzle_target_temper"))
        if nozzle_target is not None:
            BAMBU_NOZZLE_TARGET_TEMP.labels(printer_id=pid).set(nozzle_target)

        bed_temp = _maybe_parse_number(new_status.get("bed_temper"))
        if bed_temp is not None:
            BAMBU_BED_TEMP.labels(printer_id=pid).set(bed_temp)

        bed_target = _maybe_parse_number(new_status.get("bed_target_temper"))
        if bed_target is not None:
            BAMBU_BED_TARGET_TEMP.labels(printer_id=pid).set(bed_target)

        chamber_temp = _maybe_parse_number(new_status.get("chamber_temper"))
        if chamber_temp is not None:
            BAMBU_CHAMBER_TEMP.labels(printer_id=pid).set(chamber_temp)

        speed = _maybe_parse_number(new_status.get("spd_mag"))
        if speed is not None:
            BAMBU_PRINT_SPEED.labels(printer_id=pid).set(speed)

        fan_cool = _maybe_parse_number(new_status.get("cooling_fan_speed"))
        if fan_cool is not None:
            BAMBU_COOLING_FAN_SPEED.labels(printer_id=pid).set(fan_cool)

        fan_hb = _maybe_parse_number(new_status.get("heatbreak_fan_speed"))
        if fan_hb is not None:
            BAMBU_HEATBREAK_FAN_SPEED.labels(printer_id=pid).set(fan_hb)

        error_code = _maybe_parse_number(new_status.get("print_error"))
        if error_code is not None:
            BAMBU_PRINT_ERROR.labels(printer_id=pid).set(error_code)

        wifi = _maybe_parse_number(new_status.get("wifi_signal"))
        if wifi is not None:
            BAMBU_WIFI_SIGNAL.labels(printer_id=pid).set(wifi)

    except Exception as e:
        logger.warning("[%s] Failed to update metrics from WebSocket: %s", pid, e)
    prev = pstate.prev_status or {}
    new_state = (safe_get(new_status, "gcode_state") or "").upper()
    old_state = (safe_get(prev, "gcode_state") or "").upper()
    new_task = safe_get(new_status, "subtask_name")
    new_percent = safe_get(new_status, "mc_percent")
    print_error = safe_get(new_status, "print_error")
    remaining = safe_get(new_status, "mc_remaining_time")

    # Task change (new file or restarted job)
    if new_task and new_task != pstate.last_notified_task:
        msg = f"🖨️ [{pstate.printer_id}] Print job: {new_task}"
        if new_state:
            msg += f"\nState: {new_state}"
        if isinstance(new_percent, int):
            msg += f"\nProgress: {new_percent}%"
        await telegram_send(msg)
        pstate.last_notified_task = new_task
        pstate.last_notified_percent = None
        pstate.last_photo_job = new_task
        pstate.last_photo_sent_at = None
        # Mark new job session
        pstate.current_job_name = new_task
        pstate.current_job_started_at = now_utc()

    # State change
    if new_state and new_state != pstate.last_notified_state:
        prev_state = pstate.last_notified_state
        pstate.last_notified_state = new_state
        if state_change_requires_notification(prev_state, new_state):
            await telegram_send(
                f"🔄 [{pstate.printer_id}] State changed: {old_state or 'unknown'} → {new_state}\n{summarize_status_for_notification(new_status)}"
            )

            # If job just finished/idle, summary + final photo + timelapse
            if is_finished_state(new_state):
                await telegram_send(
                    f"✅ [{pstate.printer_id}] Print completed.\n{summarize_status_for_notification(new_status)}"
                )
                img = pstate.latest_image_bytes
                if img:
                    caption = f"🏁 Final photo\n{summarize_status_for_notification(new_status)}\nTime: {now_utc().isoformat()}"
                    await telegram_send_photo(img, caption)
                pstate.last_photo_sent_at = None
                # Spawn timelapse build in background to avoid blocking
                try:
                    asyncio.create_task(
                        build_and_send_timelapse_if_available(pstate, new_status)
                    )
                except Exception:
                    logger.debug("Failed to schedule timelapse task")

    # Error
    try:
        err_int = int(print_error) if print_error is not None else 0
    except Exception:
        err_int = 0
    if err_int:
        await telegram_send(
            f"❗ [{pstate.printer_id}] Printer reported error code: {err_int}\n{summarize_status_for_notification(new_status)}"
        )

    # Progress step notification
    if isinstance(new_percent, int):
        threshold = pstate.last_notified_percent
        should_notify = False
        current_step = (int(new_percent) // PROGRESS_STEP) * PROGRESS_STEP
        if threshold is None:
            if new_percent == 0 or current_step > 0:
                should_notify = True
        elif new_percent >= threshold + PROGRESS_STEP:
            should_notify = True

        if should_notify:
            msg = f"⏳ [{pstate.printer_id}] Progress: {current_step if current_step > 0 else int(new_percent)}%"
            if remaining is not None:
                msg += f" • ETA {format_time_remaining(remaining)}"
            layer = safe_get(new_status, "layer_num")
            total_layer = safe_get(new_status, "total_layer_num")
            if layer is not None and total_layer not in (None, 0, "0"):
                msg += f" • Layer {layer}/{total_layer}"
            await telegram_send(msg)
            pstate.last_notified_percent = (
                current_step if current_step > 0 else int(new_percent)
            )

    pstate.prev_status = new_status.copy()

def state_change_requires_notification(prev_state: Optional[str], new_state: str) -> bool:
    return not (prev_state in IGNORE_TRANSITIONS and new_state in IGNORE_TRANSITIONS)

# -----------------------------------------------------------------------------
# Background hourly photo loop
# -----------------------------------------------------------------------------
async def photo_loop() -> None:
    """Lightweight ticker to evaluate hourly photo sending and AI checks per printer."""
    tick_seconds = max(
        20, min(120, PHOTO_INTERVAL_SECONDS // 90 or 30)
    )  # 20–120s ticks
    while state.run_event.is_set():
        try:
            # Iterate over known printers
            async with state.lock:
                printers_list = list(state.printers.values())
            for p in printers_list:
                if telegram_is_enabled():
                    await send_hourly_photo_if_needed(p)
                # Run AI check on a similar lightweight cadence
                if ai_is_enabled():
                    await ai_check_and_alert_if_needed(p)
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
@router.get("/", response_class=PlainTextResponse)
async def root():
    return "OK"


@router.get("/debug/state")
async def debug_state():
    """Debug endpoint to see current printer state"""
    async with state.lock:
        debug_info = {}
        for pid, p in state.printers.items():
            debug_info[pid] = {
                "is_online": p.is_online,
                "has_status": p.latest_status is not None,
                "status_timestamp": p.latest_status_ts.isoformat()
                if p.latest_status_ts
                else None,
                "has_image": p.latest_image_bytes is not None,
                "image_seq": p.image_seq,
                "status_fields_count": len(p.latest_status) if p.latest_status else 0,
                "sample_status_fields": list(p.latest_status.keys())[:10]
                if p.latest_status
                else [],
            }
    return JSONResponse(debug_info)


@router.post("/debug/force-seed")
async def force_seed():
    """Force an immediate HTTP status seed for debugging"""
    try:
        session = await ensure_http_session()
        async with state.lock:
            ids = list(state.printers.keys())

        results = {}
        for pid in ids:
            url = HTTP_STATUS_URL_TEMPLATE.format(printer_id=pid)
            try:
                req_timeout = aiohttp.ClientTimeout(total=5, connect=3)
                async with session.get(url, timeout=req_timeout) as resp:
                    if resp.status != 200:
                        results[pid] = {"error": f"HTTP {resp.status}"}
                        continue
                    data = await resp.json()
                    if not isinstance(data, dict):
                        results[pid] = {"error": "Invalid response format"}
                        continue
                    status = data.get("status")
                    if not isinstance(status, dict):
                        results[pid] = {"error": "No status in response"}
                        continue

                    ts = data.get("received_at")
                    parsed_ts = parse_iso8601(ts) if isinstance(ts, str) else None
                    if parsed_ts is None:
                        parsed_ts = now_utc()

                    p = state.printers.get(pid)
                    if p is None:
                        results[pid] = {"error": "Printer not found in state"}
                        continue

                    async with p.lock:
                        p.latest_status = status
                        p.latest_status_ts = parsed_ts

                    results[pid] = {
                        "success": True,
                        "status_fields": len(status),
                        "timestamp": parsed_ts.isoformat(),
                        "sample_data": {
                            "mc_percent": status.get("mc_percent"),
                            "gcode_state": status.get("gcode_state"),
                            "nozzle_temper": status.get("nozzle_temper"),
                        },
                    }
            except Exception as e:
                results[pid] = {"error": str(e)}

        return JSONResponse(results)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/printers")
async def list_printers():
    async with state.lock:
        printers = [
            {
                "printer_id": pid,
                "model": p.model,
                "is_online": p.is_online,
                "has_status": p.latest_status is not None,
            }
            for pid, p in state.printers.items()
        ]
    return JSONResponse(printers)


@router.get("/printer/{any_printer_id}")
async def get_printer_status(any_printer_id: str):
    async with state.lock:
        p = state.printers.get(any_printer_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Unknown printer.")
    async with p.lock:
        if p.latest_status is None:
            raise HTTPException(status_code=404, detail="No status received yet.")
        payload = {
            "printer_id": p.printer_id,
            "received_at": p.latest_status_ts.isoformat()
            if p.latest_status_ts
            else None,
            "status": p.latest_status,
        }
    return JSONResponse(payload)


@router.get("/printer/{any_printer_id}/image")
async def get_printer_image(any_printer_id: str):
    async with state.lock:
        p = state.printers.get(any_printer_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Unknown printer.")
    async with p.lock:
        if p.latest_image_bytes is None:
            raise HTTPException(status_code=404, detail="No image received yet.")
        img = p.latest_image_bytes
        ts = p.latest_image_ts.isoformat() if p.latest_image_ts else None

    headers = {
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "X-Image-Timestamp": ts or "",
    }
    return Response(content=img, media_type="image/jpeg", headers=headers)


# Back-compat endpoints (fixed PRINTER_ID)
@router.get(f"/printer/{PRINTER_ID}")
async def get_printer_status_backcompat():
    return await get_printer_status(PRINTER_ID)


@router.get(f"/printer/{PRINTER_ID}/image")
async def get_printer_image_backcompat():
    return await get_printer_image(PRINTER_ID)


app.include_router(router)

# -----------------------------------------------------------------------------
# Lifecycle
# -----------------------------------------------------------------------------
_ws_task: Optional[asyncio.Task] = None
_photo_task: Optional[asyncio.Task] = None
_http_seed_task: Optional[asyncio.Task] = None


@app.on_event("startup")
async def on_startup():
    logger.info(
        "Starting app. PRINTER_ID=%s | WS_URL=%s | PHOTO_INTERVAL_SECONDS=%s | IMAGE_TIMEOUT_SECONDS=%s",
        PRINTER_ID,
        WS_URL,
        PHOTO_INTERVAL_SECONDS,
        IMAGE_TIMEOUT_SECONDS,
    )
    logger.info(
        "HTTP Status Seeding: ENABLED=%s | URL_TEMPLATE=%s | INTERVAL=%s",
        HTTP_STATUS_SEED_ON_STARTUP,
        HTTP_STATUS_URL_TEMPLATE,
        HTTP_STATUS_SEED_INTERVAL_SECONDS,
    )
    global _ws_task, _photo_task, _http_seed_task
    # In dynamic mode, we start a discovery loop that will spawn per-printer WS tasks.
    _ws_task = asyncio.create_task(printers_discovery_loop())
    _photo_task = asyncio.create_task(photo_loop())
    _http_seed_task = asyncio.create_task(http_status_seed_loop())
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
    global _ws_task, _photo_task, _http_seed_task
    for task in (_ws_task, _photo_task, _http_seed_task):
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    _ws_task = None
    _photo_task = None
    _http_seed_task = None
    # Cancel per-printer WS tasks
    async with state.lock:
        tasks = list(state.ws_tasks.values())
        state.ws_tasks.clear()
    for t in tasks:
        if t and not t.done():
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
    if state.http_session and not state.http_session.closed:
        await state.http_session.close()
    # No explicit close needed for AsyncOpenAI


# -----------------------------------------------------------------------------
# Entrypoint
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=PORT, reload=False)
