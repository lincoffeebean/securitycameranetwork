import asyncio
import base64
from dataclasses import dataclass
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Awaitable, Callable
from urllib.parse import quote

try:
    import cv2
except ImportError:  # pragma: no cover - handled at runtime
    cv2 = None


logger = logging.getLogger(__name__)

ENV_PATTERN = re.compile(r"\$\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
SAFE_CAMERA_ID = re.compile(r"[^a-zA-Z0-9._-]+")


CameraCallback = Callable[[dict[str, Any]], Awaitable[None]]
StatusCallback = Callable[[str, dict[str, Any]], Awaitable[None]]
FrameCallback = Callable[[str, str, str, dict[str, Any]], Awaitable[None]]
ClipCallback = Callable[[str, dict[str, Any]], Awaitable[None]]
RecordingFailureCallback = Callable[[str, str, str | None], Awaitable[None]]


@dataclass(frozen=True)
class RTSPCameraConfig:
    id: str
    name: str
    url: str
    enabled: bool
    fps: int
    detect: bool
    record: bool
    source: str = "rtsp"
    missing_env: tuple[str, ...] = ()

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "url": redact_rtsp_url(self.url),
            "enabled": self.enabled,
            "fps": self.fps,
            "detect": self.detect,
            "record": self.record,
            "source": self.source,
            "missing_env": list(self.missing_env),
        }


def sanitize_camera_id(camera_id: str) -> str:
    sanitized = SAFE_CAMERA_ID.sub("-", camera_id).strip("-")
    return sanitized or "camera"


def safe_timestamp(timestamp: str) -> str:
    return timestamp.replace(":", "-").replace(".", "-")


def redact_rtsp_url(url: str) -> str:
    return re.sub(r"(rtsp://[^:/@]+:)([^@]+)(@)", r"\1****\3", url)


def encode_rtsp_password_in_url(url: str) -> str:
    if not url.startswith("rtsp://") or "@" not in url:
        return url

    scheme, remainder = url.split("://", 1)
    userinfo, hostpart = remainder.split("@", 1)
    if ":" not in userinfo:
        return url

    username, password = userinfo.split(":", 1)
    encoded_password = quote(password, safe="")
    return f"{scheme}://{username}:{encoded_password}@{hostpart}"


def substitute_env(value: str) -> tuple[str, tuple[str, ...]]:
    missing: list[str] = []
    replaced = False

    def replace(match: re.Match[str]) -> str:
        nonlocal replaced
        name = match.group(1)
        replacement = os.getenv(name)
        if replacement is None:
            missing.append(name)
            return match.group(0)
        replaced = True
        return replacement

    substituted = ENV_PATTERN.sub(replace, value)
    if replaced:
        substituted = encode_rtsp_password_in_url(substituted)

    return substituted, tuple(sorted(set(missing)))


def load_rtsp_config(config_path: Path, example_path: Path) -> tuple[list[RTSPCameraConfig], Path | None]:
    source_path = config_path if config_path.exists() else example_path if example_path.exists() else None
    if source_path is None:
        return [], None

    with source_path.open("r", encoding="utf-8") as file:
        payload = json.load(file)

    raw_cameras = payload.get("cameras", payload) if isinstance(payload, dict) else payload
    if not isinstance(raw_cameras, list):
        raise ValueError("RTSP camera config must be a JSON list or an object with a cameras list.")

    cameras: list[RTSPCameraConfig] = []
    for item in raw_cameras:
        if not isinstance(item, dict):
            continue

        camera_id = str(item.get("id", "")).strip()
        if not camera_id:
            continue

        url, missing_env = substitute_env(str(item.get("url", "")).strip())
        fps = max(1, min(30, int(item.get("fps", 10))))
        cameras.append(
            RTSPCameraConfig(
                id=camera_id,
                name=str(item.get("name") or camera_id).strip(),
                url=url,
                enabled=bool(item.get("enabled", True)),
                fps=fps,
                detect=bool(item.get("detect", True)),
                record=bool(item.get("record", True)),
                source=str(item.get("source") or "rtsp"),
                missing_env=missing_env,
            )
        )

    return cameras, source_path


