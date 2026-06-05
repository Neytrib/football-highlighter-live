import numpy as np

from app.vision.cropper import ClipCropper


def test_pick_detection_prefers_target_class() -> None:
    cropper = ClipCropper(
        detector_model_path="models/soccer_yolov8s.pt",
        detection_frame_stride=3,
        smoothing_alpha=0.25,
        target_class_names=("ball",),
        min_confidence=0.1,
    )

    xyxy = np.array(
        [
            [100.0, 100.0, 260.0, 260.0],  # class 1, larger
            [400.0, 200.0, 430.0, 230.0],  # class 0, smaller
        ],
        dtype=float,
    )
    conf = np.array([0.95, 0.60], dtype=float)
    cls = np.array([1, 0], dtype=int)

    best = cropper._pick_detection_index(xyxy=xyxy, conf=conf, cls=cls, target_class_ids={0})
    assert best == 1


def test_pick_detection_returns_none_when_confidence_too_low() -> None:
    cropper = ClipCropper(
        detector_model_path="models/soccer_yolov8s.pt",
        detection_frame_stride=3,
        smoothing_alpha=0.25,
        target_class_names=("ball",),
        min_confidence=0.9,
    )

    xyxy = np.array([[10.0, 10.0, 20.0, 20.0]], dtype=float)
    conf = np.array([0.5], dtype=float)
    cls = np.array([0], dtype=int)

    assert cropper._pick_detection_index(xyxy=xyxy, conf=conf, cls=cls, target_class_ids={0}) is None


def test_pick_action_center_prefers_ball_then_player_cluster() -> None:
    cropper = ClipCropper(
        detector_model_path="models/soccer_yolov8s.pt",
        detection_frame_stride=3,
        smoothing_alpha=0.25,
        target_class_names=("ball", "player", "goalkeeper", "referee"),
        min_confidence=0.1,
    )

    xyxy = np.array(
        [
            [100.0, 100.0, 200.0, 300.0],  # player
            [300.0, 100.0, 400.0, 300.0],  # player
            [700.0, 500.0, 720.0, 520.0],  # ball
        ],
        dtype=float,
    )
    conf = np.array([0.95, 0.95, 0.5], dtype=float)
    cls = np.array([1, 1, 0], dtype=int)

    assert cropper._pick_action_center(xyxy=xyxy, conf=conf, cls=cls, names={0: "ball", 1: "player"}) == (710, 510)

    center = cropper._pick_action_center(xyxy=xyxy[:2], conf=conf[:2], cls=cls[:2], names={0: "ball", 1: "player"})
    assert center == (250, 200)
