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
        hls_dir: str | None = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.stream_url = stream_url
        self.segment_dir = Path(segment_dir)
        self.segment_dir.mkdir(parents=True, exist_ok=True)
        self.hls_dir = Path(hls_dir) if hls_dir else None
        if self.hls_dir is not None:
            self.hls_dir.mkdir(parents=True, exist_ok=True)
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
                    "hls_dir": str(self.hls_dir) if self.hls_dir is not None else None,
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
        if self.hls_dir is not None:
            self._clear_hls_files()

        while self._running:
            cmd = self._build_ffmpeg_command()

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

    def _build_ffmpeg_command(self) -> list[str]:
        base = [
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
        ]

        segment_output = str(self.segment_dir / "segment_%Y%m%dT%H%M%S.mp4")
        if self.hls_dir is None:
            return [
                *base,
                "-f",
                "segment",
                "-segment_time",
                str(self.stream_config.segment_seconds),
                "-reset_timestamps",
                "1",
                "-strftime",
                "1",
                segment_output,
            ]

        hls_output = str(self.hls_dir / "stream.m3u8")
        hls_segment_output = str(self.hls_dir / "live_%05d.ts")
        tee_output = "|".join(
            [
                (
                    "[f=segment:"
                    f"segment_time={self.stream_config.segment_seconds}:"
                    "reset_timestamps=1:"
                    "strftime=1]"
                    f"{segment_output}"
                ),
                (
                    "[f=hls:"
                    f"hls_time={self.stream_config.segment_seconds}:"
                    "hls_list_size=8:"
                    "hls_flags=delete_segments+omit_endlist+program_date_time+temp_file:"
                    f"hls_segment_filename={hls_segment_output}]"
                    f"{hls_output}"
                ),
            ]
        )
        return [
            *base,
            "-map",
            "0:v:0",
            "-f",
            "tee",
            tee_output,
        ]

    def _clear_hls_files(self) -> None:
        if self.hls_dir is None:
            return
        self.hls_dir.mkdir(parents=True, exist_ok=True)
        for pattern in (
            "stream.m3u8",
            "init.mp4",
            "live_*.m4s",
            "live_*.m4s.tmp",
            "live_*.ts",
            "live_*.ts.tmp",
            "stream.m3u8.tmp",
        ):
            for path in self.hls_dir.glob(pattern):
                if path.is_file():
                    try:
                        path.unlink()
                    except OSError as exc:
                        self.logger.warning(
                            "failed to delete live hls file",
                            extra={"extra": {"path": str(path), "error": str(exc)}},
                        )