def build_rtsp_config_entries(example_path: Path, password: str) -> list[dict[str, Any]]:
    with example_path.open("r", encoding="utf-8") as file:
        payload = json.load(file)

    raw_cameras = payload.get("cameras", payload) if isinstance(payload, dict) else payload
    if not isinstance(raw_cameras, list):
        raise ValueError("RTSP camera example config must be a JSON list or an object with a cameras list.")

    rendered = json.dumps(raw_cameras).replace(
        "${HIKVISION_DVR_PASSWORD}",
        quote(password, safe=""),
    )
    entries = json.loads(rendered)
    if not isinstance(entries, list):
        raise ValueError("RTSP camera example config could not be rendered.")
    return entries


def write_rtsp_config(config_path: Path, example_path: Path, password: str) -> Path:
    if not password:
        raise ValueError("Password is required.")

    config_path.parent.mkdir(parents=True, exist_ok=True)
    entries = build_rtsp_config_entries(example_path, password)
    config_path.write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")
    return config_path


class RTSPWorker:
    def __init__(
        self,
        manager: "RTSPManager",
        config: RTSPCameraConfig,
        recordings_dir: Path,
    ) -> None:
        self.manager = manager
        self.config = config
        self.recordings_dir = recordings_dir
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.recording_lock = threading.Lock()
        self.recording: dict[str, Any] | None = None

    def start(self) -> None:
        self.thread = threading.Thread(
            target=self.run,
            name=f"rtsp-{self.config.id}",
            daemon=True,
        )
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()

    def join(self, timeout: float = 3.0) -> None:
        if self.thread is not None:
            self.thread.join(timeout=timeout)

    def start_recording(self, duration_seconds: int, reason: str) -> tuple[bool, str | None]:
        if cv2 is None:
            return False, "OpenCV is not available for RTSP recording."

        with self.recording_lock:
            if self.recording is not None:
                return False, "RTSP camera is already recording."

            started_at = datetime.utcnow().isoformat()
            self.recording = {
                "reason": reason,
                "started_at": started_at,
                "deadline": time.monotonic() + max(1, duration_seconds),
                "duration_seconds": duration_seconds,
                "writer": None,
                "path": None,
                "relative_path": None,
                "mime_type": None,
                "frames_written": 0,
            }

        return True, None

    def run(self) -> None:
        if cv2 is None:
            self.manager.submit_status(
                self.config.id,
                "offline",
                "OpenCV is not installed; RTSP streams cannot start.",
            )
            return

        reconnect_count = 0
        backoff_seconds = 1.0

        while not self.stop_event.is_set():
            self.manager.submit_status(
                self.config.id,
                "reconnecting" if reconnect_count else "connecting",
                None,
                reconnect_count,
            )
            logger.info("Connecting RTSP camera %s to %s", self.config.id, redact_rtsp_url(self.config.url))

            capture = cv2.VideoCapture(self.config.url)
            if not capture.isOpened():
                reconnect_count += 1
                message = "Failed to open RTSP stream. Check network, credentials, and channel path."
                logger.warning("%s: %s", self.config.id, message)
                self.manager.submit_status(self.config.id, "offline", message, reconnect_count)
                self.sleep_with_stop(backoff_seconds)
                backoff_seconds = min(30.0, backoff_seconds * 1.7)
                continue

            self.manager.submit_status(self.config.id, "online", None, reconnect_count)
            logger.info("RTSP camera %s is online.", self.config.id)
            backoff_seconds = 1.0
            consecutive_failures = 0
            last_emit_at = 0.0
            frame_interval = 1.0 / max(1, self.config.fps)

            try:
                while not self.stop_event.is_set():
                    ok, frame = capture.read()
                    self.finish_expired_recording()

                    if not ok or frame is None:
                        consecutive_failures += 1
                        if consecutive_failures >= 8:
                            raise RuntimeError("RTSP stream stopped returning frames.")
                        self.sleep_with_stop(0.2)
                        continue

                    consecutive_failures = 0
                    now = time.monotonic()
                    if now - last_emit_at < frame_interval:
                        self.record_frame_if_needed(frame)
                        self.sleep_with_stop(min(0.05, frame_interval - (now - last_emit_at)))
                        continue

                    last_emit_at = now
                    self.record_frame_if_needed(frame)
                    self.publish_frame(frame)
            except Exception as error:
                reconnect_count += 1
                message = str(error)
                logger.warning("RTSP camera %s disconnected: %s", self.config.id, message)
                self.manager.submit_status(self.config.id, "reconnecting", message, reconnect_count)
            finally:
                capture.release()

            self.sleep_with_stop(backoff_seconds)
            backoff_seconds = min(30.0, backoff_seconds * 1.7)

        self.finish_recording(success=False, failure_message="RTSP worker stopped before recording completed.")
        self.manager.submit_status(self.config.id, "offline", "RTSP worker stopped.")

    def publish_frame(self, frame: Any) -> None:
        encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), 80]
        ok, encoded = cv2.imencode(".jpg", frame, encode_params)
        if not ok:
            return

        timestamp = datetime.utcnow().isoformat()
        frame_data = base64.b64encode(encoded.tobytes()).decode("ascii")
        data_url = f"data:image/jpeg;base64,{frame_data}"
        self.manager.submit_frame(self.config.id, data_url, timestamp)

    def record_frame_if_needed(self, frame: Any) -> None:
        failure_message = None
        clip_payload = None

        with self.recording_lock:
            if self.recording is None:
                return

            if self.recording["writer"] is None:
                failure_message = self.open_recording_writer(frame)

            if failure_message is None and self.recording is not None:
                writer = self.recording["writer"]
                writer.write(frame)
                self.recording["frames_written"] += 1

            if self.recording is not None and time.monotonic() >= self.recording["deadline"]:
                clip_payload, failure_message = self.finish_recording_locked(success=True)

        if clip_payload is not None:
            self.manager.submit_clip_saved(self.config.id, clip_payload)
        if failure_message is not None:
            self.manager.submit_recording_failed(self.config.id, failure_message)

    def finish_expired_recording(self) -> None:
        clip_payload = None
        failure_message = None

        with self.recording_lock:
            if self.recording is None:
                return
            if time.monotonic() < self.recording["deadline"]:
                return
            clip_payload, failure_message = self.finish_recording_locked(success=True)

        if clip_payload is not None:
            self.manager.submit_clip_saved(self.config.id, clip_payload)
        if failure_message is not None:
            self.manager.submit_recording_failed(self.config.id, failure_message)

    def finish_recording(self, success: bool, failure_message: str) -> None:
        clip_payload = None
        next_failure = None

        with self.recording_lock:
            if self.recording is None:
                return
            clip_payload, next_failure = self.finish_recording_locked(success=success)

        if clip_payload is not None:
            self.manager.submit_clip_saved(self.config.id, clip_payload)
        if next_failure is not None:
            self.manager.submit_recording_failed(self.config.id, next_failure or failure_message)

    def open_recording_writer(self, frame: Any) -> str | None:
        if self.recording is None:
            return None

        height, width = frame.shape[:2]
        camera_dir = self.recordings_dir / sanitize_camera_id(self.config.id)
        camera_dir.mkdir(parents=True, exist_ok=True)
        started_at = self.recording["started_at"]
        base_name = safe_timestamp(started_at)
        fps = float(max(1, self.config.fps))

        candidates = [
            ("mp4", "video/mp4", cv2.VideoWriter_fourcc(*"mp4v")),
            ("avi", "video/x-msvideo", cv2.VideoWriter_fourcc(*"MJPG")),
        ]

        for extension, mime_type, fourcc in candidates:
            path = camera_dir / f"{base_name}.{extension}"
            writer = cv2.VideoWriter(str(path), fourcc, fps, (width, height))
            if writer.isOpened():
                self.recording["writer"] = writer
                self.recording["path"] = path
                self.recording["relative_path"] = (
                    f"/recordings/{sanitize_camera_id(self.config.id)}/{path.name}"
                )
                self.recording["mime_type"] = mime_type
                return None
            writer.release()

        self.recording = None
        return "OpenCV could not open a video writer for RTSP recording."

    def finish_recording_locked(self, success: bool) -> tuple[dict[str, Any] | None, str | None]:
        if self.recording is None:
            return None, None

        recording = self.recording
        writer = recording.get("writer")
        if writer is not None:
            writer.release()

        self.recording = None
        if not success:
            return None, "RTSP recording stopped before completion."

        path = recording.get("path")
        if path is None or recording.get("frames_written", 0) <= 0:
            return None, "RTSP recording ended without any captured frames."

        ended_at = datetime.utcnow().isoformat()
        return (
            {
                "timestamp": recording["started_at"],
                "started_at": recording["started_at"],
                "ended_at": ended_at,
                "clip_path": recording["relative_path"],
                "mime_type": recording["mime_type"],
            },
            None,
        )

    def sleep_with_stop(self, seconds: float) -> None:
        self.stop_event.wait(max(0.0, seconds))


