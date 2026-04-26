from __future__ import annotations

import logging
import time
from typing import List, Optional

import requests

from app.config import ApiConfig
from app.models import MatchGoalSnapshot, MatchSnapshot


class FootballDataClient:
    def __init__(
        self,
        *,
        api_token: str,
        base_url: str,
        config: ApiConfig,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.api_token = api_token
        self.base_url = base_url.rstrip("/")
        self.config = config
        self.logger = logger or logging.getLogger(__name__)
        self.session = requests.Session()
        self.next_allowed_at = 0.0
        self.min_interval = max(60.0 / 10.0, float(self.config.poll_interval_seconds))

    def _headers(self) -> dict[str, str]:
        headers = {"X-Auth-Token": self.api_token}
        if self.config.unfold_goals:
            headers["X-Unfold-Goals"] = "true"
        return headers

    def _respect_local_throttle(self) -> None:
        now = time.time()
        wait = self.next_allowed_at - now
        if wait > 0:
            self.logger.debug("throttling football-data request", extra={"extra": {"wait_seconds": round(wait, 3)}})
            time.sleep(wait)

    @staticmethod
    def _read_reset_seconds(response: requests.Response) -> Optional[float]:
        raw = response.headers.get("X-RequestCounter-Reset")
        if not raw:
            return None
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return None

    def _update_rate_limit_state(self, response: requests.Response) -> None:
        now = time.time()
        self.next_allowed_at = max(self.next_allowed_at, now + self.min_interval)

        remaining_raw = response.headers.get("X-RequestsAvailable")
        if remaining_raw is None:
            self.logger.debug(
                "football-data response missing rate-limit headers",
                extra={"extra": {"status_code": response.status_code}},
            )
            return

        try:
            remaining = int(remaining_raw)
        except ValueError:
            self.logger.debug(
                "football-data returned invalid X-RequestsAvailable header",
                extra={"extra": {"header_value": remaining_raw}},
            )
            return

        if remaining <= 0:
            reset_seconds = self._read_reset_seconds(response)
            if reset_seconds is not None:
                self.next_allowed_at = max(self.next_allowed_at, now + reset_seconds)
        self.logger.debug(
            "football-data rate-limit state updated",
            extra={
                "extra": {
                    "requests_available": remaining,
                    "reset_header": response.headers.get("X-RequestCounter-Reset"),
                    "next_allowed_at_unix": round(self.next_allowed_at, 3),
                }
            },
        )

    def get_live_matches(self) -> List[MatchSnapshot]:
        if not self.api_token:
            raise RuntimeError("FOOTBALL_DATA_API_TOKEN is missing")

        url = f"{self.base_url}/matches"
        params = {"status": "LIVE"}
        backoff = self.config.backoff_base_seconds

        for attempt in range(1, self.config.max_retries + 1):
            self._respect_local_throttle()
            self.logger.debug(
                "calling football-data live endpoint",
                extra={
                    "extra": {
                        "url": url,
                        "params": params,
                        "attempt": attempt,
                        "timeout_seconds": self.config.timeout_seconds,
                    }
                },
            )
            try:
                response = self.session.get(
                    url,
                    params=params,
                    headers=self._headers(),
                    timeout=self.config.timeout_seconds,
                )
            except requests.RequestException as exc:
                self.logger.warning("football-data request failed", extra={"extra": {"error": str(exc), "attempt": attempt}})
                if attempt >= self.config.max_retries:
                    raise
                time.sleep(backoff)
                backoff *= self.config.backoff_base_seconds
                continue

            self._update_rate_limit_state(response)
            self.logger.debug(
                "football-data response received",
                extra={
                    "extra": {
                        "status_code": response.status_code,
                        "requests_available": response.headers.get("X-RequestsAvailable"),
                        "request_counter_reset": response.headers.get("X-RequestCounter-Reset"),
                    }
                },
            )

            if response.status_code == 429:
                reset_seconds = self._read_reset_seconds(response)
                sleep_for = reset_seconds if reset_seconds is not None else backoff
                self.logger.warning(
                    "football-data returned 429",
                    extra={"extra": {"sleep_seconds": sleep_for, "attempt": attempt}},
                )
                if attempt >= self.config.max_retries:
                    response.raise_for_status()
                time.sleep(sleep_for)
                backoff *= self.config.backoff_base_seconds
                continue

            response.raise_for_status()
            data = response.json()
            matches_raw = data.get("matches", [])
            parsed = self._parse_matches(matches_raw)
            self.logger.info(
                "football-data live poll completed",
                extra={
                    "extra": {
                        "match_count": len(parsed),
                        "status": params["status"],
                    }
                },
            )
            return parsed

        return []

    def _parse_matches(self, matches_raw: list[dict]) -> List[MatchSnapshot]:
        parsed: List[MatchSnapshot] = []
        for raw in matches_raw:
            home = raw.get("homeTeam", {})
            away = raw.get("awayTeam", {})
            full_time = (raw.get("score", {}) or {}).get("fullTime", {}) or {}
            goals = []
            for goal in raw.get("goals", []) or []:
                team = goal.get("team", {}) or {}
                scorer = goal.get("scorer", {}) or {}
                assist = goal.get("assist", {}) or {}
                score = goal.get("score", {}) or {}
                team_name = team.get("name") or ""
                team_side = "home" if team_name and team_name == home.get("name") else "away"
                goals.append(
                    MatchGoalSnapshot(
                        minute=int(goal.get("minute") or 0),
                        injury_time=int(goal.get("injuryTime") or 0),
                        team_side=team_side,
                        team_name=team_name,
                        scorer=scorer.get("name") or "",
                        assist=assist.get("name") or "",
                        goal_type=goal.get("type") or "REGULAR",
                        score_home=int(score.get("home") or 0),
                        score_away=int(score.get("away") or 0),
                    )
                )

            parsed.append(
                MatchSnapshot(
                    match_id=int(raw.get("id")),
                    utc_date=raw.get("utcDate") or "",
                    status=raw.get("status") or "",
                    home_name=home.get("name") or "",
                    home_short_name=home.get("shortName") or "",
                    home_tla=home.get("tla") or "",
                    away_name=away.get("name") or "",
                    away_short_name=away.get("shortName") or "",
                    away_tla=away.get("tla") or "",
                    score_home=int(full_time.get("home") or 0),
                    score_away=int(full_time.get("away") or 0),
                    goals=goals,
                )
            )
        self.logger.debug(
            "parsed football-data matches",
            extra={
                "extra": {
                    "count": len(parsed),
                    "matches": [
                        {
                            "match_id": m.match_id,
                            "status": m.status,
                            "home": m.home_name,
                            "away": m.away_name,
                            "score_home": m.score_home,
                            "score_away": m.score_away,
                            "goals": len(m.goals),
                        }
                        for m in parsed
                    ],
                }
            },
        )

        return parsed
