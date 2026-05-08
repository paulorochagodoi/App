from __future__ import annotations
import glob
import os
import threading
from babymonitor.common.config import StreamingConfig
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
    for path in sorted(glob.glob("/dev/video*")):
        try:
            import fcntl, struct
            VIDIOC_QUERYCAP = 0x80685600
            with open(path, "rb") as f:
                buf = b"\x00" * 104
                result = fcntl.ioctl(f, VIDIOC_QUERYCAP, buf)
            caps = struct.unpack_from("<I", result, 20)[0]
            if caps & 0x1:
                log.info("Found V4L2 capture device: %s", path)
                return path
        except Exception:
            continue
    return None


def _detect_camera_source(video_device: str = "") -> tuple[str, str]:
    if Gst.ElementFactory.find("libcamerasrc"):
        log.info("Using libcamerasrc (Pi Camera Module)")
        return "libcamerasrc", ""

    log.warning("libcamerasrc not found; looking for a V4L2 webcam.")
    device = video_device.strip() if video_device else ""

    if device:
        if os.path.exists(device):
            return "v4l2src", device
        log.warning("Configured video_device '%s' does not exist; auto-detecting.", device)

    detected = _find_v4l2_device()
    if detected:
        return "v4l2src", detected

    log.warning("No V4L2 capture device found; defaulting to /dev/video0")
    return "v4l2src", "/dev/video0"


def _detect_h264_encoders() -> list[str]:
    available = [enc for enc in ("v4l2h264enc", "x264enc", "openh264enc") if Gst.ElementFactory.find(enc)]
    if not available:
        raise RuntimeError(
            "No H.264 encoder found. Install one of: "
            "gstreamer1.0-plugins-good (x264enc) or gstreamer1.0-plugins-ugly"
        )
    log.info("Available H.264 encoders: %s", available)
    return available


def _detect_audio_source(audio_device: str = "") -> str | None:
    if audio_device:
        if Gst.ElementFactory.find("alsasrc"):
            log.info("Using configured ALSA audio device: %s", audio_device)
            return f"alsasrc device={audio_device}"
        log.warning("alsasrc not found; ignoring configured audio_device '%s'", audio_device)

    for src in ("pulsesrc", "alsasrc", "autoaudiosrc"):
        if Gst.ElementFactory.find(src):
            log.info("Using audio source: %s", src)
            return src

    log.warning("No audio source found; stream will have no audio")
    return None


def _detect_aac_encoder() -> str | None:
    for enc in ("avenc_aac", "voaacenc", "faac"):
        if Gst.ElementFactory.find(enc):
            log.info("Using AAC encoder: %s", enc)
            return enc
    log.warning("No AAC encoder found; stream will have no audio.")
    return None


def _webcam_input_caps(device: str, width: int, height: int, framerate: int) -> str:
    mjpeg_probe = (
        f"v4l2src device={device} "
        f"! image/jpeg,width={width},height={height},framerate={framerate}/1 "
        f"! jpegdec ! videoconvert ! videoscale ! videorate "
        f"! video/x-raw,width={width},height={height},framerate={framerate}/1"
    )
    raw_probe = (
        f"v4l2src device={device} "
        f"! videoconvert ! videoscale ! videorate "
        f"! video/x-raw,width={width},height={height},framerate={framerate}/1"
    )

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
                if caps and "image/jpeg" in caps.to_string():
                    log.info("Webcam %s supports MJPEG — using jpegdec path", device)
                    return mjpeg_probe
    except Exception as exc:
        log.debug("Device monitor probe failed (%s); defaulting to raw path", exc)

    log.info("Webcam %s: using raw YUV path", device)
    return raw_probe


