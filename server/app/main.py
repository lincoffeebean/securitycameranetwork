import asyncio
import base64
import binascii
from datetime import datetime
import math
import re
import time
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .rtsp_manager import RTSPManager

try:
    import cv2
    import numpy as np
except ImportError:  # pragma: no cover - handled at runtime
    cv2 = None
    np = None


app = FastAPI()

APP_DIR = Path(__file__).resolve().parent
SERVER_DIR = APP_DIR.parent
PROJECT_DIR = SERVER_DIR.parent
STATIC_DIR = APP_DIR / "static"
CONFIG_DIR = SERVER_DIR / "config"
RTSP_CONFIG_PATH = CONFIG_DIR / "rtsp_cameras.json"
RTSP_EXAMPLE_CONFIG_PATH = CONFIG_DIR / "rtsp_cameras.example.json"
RECORDINGS_DIR = PROJECT_DIR / "recordings"
RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/recordings", StaticFiles(directory=RECORDINGS_DIR), name="recordings")

latest_frames: dict[str, dict[str, Any]] = {}
viewer_connections: list[WebSocket] = []
camera_connections: dict[str, WebSocket] = {}
camera_states: dict[str, dict[str, Any]] = {}
camera_configs: dict[str, dict[str, Any]] = {}
recent_events: list[dict[str, Any]] = []
detection_tasks: dict[str, asyncio.Task[None]] = {}
recording_requests: dict[str, float] = {}
camera_labels: dict[str, str] = {}
rtsp_manager: RTSPManager | None = None

SAFE_CAMERA_ID = re.compile(r"[^a-zA-Z0-9._-]+")
MAX_RECENT_EVENTS = 30
PERSON_DETECTOR = None
UPPER_BODY_DETECTOR = None


def default_camera_config() -> dict[str, Any]:
    return {
        "control_mode": "automatic",
        "manual_profile": "idle",
        "idle": {
            "width": 1280,
            "height": 720,
            "fps": 3,
            "quality": 0.6,
            "zoom": 0.5,
        },
        "active": {
            "width": 1920,
            "height": 1080,
            "fps": 30,
            "quality": 0.85,
            "zoom": 0.5,
        },
        "recording_duration_seconds": 10,
        "cooldown_seconds": 12,
        "detection_enabled": True,
        "detection_streak_threshold": 3,
    }


def default_camera_state(camera_id: str) -> dict[str, Any]:
    return {
        "camera_id": camera_id,
        "display_name": camera_id,
        "connected": True,
        "current_profile": "idle",
        "recording": False,
        "last_frame_at": None,
        "last_detection_at": None,
        "detection_hits": 0,
        "last_clip_path": None,
        "last_clip_at": None,
        "source": "browser",
        "rtsp_status": None,
        "rtsp_reconnect_count": 0,
        "rtsp_error": None,
        "status": "connected",
    }


def copy_config(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "control_mode": config["control_mode"],
        "manual_profile": config["manual_profile"],
        "idle": dict(config["idle"]),
        "active": dict(config["active"]),
        "recording_duration_seconds": int(config["recording_duration_seconds"]),
        "cooldown_seconds": int(config["cooldown_seconds"]),
        "detection_enabled": bool(config["detection_enabled"]),
        "detection_streak_threshold": int(config["detection_streak_threshold"]),
    }


def get_camera_config(camera_id: str) -> dict[str, Any]:
    if camera_id not in camera_configs:
        camera_configs[camera_id] = default_camera_config()
    return camera_configs[camera_id]


def get_camera_state(camera_id: str) -> dict[str, Any]:
    if camera_id not in camera_states:
        camera_states[camera_id] = default_camera_state(camera_id)
    camera_states[camera_id]["display_name"] = camera_labels.get(camera_id, camera_id)
    return camera_states[camera_id]


def set_camera_label(camera_id: str, display_name: str) -> None:
    cleaned = display_name.strip() or camera_id
    camera_labels[camera_id] = cleaned
    get_camera_state(camera_id)["display_name"] = cleaned


def sanitize_camera_id(camera_id: str) -> str:
    sanitized = SAFE_CAMERA_ID.sub("-", camera_id).strip("-")
    return sanitized or "camera"


