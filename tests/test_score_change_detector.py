from app.vision.score_change_detector import ScoreChangeDetector, ScoreReading


class _FakeScoreReader:
    def __init__(self, readings: list[ScoreReading]) -> None:
        self.readings = readings

    def read(self, frame: object) -> ScoreReading:
        return self.readings.pop(0)


def _reading(home: int, away: int, confidence: float = 0.9) -> ScoreReading:
    return ScoreReading(home_score=home, away_score=away, confidence=confidence)


def _detector(readings: list[ScoreReading], *, stable_frames: int = 2) -> ScoreChangeDetector:
    return ScoreChangeDetector(
        home_score_roi=(0.0, 0.0, 0.5, 1.0),
        away_score_roi=(0.5, 0.0, 0.5, 1.0),
        search_roi=(0.0, 0.0, 1.0, 1.0),
        score_stable_frames=stable_frames,
        min_confidence=0.65,
        var_watch_seconds=300,
        score_reader=_FakeScoreReader(readings),
    )


def test_score_increase_emits_confirmed_goal_after_stabilization() -> None:
    detector = _detector([_reading(0, 0), _reading(0, 0), _reading(1, 0), _reading(1, 0)])

    assert detector.process(None, 1.0) is None
    assert detector.process(None, 2.0) is None
    assert detector.process(None, 3.0) is None
    event = detector.process(None, 4.0)

    assert event is not None
    assert event.event_kind == "GOAL"
    assert event.goal_ts_unix == 3.0
    assert event.previous_score_home == 0
    assert event.previous_score_away == 0
    assert event.score_home == 1
    assert event.score_away == 0


def test_unchanged_score_does_not_emit_for_timer_or_visibility_noise() -> None:
    detector = _detector([_reading(0, 0), _reading(0, 0), _reading(0, 0), _reading(0, 0)])

    assert detector.process(None, 1.0) is None
    assert detector.process(None, 2.0) is None
    assert detector.process(None, 3.0) is None
    assert detector.process(None, 4.0) is None


def test_low_confidence_score_change_goes_to_uncertain() -> None:
    detector = _detector([_reading(0, 0), _reading(0, 0), _reading(1, 0, confidence=0.4), _reading(1, 0, confidence=0.4)])

    assert detector.process(None, 1.0) is None
    assert detector.process(None, 2.0) is None
    assert detector.process(None, 3.0) is None
    event = detector.process(None, 4.0)

    assert event is not None
    assert event.event_kind == "UNCERTAIN"
    assert event.reason == "score_ocr_low_confidence"


def test_var_reversal_emits_separate_event_inside_watch_window() -> None:
    detector = _detector(
        [
            _reading(0, 0),
            _reading(0, 0),
            _reading(1, 0),
            _reading(1, 0),
            _reading(0, 0),
            _reading(0, 0),
        ]
    )

    assert detector.process(None, 1.0) is None
    assert detector.process(None, 2.0) is None
    assert detector.process(None, 3.0) is None
    goal = detector.process(None, 4.0)
    assert goal is not None
    assert goal.event_kind == "GOAL"

    assert detector.process(None, 120.0) is None
    reversal = detector.process(None, 121.0)
    assert reversal is not None
    assert reversal.event_kind == "VAR_REVERSAL"
    assert reversal.reason == "score_reverted_within_var_window"


def test_score_jump_goes_to_uncertain() -> None:
    detector = _detector([_reading(0, 0), _reading(0, 0), _reading(2, 0), _reading(2, 0)])

    assert detector.process(None, 1.0) is None
    assert detector.process(None, 2.0) is None
    assert detector.process(None, 3.0) is None
    event = detector.process(None, 4.0)

    assert event is not None
    assert event.event_kind == "UNCERTAIN"
    assert event.reason == "score_jump_greater_than_one"
