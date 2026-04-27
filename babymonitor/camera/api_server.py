from __future__ import annotations
import asyncio
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

import aiofiles
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import FileResponse, StreamingResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from babymonitor.common.logger import get_logger

if TYPE_CHECKING:
    from babymonitor.camera.recorder import Recorder
    from babymonitor.common.config import CameraConfig

log = get_logger(__name__)

WEB_DIR = Path(__file__).parent.parent.parent / "web"


def create_app(cfg: "CameraConfig", recorder: "Recorder") -> FastAPI:
    app = FastAPI(title="BabyMonitor API")
    connected_ws: set[WebSocket] = set()

    # ── Static frontend ──────────────────────────────────────────────────────
    @app.get("/", response_class=HTMLResponse)
    async def index():
        return FileResponse(WEB_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    # ── HLS stream ───────────────────────────────────────────────────────────
    @app.get("/stream/live.m3u8")
    async def hls_playlist():
        path = Path(cfg.streaming.hls_dir) / "live.m3u8"
        if not path.exists():
            raise HTTPException(status_code=503, detail="Stream not ready")
        return FileResponse(path, media_type="application/vnd.apple.mpegurl")

    @app.get("/stream/{segment}")
    async def hls_segment(segment: str):
        path = Path(cfg.streaming.hls_dir) / segment
        if not path.exists():
            raise HTTPException(status_code=404, detail="Segment not found")
        return FileResponse(path, media_type="video/MP2T")

    # ── Recordings ───────────────────────────────────────────────────────────
    @app.get("/api/recordings")
    async def list_recordings():
        return {"recordings": recorder.list_recordings()}

    @app.get("/api/recordings/{filename}")
    async def get_recording(filename: str):
        if ".." in filename or "/" in filename:
            raise HTTPException(status_code=400, detail="Invalid filename")
        path = Path(cfg.recordings.output_dir) / filename
        if not path.exists():
            raise HTTPException(status_code=404, detail="Recording not found")
        return FileResponse(
            path,
            media_type="video/mp4",
            headers={"Accept-Ranges": "bytes"},
        )

    @app.post("/api/recording/start")
    async def start_recording():
        path = recorder.start()
        if path is None:
            raise HTTPException(status_code=409, detail="Already recording or stream not ready")
        return {"status": "recording", "file": os.path.basename(path)}

    @app.post("/api/recording/stop")
    async def stop_recording():
        path = recorder.stop()
        return {"status": "stopped", "file": os.path.basename(path) if path else None}

    # ── WiFi config ──────────────────────────────────────────────────────────
    class WifiRequest(BaseModel):
        ssid: str
        password: str

    @app.post("/api/wifi/configure")
    async def configure_wifi(req: WifiRequest):
        import yaml
        from babymonitor.common.config import save_camera_config
        cfg.fallback_wifi.ssid = req.ssid.strip()
        cfg.fallback_wifi.password = req.password
        # Persist to config file so the FSM picks it up on restart
        config_path = os.environ.get("CAMERA_CONFIG", "/etc/babymonitor/camera.yaml")
        try:
            save_camera_config(cfg, config_path)
            log.info("WiFi credentials updated: ssid=%s", req.ssid)
        except OSError as e:
            log.warning("Could not save config: %s", e)
        return {"status": "ok", "ssid": req.ssid}

    # ── Status ───────────────────────────────────────────────────────────────
    @app.get("/api/status")
    async def status():
        return {
            "recording": recorder.is_recording(),
            "current_file": recorder.current_file(),
        }

    # ── WebSocket alerts ─────────────────────────────────────────────────────
    @app.websocket("/ws/alerts")
    async def ws_alerts(websocket: WebSocket):
        await websocket.accept()
        connected_ws.add(websocket)
        log.info("WS client connected (%d total)", len(connected_ws))
        try:
            while True:
                await asyncio.sleep(30)  # keep-alive ping
                await websocket.send_text(json.dumps({"type": "ping"}))
        except WebSocketDisconnect:
            pass
        finally:
            connected_ws.discard(websocket)
            log.info("WS client disconnected (%d remaining)", len(connected_ws))

    async def broadcast_alert(confidence: float) -> None:
        if not connected_ws:
            return
        payload = json.dumps({"type": "cry", "confidence": round(confidence, 3)})
        dead = set()
        for ws in list(connected_ws):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.add(ws)
        connected_ws -= dead

    # Expose broadcaster so camera_node can call it
    app.state.broadcast_alert = broadcast_alert

    return app