def add_recent_event(event: dict[str, Any]) -> None:
    recent_events.append(event)
    if len(recent_events) > MAX_RECENT_EVENTS:
        del recent_events[:-MAX_RECENT_EVENTS]


def detector_available() -> bool:
    return cv2 is not None and np is not None


def get_person_detector():
    global PERSON_DETECTOR

    if not detector_available():
        return None

    if PERSON_DETECTOR is None:
        detector = cv2.HOGDescriptor()
        detector.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
        PERSON_DETECTOR = detector

    return PERSON_DETECTOR


def get_upper_body_detector():
    global UPPER_BODY_DETECTOR

    if not detector_available():
        return None

    if UPPER_BODY_DETECTOR is None:
        cascade_path = cv2.data.haarcascades + "haarcascade_upperbody.xml"
        detector = cv2.CascadeClassifier(cascade_path)
        if detector.empty():
            return None
        UPPER_BODY_DETECTOR = detector

    return UPPER_BODY_DETECTOR


def decode_data_url(frame_data_url: str):
    if not detector_available():
        return None

    _, _, encoded = frame_data_url.partition(",")
    if not encoded:
        return None

    try:
        binary = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return None

    array = np.frombuffer(binary, dtype=np.uint8)
    return cv2.imdecode(array, cv2.IMREAD_COLOR)


def detect_person(frame_data_url: str) -> bool:
    image = decode_data_url(frame_data_url)
    if image is None or not detector_available():
        return False

    # Downscale large frames before detection so 720p idle analysis stays light.
    max_width = 960
    if image.shape[1] > max_width:
        scale = max_width / image.shape[1]
        image = cv2.resize(
            image,
            (int(image.shape[1] * scale), int(image.shape[0] * scale)),
            interpolation=cv2.INTER_AREA,
        )

    person_detector = get_person_detector()
    upper_body_detector = get_upper_body_detector()
    if person_detector is None and upper_body_detector is None:
        return False

    frame_area = image.shape[0] * image.shape[1]
    boxes = []
    weights = []
    if person_detector is not None:
        boxes, weights = person_detector.detectMultiScale(
            image,
            winStride=(6, 6),
            padding=(8, 8),
            scale=1.03,
        )

    for box, weight in zip(boxes, weights):
        x, y, width, height = [int(value) for value in box]
        area_ratio = (width * height) / max(1, frame_area)
        aspect_ratio = width / max(1, height)
        if (
            float(weight) >= 0.42
            and area_ratio >= 0.05
            and 0.2 <= aspect_ratio <= 0.9
        ):
            return True

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    gray = cv2.equalizeHist(gray)

    if upper_body_detector is None:
        return len(boxes) > 0

    upper_boxes = upper_body_detector.detectMultiScale(
        gray,
        scaleFactor=1.05,
        minNeighbors=7,
        minSize=(96, 96),
    )

    for x, y, width, height in upper_boxes:
        area_ratio = (width * height) / max(1, frame_area)
        aspect_ratio = width / max(1, height)
        center_x = x + (width / 2)
        horizontal_center_offset = abs(center_x - (image.shape[1] / 2)) / max(1, image.shape[1] / 2)
        if (
            area_ratio >= 0.08
            and 0.25 <= aspect_ratio <= 1.2
            and horizontal_center_offset <= 0.8
        ):
            return True

    # Fall back to the broader person boxes when the detector is confident enough.
    wide_boxes, wide_weights = person_detector.detectMultiScale(
        image,
        winStride=(8, 8),
        padding=(8, 8),
        scale=1.05,
    )
    for box, weight in zip(wide_boxes, wide_weights):
        x, y, width, height = [int(value) for value in box]
        area_ratio = (width * height) / max(1, frame_area)
        diagonal = math.hypot(width, height)
        if float(weight) >= 0.75 and area_ratio >= 0.09 and diagonal >= 180:
            return True

    return False


@app.get("/health")
def health():
    rtsp_cameras = rtsp_manager.list_cameras() if rtsp_manager is not None else []
    return {
        "status": "online",
        "service": "security-camera-network",
        "detector_available": detector_available(),
        "connected_cameras": len(camera_connections),
        "rtsp_cameras": len(rtsp_cameras),
    }


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/camera")
def camera_page():
    return FileResponse(STATIC_DIR / "camera.html")