class CameraStream:
    """
    Pipeline: [libcamerasrc | v4l2src] → encoder → tee name=enct
      enct. → h264parse → hlssink2  (HLS)
      enct. → rtph264pay → webrtcbin (WebRTC peers, dynamic)
    """

    def __init__(self, streaming: StreamingConfig):
        if not _GST_AVAILABLE:
            raise RuntimeError(_GST_ERROR)
        self._scfg = streaming
        self._camera_src, self._device = _detect_camera_source(streaming.video_device)
        self._encoder_candidates = _detect_h264_encoders()
        self._encoder = self._encoder_candidates[0]
        self._audio_src = _detect_audio_source(streaming.audio_device)
        self._audio_encoder = _detect_aac_encoder() if self._audio_src else None
        self._pipeline: Gst.Pipeline | None = None
        self._enctee: Gst.Element | None = None
        self._loop: GLib.MainLoop | None = None
        self._thread: threading.Thread | None = None
        self._webrtc_peers: dict[str, dict] = {}
        self._webrtc_lock = threading.Lock()
        self._pipeline_running = False
        self._pipeline_error: str | None = None
        os.makedirs(streaming.hls_dir, exist_ok=True)

    def start(self) -> None:
        self._loop = GLib.MainLoop()
        self._thread = threading.Thread(target=self._loop.run, daemon=True)
        self._thread.start()
        self._launch_pipeline()

    def stop(self) -> None:
        if self._pipeline:
            self._pipeline.set_state(Gst.State.NULL)
        if self._loop:
            self._loop.quit()
        self._pipeline_running = False
        log.info("Camera stream stopped")

    def _launch_pipeline(self) -> None:
        pipeline_str = self._build_pipeline()
        log.info("Launching GStreamer pipeline (encoder=%s):\n%s", self._encoder, pipeline_str)
        try:
            self._pipeline = Gst.parse_launch(pipeline_str)
        except GLib.Error as exc:
            if self._audio_src and self._audio_encoder:
                log.warning("Pipeline with audio failed (%s) — falling back to video-only", exc)
                self._audio_src = None
                self._audio_encoder = None
                pipeline_str = self._build_pipeline()
                self._pipeline = Gst.parse_launch(pipeline_str)
            else:
                self._pipeline_error = f"Failed to build GStreamer pipeline: {exc}"
                log.error(self._pipeline_error)
                return

        self._enctee = self._pipeline.get_by_name("enct")
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
                "Camera stream started — source=%s device=%s encoder=%s audio=%s hls_dir=%s",
                self._camera_src, self._device or "N/A", self._encoder,
                self._audio_src or "none", self._scfg.hls_dir,
            )

    def _build_pipeline(self) -> str:
        w, h, fps = self._scfg.width, self._scfg.height, self._scfg.framerate
        hls_dir = self._scfg.hls_dir
        key_int = fps

        if self._camera_src == "libcamerasrc":
            src_str = (
                f"libcamerasrc "
                f"! video/x-raw,width={w},height={h},framerate={fps}/1 "
                f"! videoconvert"
            )
        else:
            src_str = _webcam_input_caps(self._device, w, h, fps)

        enc_opts = (
            f'extra-controls="controls,repeat_sequence_header=1,h264_i_frame_period={key_int}"'
            if self._encoder == "v4l2h264enc"
            else f"tune=zerolatency speed-preset=ultrafast key-int-max={key_int} bitrate={self._scfg.video_bitrate}"
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

        audio_branch = ""
        if self._audio_src and self._audio_encoder:
            audio_enc_opts = "bitrate=128000" if self._audio_encoder == "avenc_aac" else ""
            audio_branch = (
                f"  {self._audio_src} "
                f"! audioconvert ! audioresample "
                f"! audio/x-raw,rate=44100,channels=1 "
                f"! {self._audio_encoder} {audio_enc_opts} "
                f"! aacparse ! hlssink.audio_0 "
            )

        return (
            f"{src_str} "
            f"{overlay_str}"
            f"! queue max-size-buffers=4 max-size-bytes=0 max-size-time=0 leaky=downstream "
            f"! {self._encoder} {enc_opts} "
            f"! tee name=enct "
            f"enct. ! h264parse "
            f"       ! hlssink2 name=hlssink "
            f"           target-duration={self._scfg.hls_target_duration} "
            f"           max-files={self._scfg.hls_max_files} "
            f"           send-keyframe-requests=true "
            f"           location={hls_dir}/seg%05d.ts "
            f"           playlist-location={hls_dir}/live.m3u8 "
            f"           playlist-root=. "
            f"{audio_branch}"
        )

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
        self._pipeline_running = False
        with self._webrtc_lock:
            peer_ids = list(self._webrtc_peers.keys())
        for pid in peer_ids:
            self.remove_webrtc_peer(pid)
        if self._pipeline:
            self._pipeline.set_state(Gst.State.NULL)
            self._pipeline.get_bus().remove_signal_watch()
            self._pipeline = None
            self._enctee = None
        self._launch_pipeline()
        return False

    def add_webrtc_peer(self, peer_id: str, loop, on_send) -> "object | None":
        from babymonitor.streaming.webrtc_stream import WebRTCPeer, STUN_SERVER, _WEBRTC_AVAILABLE
        if not _WEBRTC_AVAILABLE:
            log.warning("webrtcbin not available — WebRTC peer rejected")
            return None
        if not self._enctee or not self._pipeline:
            log.warning("Pipeline not ready for WebRTC peer %s", peer_id)
            return None

        from gi.repository import GstWebRTC

        queue = Gst.ElementFactory.make("queue",      f"wrtc_q_{peer_id}")
        pay   = Gst.ElementFactory.make("rtph264pay", f"wrtc_pay_{peer_id}")
        cf    = Gst.ElementFactory.make("capsfilter", f"wrtc_cf_{peer_id}")
        wb    = Gst.ElementFactory.make("webrtcbin",  f"wrtc_{peer_id}")

        if not all([queue, pay, cf, wb]):
            log.error("Failed to create WebRTC elements for peer %s", peer_id)
            return None

        pay.set_property("config-interval", -1)
        try:
            pay.set_property("aggregate-mode", 1)
        except Exception:
            pass

        cf.set_property("caps", Gst.Caps.from_string(
            "application/x-rtp,media=video,encoding-name=H264,payload=96"
        ))
        wb.set_property("stun-server", STUN_SERVER)
        try:
            wb.set_property("bundle-policy", GstWebRTC.WebRTCBundlePolicy.MAX_BUNDLE)
        except Exception:
            pass

        for el in [queue, pay, cf, wb]:
            self._pipeline.add(el)
            el.sync_state_with_parent()

        queue.link(pay)
        pay.link(cf)
        cf.link(wb)

        tee_pad = self._enctee.get_request_pad("src_%u")
        if tee_pad:
            tee_pad.link(queue.get_static_pad("sink"))

        with self._webrtc_lock:
            self._webrtc_peers[peer_id] = {"elements": [queue, pay, cf, wb], "tee_pad": tee_pad}

        log.info("WebRTC peer added: %s", peer_id)
        return WebRTCPeer(peer_id, wb, loop, on_send)

    def remove_webrtc_peer(self, peer_id: str) -> None:
        with self._webrtc_lock:
            data = self._webrtc_peers.pop(peer_id, None)
        if not data:
            return

        tee_pad = data["tee_pad"]
        if tee_pad and self._enctee:
            tee_pad.send_event(Gst.Event.new_eos())
            import time as _time; _time.sleep(0.1)
            self._enctee.release_request_pad(tee_pad)

        for el in data["elements"]:
            el.set_state(Gst.State.NULL)
            if self._pipeline:
                self._pipeline.remove(el)

        log.info("WebRTC peer removed: %s", peer_id)
