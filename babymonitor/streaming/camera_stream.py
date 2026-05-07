from __future__ import annotations
import glob
import os
import threading
from babymonitor.common.config import StreamingConfig, RecordingsConfig
from babymonitor.common.logger import get_logger

log = get_logger(__name__)

try:
    import gi
    gi.require_version("Gst", "1.0")
    gi.require_version("GLib", "2.0")
    from gi.repository import Gst, GLib
    Gst.init(None)
    _GST_AVAILABLE = True
except (ImportError, ValueError) as _gst_err:
    _GST_AVAILABLE = False
    _GST_ERROR = (
        f"GStreamer Python bindings not available: {_gst_err}\n"
        "Install with: sudo apt-get install python3-gst-1.0 gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0"
    )


def _find_v4l2_device() -> str | None:
    """Return the first /dev/video* node that supports video capture, or None."""
    for path in sorted(glob.glob("/dev/video*")):
        try:
            # Check capabilities via ioctl VIDIOC_QUERYCAP (capability flag 0x1 = V4L2_CAP_VIDEO_CAPTURE)
            import fcntl, struct
            VIDIOC_QUERYCAP = 0x80685600
            with open(path, "rb") as f:
                buf = b"\x00" * 104
                result = fcntl.ioctl(f, VIDIOC_QUERYCAP, buf)
            caps = struct.unpack_from("<I", result, 20)[0]
            if caps & 0x1:  # V4L2_CAP_VIDEO_CAPTURE
                log.info("Found V4L2 capture device: %s", path)
                return path
        except Exception:
            continue
    return None


def _detect_camera_source(video_device: str = "") -> tuple[str, str]:
    """
    Return (source_type, device_path).

    source_type is one of: "libcamerasrc", "v4l2src"
    device_path is the /dev/videoX path (empty string for libcamerasrc).

    Priority:
    1. libcamerasrc  — Pi Camera Module via libcamera
    2. video_device  — explicit path from config if set
    3. auto-scan     — first /dev/video* that supports capture
    """
    if Gst.ElementFactory.find("libcamerasrc"):
        log.info("Using libcamerasrc (Pi Camera Module)")
        return "libcamerasrc", ""

    log.warning(
        "libcamerasrc not found; looking for a V4L2 webcam. "
        "To use a Pi Camera Module install: sudo apt-get install gstreamer1.0-libcamera"
    )

    device = video_device.strip() if video_device else ""

    if device:
        if os.path.exists(device):
            log.info("Using configured V4L2 device: %s", device)
            return "v4l2src", device
        log.warning("Configured video_device '%s' does not exist; auto-detecting.", device)

    detected = _find_v4l2_device()
    if detected:
        return "v4l2src", detected

    log.warning("No V4L2 capture device found; defaulting to /dev/video0")
    return "v4l2src", "/dev/video0"


def _detect_h264_encoders() -> list[str]:
    """Return all available H.264 encoders in priority order."""
    available = [enc for enc in ("v4l2h264enc", "x264enc", "openh264enc") if Gst.ElementFactory.find(enc)]
    if not available:
        raise RuntimeError(
            "No H.264 encoder found. Install one of: "
            "gstreamer1.0-plugins-good (x264enc) or gstreamer1.0-plugins-ugly"
        )
    log.info("Available H.264 encoders: %s", available)
    return available


def _webcam_input_caps(device: str, width: int, height: int, framerate: int) -> str:
    """
    Build GStreamer source + caps string for a V4L2 webcam.

    Webcams expose either raw (YUYV/NV12) or MJPEG frames. We probe for MJPEG
    first (higher resolution, less USB bandwidth) and fall back to raw.
    """
    mjpeg_probe = (
        f"v4l2src device={device} "
        f"! image/jpeg,width={width},height={height},framerate={framerate}/1 "
        f"! jpegdec "
        f"! videoconvert "
        f"! videoscale "
        f"! videorate "
        f"! video/x-raw,width={width},height={height},framerate={framerate}/1"
    )
    raw_probe = (
        f"v4l2src device={device} "
        f"! videoconvert "
        f"! videoscale "
        f"! videorate "
        f"! video/x-raw,width={width},height={height},framerate={framerate}/1"
    )

    # Check if device exposes MJPEG capability via v4l2-ctl / sysfs
    # We use GStreamer's device monitor as a lightweight probe.
    try:
        monitor = Gst.DeviceMonitor.new()
        monitor.add_filter("Video/Source", None)
        monitor.start()
        devices = monitor.get_devices()
        monitor.stop()
        for dev in devices:
            props = dev.get_properties()
            if props and device in (props.get_string("device.path") or ""):
                caps = dev.get_caps()
                if caps:
                    caps_str = caps.to_string()
                    if "image/jpeg" in caps_str:
                        log.info("Webcam %s supports MJPEG — using jpegdec path", device)
                        return mjpeg_probe
    except Exception as exc:
        log.debug("Device monitor probe failed (%s); defaulting to raw path", exc)

    log.info("Webcam %s: using raw YUV path", device)
    return raw_probe


