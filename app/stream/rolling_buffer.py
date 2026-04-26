from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Deque, List

from app.models import SegmentMeta


class RollingBuffer:
    def __init__(self, *, buffer_seconds: int, logger: logging.Logger | None = None) -> None:
        self.buffer_seconds = buffer_seconds
        self.logger = logger or logging.getLogger(__name__)
        self._segments: Deque[SegmentMeta] = deque()
        self._lock = Lock()

    def add_segment(self, segment: SegmentMeta) -> None:
        with self._lock:
            action = self._upsert_segment_locked(segment)
            self._prune_locked(now_ts=segment.end_ts)
            if action == "added":
                self.logger.debug(
                    "segment added to rolling buffer",
                    extra={
                        "extra": {
                            "path": segment.path,
                            "start_ts": round(segment.start_ts, 3),
                            "end_ts": round(segment.end_ts, 3),
                            "buffer_size": len(self._segments),
                        }
                    },
                )
            elif action == "updated":
                self.logger.debug(
                    "segment timestamp updated in rolling buffer",
                    extra={
                        "extra": {
                            "path": segment.path,
                            "start_ts": round(segment.start_ts, 3),
                            "end_ts": round(segment.end_ts, 3),
                            "buffer_size": len(self._segments),
                        }
                    },
                )

    def refresh_from_disk(self, segment_dir: str, segment_seconds: int) -> None:
        paths = sorted(Path(segment_dir).glob("segment_*.mp4"))
        added = 0
        updated = 0
        with self._lock:
            existing_by_path = {str(Path(segment.path).resolve()): segment.end_ts for segment in self._segments}

        for path in paths:
            bounds = _segment_time_bounds(path, segment_seconds)
            if bounds is None:
                continue
            start_ts, end_ts = bounds
            key = str(path.resolve())
            prev_end = existing_by_path.get(key)
            if prev_end is not None and end_ts <= prev_end + 0.001:
                continue
            before_count = len(self._segments)
            self.add_segment(SegmentMeta(start_ts=start_ts, end_ts=end_ts, path=key))
            after_count = len(self._segments)
            if after_count > before_count:
                added += 1
            else:
                updated += 1
        if added > 0 or updated > 0:
            self.logger.debug(
                "rolling buffer refreshed from disk",
                extra={
                    "extra": {
                        "segment_dir": segment_dir,
                        "added_segments": added,
                        "updated_segments": updated,
                        "segment_seconds": segment_seconds,
                    }
                },
            )

    def get_segments_for_window(self, start_ts: float, end_ts: float) -> List[SegmentMeta]:
        with self._lock:
            return [s for s in self._segments if not (s.end_ts < start_ts or s.start_ts > end_ts)]

    def _prune_locked(self, now_ts: float) -> None:
        cutoff = now_ts - float(self.buffer_seconds)
        pruned = 0
        while self._segments and self._segments[0].end_ts < cutoff:
            old = self._segments.popleft()
            path = Path(old.path)
            if path.exists():
                try:
                    path.unlink()
                    pruned += 1
                except OSError as exc:
                    self.logger.warning("failed to delete old segment", extra={"extra": {"path": old.path, "error": str(exc)}})
        if pruned > 0:
            self.logger.debug(
                "rolling buffer pruned old segments",
                extra={"extra": {"pruned_segments": pruned, "remaining_segments": len(self._segments), "cutoff_ts": round(cutoff, 3)}},
            )

    def _upsert_segment_locked(self, segment: SegmentMeta) -> str:
        for idx, existing in enumerate(self._segments):
            if existing.path == segment.path or Path(existing.path).name == Path(segment.path).name:
                self._segments[idx] = segment
                return "updated"
        self._segments.append(segment)
        return "added"


def _segment_time_bounds(path: Path, segment_seconds: int) -> tuple[float, float] | None:
    # Prefer filesystem mtime to avoid timezone ambiguity from ffmpeg strftime names.
    try:
        end_ts = path.stat().st_mtime
    except OSError:
        end_ts = None
    if end_ts is not None and end_ts > 0:
        start_ts = end_ts - float(segment_seconds)
        return start_ts, end_ts

    # Fallback parser for filenames like segment_YYYYmmddTHHMMSS.mp4 in local timezone.
    stem = path.stem
    if not stem.startswith("segment_"):
        return None
    dt_raw = stem.replace("segment_", "", 1)
    try:
        naive_local = datetime.strptime(dt_raw, "%Y%m%dT%H%M%S")
    except ValueError:
        return None
    local_tz = datetime.now().astimezone().tzinfo
    if local_tz is None:
        local_tz = timezone.utc
    end_dt = naive_local.replace(tzinfo=local_tz)
    end_ts = end_dt.timestamp()
    start_ts = end_ts - float(segment_seconds)
    return start_ts, end_ts
