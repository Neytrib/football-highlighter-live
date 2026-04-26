from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import List, Optional

from rapidfuzz import fuzz

from app.models import MatchSnapshot, ScoreboardReading


@dataclass
class MatchResolution:
    match: Optional[MatchSnapshot]
    confidence: float


@dataclass
class MatchCandidateScore:
    match_id: int
    home_name: str
    away_name: str
    score: float


class MatchResolver:
    def __init__(self, *, lock_threshold: float = 65.0, logger: Optional[logging.Logger] = None) -> None:
        self.lock_threshold = lock_threshold
        self.logger = logger or logging.getLogger(__name__)

    def score_candidates(self, scoreboard: ScoreboardReading, live_matches: List[MatchSnapshot]) -> List[MatchCandidateScore]:
        scores: List[MatchCandidateScore] = []
        for match in live_matches:
            score = self._score_match(scoreboard, match)
            scores.append(
                MatchCandidateScore(
                    match_id=match.match_id,
                    home_name=match.home_name,
                    away_name=match.away_name,
                    score=score,
                )
            )
        scores.sort(key=lambda item: item.score, reverse=True)
        return scores

    def resolve(
        self,
        *,
        scoreboard: ScoreboardReading,
        live_matches: List[MatchSnapshot],
        manual_match_id: Optional[int] = None,
    ) -> MatchResolution:
        if manual_match_id is not None:
            for match in live_matches:
                if match.match_id == manual_match_id:
                    return MatchResolution(match=match, confidence=100.0)
            return MatchResolution(match=None, confidence=0.0)

        candidates = self.score_candidates(scoreboard, live_matches)
        if not candidates:
            return MatchResolution(match=None, confidence=0.0)

        best = candidates[0]
        self.logger.debug(
            "match resolver candidates",
            extra={
                "extra": {
                    "home_label": scoreboard.home_label,
                    "away_label": scoreboard.away_label,
                    "home_score": scoreboard.home_score,
                    "away_score": scoreboard.away_score,
                    "top_candidates": [
                        {
                            "match_id": c.match_id,
                            "home": c.home_name,
                            "away": c.away_name,
                            "score": round(c.score, 3),
                        }
                        for c in candidates[:5]
                    ],
                    "lock_threshold": self.lock_threshold,
                }
            },
        )

        best_match: Optional[MatchSnapshot] = None
        for match in live_matches:
            if match.match_id == best.match_id:
                best_match = match
                break
        if best_match is None:
            return MatchResolution(match=None, confidence=0.0)

        if best.score < self.lock_threshold:
            return MatchResolution(match=None, confidence=best.score)
        return MatchResolution(match=best_match, confidence=best.score)

    def _score_match(self, scoreboard: ScoreboardReading, match: MatchSnapshot) -> float:
        team_score = 0.0
        home_label = (scoreboard.home_label or "").strip()
        away_label = (scoreboard.away_label or "").strip()

        if home_label:
            team_score += self._best_name_score(home_label, [match.home_name, match.home_short_name, match.home_tla])
        if away_label:
            team_score += self._best_name_score(away_label, [match.away_name, match.away_short_name, match.away_tla])

        team_score = team_score / 2.0 if home_label and away_label else team_score

        scoreline_bonus = 0.0
        if scoreboard.home_score is not None and scoreboard.away_score is not None:
            if scoreboard.home_score == match.score_home and scoreboard.away_score == match.score_away:
                scoreline_bonus = 15.0

        return min(100.0, team_score + scoreline_bonus)

    @staticmethod
    def _best_name_score(observed: str, candidates: List[str]) -> float:
        cleaned = [candidate for candidate in candidates if candidate]
        if not cleaned:
            return 0.0
        return max(float(fuzz.token_set_ratio(observed, candidate)) for candidate in cleaned)
