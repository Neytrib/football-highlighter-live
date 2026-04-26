from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.models import GoalEvent


class GoalJsonStore:
    def __init__(self, goals_dir: str) -> None:
        self.goals_dir = Path(goals_dir)
        self.goals_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, match_id: int) -> Path:
        return self.goals_dir / f"{match_id}.json"

    def _load(self, match_id: int, home_team: str = "", away_team: str = "") -> Dict[str, Any]:
        path = self._path(match_id)
        if not path.exists():
            return {
                "match_id": match_id,
                "home_team": home_team,
                "away_team": away_team,
                "events": [],
            }
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    def _save(self, match_id: int, payload: Dict[str, Any]) -> None:
        path = self._path(match_id)
        with path.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=True, indent=2)

    def append_or_update_goal(
        self,
        *,
        match_id: int,
        home_team: str,
        away_team: str,
        goal_event: GoalEvent,
    ) -> None:
        payload = self._load(match_id, home_team, away_team)
        payload["home_team"] = home_team
        payload["away_team"] = away_team

        events: List[Dict[str, Any]] = payload.setdefault("events", [])
        incoming = goal_event.to_dict()
        for idx, event in enumerate(events):
            if event.get("goal_id") == goal_event.goal_id:
                events[idx] = incoming
                self._save(match_id, payload)
                return

        events.append(incoming)
        self._save(match_id, payload)

    def find_goal(self, match_id: int, goal_id: str) -> Optional[Dict[str, Any]]:
        payload = self._load(match_id)
        for event in payload.get("events", []):
            if event.get("goal_id") == goal_id:
                return event
        return None

    def list_goals(self, match_id: int) -> List[Dict[str, Any]]:
        payload = self._load(match_id)
        return list(payload.get("events", []))
