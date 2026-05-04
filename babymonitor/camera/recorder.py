from __future__ import annotations
import json
import os
import shutil
from datetime import datetime
from babymonitor.common.logger import get_logger

log = get_logger(__name__)


class Recorder:
    def __init__(
        self,
        stream,
        output_dir: str,
        max_recordings: int = 50,
        min_free_mb: int = 500,
    ) -> None:
        self._stream = stream
        self._output_dir = output_dir
        self._max_recordings = max_recordings
        self._min_free_mb = min_free_mb
        self._current_file: str | None = None
        self._start_time: datetime | None = None

    def start(self) -> str | None:
        if self._current_file:
            log.warning("Already recording")
            return self._current_file
        if not self._has_free_space():
            log.error("Insufficient disk space (< %d MB free) — skipping recording", self._min_free_mb)
            return None
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(self._output_dir, f"{timestamp}.mp4")
        if self._stream.start_recording(path):
            self._current_file = path
            self._start_time = datetime.now()
            log.info("Recording started: %s", path)
            self._evict_old_recordings()
            return path
        return None

    def stop(self) -> str | None:
        if not self._current_file:
            return None
        self._stream.stop_recording()
        path = self._current_file
        ended_at = datetime.now()
        duration_s = (ended_at - self._start_time).total_seconds() if self._start_time else 0.0
        self._write_sidecar(path, self._start_time or ended_at, ended_at, duration_s)
        self._current_file = None
        self._start_time = None
        log.info("Recording stopped: %s (%.0fs)", path, duration_s)
        return path

    def is_recording(self) -> bool:
        return self._current_file is not None

    def current_file(self) -> str | None:
        return self._current_file

    def list_recordings(self) -> list[dict]:
        recordings: list[dict] = []
        try:
            for fname in sorted(os.listdir(self._output_dir), reverse=True):
                if not fname.endswith(".mp4"):
                    continue
                fpath = os.path.join(self._output_dir, fname)
                stat = os.stat(fpath)
                rec: dict = {
                    "filename": fname,
                    "size": stat.st_size,
                    "created": datetime.fromtimestamp(stat.st_ctime).isoformat(),
                    "duration_s": None,
                }
                sidecar = fpath.replace(".mp4", ".json")
                if os.path.exists(sidecar):
                    try:
                        with open(sidecar) as f:
                            meta = json.load(f)
                            rec["duration_s"] = meta.get("duration_s")
                    except (OSError, json.JSONDecodeError):
                        pass
                recordings.append(rec)
        except OSError as e:
            log.error("Cannot list recordings: %s", e)
        return recordings

    # ── Private helpers ─────────────────────────────────────────────────────

    def _has_free_space(self) -> bool:
        try:
            free_mb = shutil.disk_usage(self._output_dir).free / (1024 * 1024)
            return free_mb >= self._min_free_mb
        except OSError:
            return True  # can't check — allow recording

    def _evict_old_recordings(self) -> None:
        recordings = self.list_recordings()
        while len(recordings) > self._max_recordings:
            oldest = recordings.pop()  # sorted newest-first, so last = oldest
            for suffix in (".mp4", ".json"):
                p = os.path.join(self._output_dir, oldest["filename"].replace(".mp4", suffix))
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except OSError as e:
                    log.warning("Could not remove %s: %s", p, e)
            log.info("Evicted old recording: %s", oldest["filename"])

    def _write_sidecar(
        self, mp4_path: str, started: datetime, ended: datetime, duration_s: float
    ) -> None:
        sidecar = mp4_path.replace(".mp4", ".json")
        try:
            with open(sidecar, "w") as f:
                json.dump({
                    "started_at": started.isoformat(),
                    "ended_at": ended.isoformat(),
                    "duration_s": round(duration_s, 1),
                }, f)
        except OSError as e:
            log.warning("Could not write sidecar: %s", e)
