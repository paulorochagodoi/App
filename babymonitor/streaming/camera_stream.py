from __future__ import annotations
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


class CameraStream:
    """
    GStreamer pipeline:
      libcamerasrc → videoconvert → tee
        branch A: v4l2h264enc → h264parse → hlssink2   (live HLS)
        branch B: v4l2h264enc → h264parse → mp4mux → filesink  (recording, dynamic)
    """

    def __init__(self, streaming: StreamingConfig, recordings: RecordingsConfig):
        if not _GST_AVAILABLE:
            raise RuntimeError(_GST_ERROR)
        self._scfg = streaming
        self._rcfg = recordings
        self._pipeline: Gst.Pipeline | None = None
        self._tee: Gst.Element | None = None
        self._loop: GLib.MainLoop | None = None
        self._thread: threading.Thread | None = None
        self._rec_elements: list[Gst.Element] = []
        self._rec_lock = threading.Lock()
        os.makedirs(streaming.hls_dir, exist_ok=True)
        os.makedirs(recordings.output_dir, exist_ok=True)

    def start(self) -> None:
        pipeline_str = self._build_pipeline()
        log.info("Launching GStreamer pipeline:\n%s", pipeline_str)
        self._pipeline = Gst.parse_launch(pipeline_str)
        self._tee = self._pipeline.get_by_name("t")

        bus = self._pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)

        self._pipeline.set_state(Gst.State.PLAYING)
        self._loop = GLib.MainLoop()
        self._thread = threading.Thread(target=self._loop.run, daemon=True)
        self._thread.start()
        log.info("Camera stream started (HLS → %s)", self._scfg.hls_dir)

    def stop(self) -> None:
        if self._pipeline:
            self._pipeline.set_state(Gst.State.NULL)
        if self._loop:
            self._loop.quit()
        log.info("Camera stream stopped")

    def _build_pipeline(self) -> str:
        w, h, fps = self._scfg.width, self._scfg.height, self._scfg.framerate
        hls_dir = self._scfg.hls_dir
        dur = self._scfg.hls_target_duration
        maxf = self._scfg.hls_max_files

        return (
            f"libcamerasrc "
            f"! video/x-raw,width={w},height={h},framerate={fps}/1 "
            f"! videoconvert "
            f"! tee name=t "
            f"  t. ! queue max-size-buffers=200 leaky=downstream "
            f"       ! v4l2h264enc extra-controls=\"controls,repeat_sequence_header=1\" "
            f"       ! h264parse "
            f"       ! hlssink2 name=hlssink "
            f"           target-duration={dur} "
            f"           max-files={maxf} "
            f"           location={hls_dir}/seg%05d.ts "
            f"           playlist-location={hls_dir}/live.m3u8 "
            f"           playlist-root=. "
        )

    def start_recording(self, filepath: str) -> bool:
        with self._rec_lock:
            if self._rec_elements:
                log.warning("Already recording")
                return False
            if not self._tee or not self._pipeline:
                return False

            queue = Gst.ElementFactory.make("queue", "rec_queue")
            enc = Gst.ElementFactory.make("v4l2h264enc", "rec_enc")
            parse = Gst.ElementFactory.make("h264parse", "rec_parse")
            mux = Gst.ElementFactory.make("mp4mux", "rec_mux")
            sink = Gst.ElementFactory.make("filesink", "rec_sink")

            if not all([queue, enc, parse, mux, sink]):
                log.error("Failed to create recording elements")
                return False

            sink.set_property("location", filepath)
            queue.set_property("max-size-buffers", 200)
            queue.set_property("leaky", 2)  # downstream

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

    def _on_bus_message(self, bus: Gst.Bus, message: Gst.Message) -> None:
        if message.type == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            log.error("GStreamer error: %s — %s", err, debug)
            if self._loop:
                self._loop.quit()
        elif message.type == Gst.MessageType.EOS:
            log.info("GStreamer EOS")