class RTSPManager:
    def __init__(
        self,
        config_path: Path,
        example_path: Path,
        recordings_dir: Path,
        on_camera: CameraCallback,
        on_status: StatusCallback,
        on_frame: FrameCallback,
        on_clip_saved: ClipCallback,
        on_recording_failed: RecordingFailureCallback,
    ) -> None:
        self.config_path = config_path
        self.example_path = example_path
        self.recordings_dir = recordings_dir
        self.on_camera = on_camera
        self.on_status = on_status
        self.on_frame = on_frame
        self.on_clip_saved = on_clip_saved
        self.on_recording_failed = on_recording_failed
        self.loop: asyncio.AbstractEventLoop | None = None
        self.config_source: Path | None = None
        self.cameras: dict[str, RTSPCameraConfig] = {}
        self.statuses: dict[str, dict[str, Any]] = {}
        self.workers: dict[str, RTSPWorker] = {}
        self.lock = threading.Lock()

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        await self.reload()

    async def stop(self) -> None:
        for worker in list(self.workers.values()):
            worker.stop()
        await asyncio.gather(
            *[asyncio.to_thread(worker.join) for worker in list(self.workers.values())],
            return_exceptions=True,
        )
        self.workers.clear()

    async def reload(self) -> dict[str, Any]:
        self.loop = asyncio.get_running_loop()
        await self.stop()

        cameras, source_path = load_rtsp_config(self.config_path, self.example_path)
        self.config_source = source_path
        self.cameras = {camera.id: camera for camera in cameras}

        with self.lock:
            self.statuses = {
                camera.id: {
                    "camera_id": camera.id,
                    "status": "offline",
                    "last_frame_at": None,
                    "reconnect_count": 0,
                    "error_message": None,
                    "enabled": camera.enabled,
                }
                for camera in cameras
            }

        for camera in cameras:
            await self.on_camera(camera.public_dict())
            if not camera.enabled:
                await self.set_status(camera.id, "offline", "Camera is disabled in RTSP config.")
                continue
            if camera.missing_env:
                missing = ", ".join(camera.missing_env)
                await self.set_status(camera.id, "offline", f"Missing environment variable(s): {missing}")
                continue

            worker = RTSPWorker(self, camera, self.recordings_dir)
            self.workers[camera.id] = worker
            worker.start()

        return {
            "status": "reloaded",
            "config_path": str(source_path) if source_path else None,
            "camera_count": len(cameras),
            "enabled_count": len(self.workers),
        }

    def has_camera(self, camera_id: str) -> bool:
        return camera_id in self.cameras

    def can_record(self, camera_id: str) -> bool:
        camera = self.cameras.get(camera_id)
        return bool(camera and camera.record)

    async def start_recording(
        self,
        camera_id: str,
        duration_seconds: int,
        reason: str,
    ) -> tuple[bool, str | None]:
        camera = self.cameras.get(camera_id)
        if camera is None:
            return False, "RTSP camera is not configured."
        if not camera.record:
            return False, "Recording is disabled for this RTSP camera."

        worker = self.workers.get(camera_id)
        if worker is None:
            return False, "RTSP worker is not running for this camera."

        status = self.statuses.get(camera_id, {})
        if status.get("status") not in {"online", "recording"}:
            return False, f"RTSP camera is not online ({status.get('status', 'unknown')})."

        return worker.start_recording(duration_seconds, reason)

    def list_cameras(self) -> list[dict[str, Any]]:
        with self.lock:
            statuses = {camera_id: dict(status) for camera_id, status in self.statuses.items()}

        items = []
        for camera in self.cameras.values():
            item = camera.public_dict()
            item.update(statuses.get(camera.id, {}))
            items.append(item)
        return items

    async def set_status(
        self,
        camera_id: str,
        status: str,
        error_message: str | None = None,
        reconnect_count: int | None = None,
    ) -> None:
        with self.lock:
            item = self.statuses.setdefault(
                camera_id,
                {
                    "camera_id": camera_id,
                    "status": status,
                    "last_frame_at": None,
                    "reconnect_count": 0,
                    "error_message": None,
                    "enabled": True,
                },
            )
            item["status"] = status
            item["error_message"] = error_message
            if reconnect_count is not None:
                item["reconnect_count"] = reconnect_count
            status_payload = dict(item)

        await self.on_status(camera_id, status_payload)

    def submit_status(
        self,
        camera_id: str,
        status: str,
        error_message: str | None = None,
        reconnect_count: int | None = None,
    ) -> None:
        self.submit(self.set_status(camera_id, status, error_message, reconnect_count))

    def submit_frame(self, camera_id: str, frame_data_url: str, timestamp: str) -> None:
        self.submit(self.handle_frame(camera_id, frame_data_url, timestamp))

    async def handle_frame(self, camera_id: str, frame_data_url: str, timestamp: str) -> None:
        camera = self.cameras.get(camera_id)
        if camera is None:
            return

        with self.lock:
            status = self.statuses.setdefault(
                camera_id,
                {
                    "camera_id": camera_id,
                    "status": "online",
                    "last_frame_at": timestamp,
                    "reconnect_count": 0,
                    "error_message": None,
                    "enabled": True,
                },
            )
            status["status"] = "online"
            status["last_frame_at"] = timestamp
            status["error_message"] = None

        await self.on_frame(camera_id, frame_data_url, timestamp, camera.public_dict())

    def submit_clip_saved(self, camera_id: str, clip: dict[str, Any]) -> None:
        self.submit(self.on_clip_saved(camera_id, clip))

    def submit_recording_failed(
        self,
        camera_id: str,
        message: str,
        timestamp: str | None = None,
    ) -> None:
        self.submit(self.on_recording_failed(camera_id, message, timestamp))

    def submit(self, coroutine: Awaitable[None]) -> None:
        if self.loop is None or self.loop.is_closed():
            close = getattr(coroutine, "close", None)
            if close is not None:
                close()
            return
        asyncio.run_coroutine_threadsafe(coroutine, self.loop)

    async def test_camera(
        self,
        camera_id: str | None = None,
        url: str | None = None,
        timeout_seconds: float = 8.0,
    ) -> dict[str, Any]:
        test_url = url
        if camera_id:
            camera = self.cameras.get(camera_id)
            if camera is None:
                return {"ok": False, "error": "Unknown RTSP camera ID."}
            test_url = camera.url

        if not test_url:
            return {"ok": False, "error": "Provide a camera_id or url."}

        resolved_url, missing_env = substitute_env(test_url)
        if missing_env:
            return {
                "ok": False,
                "error": f"Missing environment variable(s): {', '.join(missing_env)}",
            }

        if cv2 is None:
            return {"ok": False, "error": "OpenCV is not installed."}

        try:
            return await asyncio.wait_for(
                asyncio.to_thread(test_rtsp_url, resolved_url),
                timeout=timeout_seconds + 2.0,
            )
        except asyncio.TimeoutError:
            return {"ok": False, "error": "RTSP test timed out."}


def test_rtsp_url(url: str) -> dict[str, Any]:
    if cv2 is None:
        return {"ok": False, "error": "OpenCV is not installed."}

    capture = cv2.VideoCapture(url)
    if not capture.isOpened():
        capture.release()
        return {"ok": False, "error": "Could not open RTSP stream.", "url": redact_rtsp_url(url)}

    ok, frame = capture.read()
    capture.release()

    if not ok or frame is None:
        return {"ok": False, "error": "Connected but could not read a frame.", "url": redact_rtsp_url(url)}

    height, width = frame.shape[:2]
    return {
        "ok": True,
        "url": redact_rtsp_url(url),
        "width": width,
        "height": height,
    }