@app.get("/viewer")
def viewer_page():
    return FileResponse(STATIC_DIR / "viewer.html")


@app.get("/recordings-browser")
def recordings_page():
    return FileResponse(STATIC_DIR / "recordings.html")


def parse_recording_timestamp(file_path: Path) -> datetime:
    timestamp_text = file_path.stem
    try:
        normalized = timestamp_text
        if "T" in normalized:
            date_part, time_part = normalized.split("T", 1)
            time_part = time_part.replace("-", ":")
            normalized = f"{date_part}T{time_part}"
        return datetime.fromisoformat(normalized)
    except ValueError:
        return datetime.fromtimestamp(file_path.stat().st_mtime)


def build_recording_entry(file_path: Path) -> dict[str, Any]:
    camera_id = file_path.parent.name
    timestamp = parse_recording_timestamp(file_path)
    display_name = camera_labels.get(camera_id, camera_id)
    return {
        "camera_id": camera_id,
        "display_name": display_name,
        "filename": file_path.name,
        "relative_path": f"/recordings/{camera_id}/{file_path.name}",
        "timestamp": timestamp.isoformat(),
        "date": timestamp.strftime("%Y-%m-%d"),
        "hour": timestamp.strftime("%H:00"),
        "filesize_bytes": file_path.stat().st_size,
    }


def list_recording_entries() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for file_path in sorted(RECORDINGS_DIR.glob("*/*"), reverse=True):
        if not file_path.is_file():
            continue
        entries.append(build_recording_entry(file_path))
    return entries


@app.get("/api/recordings")
def recordings_api(
    camera_id: str | None = Query(default=None),
    date: str | None = Query(default=None),
    hour: str | None = Query(default=None),
):
    all_entries = list_recording_entries()
    entries = list(all_entries)
    if camera_id:
        entries = [entry for entry in entries if entry["camera_id"] == camera_id]
    if date:
        entries = [entry for entry in entries if entry["date"] == date]
    if hour:
        entries = [entry for entry in entries if entry["hour"] == hour]

    cameras = sorted(
        {
            (entry["camera_id"], entry["display_name"])
            for entry in all_entries
        }
    )
    return {
        "recordings": entries,
        "filters": {
            "cameras": [
                {"camera_id": camera_key, "display_name": display_name}
                for camera_key, display_name in cameras
            ],
            "dates": sorted({entry["date"] for entry in all_entries}, reverse=True),
            "hours": sorted({entry["hour"] for entry in all_entries}),
        },
    }


async def send_json_safe(websocket: WebSocket, message: dict[str, Any]) -> bool:
    try:
        await websocket.send_json(message)
        return True
    except Exception:
        return False


async def broadcast_to_viewers(message: dict[str, Any]) -> None:
    disconnected_viewers: list[WebSocket] = []

    for viewer in list(viewer_connections):
        sent = await send_json_safe(viewer, message)
        if not sent:
            disconnected_viewers.append(viewer)

    for viewer in disconnected_viewers:
        if viewer in viewer_connections:
            viewer_connections.remove(viewer)


async def push_config_to_camera(camera_id: str) -> None:
    websocket = camera_connections.get(camera_id)
    config = copy_config(get_camera_config(camera_id))
    if websocket is not None:
        await send_json_safe(
            websocket,
            {
                "type": "config",
                "camera_id": camera_id,
                "config": config,
            },
        )
    await broadcast_to_viewers(
        {
            "type": "camera_config",
            "camera_id": camera_id,
            "config": config,
        }
    )


async def broadcast_camera_state(camera_id: str) -> None:
    state = dict(get_camera_state(camera_id))
    await broadcast_to_viewers(
        {
            "type": "camera_state",
            "camera_id": camera_id,
            "state": state,
        }
    )


def is_rtsp_camera(camera_id: str) -> bool:
    return rtsp_manager is not None and rtsp_manager.has_camera(camera_id)


