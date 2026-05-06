from __future__ import annotations
import asyncio
import json
import os
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING

import aiofiles
from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from babymonitor.common.logger import get_logger

if TYPE_CHECKING:
    from babymonitor.camera.cry_detector import CryDetector
    from babymonitor.camera.recorder import Recorder
    from babymonitor.common.config import CameraConfig

log = get_logger(__name__)

WEB_DIR = Path(__file__).parent.parent.parent / "web"


class WifiRequest(BaseModel):
    ssid: str
    password: str


def create_app(
    cfg: "CameraConfig",
    recorder: "Recorder",
    cry_detector: "CryDetector | None" = None,
    stream: "object | None" = None,
) -> FastAPI:
    app = FastAPI(title="BabyMonitor API")
    connected_ws: set[WebSocket] = set()

    # ── Auth dependency (POST endpoints only) ────────────────────────────────
    def require_token(x_api_token: str | None = Header(None)) -> None:
        if cfg.security.api_token and x_api_token != cfg.security.api_token:
            raise HTTPException(status_code=401, detail="Unauthorized")

    # ── Frontend (inject token into HTML so the JS can read it) ─────────────
    @app.get("/", response_class=HTMLResponse)
    async def index():
        html = (WEB_DIR / "index.html").read_text()
        token = cfg.security.api_token or ""
        return HTMLResponse(html.replace("__API_TOKEN__", token))

    @app.get("/sw.js")
    async def service_worker():
        return FileResponse(
            WEB_DIR / "sw.js",
            media_type="application/javascript",
            headers={"Service-Worker-Allowed": "/"},
        )

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

    @app.post("/api/recording/start", dependencies=[Depends(require_token)])
    async def start_recording():
        path = recorder.start()
        if path is None:
            raise HTTPException(
                status_code=409,
                detail="Already recording, stream not ready, or insufficient disk space",
            )
        return {"status": "recording", "file": os.path.basename(path)}

    @app.post("/api/recording/stop", dependencies=[Depends(require_token)])
    async def stop_recording():
        path = recorder.stop()
        return {"status": "stopped", "file": os.path.basename(path) if path else None}

    # ── WiFi config ──────────────────────────────────────────────────────────
    @app.post("/api/wifi/configure", dependencies=[Depends(require_token)])
    async def configure_wifi(req: WifiRequest):
        from babymonitor.common.config import save_camera_config
        cfg.fallback_wifi.ssid = req.ssid.strip()
        cfg.fallback_wifi.password = req.password
        config_path = os.environ.get("CAMERA_CONFIG", "/etc/babymonitor/camera.yaml")
        try:
            save_camera_config(cfg, config_path)
            log.info("WiFi credentials updated: ssid=%s", req.ssid)
        except OSError as e:
            log.error("Could not save config: %s", e)
            raise HTTPException(status_code=500, detail=f"Configuração não pôde ser gravada: {e}")
        return {"status": "ok", "ssid": req.ssid}

    # ── Status ───────────────────────────────────────────────────────────────
    @app.get("/api/status")
    async def status():
        return {
            "recording": recorder.is_recording(),
            "current_file": recorder.current_file(),
        }

    # ── Health ───────────────────────────────────────────────────────────────
    @app.get("/api/health")
    async def health():
        hls_path = Path(cfg.streaming.hls_dir) / "live.m3u8"
        hls_exists = hls_path.exists()
        hls_fresh = hls_exists and (time.time() - hls_path.stat().st_mtime) < 10

        try:
            du = shutil.disk_usage(cfg.recordings.output_dir)
            disk = {
                "free_gb": round(du.free / 1e9, 2),
                "used_pct": round(du.used / du.total * 100, 1),
            }
        except OSError:
            disk = {"free_gb": None, "used_pct": None}

        stream_ok = hls_exists and hls_fresh
        camera_info = stream.source_info if stream and hasattr(stream, "source_info") else {}
        return {
            "status": "ok" if stream_ok else "degraded",
            "stream": {"hls_ready": hls_exists, "hls_fresh": hls_fresh},
            "camera": camera_info,
            "detector": {"running": cry_detector.is_running() if cry_detector else False},
            "recording": recorder.is_recording(),
            "disk": disk,
        }

    # ── WebSocket alerts ─────────────────────────────────────────────────────
    @app.websocket("/ws/alerts")
    async def ws_alerts(websocket: WebSocket):
        await websocket.accept()
        connected_ws.add(websocket)
        log.info("WS client connected (%d total)", len(connected_ws))
        try:
            while True:
                await asyncio.sleep(30)
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
        dead: set[WebSocket] = set()
        for ws in list(connected_ws):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.add(ws)
        connected_ws -= dead

    app.state.broadcast_alert = broadcast_alert

    return app
