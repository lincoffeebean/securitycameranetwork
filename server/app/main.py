import asyncio
import base64
import binascii
import re
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

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

SAFE_CAMERA_ID = re.compile(r"[^a-zA-Z0-9._-]+")
MAX_RECENT_EVENTS = 30
PERSON_DETECTOR = None


def default_camera_config() -> dict[str, Any]:
    return {
        "control_mode": "automatic",
        "manual_profile": "idle",
        "idle": {
            "width": 1280,
            "height": 720,
            "fps": 3,
            "quality": 0.6,
        },
        "active": {
            "width": 1920,
            "height": 1080,
            "fps": 30,
            "quality": 0.85,
        },
        "recording_duration_seconds": 10,
        "cooldown_seconds": 12,
        "detection_enabled": True,
    }


def default_camera_state(camera_id: str) -> dict[str, Any]:
    return {
        "camera_id": camera_id,
        "connected": True,
        "current_profile": "idle",
        "recording": False,
        "last_frame_at": None,
        "last_detection_at": None,
        "last_clip_path": None,
        "last_clip_at": None,
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
    }


def get_camera_config(camera_id: str) -> dict[str, Any]:
    if camera_id not in camera_configs:
        camera_configs[camera_id] = default_camera_config()
    return camera_configs[camera_id]


def get_camera_state(camera_id: str) -> dict[str, Any]:
    if camera_id not in camera_states:
        camera_states[camera_id] = default_camera_state(camera_id)
    return camera_states[camera_id]


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

    detector = get_person_detector()
    if detector is None:
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

    boxes, _weights = detector.detectMultiScale(
        image,
        winStride=(8, 8),
        padding=(8, 8),
        scale=1.05,
    )
    return len(boxes) > 0


@app.get("/health")
def health():
    return {
        "status": "online",
        "service": "security-camera-network",
        "detector_available": detector_available(),
        "connected_cameras": len(camera_connections),
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
    if websocket is None:
        return

    config = copy_config(get_camera_config(camera_id))
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


async def trigger_recording(camera_id: str, reason: str) -> None:
    websocket = camera_connections.get(camera_id)
    if websocket is None:
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
    state["status"] = f"recording ({reason})"
    await broadcast_camera_state(camera_id)

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


async def process_detection(camera_id: str, frame_data_url: str, timestamp: str) -> None:
    try:
        detected = await asyncio.to_thread(detect_person, frame_data_url)
        if not detected:
            return

        state = get_camera_state(camera_id)
        state["last_detection_at"] = timestamp

        event = {
            "type": "detection_event",
            "camera_id": camera_id,
            "timestamp": timestamp,
            "message": "Human detected",
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


@app.websocket("/ws/camera/{camera_id}")
async def camera_socket(websocket: WebSocket, camera_id: str):
    await websocket.accept()
    camera_connections[camera_id] = websocket
    get_camera_config(camera_id)
    state = get_camera_state(camera_id)
    state["connected"] = True
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
                        process_detection(camera_id, frame, timestamp)
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

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if websocket in viewer_connections:
            viewer_connections.remove(websocket)
