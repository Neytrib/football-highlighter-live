from app.api.goal_detector import GoalDetector, generate_goal_id
from app.models import MatchGoalSnapshot, MatchSnapshot


def _build_match() -> MatchSnapshot:
    goal = MatchGoalSnapshot(
        minute=54,
        injury_time=0,
        team_side="home",
        team_name="Team A",
        scorer="Player X",
        assist="Player Y",
        goal_type="REGULAR",
        score_home=2,
        score_away=1,
    )
    return MatchSnapshot(
        match_id=123456,
        utc_date="2026-03-15T18:00:00Z",
        status="LIVE",
        home_name="Team A",
        home_short_name="A",
        home_tla="TMA",
        away_name="Team B",
        away_short_name="B",
        away_tla="TMB",
        score_home=2,
        score_away=1,
        goals=[goal],
    )


def test_generate_goal_id_is_deterministic() -> None:
    match = _build_match()
    goal = match.goals[0]
    assert generate_goal_id(match.match_id, goal) == "123456_54_0_home_2_1"


def test_goal_detector_deduplicates_seen_ids() -> None:
    match = _build_match()
    detector = GoalDetector()

    first = detector.detect_new_goals(match, seen_goal_ids=[])
    assert len(first) == 1

    seen = {first[0].event.goal_id}
    second = detector.detect_new_goals(match, seen_goal_ids=seen)
    assert second == []


def test_score_delta_fallback_generates_goal_when_goals_array_is_empty() -> None:
    match = MatchSnapshot(
        match_id=123456,
        utc_date="2026-03-15T18:00:00Z",
        status="LIVE",
        home_name="Team A",
        home_short_name="A",
        home_tla="TMA",
        away_name="Team B",
        away_short_name="B",
        away_tla="TMB",
        score_home=1,
        score_away=0,
        goals=[],
    )
    detector = GoalDetector()
    detected = detector.detect_score_delta_goals(
        match=match,
        seen_goal_ids=[],
        previous_score=(0, 0),
        inferred_minute=12,
    )
    assert len(detected) == 1
    event = detected[0].event
    assert event.goal_id == "123456_sd_home_1_0"
    assert event.source == "score-delta-fallback"
    assert event.minute == 12