async def trigger_recording(camera_id: str, reason: str) -> None:
    websocket = camera_connections.get(camera_id)
    rtsp_camera = is_rtsp_camera(camera_id)
    if websocket is None and not rtsp_camera:
        return

    config = get_camera_config(camera_id)
    state = get_camera_state(camera_id)
    now = time.time()
    last_request_at = recording_requests.get(camera_id, 0)
    cooldown = int(config["cooldown_seconds"])

    if state["recording"] or now - last_request_at < cooldown:
        return

    recording_requests[camera_id] = now
    state["recording"] = True
    state["current_profile"] = "active"
    state["status"] = f"recording ({reason})"
    await broadcast_camera_state(camera_id)

    if rtsp_camera and websocket is None:
        assert rtsp_manager is not None
        started, error_message = await rtsp_manager.start_recording(
            camera_id,
            int(config["recording_duration_seconds"]),
            reason,
        )
        if not started:
            state["recording"] = False
            state["current_profile"] = "idle"
            state["status"] = error_message or "recording failed"
            await broadcast_camera_state(camera_id)
            await broadcast_to_viewers(
                {
                    "type": "recording_failed",
                    "camera_id": camera_id,
                    "timestamp": datetime.utcnow().isoformat(),
                    "message": state["status"],
                }
            )
        return

    started = await send_json_safe(
        websocket,
        {
            "type": "start_recording",
            "camera_id": camera_id,
            "reason": reason,
            "duration_seconds": int(config["recording_duration_seconds"]),
            "profile": dict(config["active"]),
        },
    )

    if not started:
        state["recording"] = False
        state["status"] = "connected"
        await broadcast_camera_state(camera_id)


async def disconnect_camera(camera_id: str, reason: str = "viewer_disconnect") -> None:
    websocket = camera_connections.get(camera_id)
    if websocket is None:
        return

    state = get_camera_state(camera_id)
    state["status"] = reason
    await broadcast_camera_state(camera_id)
    await send_json_safe(
        websocket,
        {
            "type": "disconnect",
            "camera_id": camera_id,
            "reason": reason,
        },
    )
    await websocket.close()


async def process_detection(
    camera_id: str,
    frame_data_url: str,
    timestamp: str,
    client_human_present: bool | None = None,
) -> None:
    try:
        if isinstance(client_human_present, bool):
            detected = client_human_present
        else:
            detected = await asyncio.to_thread(detect_person, frame_data_url)
        state = get_camera_state(camera_id)
        config = get_camera_config(camera_id)
        if not detected:
            state["detection_hits"] = 0
            return

        state["detection_hits"] += 1
        state["last_detection_at"] = timestamp
        threshold = int(config["detection_streak_threshold"])
        if state["detection_hits"] < threshold:
            return

        state["detection_hits"] = 0

        event = {
            "type": "detection_event",
            "camera_id": camera_id,
            "timestamp": timestamp,
            "message": "Human detected",
            "detection_hits": threshold,
        }
        add_recent_event(event)
        await broadcast_to_viewers(event)
        await trigger_recording(camera_id, "human_detected")
    finally:
        detection_tasks.pop(camera_id, None)


