from __future__ import annotations
import threading
import numpy as np
from typing import Callable
from babymonitor.common.logger import get_logger

log = get_logger(__name__)

try:
    import pyaudio
    PYAUDIO_AVAILABLE = True
except ImportError:
    PYAUDIO_AVAILABLE = False
    log.warning("pyaudio not available — cry detection disabled")


class CryDetector:
    """
    Detects infant crying using spectral energy in 300–2000 Hz band
    combined with periodicity analysis via autocorrelation.
    No ML models — runs on CPU on Raspberry Pi Zero.
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        chunk_size: int = 2048,
        threshold: float = 0.65,
        silence_timeout: int = 10,
    ):
        self._rate = sample_rate
        self._chunk = chunk_size
        self._threshold = threshold
        self._silence_timeout = silence_timeout
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._on_cry: Callable[[float], None] | None = None
        self._on_silence: Callable[[], None] | None = None
        self._crying = False
        self._silence_counter = 0

    def start(
        self,
        on_cry_detected: Callable[[float], None],
        on_cry_ended: Callable[[], None] | None = None,
    ) -> None:
        if not PYAUDIO_AVAILABLE:
            log.warning("Cry detection unavailable (pyaudio missing)")
            return
        self._on_cry = on_cry_detected
        self._on_silence = on_cry_ended
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        log.info("Cry detector started (threshold=%.2f)", self._threshold)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=3)

    def _run(self) -> None:
        pa = pyaudio.PyAudio()
        stream = pa.open(
            format=pyaudio.paFloat32,
            channels=1,
            rate=self._rate,
            input=True,
            frames_per_buffer=self._chunk,
        )
        buffer = np.zeros(self._rate * 2, dtype=np.float32)  # 2s ring buffer

        try:
            while not self._stop_event.is_set():
                data = stream.read(self._chunk, exception_on_overflow=False)
                chunk = np.frombuffer(data, dtype=np.float32)
                buffer = np.roll(buffer, -len(chunk))
                buffer[-len(chunk):] = chunk

                confidence = self._analyze(buffer)
                if confidence >= self._threshold:
                    self._silence_counter = 0
                    if not self._crying:
                        self._crying = True
                        log.info("Cry detected (confidence=%.2f)", confidence)
                    if self._on_cry:
                        self._on_cry(confidence)
                else:
                    if self._crying:
                        self._silence_counter += 1
                        chunks_per_second = self._rate // self._chunk
                        if self._silence_counter >= self._silence_timeout * chunks_per_second:
                            self._crying = False
                            self._silence_counter = 0
                            log.info("Cry ended")
                            if self._on_silence:
                                self._on_silence()
        finally:
            stream.stop_stream()
            stream.close()
            pa.terminate()

    def _analyze(self, buffer: np.ndarray) -> float:
        rms = float(np.sqrt(np.mean(buffer ** 2)))
        if rms < 0.01:
            return 0.0

        # FFT-based spectral analysis
        spectrum = np.abs(np.fft.rfft(buffer))
        freqs = np.fft.rfftfreq(len(buffer), d=1.0 / self._rate)

        total_energy = float(np.sum(spectrum) + 1e-10)
        cry_band = float(np.sum(spectrum[(freqs >= 300) & (freqs <= 2000)]))
        upper_band = float(np.sum(spectrum[(freqs > 2000) & (freqs <= 4000)]))

        spectral_score = min(cry_band / total_energy * 2.0, 1.0)

        # Penalise if most energy is outside cry band (e.g. broadband noise)
        noise_ratio = upper_band / (cry_band + 1e-10)
        if noise_ratio > 1.5:
            spectral_score *= 0.5

        # Autocorrelation periodicity check — crying has bursts every 0.5–2s
        autocorr = np.correlate(buffer, buffer, mode="full")
        autocorr = autocorr[len(autocorr) // 2:]
        autocorr /= autocorr[0] + 1e-10

        lag_min = int(self._rate * 0.5)
        lag_max = int(self._rate * 2.0)
        if lag_max < len(autocorr):
            periodicity = float(np.max(autocorr[lag_min:lag_max]))
        else:
            periodicity = 0.0

        confidence = 0.6 * spectral_score + 0.4 * max(periodicity, 0.0)
        return min(confidence, 1.0)
