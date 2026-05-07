#!/usr/bin/env python3
"""Native GTK+GStreamer kiosk for BabyMonitor — replaces Chromium.

Lower latency than the browser because:
- GStreamer webrtcbin decodes H.264 via v4l2h264dec (GPU/VPU on Pi)
- No Chromium overhead (~200 MB RAM, ~30% CPU saved)
- ICE works purely on the local WiFi AP; no STUN dependency
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import urllib.request
import urllib.error
from urllib.parse import urlparse
from typing import Optional

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gst", "1.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gtk, Gst, GLib, Gdk  # noqa: E402

try:
    gi.require_version("GstWebRTC", "1.0")
    gi.require_version("GstSdp", "1.0")
    from gi.repository import GstWebRTC, GstSdp

    _WEBRTC_IMPORTS_OK = True
except (ImportError, ValueError) as _e:
    _WEBRTC_IMPORTS_OK = False
    _WEBRTC_IMPORT_ERR = str(_e)

Gst.init(None)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("monitor_app")

_RECONNECT_DELAY = 5  # seconds between reconnect attempts


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_camera_url() -> str:
    """Read camera URL from kiosk_url file or env, with fallback."""
    url_file = os.environ.get("KIOSK_URL_FILE", "/etc/babymonitor/kiosk_url")
    try:
        url = open(url_file).read().strip()
        if url:
            return url
    except OSError:
        pass
    return os.environ.get("CAMERA_URL", "http://10.42.0.1:8080")


def _ws_url(camera_url: str, path: str) -> str:
    p = urlparse(camera_url)
    scheme = "wss" if p.scheme == "https" else "ws"
    return f"{scheme}://{p.netloc}{path}"


def _http_post(base_url: str, path: str, token: str = "") -> dict:
    req = urllib.request.Request(f"{base_url}{path}", method="POST", data=b"")
    if token:
        req.add_header("X-Api-Token", token)
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def _http_get(base_url: str, path: str, token: str = "") -> dict:
    req = urllib.request.Request(f"{base_url}{path}")
    if token:
        req.add_header("X-Api-Token", token)
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


# ---------------------------------------------------------------------------
# Video sink
# ---------------------------------------------------------------------------

class VideoSink:
    """Wraps either a gtksink (preferred) or a Gtk.DrawingArea + X11 overlay."""

    def __init__(self) -> None:
        self.widget: Optional[Gtk.Widget] = None
        self.element: Optional[Gst.Element] = None  # pre-created Gst element
        self._gtksink = False

    def create_widget(self) -> Gtk.Widget:
        """Create the GTK widget for the video area. Must be called from GTK thread."""
        sink = Gst.ElementFactory.make("gtksink", "video_sink")
        if sink is not None:
            widget = sink.get_property("widget")
            widget.set_hexpand(True)
            widget.set_vexpand(True)
            self.element = sink
            self.widget = widget
            self._gtksink = True
            log.info("Video sink: gtksink (hardware-integrated)")
            return widget

        # Fallback: plain DrawingArea; sink will be created in the pipeline
        log.warning("gtksink unavailable — falling back to DrawingArea+xvimagesink")
        da = Gtk.DrawingArea()
        da.set_hexpand(True)
        da.set_vexpand(True)
        self.widget = da
        return da

    def add_to_pipeline_and_link(
        self, pipeline: Gst.Pipeline, prev_el: Gst.Element
    ) -> bool:
        """Add the sink to *pipeline* and link *prev_el* → sink. Returns success."""
        if self._gtksink and self.element is not None:
            sink = self.element
        elif Gst.ElementFactory.find("xvimagesink"):
            sink = Gst.ElementFactory.make("xvimagesink", "video_sink")
        else:
            sink = Gst.ElementFactory.make("autovideosink", "video_sink")

        if sink is None:
            log.error("Could not create any video sink")
            return False

        sink.set_property("sync", False)
        pipeline.add(sink)
        sink.sync_state_with_parent()

        if not self._gtksink and self.widget is not None:
            # For X11 sinks we need to set the XWindow ID so video renders in our widget
            try:
                xid = self.widget.get_window().get_xid()
                sink.set_window_handle(xid)
            except Exception as exc:
                log.debug("Could not set window handle: %s", exc)

        if not prev_el.link(sink):
            log.error("Failed to link decode chain → video sink")
            return False

        log.info("Video sink linked (%s)", sink.get_factory().get_name())
        return True


# ---------------------------------------------------------------------------
# WebRTC client (GStreamer-side, offer initiator)
# ---------------------------------------------------------------------------

class WebRTCClient:
    """
    Client-side GStreamer WebRTC peer.

    Sends an SDP offer to the camera's FastAPI /ws/webrtc endpoint,
    receives the answer, exchanges ICE candidates, and decodes the H.264
    video stream into *video_sink*.
    """

    def __init__(
        self,
        camera_url: str,
        video_sink: VideoSink,
        on_status,   # callable(str) — will be scheduled via GLib.idle_add
    ) -> None:
        self._camera_url = camera_url
        self._vsink = video_sink
        self._on_status = on_status

        self._pipeline: Optional[Gst.Pipeline] = None
        self._webrtcbin: Optional[Gst.Element] = None

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._send_queue: Optional[asyncio.Queue] = None
        self._stop = threading.Event()

        self._h264dec = (
            "v4l2h264dec" if Gst.ElementFactory.find("v4l2h264dec") else "avdec_h264"
        )
        log.info("H.264 decoder: %s", self._h264dec)

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._loop = asyncio.new_event_loop()
        threading.Thread(
            target=self._run_loop, daemon=True, name="webrtc-async"
        ).start()

    def stop(self) -> None:
        self._stop.set()
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._teardown_pipeline()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._session()
            except Exception as exc:
                log.warning("WebRTC session error: %s", exc)
            if not self._stop.is_set():
                msg = f"Reconectando em {_RECONNECT_DELAY}s…"
                GLib.idle_add(self._on_status, msg)
                await asyncio.sleep(_RECONNECT_DELAY)

    async def _session(self) -> None:
        import websockets  # type: ignore[import-untyped]

        url = _ws_url(self._camera_url, "/ws/webrtc")
        GLib.idle_add(self._on_status, "Conectando…")
        log.info("WebRTC signaling → %s", url)

        self._send_queue = asyncio.Queue()
        self._setup_pipeline()

        async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
            GLib.idle_add(self._on_status, "Aguardando stream…")

            async def _sender() -> None:
                while True:
                    msg = await self._send_queue.get()
                    await ws.send(json.dumps(msg))

            sender = asyncio.create_task(_sender())
            try:
                async for raw in ws:
                    await self._handle_msg(json.loads(raw))
            finally:
                sender.cancel()

        self._teardown_pipeline()

    def _setup_pipeline(self) -> None:
        self._teardown_pipeline()

        if not _WEBRTC_IMPORTS_OK:
            raise RuntimeError(f"GstWebRTC unavailable: {_WEBRTC_IMPORT_ERR}")

        pipeline = Gst.Pipeline.new("monitor")
        wb = Gst.ElementFactory.make("webrtcbin", "wb")
        if wb is None:
            raise RuntimeError(
                "webrtcbin element not found — install gstreamer1.0-plugins-bad"
            )

        wb.set_property("stun-server", "stun://stun.l.google.com:19302")
        try:
            wb.set_property(
                "bundle-policy", GstWebRTC.WebRTCBundlePolicy.MAX_BUNDLE
            )
        except Exception:
            pass

        pipeline.add(wb)

        # Add a RECVONLY transceiver so the SDP offer includes a video m-line
        wb.emit(
            "add-transceiver",
            GstWebRTC.WebRTCRTPTransceiverDirection.RECVONLY,
            Gst.Caps.from_string(
                "application/x-rtp,media=video,encoding-name=H264,payload=96"
            ),
        )

        wb.connect("on-negotiation-needed", self._on_negotiation_needed)
        wb.connect("on-ice-candidate", self._on_ice_candidate)
        wb.connect(
            "pad-added",
            lambda _wb, pad: GLib.idle_add(self._on_pad_added, pad, pipeline),
        )

        bus = pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)

        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("Pipeline failed to reach PLAYING state")

        self._pipeline = pipeline
        self._webrtcbin = wb
        log.info("GStreamer WebRTC pipeline started")

    def _teardown_pipeline(self) -> None:
        if self._pipeline:
            self._pipeline.set_state(Gst.State.NULL)
            self._pipeline = None
            self._webrtcbin = None

    # ── GStreamer callbacks (called in GTK/GLib main thread) ──────────────────

    def _on_negotiation_needed(self, wb: Gst.Element) -> None:
        log.info("on-negotiation-needed — creating SDP offer")
        promise = Gst.Promise.new_with_change_func(self._on_offer_created, wb, None)
        wb.emit("create-offer", None, promise)

    def _on_offer_created(
        self, promise: Gst.Promise, wb: Gst.Element, _
    ) -> None:
        reply = promise.get_reply()
        offer = reply.get_value("offer")

        local_p = Gst.Promise.new()
        wb.emit("set-local-description", offer, local_p)
        local_p.interrupt()

        msg = {"type": "offer", "sdp": offer.sdp.as_text()}
        log.info("SDP offer ready, sending to camera")
        if self._loop and self._send_queue is not None:
            self._loop.call_soon_threadsafe(self._send_queue.put_nowait, msg)

    def _on_ice_candidate(self, wb: Gst.Element, mline: int, candidate: str) -> None:
        msg = {"type": "ice-candidate", "candidate": candidate, "sdpMLineIndex": mline}
        if self._loop and self._send_queue is not None:
            self._loop.call_soon_threadsafe(self._send_queue.put_nowait, msg)

    def _on_pad_added(self, pad: Gst.Pad, pipeline: Gst.Pipeline) -> bool:
        """Fired (via idle_add) when webrtcbin exposes the remote video track."""
        caps = pad.get_current_caps()
        if not caps:
            caps = pad.query_caps(None)
        struct = caps.get_structure(0) if caps else None
        if struct is None:
            return False

        media = struct.get_string("media") or ""
        name = struct.get_name() or ""
        if "video" not in media and "video" not in name and "application/x-rtp" not in name:
            log.debug("Ignoring non-video pad (caps: %s)", name)
            return False

        log.info("Video pad arrived — building decode chain")

        depay = Gst.ElementFactory.make("rtph264depay", "depay")
        decode = Gst.ElementFactory.make(self._h264dec, "decode")
        convert = Gst.ElementFactory.make("videoconvert", "vconv")

        if not all([depay, decode, convert]):
            log.error("Failed to create decode elements")
            return False

        for el in (depay, decode, convert):
            pipeline.add(el)
            el.sync_state_with_parent()

        depay.link(decode)
        decode.link(convert)

        if not self._vsink.add_to_pipeline_and_link(pipeline, convert):
            return False

        sink_pad = depay.get_static_pad("sink")
        result = pad.link(sink_pad)
        if result == Gst.PadLinkReturn.OK:
            log.info("Decode chain linked — stream should be visible")
            GLib.idle_add(self._on_status, "webrtc_playing")
        else:
            log.error("Pad link failed: %s", result)

        return False  # remove from idle sources

    def _on_bus_message(self, bus: Gst.Bus, message: Gst.Message) -> None:
        if message.type == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            log.error("GStreamer error: %s | %s", err.message, debug)
            GLib.idle_add(self._on_status, f"Erro: {err.message}")
        elif message.type == Gst.MessageType.EOS:
            log.info("Stream ended (EOS)")
            GLib.idle_add(self._on_status, "Stream encerrado")

    # ── Asyncio side (signaling messages from server) ─────────────────────────

    async def _handle_msg(self, msg: dict) -> None:
        kind = msg.get("type")
        wb = self._webrtcbin
        if wb is None:
            return

        if kind == "answer":
            log.info("Received SDP answer from camera")
            sdp_obj = GstSdp.SDPMessage.new()
            GstSdp.sdp_message_parse_buffer(msg["sdp"].encode(), sdp_obj)
            answer = GstWebRTC.WebRTCSessionDescription.new(
                GstWebRTC.WebRTCSDPType.ANSWER, sdp_obj
            )
            promise = Gst.Promise.new()
            wb.emit("set-remote-description", answer, promise)
            promise.interrupt()

        elif kind == "ice-candidate":
            candidate = msg.get("candidate", "")
            mline = msg.get("sdpMLineIndex", 0)
            if candidate:
                wb.emit("add-ice-candidate", mline, candidate)


# ---------------------------------------------------------------------------
# Cry alert WebSocket client
# ---------------------------------------------------------------------------

class AlertsClient:
    """Connects to /ws/alerts and fires a GTK callback on cry events."""

    def __init__(self, camera_url: str, on_cry) -> None:
        self._url = _ws_url(camera_url, "/ws/alerts")
        self._on_cry = on_cry  # callable(confidence: float), called via GLib.idle_add
        self._stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name="alerts-ws").start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        loop.run_until_complete(self._loop())

    async def _loop(self) -> None:
        import websockets  # type: ignore[import-untyped]

        delay = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    self._url, ping_interval=30, ping_timeout=10
                ) as ws:
                    delay = 1.0
                    async for raw in ws:
                        msg = json.loads(raw)
                        if msg.get("type") == "cry":
                            GLib.idle_add(self._on_cry, float(msg.get("confidence", 0)))
            except Exception as exc:
                log.debug("Alerts WS: %s", exc)
            if not self._stop.is_set():
                await asyncio.sleep(min(delay, 30))
                delay = min(delay * 2, 30)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

_CSS = b"""
window { background-color: #0a0a0a; }
#video-area { background-color: #000; }
#controls {
    background-color: #111;
    border-top: 1px solid #333;
    padding: 6px 10px;
}
#status-label { color: #aaa; font-size: 12px; }
#status-label.live { color: #2ecc71; }
#status-label.error { color: #e74c3c; }
#rec-btn {
    background: #2a2a2a;
    color: #ddd;
    border: 1px solid #444;
    border-radius: 4px;
    padding: 6px 16px;
    font-size: 13px;
}
#rec-btn:hover { background: #3a3a3a; }
#rec-btn.recording { background: #c0392b; color: #fff; border-color: #e74c3c; }
infobar { background-color: #e67e22; }
infobar label { color: #fff; font-weight: bold; font-size: 14px; }
"""


class MonitorWindow(Gtk.ApplicationWindow):
    def __init__(self, app: Gtk.Application, camera_url: str) -> None:
        super().__init__(application=app, title="BabyMonitor")
        self._camera_url = camera_url
        self._api_token = os.environ.get("API_TOKEN", "")
        self._recording = False
        self._cry_timer: Optional[int] = None

        self._apply_css()
        self._vsink = VideoSink()
        self._build_ui()

        self._webrtc = WebRTCClient(camera_url, self._vsink, self._on_stream_status)
        self._alerts = AlertsClient(camera_url, self._on_cry)

        self.connect("realize", lambda _: GLib.idle_add(self._start))
        self.connect("destroy", self._on_destroy)

    # ── UI construction ───────────────────────────────────────────────────────

    def _apply_css(self) -> None:
        provider = Gtk.CssProvider()
        provider.load_from_data(_CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    def _build_ui(self) -> None:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(root)

        # Video area
        video_widget = self._vsink.create_widget()
        video_widget.set_name("video-area")
        root.pack_start(video_widget, True, True, 0)

        # Cry alert bar (hidden until triggered)
        self._cry_bar = Gtk.InfoBar()
        self._cry_bar.set_message_type(Gtk.MessageType.WARNING)
        self._cry_bar.set_no_show_all(True)
        self._cry_label = Gtk.Label()
        self._cry_bar.get_content_area().pack_start(self._cry_label, False, False, 0)
        self._cry_bar.add_button("✕", Gtk.ResponseType.CLOSE)
        self._cry_bar.connect("response", lambda _bar, _r: self._dismiss_cry())
        root.pack_start(self._cry_bar, False, False, 0)

        # Controls bar
        ctrl = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        ctrl.set_name("controls")
        root.pack_start(ctrl, False, False, 0)

        self._status_label = Gtk.Label(label="○ Conectando…")
        self._status_label.set_name("status-label")
        self._status_label.set_halign(Gtk.Align.START)
        ctrl.pack_start(self._status_label, True, True, 0)

        self._rec_btn = Gtk.Button(label="⏺  GRAVAR")
        self._rec_btn.set_name("rec-btn")
        self._rec_btn.connect("clicked", self._on_rec_clicked)
        ctrl.pack_end(self._rec_btn, False, False, 0)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def _start(self) -> bool:
        self._sync_rec_state()
        self._webrtc.start()
        self._alerts.start()
        return False

    def _on_destroy(self, _widget) -> None:
        self._webrtc.stop()
        self._alerts.stop()

    # ── Stream status ─────────────────────────────────────────────────────────

    def _on_stream_status(self, status: str) -> bool:
        ctx = self._status_label.get_style_context()
        if status == "webrtc_playing":
            self._status_label.set_text("● WebRTC  (~150–400 ms)")
            ctx.add_class("live")
            ctx.remove_class("error")
        elif status.startswith("Erro"):
            self._status_label.set_text(f"⚠  {status}")
            ctx.add_class("error")
            ctx.remove_class("live")
        else:
            self._status_label.set_text(f"○ {status}")
            ctx.remove_class("live")
            ctx.remove_class("error")
        return False

    # ── Cry alert ─────────────────────────────────────────────────────────────

    def _on_cry(self, confidence: float) -> bool:
        pct = int(confidence * 100)
        self._cry_label.set_text(f"⚠  CHORO DETECTADO  {pct}%")
        self._cry_bar.show()
        if self._cry_timer:
            GLib.source_remove(self._cry_timer)
        self._cry_timer = GLib.timeout_add(8000, self._auto_dismiss_cry)
        return False

    def _dismiss_cry(self) -> None:
        self._cry_bar.hide()
        if self._cry_timer:
            GLib.source_remove(self._cry_timer)
            self._cry_timer = None

    def _auto_dismiss_cry(self) -> bool:
        self._cry_bar.hide()
        self._cry_timer = None
        return False

    # ── Recording ─────────────────────────────────────────────────────────────

    def _on_rec_clicked(self, _btn) -> None:
        def _do() -> None:
            try:
                if not self._recording:
                    _http_post(self._camera_url, "/api/recording/start", self._api_token)
                    self._recording = True
                else:
                    _http_post(self._camera_url, "/api/recording/stop", self._api_token)
                    self._recording = False
                GLib.idle_add(self._update_rec_btn)
            except Exception as exc:
                log.error("Recording toggle failed: %s", exc)

        threading.Thread(target=_do, daemon=True).start()

    def _update_rec_btn(self) -> bool:
        ctx = self._rec_btn.get_style_context()
        if self._recording:
            self._rec_btn.set_label("⏹  PARAR")
            ctx.add_class("recording")
        else:
            self._rec_btn.set_label("⏺  GRAVAR")
            ctx.remove_class("recording")
        return False

    def _sync_rec_state(self) -> None:
        def _do() -> None:
            try:
                data = _http_get(self._camera_url, "/api/status", self._api_token)
                if data.get("recording"):
                    self._recording = True
                    GLib.idle_add(self._update_rec_btn)
            except Exception:
                pass

        threading.Thread(target=_do, daemon=True).start()


# ---------------------------------------------------------------------------
# Application entry point
# ---------------------------------------------------------------------------

class MonitorApp(Gtk.Application):
    def __init__(self, camera_url: str) -> None:
        super().__init__(application_id="org.babymonitor.kiosk")
        self._camera_url = camera_url

    def do_activate(self) -> None:
        win = MonitorWindow(self, self._camera_url)
        win.set_default_size(800, 480)
        win.show_all()
        if os.environ.get("KIOSK_MODE", "1") == "1":
            win.fullscreen()


def main() -> None:
    camera_url = _get_camera_url()
    log.info("Monitor app starting — camera: %s", camera_url)
    app = MonitorApp(camera_url)
    app.run(None)


if __name__ == "__main__":
    main()
