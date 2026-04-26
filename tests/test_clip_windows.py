from app.pipeline.clip_scheduler import compute_clip_window


def test_default_window_math() -> None:
    window = compute_clip_window(1000.0, pre_goal_seconds=45, post_goal_seconds=30)
    assert window.start_ts == 955.0
    assert window.end_ts == 1030.0


def test_custom_window_math() -> None:
    window = compute_clip_window(500.0, pre_goal_seconds=20, post_goal_seconds=15)
    assert window.start_ts == 480.0
    assert window.end_ts == 515.0
