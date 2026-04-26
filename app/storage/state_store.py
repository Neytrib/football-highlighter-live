from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Set


class StateStore:
    def __init__(self, state_dir: str) -> None:
        self.path = Path(state_dir) / "runtime_state.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._save(
                {
                    "seen_goal_ids": [],
                    "processed_goal_ids": [],
                    "locked_match_id": None,
                    "lock_confidence": 0.0,
                    "match_scores": {},
                }
            )

    def _load(self) -> Dict[str, Any]:
        with self.path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
        payload.setdefault("seen_goal_ids", [])
        payload.setdefault("processed_goal_ids", [])
        payload.setdefault("locked_match_id", None)
        payload.setdefault("lock_confidence", 0.0)
        payload.setdefault("match_scores", {})
        return payload

    def _save(self, payload: Dict[str, Any]) -> None:
        with self.path.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=True, indent=2)

    def seen_goal_ids(self) -> Set[str]:
        return set(self._load().get("seen_goal_ids", []))

    def add_seen_goal_id(self, goal_id: str) -> None:
        state = self._load()
        seen = set(state.get("seen_goal_ids", []))
        seen.add(goal_id)
        state["seen_goal_ids"] = sorted(seen)
        self._save(state)

    def processed_goal_ids(self) -> Set[str]:
        return set(self._load().get("processed_goal_ids", []))

    def mark_processed(self, goal_id: str) -> None:
        state = self._load()
        processed = set(state.get("processed_goal_ids", []))
        processed.add(goal_id)
        state["processed_goal_ids"] = sorted(processed)
        self._save(state)

    def get_locked_match(self) -> tuple[Optional[int], float]:
        state = self._load()
        return state.get("locked_match_id"), float(state.get("lock_confidence", 0.0))

    def set_locked_match(self, match_id: Optional[int], confidence: float) -> None:
        state = self._load()
        state["locked_match_id"] = match_id
        state["lock_confidence"] = float(confidence)
        self._save(state)

    def get_match_score(self, match_id: int) -> Optional[tuple[int, int]]:
        state = self._load()
        match_scores = state.get("match_scores", {})
        raw = match_scores.get(str(match_id))
        if not isinstance(raw, dict):
            return None
        try:
            home = int(raw.get("home"))
            away = int(raw.get("away"))
        except (TypeError, ValueError):
            return None
        return home, away

    def set_match_score(self, match_id: int, home: int, away: int) -> None:
        state = self._load()
        match_scores = state.setdefault("match_scores", {})
        match_scores[str(match_id)] = {"home": int(home), "away": int(away)}
        self._save(state)
