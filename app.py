import os
import asyncio
import base64
import json
import logging
import math
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, Set, List, Tuple
from pathlib import Path
import shutil
import tempfile

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
class PrinterState:
    def __init__(self, printer_id: str, model: Optional[str] = None) -> None:
        self.printer_id = printer_id
        self.model = model
        self.is_online: bool = False

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

    refresh = max(60, PRINTERS_REFRESH_SECONDS)
    while state.run_event.is_set():
        try:
            session = await ensure_http_session()
            fetched: List[Dict[str, Any]] = []
            try:
                async with session.get(PRINTERS_API_URL) as resp:
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

            # Mark missing printers offline (but keep them for history)
            async with state.lock:
                current_ids = list(state.printers.keys())
            for pid in current_ids:
                if fetched and pid not in seen:
                    async with state.lock:
                        p = state.printers.get(pid)
                        if p:
                            p.is_online = False

            # Reconcile WS tasks
            async with state.lock:
                printers_snapshot = list(state.printers.values())
            for p in printers_snapshot:
                if p.is_online:
                    await _start_ws_task_if_needed(p)
                else:
                    await _stop_ws_task_if_running(p.printer_id)

        except Exception as e:
            logger.exception("Discovery loop error: %s", e)

        await asyncio.sleep(refresh)


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


async def _run_ffmpeg_build(
    frames_dir: Path,
    frame_fps: int,
    max_width: int,
    crf: int,
    out_path: Path,
    target_bitrate_kbps: Optional[int] = None,
) -> bool:
    vf_parts = [f"scale='if(gt(iw,{max_width}),{max_width},iw)':-2", "format=yuv420p"]
    vf = ",".join(vf_parts)
    cmd: List[str] = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-framerate",
        str(frame_fps),
        "-i",
        str(frames_dir / "%06d.jpg"),
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-crf",
        str(crf),
    ]
    if target_bitrate_kbps is not None:
        # Apply a capped bitrate to enforce smaller sizes
        kbps = max(200, target_bitrate_kbps)
        cmd += ["-b:v", f"{kbps}k", "-maxrate", f"{kbps}k", "-bufsize", f"{kbps * 2}k"]
    cmd.append(str(out_path))

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

    # Compute fps to target desired duration
    frames_count = len(images)
    target_duration = max(5, TIMELAPSE_TARGET_DURATION_SECONDS)
    fps_float = min(TIMELAPSE_FPS_CAP, max(1.0, frames_count / float(target_duration)))
    fps = int(math.ceil(fps_float))

    job_dir = ensure_images_dir(job_name, pstate)
    out_name = f"timelapse_{now_utc().strftime('%Y%m%dT%H%M%S')}.mp4"
    out_path = job_dir / out_name

    # Prepare temp directory with sequential links
    with tempfile.TemporaryDirectory(prefix="frames_") as tmp:
        tmp_dir = Path(tmp)
        for idx, src in enumerate(images, start=1):
            dst = tmp_dir / f"{idx:06d}.jpg"
            try:
                os.symlink(src.resolve(), dst)
            except Exception:
                try:
                    os.link(src.resolve(), dst)
                except Exception:
                    shutil.copy2(src, dst)

        attempts: List[Tuple[int, int, Optional[int]]] = [
            # (max_width, crf, bitrate_kbps)
            (TIMELAPSE_MAX_WIDTH, 28, None),
            (min(TIMELAPSE_MAX_WIDTH, 960), 30, None),
            (min(TIMELAPSE_MAX_WIDTH, 720), 32, None),
        ]
        # Final attempt: enforce bitrate to fit within limit
        # bitrate (kbps) ~= (MAX_BYTES * 8) / duration / 1000 (with 10% safety)
        est_duration = max(1.0, frames_count / float(fps))
        target_kbps = int(((TIMELAPSE_MAX_BYTES * 8.0) / est_duration) / 1000.0 * 0.9)
        attempts.append((min(TIMELAPSE_MAX_WIDTH, 640), 34, max(200, target_kbps)))

        for max_w, crf, kbps in attempts:
            tmp_out = out_path.with_suffix("")
            tmp_out = (
                tmp_out.parent
                / f".{tmp_out.name}_{max_w}_{crf}{'_br' if kbps else ''}.mp4"
            )
            ok = await _run_ffmpeg_build(tmp_dir, fps, max_w, crf, tmp_out, kbps)
            if not ok:
                continue
            try:
                size = tmp_out.stat().st_size
            except Exception:
                size = 0
            if 0 < size <= TIMELAPSE_MAX_BYTES:
                # Move into place
                try:
                    os.replace(tmp_out, out_path)
                except Exception:
                    shutil.copy2(tmp_out, out_path)
                return out_path
            else:
                try:
                    tmp_out.unlink(missing_ok=True)
                except Exception:
                    pass

    logger.warning("Failed to produce timelapse under size limit for job %s", job_name)
    return None


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
# WebSocket watchdog (no printer_status -> mark UNKNOWN)
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
            if current_state != "UNKNOWN":
                logger.warning(
                    "[%s] No printer_status in %ds; marking status UNKNOWN.",
                    pstate.printer_id,
                    STATUS_TIMEOUT_SECONDS,
                )
                unknown_status: Dict[str, Any] = {"gcode_state": "UNKNOWN"}
                async with pstate.lock:
                    pstate.latest_status = unknown_status
                    pstate.latest_status_ts = now_utc()
                try:
                    await maybe_notify_on_update(pstate, unknown_status)
                except Exception as e:
                    logger.exception(
                        "[%s] Notify error (UNKNOWN): %s", pstate.printer_id, e
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
                                        async with pstate.lock:
                                            pstate.latest_image_bytes = decoded
                                            pstate.latest_image_ts = now_utc()
                                            pstate.image_seq += 1
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
# Notification logic
# -----------------------------------------------------------------------------
async def maybe_notify_on_update(
    pstate: PrinterState, new_status: Dict[str, Any]
) -> None:
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
        await telegram_send(
            f"🔄 [{pstate.printer_id}] State changed: {old_state or 'unknown'} → {new_state}\n{summarize_status_for_notification(new_status)}"
        )
        pstate.last_notified_state = new_state

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
    # In dynamic mode, we start a discovery loop that will spawn per-printer WS tasks.
    _ws_task = asyncio.create_task(printers_discovery_loop())
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
