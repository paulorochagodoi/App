from __future__ import annotations
import os
from datetime import datetime
from babymonitor.common.logger import get_logger

log = get_logger(__name__)


class Recorder:
    def __init__(self, stream, output_dir: str):
        self._stream = stream
        self._output_dir = output_dir
        self._current_file: str | None = None

    def start(self) -> str | None:
        if self._current_file:
            log.warning("Already recording")
            return self._current_file
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(self._output_dir, f"{timestamp}.mp4")
        if self._stream.start_recording(path):
            self._current_file = path
            log.info("Recording started: %s", path)
            return path
        return None

    def stop(self) -> str | None:
        if not self._current_file:
            return None
        self._stream.stop_recording()
        path = self._current_file
        self._current_file = None
        log.info("Recording stopped: %s", path)
        return path

    def is_recording(self) -> bool:
        return self._current_file is not None

    def current_file(self) -> str | None:
        return self._current_file

    def list_recordings(self) -> list[dict]:
        recordings = []
        try:
            for fname in sorted(os.listdir(self._output_dir), reverse=True):
                if not fname.endswith(".mp4"):
                    continue
                fpath = os.path.join(self._output_dir, fname)
                stat = os.stat(fpath)
                recordings.append({
                    "filename": fname,
                    "size": stat.st_size,
                    "created": datetime.fromtimestamp(stat.st_ctime).isoformat(),
                })
        except OSError as e:
            log.error("Cannot list recordings: %s", e)
        return recordings
