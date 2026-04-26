from __future__ import annotations

import logging
import time
from typing import Generator, Optional, Tuple

try:
    import cv2  # type: ignore
except ImportError:  # pragma: no cover - optional runtime dependency
    cv2 = None


class FrameSampler:
    def __init__(self, *, stream_url: str, fps: float, reconnect_delay_seconds: int, logger: Optional[logging.Logger] = None) -> None:
        self.stream_url = stream_url
        self.fps = max(fps, 0.25)
        self.reconnect_delay_seconds = reconnect_delay_seconds
        self.logger = logger or logging.getLogger(__name__)

    def frames(self) -> Generator[Tuple[float, object], None, None]:
        if cv2 is None:
            raise RuntimeError("opencv-python is required for frame sampling")

        frame_interval = 1.0 / self.fps
        attempts = 0
        while True:
            attempts += 1
            self.logger.debug(
                "opening OCR frame stream",
                extra={"extra": {"attempt": attempts, "stream_url": self.stream_url, "target_fps": self.fps}},
            )
            cap = cv2.VideoCapture(self.stream_url)
            if not cap.isOpened():
                self.logger.warning("failed to open stream for OCR sampling")
                time.sleep(self.reconnect_delay_seconds)
                continue
            self.logger.info("OCR frame sampler connected")

            last_emit = 0.0
            emitted = 0
            while True:
                ok, frame = cap.read()
                if not ok:
                    self.logger.warning(
                        "failed to read frame; reconnecting sampler",
                        extra={"extra": {"frames_emitted": emitted}},
                    )
                    break

                now = time.time()
                if now - last_emit >= frame_interval:
                    last_emit = now
                    emitted += 1
                    if emitted % 30 == 0:
                        self.logger.debug("OCR frame sampler heartbeat", extra={"extra": {"frames_emitted": emitted}})
                    yield now, frame

            cap.release()
            self.logger.info("OCR frame sampler disconnected", extra={"extra": {"reconnect_delay_seconds": self.reconnect_delay_seconds}})
            time.sleep(self.reconnect_delay_seconds)
