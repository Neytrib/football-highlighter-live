from __future__ import annotations

import logging
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from app.config import StreamConfig
from app.stream.rolling_buffer import RollingBuffer


class StreamRecorder:
    def __init__(
        self,
        *,
        stream_url: str,
        segment_dir: str,
        stream_config: StreamConfig,
        rolling_buffer: RollingBuffer,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.stream_url = stream_url
        self.segment_dir = Path(segment_dir)
        self.segment_dir.mkdir(parents=True, exist_ok=True)
        self.stream_config = stream_config
        self.rolling_buffer = rolling_buffer
        self.logger = logger or logging.getLogger(__name__)

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._proc: Optional[subprocess.Popen] = None

    def start(self) -> None:
        if self._running:
            self.logger.debug("stream recorder start skipped; already running")
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, name="stream-recorder", daemon=True)
        self.logger.info(
            "starting stream recorder thread",
            extra={
                "extra": {
                    "segment_dir": str(self.segment_dir),
                    "segment_seconds": self.stream_config.segment_seconds,
                    "stream_url": self.stream_url,
                }
            },
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self.logger.info("stopping stream recorder")
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self.logger.warning("stream recorder process force-killed")
        if self._thread:
            self._thread.join(timeout=5)
            self.logger.debug("stream recorder thread joined")

    def _run_loop(self) -> None:
        while self._running:
            cmd = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                self.stream_url,
                "-an",
                "-c:v",
                "copy",
                "-f",
                "segment",
                "-segment_time",
                str(self.stream_config.segment_seconds),
                "-reset_timestamps",
                "1",
                "-strftime",
                "1",
                str(self.segment_dir / "segment_%Y%m%dT%H%M%S.mp4"),
            ]

            try:
                self._proc = subprocess.Popen(cmd)
            except FileNotFoundError as exc:
                self.logger.error("ffmpeg binary not found", extra={"extra": {"error": str(exc)}})
                self._running = False
                return

            self.logger.info("stream recorder started", extra={"extra": {"ffmpeg_cmd": cmd}})
            while self._running and self._proc.poll() is None:
                self.rolling_buffer.refresh_from_disk(str(self.segment_dir), self.stream_config.segment_seconds)
                time.sleep(1.0)

            self.rolling_buffer.refresh_from_disk(str(self.segment_dir), self.stream_config.segment_seconds)
            rc = self._proc.poll() if self._proc else None
            if not self._running:
                break

            self.logger.warning(
                "stream recorder exited, retrying",
                extra={"extra": {"return_code": rc, "delay_seconds": self.stream_config.reconnect_delay_seconds}},
            )
            time.sleep(self.stream_config.reconnect_delay_seconds)