def merge_profile_values(current: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(current)

    for key in ("width", "height", "fps"):
        if key in incoming:
            merged[key] = max(1, int(incoming[key]))

    if "quality" in incoming:
        quality = float(incoming["quality"])
        merged["quality"] = min(0.95, max(0.3, quality))

    if "zoom" in incoming:
        zoom = float(incoming["zoom"])
        merged["zoom"] = min(8.0, max(0.5, zoom))

    return merged


def merge_config(current: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = copy_config(current)

    if "control_mode" in incoming and incoming["control_mode"] in {"automatic", "manual"}:
        merged["control_mode"] = incoming["control_mode"]

    if "manual_profile" in incoming and incoming["manual_profile"] in {"idle", "active"}:
        merged["manual_profile"] = incoming["manual_profile"]

    if "idle" in incoming and isinstance(incoming["idle"], dict):
        merged["idle"] = merge_profile_values(merged["idle"], incoming["idle"])

    if "active" in incoming and isinstance(incoming["active"], dict):
        merged["active"] = merge_profile_values(merged["active"], incoming["active"])

    if "recording_duration_seconds" in incoming:
        merged["recording_duration_seconds"] = max(
            5,
            min(60, int(incoming["recording_duration_seconds"])),
        )

    if "cooldown_seconds" in incoming:
        merged["cooldown_seconds"] = max(3, min(120, int(incoming["cooldown_seconds"])))

    if "detection_enabled" in incoming:
        merged["detection_enabled"] = bool(incoming["detection_enabled"])

    if "detection_streak_threshold" in incoming:
        merged["detection_streak_threshold"] = max(
            1,
            min(5, int(incoming["detection_streak_threshold"])),
        )

    if merged["control_mode"] == "manual":
        merged["detection_enabled"] = False

    return merged


async def save_recording_clip(camera_id: str, message: dict[str, Any]) -> None:
    clip_data = message.get("data")
    mime_type = message.get("mime_type", "video/webm")
    timestamp = message.get("timestamp") or time.strftime("%Y-%m-%dT%H-%M-%S")
    started_at = message.get("started_at")
    ended_at = message.get("ended_at")

    if not clip_data or "," not in clip_data:
        raise ValueError("Missing clip data.")

    _, _, encoded = clip_data.partition(",")
    binary = base64.b64decode(encoded)

    extension = "webm"
    if "mp4" in mime_type:
        extension = "mp4"

    camera_dir = RECORDINGS_DIR / sanitize_camera_id(camera_id)
    camera_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{timestamp.replace(':', '-').replace('.', '-')}.{extension}"
    clip_path = camera_dir / filename
    clip_path.write_bytes(binary)

    state = get_camera_state(camera_id)
    state["recording"] = False
    state["current_profile"] = "idle"
    state["last_clip_path"] = f"/recordings/{sanitize_camera_id(camera_id)}/{filename}"
    state["last_clip_at"] = timestamp
    state["status"] = "connected"

    event = {
        "type": "clip_saved",
        "camera_id": camera_id,
        "timestamp": timestamp,
        "clip_path": state["last_clip_path"],
        "mime_type": mime_type,
        "started_at": started_at,
        "ended_at": ended_at,
    }
    add_recent_event(event)
    await broadcast_camera_state(camera_id)
    await broadcast_to_viewers(event)
    await push_config_to_camera(camera_id)


def get_or_create_rtsp_manager() -> RTSPManager:
    global rtsp_manager

    if rtsp_manager is None:
        rtsp_manager = RTSPManager(
            config_path=RTSP_CONFIG_PATH,
            example_path=RTSP_EXAMPLE_CONFIG_PATH,
            recordings_dir=RECORDINGS_DIR,
            on_camera=register_rtsp_camera,
            on_status=handle_rtsp_status,
            on_frame=handle_rtsp_frame,
            on_clip_saved=handle_rtsp_clip_saved,
            on_recording_failed=handle_rtsp_recording_failed,
        )

    return rtsp_manager


async def register_rtsp_camera(camera: dict[str, Any]) -> None:
    camera_id = camera["id"]
    set_camera_label(camera_id, camera.get("name", camera_id))

    state = get_camera_state(camera_id)
    state["source"] = "rtsp"
    state["connected"] = False
    state["current_profile"] = "idle"
    state["status"] = "offline"
    state["rtsp_status"] = "offline"
    state["rtsp_reconnect_count"] = 0
    state["rtsp_error"] = None

    config = get_camera_config(camera_id)
    config["detection_enabled"] = bool(camera.get("detect", True))
    config["idle"]["fps"] = int(camera.get("fps", config["idle"]["fps"]))
    config["active"]["fps"] = int(camera.get("fps", config["active"]["fps"]))

    await push_config_to_camera(camera_id)
    await broadcast_camera_state(camera_id)


async def handle_rtsp_status(camera_id: str, status: dict[str, Any]) -> None:
    state = get_camera_state(camera_id)
    rtsp_status = status.get("status", "offline")
    state["source"] = "rtsp"
    state["connected"] = rtsp_status == "online"
    state["rtsp_status"] = rtsp_status
    state["rtsp_reconnect_count"] = int(status.get("reconnect_count", 0))
    state["rtsp_error"] = status.get("error_message")
    if not state["recording"]:
        state["status"] = rtsp_status
        state["current_profile"] = "idle"
    await broadcast_camera_state(camera_id)


async def handle_rtsp_frame(
    camera_id: str,
    frame_data_url: str,
    timestamp: str,
    camera: dict[str, Any],
) -> None:
    state = get_camera_state(camera_id)
    state["source"] = "rtsp"
    state["connected"] = True
    state["last_frame_at"] = timestamp
    state["rtsp_status"] = "online"
    state["rtsp_error"] = None
    state["current_profile"] = "active" if state["recording"] else "idle"
    if not state["recording"]:
        state["status"] = "online"

    message = {
        "type": "frame",
        "camera_id": camera_id,
        "frame": frame_data_url,
        "timestamp": timestamp,
        "profile": state["current_profile"],
        "source": "rtsp",
    }
    latest_frames[camera_id] = message
    await broadcast_to_viewers(message)

    config = get_camera_config(camera_id)
    should_detect = (
        bool(camera.get("detect", True))
        and config["control_mode"] == "automatic"
        and config["detection_enabled"]
        and not state["recording"]
        and camera_id not in detection_tasks
    )
    if should_detect:
        detection_tasks[camera_id] = asyncio.create_task(
            process_detection(camera_id, frame_data_url, timestamp)
        )


async def handle_rtsp_clip_saved(camera_id: str, clip: dict[str, Any]) -> None:
    timestamp = clip.get("timestamp") or datetime.utcnow().isoformat()
    state = get_camera_state(camera_id)
    state["source"] = "rtsp"
    state["connected"] = True
    state["recording"] = False
    state["current_profile"] = "idle"
    state["last_clip_path"] = clip.get("clip_path")
    state["last_clip_at"] = timestamp
    state["status"] = "online"
    state["rtsp_status"] = "online"
    state["rtsp_error"] = None

    event = {
        "type": "clip_saved",
        "camera_id": camera_id,
        "timestamp": timestamp,
        "clip_path": state["last_clip_path"],
        "mime_type": clip.get("mime_type", "video/mp4"),
        "started_at": clip.get("started_at"),
        "ended_at": clip.get("ended_at"),
    }
    add_recent_event(event)
    await broadcast_camera_state(camera_id)
    await broadcast_to_viewers(event)


async def handle_rtsp_recording_failed(
    camera_id: str,
    message: str,
    timestamp: str | None = None,
) -> None:
    state = get_camera_state(camera_id)
    state["recording"] = False
    state["current_profile"] = "idle"
    state["status"] = message
    state["rtsp_error"] = message
    await broadcast_camera_state(camera_id)
    await broadcast_to_viewers(
        {
            "type": "recording_failed",
            "camera_id": camera_id,
            "timestamp": timestamp or datetime.utcnow().isoformat(),
            "message": message,
        }
    )


@app.on_event("startup")
async def startup_rtsp_cameras() -> None:
    await get_or_create_rtsp_manager().start()


@app.on_event("shutdown")
async def shutdown_rtsp_cameras() -> None:
    if rtsp_manager is not None:
        await rtsp_manager.stop()


@app.get("/api/rtsp-cameras")
def rtsp_cameras_api():
    manager = get_or_create_rtsp_manager()
    return {
        "config_path": str(manager.config_source) if manager.config_source else None,
        "cameras": manager.list_cameras(),
    }


@app.post("/api/rtsp-cameras/reload")
async def reload_rtsp_cameras_api():
    return await get_or_create_rtsp_manager().reload()


@app.post("/api/rtsp-cameras/test")
async def test_rtsp_camera_api(payload: dict[str, Any] | None = Body(default=None)):
    payload = payload or {}
    return await get_or_create_rtsp_manager().test_camera(
        camera_id=payload.get("camera_id"),
        url=payload.get("url"),
        timeout_seconds=float(payload.get("timeout_seconds", 8)),
    )


@app.websocket("/ws/camera/{camera_id}")
async def camera_socket(websocket: WebSocket, camera_id: str):
    await websocket.accept()
    camera_connections[camera_id] = websocket
    get_camera_config(camera_id)
    state = get_camera_state(camera_id)
    state["connected"] = True
    state["source"] = "browser"
    state["rtsp_status"] = None
    state["rtsp_error"] = None
    state["status"] = "connected"

    await broadcast_to_viewers(
        {
            "type": "camera_status",
            "camera_id": camera_id,
            "status": "connected",
        }
    )
    await broadcast_camera_state(camera_id)
    await push_config_to_camera(camera_id)

    try:
        while True:
            data = await websocket.receive_json()
            message_type = data.get("type", "frame")

            if message_type == "frame":
                frame = data.get("frame")
                timestamp = data.get("timestamp")
                profile = data.get("profile", "idle")
                client_human_present = data.get("human_present")

                if not frame or not timestamp:
                    continue

                state["last_frame_at"] = timestamp
                state["current_profile"] = profile if profile in {"idle", "active"} else "idle"

                message = {
                    "type": "frame",
                    "camera_id": camera_id,
                    "frame": frame,
                    "timestamp": timestamp,
                    "profile": state["current_profile"],
                    "source": "browser",
                }
                latest_frames[camera_id] = message
                await broadcast_to_viewers(message)

                config = get_camera_config(camera_id)
                should_detect = (
                    config["control_mode"] == "automatic"
                    and config["detection_enabled"]
                    and not state["recording"]
                    and state["current_profile"] == "idle"
                    and camera_id not in detection_tasks
                )
                if should_detect:
                    detection_tasks[camera_id] = asyncio.create_task(
                        process_detection(camera_id, frame, timestamp, client_human_present)
                    )

            elif message_type == "camera_state":
                state["current_profile"] = data.get("current_profile", state["current_profile"])
                state["recording"] = bool(data.get("recording", state["recording"]))
                state["status"] = data.get("status", state["status"])
                await broadcast_camera_state(camera_id)

            elif message_type == "clip":
                await save_recording_clip(camera_id, data)

            elif message_type == "recording_failed":
                state["recording"] = False
                state["current_profile"] = "idle"
                state["status"] = data.get("message", "recording failed")
                await broadcast_camera_state(camera_id)
                await broadcast_to_viewers(
                    {
                        "type": "recording_failed",
                        "camera_id": camera_id,
                        "timestamp": data.get("timestamp"),
                        "message": state["status"],
                    }
                )
                await push_config_to_camera(camera_id)

    except WebSocketDisconnect:
        pass
    except Exception as error:
        state["status"] = f"error: {error}"
    finally:
        task = detection_tasks.pop(camera_id, None)
        if task is not None:
            task.cancel()
        camera_connections.pop(camera_id, None)
        latest_frames.pop(camera_id, None)
        state["connected"] = False
        state["recording"] = False
        state["source"] = "browser"
        state["status"] = "disconnected"
        await broadcast_camera_state(camera_id)
        await broadcast_to_viewers(
            {
                "type": "camera_status",
                "camera_id": camera_id,
                "status": "disconnected",
            }
        )


@app.websocket("/ws/viewer")
async def viewer_socket(websocket: WebSocket):
    await websocket.accept()
    viewer_connections.append(websocket)

    try:
        await websocket.send_json(
            {
                "type": "snapshot",
                "frames": list(latest_frames.values()),
                "states": list(camera_states.values()),
                "configs": [
                    {
                        "camera_id": camera_id,
                        "config": copy_config(config),
                    }
                    for camera_id, config in camera_configs.items()
                ],
                "events": list(recent_events),
                "detector_available": detector_available(),
            }
        )

        while True:
            data = await websocket.receive_json()
            message_type = data.get("type")
            camera_id = data.get("camera_id", "").strip()

            if message_type == "ping":
                continue

            if message_type == "set_config" and camera_id:
                current = get_camera_config(camera_id)
                updated = merge_config(current, data.get("config", {}))
                camera_configs[camera_id] = updated
                await push_config_to_camera(camera_id)
                await broadcast_camera_state(camera_id)

            if message_type == "trigger_recording" and camera_id:
                await trigger_recording(camera_id, "viewer_manual")

            if message_type == "set_display_name" and camera_id:
                set_camera_label(camera_id, data.get("display_name", ""))
                await broadcast_camera_state(camera_id)

            if message_type == "disconnect_camera" and camera_id:
                await disconnect_camera(camera_id)

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if websocket in viewer_connections:
            viewer_connections.remove(websocket)