class CameraStream:
    """
    GStreamer pipeline:
      [libcamerasrc | v4l2src] → videoconvert → [clockoverlay] → tee
        branch A: <h264enc> → h264parse → hlssink2           (live HLS)
        branch B: <h264enc> → h264parse → mp4mux → filesink  (recording, dynamic)

    Webcam support:
      - Auto-detects libcamerasrc (Pi Camera Module) vs. V4L2 (USB webcam)
      - Scans /dev/video* for a capture-capable device when no explicit path is configured
      - Handles MJPEG-outputting webcams transparently via jpegdec
      - Falls back from v4l2h264enc to x264enc / openh264enc when hardware encoder absent
    """

    def __init__(self, streaming: StreamingConfig, recordings: RecordingsConfig):
        if not _GST_AVAILABLE:
            raise RuntimeError(_GST_ERROR)
        self._scfg = streaming
        self._rcfg = recordings
        self._camera_src, self._device = _detect_camera_source(streaming.video_device)
        self._encoder_candidates = _detect_h264_encoders()
        self._encoder = self._encoder_candidates[0]
        self._pipeline: Gst.Pipeline | None = None
        self._tee: Gst.Element | None = None
        self._loop: GLib.MainLoop | None = None
        self._thread: threading.Thread | None = None
        self._rec_elements: list[Gst.Element] = []
        self._rec_lock = threading.Lock()
        self._pipeline_running = False
        self._pipeline_error: str | None = None
        os.makedirs(streaming.hls_dir, exist_ok=True)
        os.makedirs(recordings.output_dir, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._loop = GLib.MainLoop()
        self._thread = threading.Thread(target=self._loop.run, daemon=True)
        self._thread.start()
        self._launch_pipeline()

    def _launch_pipeline(self) -> None:
        pipeline_str = self._build_pipeline()
        log.info(
            "Launching GStreamer pipeline (encoder=%s):\n%s",
            self._encoder, pipeline_str,
        )
        self._pipeline = Gst.parse_launch(pipeline_str)
        self._tee = self._pipeline.get_by_name("t")

        bus = self._pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)

        ret = self._pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            self._pipeline_error = "Pipeline failed to enter PLAYING state"
            log.error(self._pipeline_error)
        else:
            self._pipeline_running = True
            log.info(
                "Camera stream started — source=%s device=%s encoder=%s hls_dir=%s",
                self._camera_src, self._device or "N/A", self._encoder, self._scfg.hls_dir,
            )

    def stop(self) -> None:
        if self._pipeline:
            self._pipeline.set_state(Gst.State.NULL)
        if self._loop:
            self._loop.quit()
        self._pipeline_running = False
        log.info("Camera stream stopped")

    # ------------------------------------------------------------------
    # Pipeline construction
    # ------------------------------------------------------------------

    def _build_pipeline(self) -> str:
        w, h, fps = self._scfg.width, self._scfg.height, self._scfg.framerate
        hls_dir = self._scfg.hls_dir
        dur = self._scfg.hls_target_duration
        maxf = self._scfg.hls_max_files

        if self._camera_src == "libcamerasrc":
            src_str = (
                f"libcamerasrc "
                f"! video/x-raw,width={w},height={h},framerate={fps}/1 "
                f"! videoconvert"
            )
        else:
            src_str = _webcam_input_caps(self._device, w, h, fps)

        enc_opts = (
            'extra-controls="controls,repeat_sequence_header=1,h264_i_frame_period=30"'
            if self._encoder == "v4l2h264enc"
            else "tune=zerolatency speed-preset=ultrafast key-int-max=30 bitrate=1500"
            if self._encoder == "x264enc"
            else ""
        )

        overlay_str = (
            "! clockoverlay "
            'time-format="%d/%m/%Y %H:%M:%S" '
            "halignment=right valignment=bottom "
            'font-desc="Sans Bold 14" '
            "shaded-background=true "
            if self._scfg.timestamp_overlay
            else ""
        )

        return (
            f"{src_str} "
            f"{overlay_str}"
            f"! tee name=t "
            f"  t. ! queue max-size-buffers=4 max-size-bytes=0 max-size-time=0 leaky=downstream "
            f"       ! {self._encoder} {enc_opts} "
            f"       ! h264parse "
            f"       ! hlssink2 name=hlssink "
            f"           target-duration={dur} "
            f"           max-files={maxf} "
            f"           send-keyframe-requests=true "
            f"           location={hls_dir}/seg%05d.ts "
            f"           playlist-location={hls_dir}/live.m3u8 "
            f"           playlist-root=. "
        )

    # ------------------------------------------------------------------
    # Dynamic recording branch
    # ------------------------------------------------------------------

    def start_recording(self, filepath: str) -> bool:
        with self._rec_lock:
            if self._rec_elements:
                log.warning("Already recording")
                return False
            if not self._tee or not self._pipeline:
                return False

            queue = Gst.ElementFactory.make("queue", "rec_queue")
            enc = Gst.ElementFactory.make(self._encoder, "rec_enc")
            parse = Gst.ElementFactory.make("h264parse", "rec_parse")
            mux = Gst.ElementFactory.make("mp4mux", "rec_mux")
            sink = Gst.ElementFactory.make("filesink", "rec_sink")

            if not all([queue, enc, parse, mux, sink]):
                log.error("Failed to create recording elements")
                return False

            sink.set_property("location", filepath)
            queue.set_property("max-size-buffers", 200)
            queue.set_property("leaky", 2)  # downstream

            if self._encoder == "v4l2h264enc":
                enc.set_property("extra-controls", Gst.Structure.new_from_string(
                    "controls,repeat_sequence_header=1"
                ))
            elif self._encoder == "x264enc":
                enc.set_property("tune", 4)  # zerolatency

            elements = [queue, enc, parse, mux, sink]
            for el in elements:
                self._pipeline.add(el)
            for el in elements:
                el.sync_state_with_parent()

            queue.link(enc)
            enc.link(parse)
            parse.link(mux)
            mux.link(sink)

            tee_src = self._tee.get_request_pad("src_%u")
            queue_sink = queue.get_static_pad("sink")
            if tee_src and queue_sink:
                tee_src.link(queue_sink)

            self._rec_elements = elements
            self._rec_elements.append(tee_src)
            log.info("Recording started: %s", filepath)
            return True

    def stop_recording(self) -> None:
        with self._rec_lock:
            if not self._rec_elements:
                return
            tee_src = self._rec_elements[-1]
            if isinstance(tee_src, Gst.Pad):
                tee_src.send_event(Gst.Event.new_eos())
                import time; time.sleep(0.5)
                self._tee.release_request_pad(tee_src)

            for el in self._rec_elements[:-1]:
                el.set_state(Gst.State.NULL)
                self._pipeline.remove(el)

            self._rec_elements = []
            log.info("Recording stopped")

    # ------------------------------------------------------------------
    # Bus messages & encoder fallback
    # ------------------------------------------------------------------

    def _on_bus_message(self, bus: Gst.Bus, message: Gst.Message) -> None:
        if message.type == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            log.error("GStreamer pipeline error: %s\nDebug: %s", err, debug)
            if self._try_next_encoder():
                GLib.idle_add(self._restart_pipeline)
                return
            self._pipeline_error = str(err)
            self._pipeline_running = False
            if self._loop:
                self._loop.quit()
        elif message.type == Gst.MessageType.EOS:
            self._pipeline_running = False
            log.info("GStreamer EOS")

    def _try_next_encoder(self) -> bool:
        """Advance to the next encoder candidate. Returns True if one is available."""
        try:
            idx = self._encoder_candidates.index(self._encoder)
        except ValueError:
            return False
        if idx + 1 < len(self._encoder_candidates):
            old = self._encoder
            self._encoder = self._encoder_candidates[idx + 1]
            log.warning("Encoder %s failed — retrying with %s", old, self._encoder)
            return True
        return False

    def _restart_pipeline(self) -> bool:
        """Tear down the current pipeline and relaunch with the updated encoder. GLib idle callback."""
        self._pipeline_running = False
        if self._pipeline:
            self._pipeline.set_state(Gst.State.NULL)
            bus = self._pipeline.get_bus()
            bus.remove_signal_watch()
            self._pipeline = None
            self._tee = None
        self._launch_pipeline()
        return False  # remove from GLib idle sources
