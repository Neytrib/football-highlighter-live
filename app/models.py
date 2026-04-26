from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Scoreline:
    home: int
    away: int


@dataclass
class GoalEvent:
    goal_id: str
    source: str
    api_detected_at_utc: str
    minute: int
    injury_time: int
    target_minute_label: str
    team: str
    scorer: str
    assist: str
    goal_type: str
    score_after_goal: Scoreline
    ocr_goal_time: Optional[str] = None
    stream_goal_ts_unix: Optional[float] = None
    clip_status: str = "PENDING"
    raw_clip_path: Optional[str] = None
    cropped_clip_path: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["score_after_goal"] = asdict(self.score_after_goal)
        return data


@dataclass
class MatchGoalSnapshot:
    minute: int
    injury_time: int
    team_side: str
    team_name: str
    scorer: str
    assist: str
    goal_type: str
    score_home: int
    score_away: int


@dataclass
class MatchSnapshot:
    match_id: int
    utc_date: str
    status: str
    home_name: str
    home_short_name: str
    home_tla: str
    away_name: str
    away_short_name: str
    away_tla: str
    score_home: int
    score_away: int
    goals: List[MatchGoalSnapshot] = field(default_factory=list)


@dataclass
class SegmentMeta:
    start_ts: float
    end_ts: float
    path: str


@dataclass
class TimerReading:
    raw_text: str
    minute: Optional[int]
    second: Optional[int]
    confidence: float

    @property
    def total_seconds(self) -> Optional[int]:
        if self.minute is None or self.second is None:
            return None
        return self.minute * 60 + self.second


@dataclass
class ScoreboardReading:
    home_label: Optional[str]
    away_label: Optional[str]
    home_score: Optional[int]
    away_score: Optional[int]
    confidence: float


@dataclass
class PendingClipJob:
    match_id: int
    goal_id: str
    minute: int
    injury_time: int
    expected_score_home: int
    expected_score_away: int
    triggered: bool = False
