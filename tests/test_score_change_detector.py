import numpy as np
import pytest

from app.vision.score_change_detector import ScoreChangeDetector, cv2


if cv2 is None:  # pragma: no cover - optional dependency gate
    pytest.skip("opencv-python is required for score-change detector tests", allow_module_level=True)


def _frame(fill: int) -> np.ndarray:
    img = np.zeros((120, 240, 3), dtype=np.uint8)
    img[:, :] = fill
    return img


def test_score_change_detector_emits_after_stabilization() -> None:
    detector = ScoreChangeDetector(
        home_score_roi=(0.0, 0.0, 0.5, 1.0),
        away_score_roi=(0.5, 0.0, 0.5, 1.0),
        search_roi=(0.0, 0.0, 1.0, 1.0),
        auto_locate_score_rois=False,
        auto_locate_frames=30,
        change_threshold=5.0,
        stable_threshold=1.0,
        stable_frames=2,
        cooldown_seconds=10,
    )

    black = _frame(0)
    white = _frame(255)

    assert detector.process(black, 1.0) is None
    assert detector.process(white, 2.0) is None
    event = detector.process(white, 3.0)
    assert event is not None
    assert event.goal_ts_unix == 2.0


def test_score_change_detector_respects_cooldown() -> None:
    detector = ScoreChangeDetector(
        home_score_roi=(0.0, 0.0, 0.5, 1.0),
        away_score_roi=(0.5, 0.0, 0.5, 1.0),
        search_roi=(0.0, 0.0, 1.0, 1.0),
        auto_locate_score_rois=False,
        auto_locate_frames=30,
        change_threshold=5.0,
        stable_threshold=1.0,
        stable_frames=2,
        cooldown_seconds=10,
    )

    black = _frame(0)
    white = _frame(255)

    assert detector.process(black, 1.0) is None
    assert detector.process(white, 2.0) is None
    assert detector.process(white, 3.0) is not None

    assert detector.process(black, 4.0) is None
    assert detector.process(black, 5.0) is None


def test_motion_outside_score_boxes_does_not_trigger() -> None:
    detector = ScoreChangeDetector(
        home_score_roi=(0.35, 0.0, 0.1, 0.2),
        away_score_roi=(0.55, 0.0, 0.1, 0.2),
        search_roi=(0.0, 0.0, 1.0, 0.2),
        auto_locate_score_rois=False,
        auto_locate_frames=30,
        change_threshold=5.0,
        stable_threshold=1.0,
        stable_frames=2,
        cooldown_seconds=10,
    )

    base = _frame(0)
    noisy = _frame(0)
    noisy[60:110, 20:120] = 255  # motion outside configured score boxes

    assert detector.process(base, 1.0) is None
    assert detector.process(noisy, 2.0) is None
    assert detector.process(noisy, 3.0) is None
