from __future__ import annotations

import logging
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol

import numpy as np

try:
    import cv2  # type: ignore
except ImportError:  # pragma: no cover - runtime optional
    cv2 = None


@dataclass(frozen=True)
class ScoreReading:
    home_score: Optional[int]
    away_score: Optional[int]
    confidence: float
    raw_home: str = ""
    raw_away: str = ""
    reason: str = ""

    @property
    def score(self) -> Optional[tuple[int, int]]:
        if self.home_score is None or self.away_score is None:
            return None
        return self.home_score, self.away_score


@dataclass
class ScoreChangeEvent:
    goal_ts_unix: float
    diff_value: float = 0.0
    changed_home: bool = False
    changed_away: bool = False
    previous_score_home: int = 0
    previous_score_away: int = 0
    score_home: int = 0
    score_away: int = 0
    confidence: float = 0.0
    event_kind: str = "GOAL"
    reason: str = ""


class ScoreReader(Protocol):
    def read(self, frame: object) -> ScoreReading:
        ...


@dataclass
class _VarWatch:
    goal_ts: float
    previous_score: tuple[int, int]
    score_after: tuple[int, int]


class TesseractScoreReader:
    def __init__(
        self,
        *,
        home_score_roi: tuple[float, float, float, float],
        away_score_roi: tuple[float, float, float, float],
        tesseract_cmd: str,
        temp_dir: str,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.home_score_roi = home_score_roi
        self.away_score_roi = away_score_roi
        self.tesseract_cmd = tesseract_cmd
        self.temp_dir = Path(temp_dir)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.logger = logger or logging.getLogger(__name__)

    def read(self, frame: object) -> ScoreReading:
        if cv2 is None:
            raise RuntimeError("opencv-python is required for score OCR")

        home = self._read_digit_roi(frame, self.home_score_roi, "home")
        away = self._read_digit_roi(frame, self.away_score_roi, "away")
        if home[0] is None or away[0] is None:
            return ScoreReading(
                home_score=home[0],
                away_score=away[0],
                confidence=min(home[1], away[1]),
                raw_home=home[2],
                raw_away=away[2],
                reason="score_ocr_missing_digit",
            )

        confidence = min(home[1], away[1])
        return ScoreReading(
            home_score=home[0],
            away_score=away[0],
            confidence=confidence,
            raw_home=home[2],
            raw_away=away[2],
        )

    def _read_digit_roi(
        self,
        frame: object,
        roi_rect: tuple[float, float, float, float],
        label: str,
    ) -> tuple[Optional[int], float, str]:
        patch = _crop_roi(frame, roi_rect)
        if patch is None:
            return None, 0.0, ""

        best_text = ""
        best_value: Optional[int] = None
        for idx, image in enumerate(_score_digit_variants(patch)):
            text = self._run_tesseract(image, f"{label}_{idx}")
            value = _parse_score_digits(text)
            if value is not None:
                return value, 0.85, text
            if text.strip():
                best_text = text

        return best_value, 0.0, best_text

    def _run_tesseract(self, image: np.ndarray, stem: str) -> str:
        path = self.temp_dir / f"score_{stem}_{time.time_ns()}.png"
        try:
            cv2.imwrite(str(path), image)
            cmd = [
                self.tesseract_cmd,
                str(path),
                "stdout",
                "--psm",
                "10",
                "-c",
                "tessedit_char_whitelist=0123456789",
            ]
            result = subprocess.run(cmd, check=False, capture_output=True, text=True)
            if result.returncode != 0:
                self.logger.debug(
                    "tesseract score OCR failed",
                    extra={"extra": {"return_code": result.returncode, "stderr": result.stderr.strip()}},
                )
                return ""
            return result.stdout.strip()
        except FileNotFoundError:
            self.logger.warning("tesseract binary not found", extra={"extra": {"cmd": self.tesseract_cmd}})
            return ""
        finally:
            try:
                path.unlink()
            except OSError:
                pass


class ScoreChangeDetector:
    def __init__(
        self,
        *,
        home_score_roi: tuple[float, float, float, float],
        away_score_roi: tuple[float, float, float, float],
        search_roi: tuple[float, float, float, float],
        auto_locate_score_rois: bool = False,
        auto_locate_frames: int = 30,
        change_threshold: float = 18.0,
        stable_threshold: float = 6.0,
        stable_frames: int = 3,
        cooldown_seconds: int = 15,
        score_stable_frames: Optional[int] = None,
        min_confidence: float = 0.65,
        tesseract_cmd: str = "tesseract",
        temp_dir: str = "data/tmp/score_ocr",
        var_watch_seconds: int = 300,
        uncertain_cooldown_seconds: int = 20,
        score_reader: Optional[ScoreReader] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.home_score_roi = home_score_roi
        self.away_score_roi = away_score_roi
        self.search_roi = search_roi
        self.stable_frames = max(1, int(score_stable_frames if score_stable_frames is not None else stable_frames))
        self.min_confidence = float(min_confidence)
        self.var_watch_seconds = max(1, int(var_watch_seconds))
        self.uncertain_cooldown_seconds = max(1, int(uncertain_cooldown_seconds))
        self.logger = logger or logging.getLogger(__name__)
        self.reader = score_reader or TesseractScoreReader(
            home_score_roi=home_score_roi,
            away_score_roi=away_score_roi,
            tesseract_cmd=tesseract_cmd,
            temp_dir=temp_dir,
            logger=self.logger,
        )

        self._stable_score: Optional[tuple[int, int]] = None
        self._candidate_score: Optional[tuple[int, int]] = None
        self._candidate_first_ts: float = 0.0
        self._candidate_count: int = 0
        self._candidate_confidence: float = 0.0
        self._candidate_reason: str = ""
        self._var_watches: list[_VarWatch] = []
        self._last_uncertain: Optional[tuple[tuple[int, int], tuple[int, int], str, float]] = None

        self.logger.info(
            "score value detector initialized",
            extra={
                "extra": {
                    "home_score_roi": list(home_score_roi),
                    "away_score_roi": list(away_score_roi),
                    "search_roi": list(search_roi),
                    "stable_frames": self.stable_frames,
                    "min_confidence": self.min_confidence,
                    "var_watch_seconds": self.var_watch_seconds,
                    "uncertain_cooldown_seconds": self.uncertain_cooldown_seconds,
                    "auto_locate_score_rois_ignored": bool(auto_locate_score_rois),
                    "legacy_change_threshold_ignored": change_threshold,
                    "legacy_stable_threshold_ignored": stable_threshold,
                    "legacy_cooldown_seconds_ignored": cooldown_seconds,
                    "legacy_auto_locate_frames_ignored": auto_locate_frames,
                }
            },
        )

    def process(self, frame: object, frame_ts: float) -> Optional[ScoreChangeEvent]:
        reading = self.reader.read(frame)
        score = reading.score
        if score is None:
            self._reset_candidate()
            self.logger.debug(
                "score OCR produced no usable score",
                extra={
                    "extra": {
                        "reason": reading.reason,
                        "raw_home": reading.raw_home,
                        "raw_away": reading.raw_away,
                        "confidence": round(reading.confidence, 3),
                    }
                },
            )
            return None

        if self._candidate_score != score:
            self._candidate_score = score
            self._candidate_first_ts = frame_ts
            self._candidate_count = 1
            self._candidate_confidence = reading.confidence
            self._candidate_reason = reading.reason
            if self.stable_frames > 1:
                return None
        else:
            self._candidate_count += 1
            self._candidate_confidence = min(self._candidate_confidence, reading.confidence)
            if reading.reason and not self._candidate_reason:
                self._candidate_reason = reading.reason
            if self._candidate_count < self.stable_frames:
                return None

        event = self._accept_stable_score(
            score=score,
            score_ts=self._candidate_first_ts,
            confidence=self._candidate_confidence,
            reason=self._candidate_reason,
        )
        self._reset_candidate()
        return event

    def _accept_stable_score(
        self,
        *,
        score: tuple[int, int],
        score_ts: float,
        confidence: float,
        reason: str,
    ) -> Optional[ScoreChangeEvent]:
        self._prune_var_watches(score_ts)
        previous = self._stable_score
        if previous is None:
            self._stable_score = score
            self.logger.info(
                "score baseline established",
                extra={"extra": {"home": score[0], "away": score[1], "confidence": round(confidence, 3)}},
            )
            return None

        if score == previous:
            return None

        if confidence < self.min_confidence:
            return self._uncertain_event(
                previous=previous,
                score=score,
                score_ts=score_ts,
                confidence=confidence,
                reason=reason or "score_ocr_low_confidence",
                update_baseline=False,
            )

        home_delta = score[0] - previous[0]
        away_delta = score[1] - previous[1]
        if (home_delta, away_delta) in ((1, 0), (0, 1)):
            self._stable_score = score
            self._var_watches.append(_VarWatch(goal_ts=score_ts, previous_score=previous, score_after=score))
            event = self._event(
                previous=previous,
                score=score,
                score_ts=score_ts,
                confidence=confidence,
                event_kind="GOAL",
                reason="score_increased",
            )
            self.logger.warning(
                "confirmed score increase from stream",
                extra={
                    "extra": {
                        "previous_score": {"home": previous[0], "away": previous[1]},
                        "score": {"home": score[0], "away": score[1]},
                        "goal_ts_unix": round(score_ts, 3),
                        "confidence": round(confidence, 3),
                    }
                },
            )
            return event

        watch = self._matching_var_watch(previous=previous, score=score, frame_ts=score_ts)
        if watch is not None:
            self._stable_score = score
            self._var_watches.remove(watch)
            event = self._event(
                previous=previous,
                score=score,
                score_ts=score_ts,
                confidence=confidence,
                event_kind="VAR_REVERSAL",
                reason="score_reverted_within_var_window",
            )
            self.logger.warning(
                "VAR reversal score change from stream",
                extra={
                    "extra": {
                        "previous_score": {"home": previous[0], "away": previous[1]},
                        "score": {"home": score[0], "away": score[1]},
                        "reversal_ts_unix": round(score_ts, 3),
                    }
                },
            )
            return event

        reason = _uncertain_reason(home_delta, away_delta)
        update_baseline = home_delta >= 0 and away_delta >= 0
        return self._uncertain_event(
            previous=previous,
            score=score,
            score_ts=score_ts,
            confidence=confidence,
            reason=reason,
            update_baseline=update_baseline,
        )

    def _matching_var_watch(
        self,
        *,
        previous: tuple[int, int],
        score: tuple[int, int],
        frame_ts: float,
    ) -> Optional[_VarWatch]:
        for watch in self._var_watches:
            if frame_ts - watch.goal_ts > self.var_watch_seconds:
                continue
            if previous == watch.score_after and score == watch.previous_score:
                return watch
        return None

    def _uncertain_event(
        self,
        *,
        previous: tuple[int, int],
        score: tuple[int, int],
        score_ts: float,
        confidence: float,
        reason: str,
        update_baseline: bool,
    ) -> Optional[ScoreChangeEvent]:
        if self._last_uncertain is not None:
            last_previous, last_score, last_reason, last_ts = self._last_uncertain
            if (
                last_previous == previous
                and last_score == score
                and last_reason == reason
                and score_ts - last_ts < self.uncertain_cooldown_seconds
            ):
                return None

        self._last_uncertain = (previous, score, reason, score_ts)
        if update_baseline:
            self._stable_score = score

        self.logger.warning(
            "uncertain score change from stream",
            extra={
                "extra": {
                    "previous_score": {"home": previous[0], "away": previous[1]},
                    "score": {"home": score[0], "away": score[1]},
                    "reason": reason,
                    "score_ts_unix": round(score_ts, 3),
                    "confidence": round(confidence, 3),
                }
            },
        )
        return self._event(
            previous=previous,
            score=score,
            score_ts=score_ts,
            confidence=confidence,
            event_kind="UNCERTAIN",
            reason=reason,
        )

    def _event(
        self,
        *,
        previous: tuple[int, int],
        score: tuple[int, int],
        score_ts: float,
        confidence: float,
        event_kind: str,
        reason: str,
    ) -> ScoreChangeEvent:
        home_delta = score[0] - previous[0]
        away_delta = score[1] - previous[1]
        return ScoreChangeEvent(
            goal_ts_unix=score_ts,
            diff_value=float(max(abs(home_delta), abs(away_delta))),
            changed_home=home_delta != 0,
            changed_away=away_delta != 0,
            previous_score_home=previous[0],
            previous_score_away=previous[1],
            score_home=score[0],
            score_away=score[1],
            confidence=confidence,
            event_kind=event_kind,
            reason=reason,
        )

    def _prune_var_watches(self, frame_ts: float) -> None:
        self._var_watches = [watch for watch in self._var_watches if frame_ts - watch.goal_ts <= self.var_watch_seconds]

    def _reset_candidate(self) -> None:
        self._candidate_score = None
        self._candidate_first_ts = 0.0
        self._candidate_count = 0
        self._candidate_confidence = 0.0
        self._candidate_reason = ""


def write_score_roi_previews(
    *,
    frame: object,
    output_dir: str,
    search_roi: tuple[float, float, float, float],
    home_score_roi: tuple[float, float, float, float],
    away_score_roi: tuple[float, float, float, float],
) -> list[str]:
    if cv2 is None:
        raise RuntimeError("opencv-python is required for score ROI previews")

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for name, roi in [
        ("full_frame", (0.0, 0.0, 1.0, 1.0)),
        ("score_search_roi", search_roi),
        ("home_score_roi", home_score_roi),
        ("away_score_roi", away_score_roi),
    ]:
        patch = _crop_roi(frame, roi)
        if patch is None:
            continue
        path = out / f"{name}.jpg"
        cv2.imwrite(str(path), patch)
        written.append(str(path))

    for name, roi in [("home_score_processed", home_score_roi), ("away_score_processed", away_score_roi)]:
        patch = _crop_roi(frame, roi)
        if patch is None:
            continue
        variants = _score_digit_variants(patch)
        if not variants:
            continue
        path = out / f"{name}.png"
        cv2.imwrite(str(path), variants[0])
        written.append(str(path))
    return written


def _crop_roi(frame: object, roi_rect: tuple[float, float, float, float]) -> Optional[np.ndarray]:
    bounds = _roi_bounds_from_frame(frame, roi_rect)
    if bounds is None:
        return None
    x0, y0, x1, y1 = bounds
    roi = frame[y0:y1, x0:x1]
    if roi is None or roi.size == 0:
        return None
    return roi


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


def _score_digit_variants(patch: np.ndarray) -> list[np.ndarray]:
    if cv2 is None:
        return []

    gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    scale = 5
    gray_big = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)

    _, otsu = cv2.threshold(gray_big, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    _, otsu_inv = cv2.threshold(gray_big, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    bright = cv2.inRange(gray_big, 165, 255)
    dark_text = cv2.inRange(gray_big, 0, 120)

    variants = [bright, otsu, otsu_inv, dark_text]
    bordered: list[np.ndarray] = []
    for image in variants:
        cleaned = cv2.morphologyEx(image, cv2.MORPH_OPEN, np.ones((2, 2), dtype=np.uint8))
        bordered.append(cv2.copyMakeBorder(cleaned, 30, 30, 30, 30, cv2.BORDER_CONSTANT, value=0))
    return bordered


def _parse_score_digits(raw_text: str) -> Optional[int]:
    cleaned = re.sub(r"\D+", "", raw_text or "")
    if not cleaned:
        return None
    try:
        value = int(cleaned[:2])
    except ValueError:
        return None
    if value < 0 or value > 99:
        return None
    return value


def _uncertain_reason(home_delta: int, away_delta: int) -> str:
    if home_delta < 0 or away_delta < 0:
        return "score_decreased_without_active_var_watch"
    if home_delta > 0 and away_delta > 0:
        return "both_scores_changed"
    if home_delta > 1 or away_delta > 1:
        return "score_jump_greater_than_one"
    return "unsupported_score_transition"
