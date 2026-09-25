"""
evidence_capture.py - Screen-evidence recorder for MAS-QA-Bridge.

Records ONLY a screen rectangle (the dashboard's central host QFrame) with
``mss`` on a background thread and saves the result as an animated GIF
(``imageio``) or MP4 (``cv2``) under a local ``temp_evidence`` folder.

Frames are kept JPEG-compressed in memory while recording, so long sessions
don't exhaust RAM; they are decoded only when the file is written.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import cv2
import imageio.v3 as iio
import mss
import numpy as np
from mss.exception import ScreenShotError

log = logging.getLogger(__name__)

# mss-style region in *physical* screen pixels: {"left", "top", "width", "height"}
Region = dict[str, int]
RegionProvider = Callable[[], Optional[Region]]


class EvidenceRecorder:
    """Record a (possibly moving) screen region and save it as GIF / MP4.

    ``region_provider`` is called once per frame from the capture thread, so the
    recording follows the host frame if the dashboard is moved or resized. It
    must be thread-safe (e.g. return a dict cached by the GUI thread).
    """

    def __init__(
        self,
        region_provider: RegionProvider,
        output_dir: str | Path = "temp_evidence",
        fps: int = 8,
        max_width: int = 1280,
        max_seconds: int = 180,
        jpeg_quality: int = 90,
        memory_budget_mb: int = 512,
    ) -> None:
        self.region_provider = region_provider
        self.output_dir = Path(output_dir)
        self.fps = fps
        self.max_width = max_width
        self.max_seconds = max_seconds
        self.jpeg_quality = jpeg_quality
        self.memory_budget_mb = memory_budget_mb

        self._frames: list[bytes] = []
        self._frame_size: Optional[tuple[int, int]] = None  # (width, height)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._started_at: Optional[float] = None
        self._stopped_at: Optional[float] = None

    # ------------------------------------------------------------------ state
    @property
    def is_recording(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def frame_count(self) -> int:
        with self._lock:
            return len(self._frames)

    @property
    def elapsed(self) -> float:
        if self._started_at is None:
            return 0.0
        end = self._stopped_at if self._stopped_at is not None else time.monotonic()
        return max(0.0, end - self._started_at)

    # -------------------------------------------------------------- recording
    def start(self) -> None:
        if self.is_recording:
            raise RuntimeError("Recording already in progress.")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.clear()
        self._stop.clear()
        self._started_at = time.monotonic()
        self._stopped_at = None
        self._thread = threading.Thread(target=self._capture_loop, name="EvidenceRecorder", daemon=True)
        self._thread.start()
        log.info("Recording started (%d fps, max %ds)", self.fps, self.max_seconds)

    def stop(self) -> int:
        """Stop capturing; returns the number of frames held."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        if self._started_at is not None and self._stopped_at is None:
            self._stopped_at = time.monotonic()
        count = self.frame_count
        log.info("Recording stopped: %d frames over %.1fs", count, self.elapsed)
        return count

    def clear(self) -> None:
        with self._lock:
            self._frames = []
            self._frame_size = None

    def _capture_loop(self) -> None:
        interval = 1.0 / max(1, self.fps)
        max_frames = max(1, int(self.fps * self.max_seconds))
        try:
            # mss handles must be created in the thread that uses them.
            with mss.mss() as sct:
                next_tick = time.perf_counter()
                while not self._stop.is_set():
                    region = self.region_provider()
                    if region and region.get("width", 0) > 0 and region.get("height", 0) > 0:
                        try:
                            self._store(np.asarray(sct.grab(region)))
                        except ScreenShotError as exc:
                            log.warning("Screen grab failed: %s", exc)

                    if self.frame_count >= max_frames:
                        log.warning("Reached max recording length (%ds) - stopping capture.", self.max_seconds)
                        break

                    next_tick += interval
                    delay = next_tick - time.perf_counter()
                    if delay > 0:
                        self._stop.wait(delay)
                    else:
                        next_tick = time.perf_counter()  # fell behind; don't burst
        except Exception:
            log.exception("Evidence capture thread crashed")
        finally:
            self._stopped_at = time.monotonic()

    def _store(self, bgra: np.ndarray) -> None:
        frame = cv2.cvtColor(bgra, cv2.COLOR_BGRA2BGR)
        if self._frame_size is None:
            height, width = frame.shape[:2]
            if width > self.max_width:
                height = round(height * self.max_width / width)
                width = self.max_width
            # Even dimensions keep video codecs happy.
            self._frame_size = (max(2, width - width % 2), max(2, height - height % 2))
        if (frame.shape[1], frame.shape[0]) != self._frame_size:
            frame = cv2.resize(frame, self._frame_size, interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality])
        if ok:
            with self._lock:
                self._frames.append(buf.tobytes())

    # ------------------------------------------------------------- screenshot
    def screenshot(self, filename: Optional[str] = None) -> Path:
        """Save a single full-resolution PNG of the current region."""
        region = self.region_provider()
        if not region or region.get("width", 0) <= 0 or region.get("height", 0) <= 0:
            raise RuntimeError("Capture region is not visible on screen.")
        with mss.mss() as sct:
            frame = cv2.cvtColor(np.asarray(sct.grab(region)), cv2.COLOR_BGRA2BGR)
        name = filename or f"screenshot_{datetime.now():%Y%m%d_%H%M%S_%f}"[:-3]
        path = self._output_path(name, ".png")
        if not cv2.imwrite(str(path), frame):
            raise RuntimeError(f"Could not write {path}")
        log.info("Saved screenshot: %s", path)
        return path

    # ----------------------------------------------------------------- saving
    def stop_and_save_gif(self, filename: Optional[str] = None) -> Path:
        self.stop()
        return self.save_gif(filename)

    def stop_and_save_mp4(self, filename: Optional[str] = None) -> Path:
        self.stop()
        return self.save_mp4(filename)

    def save_gif(self, filename: Optional[str] = None) -> Path:
        """Write the captured frames to an animated, looping GIF."""
        frames, (width, height) = self._snapshot()

        # Keep the decoded RGB stack within the memory budget by downscaling.
        total = len(frames) * width * height * 3
        budget = self.memory_budget_mb * 1024 * 1024
        if total > budget:
            scale = math.sqrt(budget / total)
            width, height = max(2, int(width * scale)), max(2, int(height * scale))
            log.info("GIF downscaled to %dx%d to stay within %d MB", width, height, self.memory_budget_mb)

        stack = np.empty((len(frames), height, width, 3), dtype=np.uint8)
        for i, data in enumerate(frames):
            stack[i] = cv2.cvtColor(self._decode(data, (width, height)), cv2.COLOR_BGR2RGB)

        path = self._output_path(filename, ".gif")
        iio.imwrite(
            path,
            stack,
            extension=".gif",
            plugin="pillow",
            is_batch=True,
            duration=self._frame_duration_ms(len(frames)),  # milliseconds per frame
            loop=0,
        )
        log.info("Saved GIF evidence: %s (%d frames)", path, len(frames))
        return path

    def save_mp4(self, filename: Optional[str] = None) -> Path:
        """Write the captured frames to an MP4 (mp4v) - far smaller than GIF."""
        frames, size = self._snapshot()
        path = self._output_path(filename, ".mp4")
        fps = 1000.0 / self._frame_duration_ms(len(frames))
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, size)
        if not writer.isOpened():
            raise RuntimeError(f"OpenCV could not open a video writer for {path}")
        try:
            for data in frames:
                writer.write(self._decode(data, size))
        finally:
            writer.release()
        log.info("Saved MP4 evidence: %s (%d frames)", path, len(frames))
        return path

    # ---------------------------------------------------------------- helpers
    def _snapshot(self) -> tuple[list[bytes], tuple[int, int]]:
        if self.is_recording:
            raise RuntimeError("Stop the recording before saving.")
        with self._lock:
            frames = list(self._frames)
            size = self._frame_size
        if not frames or size is None:
            raise RuntimeError("No frames were captured - is the capture region on screen?")
        return frames, size

    @staticmethod
    def _decode(data: bytes, size: tuple[int, int]) -> np.ndarray:
        frame = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
        if (frame.shape[1], frame.shape[0]) != size:
            frame = cv2.resize(frame, size, interpolation=cv2.INTER_AREA)
        return frame

    def _frame_duration_ms(self, count: int) -> int:
        """Per-frame duration that makes playback match real (wall-clock) time."""
        if count and self.elapsed > 0:
            return max(20, round(1000 * self.elapsed / count))
        return max(20, round(1000 / max(1, self.fps)))

    def _output_path(self, filename: Optional[str], suffix: str) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        name = filename or f"evidence_{datetime.now():%Y%m%d_%H%M%S}"
        return self.output_dir / Path(name).with_suffix(suffix).name
