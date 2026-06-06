from __future__ import annotations

import logging
from pathlib import Path
from time import time
from typing import Any

from app.config import StreamConfig
from app.stream.recorder import StreamRecorder
from app.stream.rolling_buffer import RollingBuffer
from app.ui.stream_manager import stream_id_from_url


class LivePreviewSupervisor:
    def __init__(
        self,
        *,
        stream_config: StreamConfig,
        segment_dir: str,
        hls_dir: str | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.stream_config = stream_config
        self.segment_dir = Path(segment_dir)
        self.hls_dir = Path(hls_dir) if hls_dir else self.segment_dir / "hls"
        self.logger = logger or logging.getLogger(__name__)
        self.stream_url = ""
        self.started_at = 0.0
        self._recorder: StreamRecorder | None = None
        self._mirror_segment_dir: Path | None = None
        self._mirror_hls_dir: Path | None = None
        self._external_hls_url = ""
        self._rolling_buffer = RollingBuffer(
            buffer_seconds=self.stream_config.rolling_buffer_seconds,
            logger=self.logger,
        )

    def status(self) -> dict[str, Any]:
        state = "stopped"
        if self._external_hls_url:
            state = "external"
        elif self._mirror_segment_dir is not None:
            state = "mirroring"
        elif self._recorder is not None:
            state = "running"

        return {
            "state": state,
            "configured": bool(self.stream_url),
            "id": stream_id_from_url(self.stream_url),
            "url": self.stream_url,
            "playbackUrl": self.stream_url,
            "segmentDir": str(self.active_segment_dir()),
            "hlsDir": str(self.active_hls_dir()),
            "externalHlsUrl": self._external_hls_url,
            "startedAt": self.started_at or None,
        }

    def start(self, stream_url: str) -> dict[str, Any]:
        if self.stream_url == stream_url and self._recorder is not None and self._mirror_segment_dir is None:
            return self.status()

        self.stop()
        self._mirror_segment_dir = None
        self._mirror_hls_dir = None
        self._external_hls_url = ""
        self.stream_url = stream_url
        self.started_at = time()
        self._clear_segments()
        self._clear_hls_assets()
        self._rolling_buffer = RollingBuffer(
            buffer_seconds=self.stream_config.rolling_buffer_seconds,
            logger=self.logger,
        )
        self._recorder = StreamRecorder(
            stream_url=stream_url,
            segment_dir=str(self.segment_dir),
            stream_config=self.stream_config,
            rolling_buffer=self._rolling_buffer,
            hls_dir=str(self.hls_dir),
            logger=self.logger,
        )
        self._recorder.start()
        return self.status()

    def use_external_hls(self, stream_url: str, hls_url: str) -> dict[str, Any]:
        if self.stream_url == stream_url and self._external_hls_url == hls_url:
            return self.status()

        if self._recorder is not None:
            self._recorder.stop()
            self._recorder = None

        self.stream_url = stream_url
        self.started_at = time()
        self._mirror_segment_dir = None
        self._mirror_hls_dir = None
        self._external_hls_url = hls_url
        return self.status()

    def use_existing(self, stream_url: str, segment_dir: str | Path, hls_dir: str | Path) -> dict[str, Any]:
        mirror_segment_dir = Path(segment_dir)
        mirror_hls_dir = Path(hls_dir)
        if (
            self.stream_url == stream_url
            and self._mirror_segment_dir == mirror_segment_dir
            and self._mirror_hls_dir == mirror_hls_dir
        ):
            return self.status()

        if self._recorder is not None:
            self._recorder.stop()
            self._recorder = None

        self.stream_url = stream_url
        self.started_at = time()
        self._mirror_segment_dir = mirror_segment_dir
        self._mirror_hls_dir = mirror_hls_dir
        self._external_hls_url = ""
        return self.status()

    def stop(self) -> dict[str, Any]:
        if self._recorder is not None:
            self._recorder.stop()
            self._recorder = None
        self._mirror_segment_dir = None
        self._mirror_hls_dir = None
        self._external_hls_url = ""
        self.stream_url = ""
        self.started_at = 0.0
        return self.status()

    def is_active(self) -> bool:
        return bool(self.stream_url) and (
            self._recorder is not None or self._mirror_segment_dir is not None or bool(self._external_hls_url)
        )

    def external_hls_url(self) -> str:
        return self._external_hls_url

    def active_segment_dir(self) -> Path:
        return self._mirror_segment_dir or self.segment_dir

    def active_hls_dir(self) -> Path:
        return self._mirror_hls_dir or self.hls_dir

    def segment_dir_for_preview(self, fallback_dir: str | Path) -> Path:
        if self.is_active():
            return self.active_segment_dir().resolve()
        return Path(fallback_dir).resolve()

    def hls_dir_for_preview(self, fallback_dir: str | Path) -> Path:
        if self.is_active():
            return self.active_hls_dir().resolve()
        return Path(fallback_dir).resolve()

    def _clear_segments(self) -> None:
        self.segment_dir.mkdir(parents=True, exist_ok=True)
        for path in self.segment_dir.glob("segment_*.mp4"):
            if path.is_file():
                try:
                    path.unlink()
                except OSError as exc:
                    self.logger.warning("failed to delete live preview segment", extra={"extra": {"path": str(path), "error": str(exc)}})

    def _clear_hls_assets(self) -> None:
        self.hls_dir.mkdir(parents=True, exist_ok=True)
        for pattern in (
            "stream.m3u8",
            "stream.m3u8.tmp",
            "init.mp4",
            "live_*.m4s",
            "live_*.m4s.tmp",
            "live_*.ts",
            "live_*.ts.tmp",
        ):
            for path in self.hls_dir.glob(pattern):
                if path.is_file():
                    try:
                        path.unlink()
                    except OSError as exc:
                        self.logger.warning(
                            "failed to delete live preview hls file",
                            extra={"extra": {"path": str(path), "error": str(exc)}},
                        )
