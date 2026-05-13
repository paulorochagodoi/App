"""RTSP streaming server via GStreamer's gst-rtsp-server.

Serves the live H.264 stream at rtsp://<host>:<port>/live.
Compatible with VLC, ffplay, and any standard RTSP client.

Required packages (beyond base GStreamer):
    sudo apt-get install libgstrtspserver-1.0-0 gir1.2-gst-rtsp-server-1.0
"""
from __future__ import annotations
import threading
from babymonitor.common.logger import get_logger

log = get_logger(__name__)

_RTSP_AVAILABLE = False
_RTSP_UNAVAILABLE_REASON: str | None = None

try:
    import gi
    gi.require_version("Gst", "1.0")
    gi.require_version("GstRtspServer", "1.0")
    from gi.repository import Gst, GstRtspServer  # noqa: F401 — presence check
    _RTSP_AVAILABLE = True
except (ImportError, ValueError, OSError) as _exc:
    _RTSP_UNAVAILABLE_REASON = str(_exc)
    log.warning(
        "RTSP unavailable: %s\n"
        "  Install missing packages with:\n"
        "    sudo apt-get install libgstrtspserver-1.0-0 gir1.2-gst-rtsp-server-1.0",
        _exc,
    )


class RtspServer:
    """
    GstRtspServer wrapper that re-streams the live H.264 feed over RTSP.

    Architecture:
      Main GStreamer pipeline (enct tee)
        → queue (leaky) → appsink
      GstRtspServer media pipeline (shared, one instance for all RTSP clients)
        appsrc → h264parse → rtph264pay name=pay0

    push_sample() is called by the main pipeline's appsink new-sample callback
    and forwards the H.264 buffer to the RTSP appsrc.

    Usage:
      server = RtspServer(port=8554, path="/live")
      stream.attach_rtsp_server(server)   # wires appsink → push_sample
      server.start()                       # attaches to the GLib main context
    """

    def __init__(self, port: int = 8554, path: str = "/live", on_client_connect=None) -> None:
        if not _RTSP_AVAILABLE:
            raise RuntimeError(
                f"GstRtspServer not available: {_RTSP_UNAVAILABLE_REASON}\n"
                "Install with:\n"
                "  sudo apt-get install libgstrtspserver-1.0-0 gir1.2-gst-rtsp-server-1.0"
            )

        self._port = port
        self._path = path
        self._appsrc: "Gst.Element | None" = None
        self._lock = threading.Lock()
        # Optional callback invoked when the first RTSP client configures the
        # media pipeline — use it to request a keyframe so VLC gets a valid
        # IDR frame immediately instead of waiting for the next one.
        self._on_client_connect = on_client_connect

        from gi.repository import GstRtspServer as _GstRtspServer
        self._server = _GstRtspServer.RTSPServer.new()
        self._server.set_service(str(port))

        self._factory = _GstRtspServer.RTSPMediaFactory.new()
        # Shared factory: all RTSP clients reuse one pipeline instance.
        #
        # The "! video/x-h264 !" between appsrc and h264parse is a standard
        # GStreamer capsfilter element (not a property assignment).  It tells
        # GstRtspServer that this stream is H.264 video BEFORE any sample is
        # pushed, so the server can generate a valid SDP and VLC knows the
        # codec up front.  h264parse auto-detects the exact stream-format from
        # the first buffer (byte-stream vs. avc) so any H.264 encoder works.
        #
        # do-timestamp=true: timestamps come from the RTSP pipeline's clock,
        # avoiding mismatches with the main pipeline's clock domain.
        #
        # h264parse config-interval=-1: inject SPS/PPS before every IDR frame
        # so a newly joining VLC client can decode without waiting.
        self._factory.set_launch(
            "( appsrc name=src format=3 is-live=true do-timestamp=true block=false"
            " ! video/x-h264"
            " ! h264parse config-interval=-1"
            " ! rtph264pay name=pay0 pt=96 config-interval=-1 )"
        )
        self._factory.set_shared(True)
        self._factory.connect("media-configure", self._on_media_configure)

        mounts = self._server.get_mount_points()
        mounts.add_factory(path, self._factory)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Attach the RTSP server to the default GLib main context."""
        source_id = self._server.attach(None)
        if source_id == 0:
            log.error(
                "RTSP server failed to attach to GLib main context "
                "(port %d may already be in use)", self._port
            )
            return
        log.info(
            "RTSP server started — rtsp://0.0.0.0:%d%s",
            self._port, self._path,
        )

    def stop(self) -> None:
        with self._lock:
            self._appsrc = None
        log.info("RTSP server stopped")

    def push_sample(self, sample: object) -> None:
        """Push a GstSample (pulled from the main pipeline's appsink) to RTSP clients."""
        with self._lock:
            src = self._appsrc
        if src is None:
            return
        from gi.repository import Gst as _Gst
        ret = src.emit("push-sample", sample)
        if ret not in (_Gst.FlowReturn.OK, _Gst.FlowReturn.FLUSHING):
            log.debug("RTSP appsrc push-sample returned: %s", ret)

    @property
    def port(self) -> int:
        return self._port

    @property
    def path(self) -> str:
        return self._path

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _on_media_configure(self, factory, media) -> None:
        pipeline = media.get_element()
        appsrc = pipeline.get_by_name("src")
        if appsrc is None:
            log.error("RTSP media configured but appsrc element not found — check factory launch string")
            return
        with self._lock:
            self._appsrc = appsrc
        log.info("RTSP client connected — appsrc ready (port %d)", self._port)
        if self._on_client_connect:
            try:
                self._on_client_connect()
            except Exception as exc:
                log.debug("on_client_connect callback raised: %s", exc)
