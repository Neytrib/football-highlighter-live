from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ClipWindow:
    start_ts: float
    end_ts: float


def compute_clip_window(goal_ts_unix: float, pre_goal_seconds: int, post_goal_seconds: int) -> ClipWindow:
    return ClipWindow(
        start_ts=goal_ts_unix - float(pre_goal_seconds),
        end_ts=goal_ts_unix + float(post_goal_seconds),
    )
