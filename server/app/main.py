from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI()

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

latest_frames: dict[str, dict[str, str]] = {}
viewer_connections: list[WebSocket] = []

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/health")
def health():
    return {
        "status": "online",
        "service": "security-camera-network",
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


async def broadcast_to_viewers(message: dict[str, str]) -> None:
    disconnected_viewers: list[WebSocket] = []

    for viewer in list(viewer_connections):
        try:
            await viewer.send_json(message)
        except Exception:
            disconnected_viewers.append(viewer)

    for viewer in disconnected_viewers:
        if viewer in viewer_connections:
            viewer_connections.remove(viewer)


@app.websocket("/ws/camera/{camera_id}")
async def camera_socket(websocket: WebSocket, camera_id: str):
    await websocket.accept()

    try:
        while True:
            data = await websocket.receive_json()
            frame = data.get("frame")
            timestamp = data.get("timestamp")

            if not frame or not timestamp:
                continue

            message = {
                "type": "frame",
                "camera_id": camera_id,
                "frame": frame,
                "timestamp": timestamp,
            }
            latest_frames[camera_id] = message
            await broadcast_to_viewers(message)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        latest_frames.pop(camera_id, None)
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
        for frame in latest_frames.values():
            await websocket.send_json(frame)

        while True:
            await websocket.receive_json()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if websocket in viewer_connections:
            viewer_connections.remove(websocket)
