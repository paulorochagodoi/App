from __future__ import annotations
import asyncio
from babymonitor.common.logger import get_logger

log = get_logger(__name__)

STUN_SERVER = "stun://stun.l.google.com:19302"

_WEBRTC_UNAVAILABLE_REASON: "str | None" = None

try:
    import gi
    gi.require_version("Gst", "1.0")
    gi.require_version("GstWebRTC", "1.0")
    gi.require_version("GstSdp", "1.0")
    from gi.repository import Gst, GstWebRTC, GstSdp
    if Gst.ElementFactory.find("webrtcbin"):
        _WEBRTC_AVAILABLE = True
    else:
        _WEBRTC_AVAILABLE = False
        _WEBRTC_UNAVAILABLE_REASON = (
            "GStreamer element 'webrtcbin' not found — "
            "install gstreamer1.0-plugins-bad"
        )
        log.warning("WebRTC unavailable: %s", _WEBRTC_UNAVAILABLE_REASON)
except (ImportError, ValueError, OSError, RuntimeError) as _exc:
    _WEBRTC_AVAILABLE = False
    _WEBRTC_UNAVAILABLE_REASON = str(_exc)
    log.warning("WebRTC unavailable: %s", _exc)


class WebRTCPeer:
    """
    Wraps a single GStreamer webrtcbin element.

    The signaling flow (browser initiates):
      browser → offer (SDP) → set_offer()
      webrtcbin creates answer → on_send({'type':'answer','sdp':...})
      ICE candidates exchanged bidirectionally
    """

    def __init__(
        self,
        peer_id: str,
        webrtcbin: "Gst.Element",
        loop: asyncio.AbstractEventLoop,
        on_send,   # async callable(dict) — sends a JSON message to the browser
    ):
        self.peer_id = peer_id
        self._wb = webrtcbin
        self._loop = loop
        self._on_send = on_send
        self._wb.connect("on-ice-candidate", self._on_ice_candidate)

    # ── Called from the asyncio task (via WebSocket message) ─────────────────

    def set_offer(self, sdp_text: str) -> None:
        sdp = GstSdp.SDPMessage.new()
        GstSdp.sdp_message_parse_buffer(sdp_text.encode(), sdp)
        offer = GstWebRTC.WebRTCSessionDescription.new(
            GstWebRTC.WebRTCSDPType.OFFER, sdp
        )
        promise = Gst.Promise.new_with_change_func(self._on_offer_set, None)
        self._wb.emit("set-remote-description", offer, promise)

    def add_ice_candidate(self, sdp_mline_index: int, candidate: str) -> None:
        self._wb.emit("add-ice-candidate", sdp_mline_index, candidate)

    # ── GLib callbacks (called from GStreamer main loop thread) ───────────────

    def _on_offer_set(self, promise, _):
        promise = Gst.Promise.new_with_change_func(self._on_answer_created, None)
        self._wb.emit("create-answer", None, promise)

    def _on_answer_created(self, promise, _):
        reply = promise.get_reply()
        answer = reply.get_value("answer")
        local_promise = Gst.Promise.new()
        self._wb.emit("set-local-description", answer, local_promise)
        local_promise.interrupt()
        self._send({"type": "answer", "sdp": answer.sdp.as_text()})

    def _on_ice_candidate(self, _, mlineindex: int, candidate: str):
        self._send({"type": "ice-candidate", "candidate": candidate,
                    "sdpMLineIndex": mlineindex})

    def _send(self, msg: dict) -> None:
        asyncio.run_coroutine_threadsafe(self._on_send(msg), self._loop)
