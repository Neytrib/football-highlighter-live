from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import List

from app.models import SegmentMeta


class ClipExtractor:
    def __init__(self, *, tmp_dir: str, logger: logging.Logger | None = None) -> None:
        self.tmp_dir = Path(tmp_dir)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self.logger = logger or logging.getLogger(__name__)

    def extract_clip(
        self,
        *,
        segments: List[SegmentMeta],
        start_ts: float,
        end_ts: float,
        output_path: str,
    ) -> str:
        if not segments:
            raise RuntimeError("No buffer segments available for clip window")

        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)

        concat_file = self.tmp_dir / f"concat_{int(start_ts)}_{int(end_ts)}.txt"
        with concat_file.open("w", encoding="utf-8") as fh:
            for segment in sorted(segments, key=lambda s: s.start_ts):
                fh.write(f"file '{Path(segment.path).resolve()}'\n")

        first_start = min(s.start_ts for s in segments)
        offset = max(0.0, start_ts - first_start)
        duration = max(1.0, end_ts - start_ts)
        self.logger.info(
            "extracting clip from rolling buffer",
            extra={
                "extra": {
                    "output_path": str(output),
                    "segments_count": len(segments),
                    "window_start_ts": round(start_ts, 3),
                    "window_end_ts": round(end_ts, 3),
                    "concat_offset": round(offset, 3),
                    "duration": round(duration, 3),
                }
            },
        )

        cmd_copy = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-ss",
            f"{offset:.3f}",
            "-t",
            f"{duration:.3f}",
            "-c",
            "copy",
            str(output),
        ]

        cmd_reencode = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-ss",
            f"{offset:.3f}",
            "-t",
            f"{duration:.3f}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-c:a",
            "aac",
            str(output),
        ]

        try:
            subprocess.run(cmd_copy, check=True)
            self.logger.debug("clip extraction with stream copy succeeded", extra={"extra": {"output_path": str(output)}})
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            self.logger.warning("copy clip extraction failed; re-encoding", extra={"extra": {"error": str(exc)}})
            subprocess.run(cmd_reencode, check=True)
            self.logger.debug("clip extraction with re-encode succeeded", extra={"extra": {"output_path": str(output)}})

        return str(output)
