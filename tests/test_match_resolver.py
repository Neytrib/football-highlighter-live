from app.models import MatchSnapshot, ScoreboardReading
from app.vision.match_resolver import MatchResolver


def _match(match_id: int, home: str, away: str, score_home: int, score_away: int) -> MatchSnapshot:
    return MatchSnapshot(
        match_id=match_id,
        utc_date="2026-03-15T18:00:00Z",
        status="LIVE",
        home_name=home,
        home_short_name=home,
        home_tla=home[:3].upper(),
        away_name=away,
        away_short_name=away,
        away_tla=away[:3].upper(),
        score_home=score_home,
        score_away=score_away,
        goals=[],
    )


def test_match_resolver_picks_best_fuzzy_candidate() -> None:
    matches = [
        _match(1, "Real Madrid", "Barcelona", 2, 1),
        _match(2, "Chelsea", "Arsenal", 0, 0),
    ]
    scoreboard = ScoreboardReading(
        home_label="R. Madrid",
        away_label="Barca",
        home_score=2,
        away_score=1,
        confidence=0.9,
    )

    resolver = MatchResolver(lock_threshold=60.0)
    resolution = resolver.resolve(scoreboard=scoreboard, live_matches=matches)

    assert resolution.match is not None
    assert resolution.match.match_id == 1
    assert resolution.confidence >= 60.0


def test_manual_match_override_wins() -> None:
    matches = [
        _match(11, "Team One", "Team Two", 1, 0),
        _match(12, "Team Three", "Team Four", 0, 0),
    ]
    scoreboard = ScoreboardReading(None, None, None, None, 0.0)

    resolver = MatchResolver()
    resolution = resolver.resolve(scoreboard=scoreboard, live_matches=matches, manual_match_id=12)
    assert resolution.match is not None
    assert resolution.match.match_id == 12
    assert resolution.confidence == 100.0
