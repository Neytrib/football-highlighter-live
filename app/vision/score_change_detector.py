from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

import numpy as np

try:
    import cv2  # type: ignore
except ImportError:  # pragma: no cover - runtime optional
    cv2 = None


@dataclass
class ScoreChangeEvent:
    goal_ts_unix: float
    diff_value: float
    changed_home: bool
    changed_away: bool


@dataclass
class _Box:
    x: int
    y: int
    w: int
    h: int


class ScoreChangeDetector:
    def __init__(
        self,
        *,
        home_score_roi: tuple[float, float, float, float],
        away_score_roi: tuple[float, float, float, float],
        search_roi: tuple[float, float, float, float],
        auto_locate_score_rois: bool,
        auto_locate_frames: int,
        change_threshold: float,
        stable_threshold: float,
        stable_frames: int,
        cooldown_seconds: int,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.home_score_roi = home_score_roi
        self.away_score_roi = away_score_roi
        self.search_roi = search_roi
        self.auto_locate_score_rois = bool(auto_locate_score_rois)
        self.auto_locate_frames = max(10, int(auto_locate_frames))
        self.change_threshold = float(change_threshold)
        self.stable_threshold = float(stable_threshold)
        self.stable_frames = max(1, int(stable_frames))
        self.cooldown_seconds = max(1, int(cooldown_seconds))
        self.logger = logger or logging.getLogger(__name__)

        self._baseline_home: Optional[np.ndarray] = None
        self._baseline_away: Optional[np.ndarray] = None
        self._candidate_home: Optional[np.ndarray] = None
        self._candidate_away: Optional[np.ndarray] = None
        self._candidate_changed_home: bool = False
        self._candidate_changed_away: bool = False
        self._candidate_ts: float = 0.0
        self._stable_count: int = 0
        self._last_goal_ts: Optional[float] = None

        self._roi_locked: bool = not self.auto_locate_score_rois
        self._locate_patches: List[np.ndarray] = []
        self._locate_attempts: int = 0

        self.logger.info(
            "score change detector initialized",
            extra={
                "extra": {
                    "home_score_roi": list(home_score_roi),
                    "away_score_roi": list(away_score_roi),
                    "search_roi": list(search_roi),
                    "auto_locate_score_rois": self.auto_locate_score_rois,
                    "auto_locate_frames": self.auto_locate_frames,
                    "change_threshold": self.change_threshold,
                    "stable_threshold": self.stable_threshold,
                    "stable_frames": self.stable_frames,
                    "cooldown_seconds": self.cooldown_seconds,
                }
            },
        )

    def process(self, frame: object, frame_ts: float) -> Optional[ScoreChangeEvent]:
        if cv2 is None:
            raise RuntimeError("opencv-python is required for stream-only score change detection")

        if not self._roi_locked:
            self._collect_roi_calibration(frame)
            if not self._roi_locked:
                return None

        home_img = self._extract_score_image(frame, self.home_score_roi)
        away_img = self._extract_score_image(frame, self.away_score_roi)
        if home_img is None or away_img is None:
            return None

        if self._baseline_home is None or self._baseline_away is None:
            self._baseline_home = home_img
            self._baseline_away = away_img
            return None

        diff_home_baseline = _mean_abs_diff(home_img, self._baseline_home)
        diff_away_baseline = _mean_abs_diff(away_img, self._baseline_away)
        changed_home = diff_home_baseline >= self.change_threshold
        changed_away = diff_away_baseline >= self.change_threshold

        if self._candidate_home is None or self._candidate_away is None:
            if changed_home or changed_away:
                self._start_candidate(
                    home_img=home_img,
                    away_img=away_img,
                    ts=frame_ts,
                    changed_home=changed_home,
                    changed_away=changed_away,
                )
            else:
                self._baseline_home = _blend(self._baseline_home, home_img)
                self._baseline_away = _blend(self._baseline_away, away_img)
            return None

        diff_home_candidate = _mean_abs_diff(home_img, self._candidate_home)
        diff_away_candidate = _mean_abs_diff(away_img, self._candidate_away)
        if diff_home_candidate <= self.stable_threshold and diff_away_candidate <= self.stable_threshold:
            keeps_home_change = True
            keeps_away_change = True
            if self._candidate_changed_home:
                keeps_home_change = _mean_abs_diff(self._candidate_home, self._baseline_home) >= self.change_threshold
            if self._candidate_changed_away:
                keeps_away_change = _mean_abs_diff(self._candidate_away, self._baseline_away) >= self.change_threshold
            if not (keeps_home_change or keeps_away_change):
                self._reset_candidate()
                self._baseline_home = _blend(self._baseline_home, home_img)
                self._baseline_away = _blend(self._baseline_away, away_img)
                return None
            self._stable_count += 1
        else:
            if changed_home or changed_away:
                self._start_candidate(
                    home_img=home_img,
                    away_img=away_img,
                    ts=frame_ts,
                    changed_home=changed_home,
                    changed_away=changed_away,
                )
            else:
                self._reset_candidate()
                self._baseline_home = _blend(self._baseline_home, home_img)
                self._baseline_away = _blend(self._baseline_away, away_img)
            return None

        if self._stable_count < self.stable_frames:
            return None
        if self._last_goal_ts is not None and frame_ts - self._last_goal_ts < self.cooldown_seconds:
            self._reset_candidate()
            return None

        goal_ts = self._candidate_ts
        diff_value = max(
            _mean_abs_diff(self._candidate_home, self._baseline_home),
            _mean_abs_diff(self._candidate_away, self._baseline_away),
        )
        changed_home_goal = self._candidate_changed_home
        changed_away_goal = self._candidate_changed_away
        self._last_goal_ts = frame_ts
        self._baseline_home = home_img
        self._baseline_away = away_img
        self._reset_candidate()

        self.logger.warning(
            "score change detected from stream",
            extra={
                "extra": {
                    "goal_ts_unix": round(goal_ts, 3),
                    "diff_value": round(diff_value, 3),
                    "changed_home": changed_home_goal,
                    "changed_away": changed_away_goal,
                }
            },
        )
        return ScoreChangeEvent(
            goal_ts_unix=goal_ts,
            diff_value=diff_value,
            changed_home=changed_home_goal,
            changed_away=changed_away_goal,
        )

    def _collect_roi_calibration(self, frame: object) -> None:
        patch = self._extract_search_patch(frame)
        if patch is None:
            return
        self._locate_patches.append(patch)
        if len(self._locate_patches) > self.auto_locate_frames:
            self._locate_patches.pop(0)
        if len(self._locate_patches) < self.auto_locate_frames:
            return

        self._locate_attempts += 1
        rois = self._auto_locate_score_rois(self._locate_patches, frame)
        if rois is not None:
            self.home_score_roi, self.away_score_roi = rois
            self._roi_locked = True
            self._baseline_home = None
            self._baseline_away = None
            self._reset_candidate()
            self.logger.info(
                "auto-located score ROIs",
                extra={
                    "extra": {
                        "attempt": self._locate_attempts,
                        "home_score_roi": [round(x, 4) for x in self.home_score_roi],
                        "away_score_roi": [round(x, 4) for x in self.away_score_roi],
                    }
                },
            )
            return

        if self._locate_attempts >= 5:
            self._roi_locked = True
            self.logger.warning(
                "auto score ROI locate failed; using configured ROIs",
                extra={
                    "extra": {
                        "attempts": self._locate_attempts,
                        "home_score_roi": [round(x, 4) for x in self.home_score_roi],
                        "away_score_roi": [round(x, 4) for x in self.away_score_roi],
                    }
                },
            )

    def _auto_locate_score_rois(
        self,
        patches: List[np.ndarray],
        frame: object,
    ) -> Optional[tuple[tuple[float, float, float, float], tuple[float, float, float, float]]]:
        bounds = _roi_bounds_from_frame(frame, self.search_roi)
        if bounds is None:
            return None
        sx0, sy0, sx1, sy1 = bounds
        ph, pw = patches[0].shape[:2]
        if ph <= 4 or pw <= 4:
            return None

        boxes = _find_candidate_boxes(patches[0])
        if len(boxes) < 2:
            return None

        pairs: List[tuple[_Box, _Box]] = []
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                left = boxes[i] if boxes[i].x <= boxes[j].x else boxes[j]
                right = boxes[j] if boxes[i].x <= boxes[j].x else boxes[i]
                dx = (right.x + right.w // 2) - (left.x + left.w // 2)
                if dx < max(10, int(0.01 * pw)) or dx > int(0.35 * pw):
                    continue
                y_diff = abs((left.y + left.h // 2) - (right.y + right.h // 2))
                if y_diff > int(0.18 * ph):
                    continue
                h_ratio = left.h / max(1.0, right.h)
                if h_ratio < 0.5 or h_ratio > 2.0:
                    continue
                pairs.append((left, right))
        if not pairs:
            return None

        best_pair: Optional[tuple[_Box, _Box]] = None
        best_score: Optional[float] = None
        for left, right in pairs:
            score = _pair_temporal_score(patches, left, right)
            if score is None:
                continue
            if best_score is None or score < best_score:
                best_score = score
                best_pair = (left, right)
        if best_pair is None:
            return None

        left, right = best_pair
        left_roi = _box_to_normalized_roi(left, sx0, sy0, pw, ph, frame, pad=0.15)
        right_roi = _box_to_normalized_roi(right, sx0, sy0, pw, ph, frame, pad=0.15)
        if left_roi is None or right_roi is None:
            return None
        return left_roi, right_roi

    def _start_candidate(
        self,
        *,
        home_img: np.ndarray,
        away_img: np.ndarray,
        ts: float,
        changed_home: bool,
        changed_away: bool,
    ) -> None:
        self._candidate_home = home_img
        self._candidate_away = away_img
        self._candidate_changed_home = changed_home
        self._candidate_changed_away = changed_away
        self._candidate_ts = ts
        self._stable_count = 1

    def _reset_candidate(self) -> None:
        self._candidate_home = None
        self._candidate_away = None
        self._candidate_changed_home = False
        self._candidate_changed_away = False
        self._candidate_ts = 0.0
        self._stable_count = 0

    def _extract_search_patch(self, frame: object) -> Optional[np.ndarray]:
        bounds = _roi_bounds_from_frame(frame, self.search_roi)
        if bounds is None:
            return None
        x0, y0, x1, y1 = bounds
        roi = frame[y0:y1, x0:x1]
        if roi is None or roi.size == 0:
            return None
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        return gray

    def _extract_score_image(self, frame: object, roi_rect: tuple[float, float, float, float]) -> Optional[np.ndarray]:
        bounds = _roi_bounds_from_frame(frame, roi_rect)
        if bounds is None:
            return None
        x0, y0, x1, y1 = bounds
        roi = frame[y0:y1, x0:x1]
        if roi is None or roi.size == 0:
            return None

        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (3, 3), 0)
        gray = cv2.resize(gray, (220, 80), interpolation=cv2.INTER_AREA)
        _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        return bw


def _roi_bounds_from_frame(frame: object, roi_rect: tuple[float, float, float, float]) -> Optional[tuple[int, int, int, int]]:
    try:
        h = int(frame.shape[0])
        w = int(frame.shape[1])
    except Exception:
        return None

    x0 = max(0, min(w - 1, int(w * roi_rect[0])))
    y0 = max(0, min(h - 1, int(h * roi_rect[1])))
    x1 = max(x0 + 1, min(w, int(w * (roi_rect[0] + roi_rect[2]))))
    y1 = max(y0 + 1, min(h, int(h * (roi_rect[1] + roi_rect[3]))))
    return x0, y0, x1, y1


def _find_candidate_boxes(gray_patch: np.ndarray) -> List[_Box]:
    ph, pw = gray_patch.shape[:2]
    candidates: List[_Box] = []
    for mode in (cv2.THRESH_BINARY, cv2.THRESH_BINARY_INV):
        _, bw = cv2.threshold(gray_patch, 0, 255, mode + cv2.THRESH_OTSU)
        bw = cv2.GaussianBlur(bw, (3, 3), 0)
        cnts, _ = cv2.findContours(bw, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for cnt in cnts:
            x, y, w, h = cv2.boundingRect(cnt)
            if w < max(8, int(0.008 * pw)) or w > max(40, int(0.12 * pw)):
                continue
            if h < max(8, int(0.15 * ph)) or h > int(0.95 * ph):
                continue
            ratio = w / max(1.0, float(h))
            if ratio < 0.2 or ratio > 2.2:
                continue
            candidates.append(_Box(x=x, y=y, w=w, h=h))
    unique: dict[tuple[int, int, int, int], _Box] = {}
    for box in candidates:
        key = (box.x, box.y, box.w, box.h)
        unique[key] = box
    return sorted(unique.values(), key=lambda b: b.x)


def _pair_temporal_score(patches: List[np.ndarray], left: _Box, right: _Box) -> Optional[float]:
    if len(patches) < 2:
        return None

    prev_left = _prepare_patch_box(patches[0], left)
    prev_right = _prepare_patch_box(patches[0], right)
    if prev_left is None or prev_right is None:
        return None

    diff_sum = 0.0
    count = 0
    for idx in range(1, len(patches)):
        cur_left = _prepare_patch_box(patches[idx], left)
        cur_right = _prepare_patch_box(patches[idx], right)
        if cur_left is None or cur_right is None:
            return None
        diff_sum += _mean_abs_diff(cur_left, prev_left)
        diff_sum += _mean_abs_diff(cur_right, prev_right)
        count += 2
        prev_left = cur_left
        prev_right = cur_right
    if count == 0:
        return None

    left_density = float(np.mean(prev_left > 0))
    right_density = float(np.mean(prev_right > 0))
    density_penalty = abs(left_density - 0.35) + abs(right_density - 0.35)
    return (diff_sum / count) + (density_penalty * 20.0)


def _prepare_patch_box(gray_patch: np.ndarray, box: _Box) -> Optional[np.ndarray]:
    x0 = max(0, box.x)
    y0 = max(0, box.y)
    x1 = min(gray_patch.shape[1], box.x + box.w)
    y1 = min(gray_patch.shape[0], box.y + box.h)
    if x1 <= x0 or y1 <= y0:
        return None
    roi = gray_patch[y0:y1, x0:x1]
    if roi.size == 0:
        return None
    roi = cv2.GaussianBlur(roi, (3, 3), 0)
    roi = cv2.resize(roi, (90, 90), interpolation=cv2.INTER_AREA)
    _, bw = cv2.threshold(roi, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return bw


def _box_to_normalized_roi(
    box: _Box,
    sx0: int,
    sy0: int,
    patch_w: int,
    patch_h: int,
    frame: object,
    *,
    pad: float,
) -> Optional[tuple[float, float, float, float]]:
    try:
        fh = int(frame.shape[0])
        fw = int(frame.shape[1])
    except Exception:
        return None

    pad_x = int(box.w * pad)
    pad_y = int(box.h * pad)
    x0 = max(0, sx0 + box.x - pad_x)
    y0 = max(0, sy0 + box.y - pad_y)
    x1 = min(fw, sx0 + box.x + box.w + pad_x)
    y1 = min(fh, sy0 + box.y + box.h + pad_y)
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0 / fw, y0 / fh, (x1 - x0) / fw, (y1 - y0) / fh)


def _mean_abs_diff(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.mean(cv2.absdiff(a, b)))


def _blend(a: np.ndarray, b: np.ndarray, alpha: float = 0.1) -> np.ndarray:
    return cv2.addWeighted(a, 1.0 - alpha, b, alpha, 0)
