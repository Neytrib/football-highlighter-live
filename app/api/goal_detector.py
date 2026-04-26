from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, List, Set

from app.models import GoalEvent, MatchGoalSnapshot, MatchSnapshot, Scoreline


def generate_goal_id(match_id: int, goal: MatchGoalSnapshot) -> str:
    return (
        f"{match_id}_{goal.minute}_{goal.injury_time}_{goal.team_side}_"
        f"{goal.score_home}_{goal.score_away}"
    )


@dataclass
class DetectedGoal:
    match_id: int
    home_team: str
    away_team: str
    event: GoalEvent


class GoalDetector:
    def detect_new_goals(self, match: MatchSnapshot, seen_goal_ids: Iterable[str]) -> List[DetectedGoal]:
        seen: Set[str] = set(seen_goal_ids)
        detected: List[DetectedGoal] = []

        for goal in match.goals:
            goal_id = generate_goal_id(match.match_id, goal)
            if goal_id in seen:
                continue

            minute_label = f"{goal.minute}+{goal.injury_time}" if goal.injury_time else str(goal.minute)
            event = GoalEvent(
                goal_id=goal_id,
                source="football-data-v4",
                api_detected_at_utc=datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                minute=goal.minute,
                injury_time=goal.injury_time,
                target_minute_label=minute_label,
                team=goal.team_name,
                scorer=goal.scorer,
                assist=goal.assist,
                goal_type=goal.goal_type,
                score_after_goal=Scoreline(home=goal.score_home, away=goal.score_away),
            )
            detected.append(
                DetectedGoal(
                    match_id=match.match_id,
                    home_team=match.home_name,
                    away_team=match.away_name,
                    event=event,
                )
            )
            seen.add(goal_id)

        return detected

    def detect_score_delta_goals(
        self,
        *,
        match: MatchSnapshot,
        seen_goal_ids: Iterable[str],
        previous_score: tuple[int, int] | None,
        inferred_minute: int | None = None,
    ) -> List[DetectedGoal]:
        # If official goal events exist in the payload, trust those and skip fallback.
        if match.goals:
            return []
        if previous_score is None:
            return []

        prev_home, prev_away = previous_score
        curr_home = int(match.score_home)
        curr_away = int(match.score_away)
        if curr_home < prev_home or curr_away < prev_away:
            return []
        if curr_home == prev_home and curr_away == prev_away:
            return []

        seen: Set[str] = set(seen_goal_ids)
        detected: List[DetectedGoal] = []
        now_utc = datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

        minute = inferred_minute if inferred_minute is not None else 0
        minute_label = str(minute) if minute > 0 else "unknown"

        for home_score in range(prev_home + 1, curr_home + 1):
            goal_id = f"{match.match_id}_sd_home_{home_score}_{curr_away}"
            if goal_id in seen:
                continue
            event = GoalEvent(
                goal_id=goal_id,
                source="score-delta-fallback",
                api_detected_at_utc=now_utc,
                minute=minute,
                injury_time=0,
                target_minute_label=minute_label,
                team=match.home_name,
                scorer="",
                assist="",
                goal_type="SCORE_DELTA_FALLBACK",
                score_after_goal=Scoreline(home=home_score, away=curr_away),
            )
            detected.append(
                DetectedGoal(
                    match_id=match.match_id,
                    home_team=match.home_name,
                    away_team=match.away_name,
                    event=event,
                )
            )
            seen.add(goal_id)

        for away_score in range(prev_away + 1, curr_away + 1):
            goal_id = f"{match.match_id}_sd_away_{curr_home}_{away_score}"
            if goal_id in seen:
                continue
            event = GoalEvent(
                goal_id=goal_id,
                source="score-delta-fallback",
                api_detected_at_utc=now_utc,
                minute=minute,
                injury_time=0,
                target_minute_label=minute_label,
                team=match.away_name,
                scorer="",
                assist="",
                goal_type="SCORE_DELTA_FALLBACK",
                score_after_goal=Scoreline(home=curr_home, away=away_score),
            )
            detected.append(
                DetectedGoal(
                    match_id=match.match_id,
                    home_team=match.home_name,
                    away_team=match.away_name,
                    event=event,
                )
            )
            seen.add(goal_id)

        return detected
