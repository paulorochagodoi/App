from __future__ import annotations
import asyncio
import json
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from babymonitor.common.logger import get_logger

if TYPE_CHECKING:
    from babymonitor.common.config import CameraConfig

log = get_logger(__name__)

WEB_DIR = Path(__file__).parent.parent.parent / "web"


class WifiRequest(BaseModel):
    ssid: str
    password: str


def create_app(cfg: "CameraConfig", stream: "object | None" = None) -> FastAPI:
    app = FastAPI(title="BabyMonitor API")
    connected_ws: set[WebSocket] = set()

    def require_token(x_api_token: str | None = Header(None)) -> None:
        if cfg.security.api_token and x_api_token != cfg.security.api_token:
            raise HTTPException(status_code=401, detail="Unauthorized")

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

    @app.get("/stream/live.m3u8")
    async def hls_playlist():
        path = Path(cfg.streaming.hls_dir) / "live.m3u8"
        if not path.exists():
            raise HTTPException(status_code=503, detail="Stream not ready")
        return FileResponse(
            path,
            media_type="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
        )

    @app.get("/stream/{segment}")
    async def hls_segment(segment: str):
        path = Path(cfg.streaming.hls_dir) / segment
        if not path.exists():
            raise HTTPException(status_code=404, detail="Segment not found")
        return FileResponse(path, media_type="video/MP2T", headers={"Cache-Control": "no-store"})

    @app.websocket("/ws/webrtc")
    async def ws_webrtc(websocket: WebSocket):
        await websocket.accept()
        peer_id = str(id(websocket))
        loop = asyncio.get_event_loop()
        send_queue: asyncio.Queue = asyncio.Queue()

        async def on_send(msg: dict) -> None:
            await send_queue.put(msg)

        if stream is None or not hasattr(stream, "add_webrtc_peer"):
            await websocket.close(code=1011)
            return

        peer = stream.add_webrtc_peer(peer_id, loop, on_send)
        if peer is None:
            await websocket.close(code=1011)
            return

        async def _sender():
            try:
                while True:
                    await websocket.send_json(await send_queue.get())
            except Exception:
                pass

        sender_task = asyncio.create_task(_sender())
        log.info("WebRTC peer connected: %s", peer_id)
        try:
            async for data in websocket.iter_json():
                msg_type = data.get("type")
                if msg_type == "offer":
                    peer.set_offer(data["sdp"])
                elif msg_type == "ice-candidate":
                    peer.add_ice_candidate(data.get("sdpMLineIndex", 0), data.get("candidate", ""))
        except WebSocketDisconnect:
            pass
        finally:
            sender_task.cancel()
            stream.remove_webrtc_peer(peer_id)

    @app.post("/api/wifi/configure", dependencies=[Depends(require_token)])
    async def configure_wifi(req: WifiRequest):
        from babymonitor.common.config import save_camera_config
        cfg.fallback_wifi.ssid = req.ssid.strip()
        cfg.fallback_wifi.password = req.password
        config_path = os.environ.get("CAMERA_CONFIG", "/etc/babymonitor/camera.yaml")
        try:
            save_camera_config(cfg, config_path)
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"Config não pôde ser gravado: {e}")
        return {"status": "ok", "ssid": req.ssid}

    @app.get("/api/health")
    async def health():
        hls_path = Path(cfg.streaming.hls_dir) / "live.m3u8"
        hls_exists = hls_path.exists()
        hls_fresh = hls_exists and (time.time() - hls_path.stat().st_mtime) < 10

        from babymonitor.streaming.webrtc_stream import _WEBRTC_AVAILABLE, _WEBRTC_UNAVAILABLE_REASON
        webrtc_peers = len(stream._webrtc_peers) if stream and hasattr(stream, "_webrtc_peers") else 0
        webrtc_info: dict = {"available": _WEBRTC_AVAILABLE, "active_peers": webrtc_peers}
        if not _WEBRTC_AVAILABLE and _WEBRTC_UNAVAILABLE_REASON:
            webrtc_info["unavailable_reason"] = _WEBRTC_UNAVAILABLE_REASON

        return {
            "status": "ok" if (hls_exists and hls_fresh) else "degraded",
            "stream": {"hls_ready": hls_exists, "hls_fresh": hls_fresh},
            "webrtc": webrtc_info,
        }

    @app.websocket("/ws/alerts")
    async def ws_alerts(websocket: WebSocket):
        await websocket.accept()
        connected_ws.add(websocket)
        try:
            while True:
                await asyncio.sleep(30)
                await websocket.send_text(json.dumps({"type": "ping"}))
        except WebSocketDisconnect:
            pass
        finally:
            connected_ws.discard(websocket)

    return app
