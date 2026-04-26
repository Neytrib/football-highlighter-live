from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from app.api.football_data_client import FootballDataClient
from app.api.goal_detector import GoalDetector
from app.config import AppConfig
from app.models import GoalEvent, MatchSnapshot, PendingClipJob, ScoreboardReading, Scoreline
from app.pipeline.clip_extractor import ClipExtractor
from app.pipeline.clip_scheduler import compute_clip_window
from app.storage.json_store import GoalJsonStore
from app.storage.state_store import StateStore
from app.stream.frame_sampler import FrameSampler
from app.stream.recorder import StreamRecorder
from app.stream.rolling_buffer import RollingBuffer
from app.vision.cropper import ClipCropper
from app.vision.match_resolver import MatchResolver
from app.vision.score_change_detector import ScoreChangeDetector, ScoreChangeEvent
from app.vision.scoreboard_ocr import ScoreboardOCR
from app.vision.timer_ocr import TimerOCR


class Orchestrator:
    def __init__(self, config: AppConfig, logger: Optional[logging.Logger] = None) -> None:
        self.config = config
        self.logger = logger or logging.getLogger(__name__)

        self.state_store = StateStore(self.config.output.state_dir)
        self.goal_store = GoalJsonStore(self.config.output.goals_dir)

        self.client = FootballDataClient(
            api_token=self.config.football_data_api_token,
            base_url=self.config.football_data_base_url,
            config=self.config.api,
            logger=self.logger,
        )
        self.goal_detector = GoalDetector()

        self.rolling_buffer = RollingBuffer(buffer_seconds=self.config.stream.rolling_buffer_seconds, logger=self.logger)
        self.segment_dir = str(Path(self.config.output.tmp_dir) / "segments")
        self.recorder = StreamRecorder(
            stream_url=self.config.stream_url,
            segment_dir=self.segment_dir,
            stream_config=self.config.stream,
            rolling_buffer=self.rolling_buffer,
            logger=self.logger,
        )

        self.frame_sampler = FrameSampler(
            stream_url=self.config.stream_url,
            fps=self.config.stream.read_fps_for_ocr,
            reconnect_delay_seconds=self.config.stream.reconnect_delay_seconds,
            logger=self.logger,
        )
        self.timer_ocr = TimerOCR(
            roi=self.config.vision.timer_roi,
            ocr_confidence_min=self.config.vision.ocr_confidence_min,
            logger=self.logger,
        )
        self.scoreboard_ocr = ScoreboardOCR(
            roi=self.config.vision.scoreboard_roi,
            ocr_confidence_min=self.config.vision.ocr_confidence_min,
            logger=self.logger,
        )
        self.match_resolver = MatchResolver(logger=self.logger)

        self.clip_extractor = ClipExtractor(tmp_dir=self.config.output.tmp_dir, logger=self.logger)
        self.cropper = ClipCropper(
            detector_model_path=self.config.crop.detector_model_path,
            detection_frame_stride=self.config.crop.detection_frame_stride,
            smoothing_alpha=self.config.crop.smoothing_alpha,
            target_class_names=self.config.crop.target_class_names,
            min_confidence=self.config.crop.min_confidence,
            logger=self.logger,
        )
        self.crop_executor = ThreadPoolExecutor(max_workers=max(1, int(self.config.crop.background_workers)))
        self.crop_futures: Dict[str, Future] = {}
        self.crop_future_meta: Dict[str, Dict[str, str]] = {}

        self.pending_jobs: Dict[str, PendingClipJob] = {}
        self.poll_iteration = 0
        self.stream_only_match: Optional[MatchSnapshot] = None
        self.score_change_detector: Optional[ScoreChangeDetector] = None
        if self.config.stream_only.enabled:
            self.stream_only_match = self._build_stream_only_match()
            self.score_change_detector = ScoreChangeDetector(
                home_score_roi=self.config.stream_only.home_score_roi,
                away_score_roi=self.config.stream_only.away_score_roi,
                search_roi=self.config.stream_only.score_roi,
                auto_locate_score_rois=self.config.stream_only.auto_locate_score_rois,
                auto_locate_frames=self.config.stream_only.auto_locate_frames,
                change_threshold=self.config.stream_only.change_threshold,
                stable_threshold=self.config.stream_only.stable_threshold,
                stable_frames=self.config.stream_only.stable_frames,
                cooldown_seconds=self.config.stream_only.cooldown_seconds,
                logger=self.logger,
            )

        self.logger.info(
            "orchestrator initialized",
            extra={
                "extra": {
                    "poll_interval_seconds": self.config.api.poll_interval_seconds,
                    "stream_url": self.config.stream_url,
                    "manual_match_id": self.config.manual_match_id,
                    "dry_run": self.config.dry_run,
                    "stream_only": self.config.stream_only.enabled,
                    "crop_background_workers": self.config.crop.background_workers,
                    "segment_dir": self.segment_dir,
                }
            },
        )

    def run_forever(self) -> None:
        if not self.config.stream_url:
            raise RuntimeError("STREAM_URL is missing. Set STREAM_URL in .env or pass --stream-url.")

        if self.config.stream_only.enabled:
            self._run_stream_only()
            return
        self._run_api_mode()

    def _run_stream_only(self) -> None:
        if self.score_change_detector is None or self.stream_only_match is None:
            raise RuntimeError("stream-only mode is enabled but score detector was not initialized")

        stream_match = self.stream_only_match
        self.logger.info(
            "orchestrator started in stream-only mode",
            extra={
                "extra": {
                    "read_fps_for_ocr": self.config.stream.read_fps_for_ocr,
                    "home_score_roi": list(self.config.stream_only.home_score_roi),
                    "away_score_roi": list(self.config.stream_only.away_score_roi),
                    "score_search_roi": list(self.config.stream_only.score_roi),
                    "auto_locate_score_rois": self.config.stream_only.auto_locate_score_rois,
                    "auto_locate_frames": self.config.stream_only.auto_locate_frames,
                    "change_threshold": self.config.stream_only.change_threshold,
                    "stable_threshold": self.config.stream_only.stable_threshold,
                    "stable_frames": self.config.stream_only.stable_frames,
                    "cooldown_seconds": self.config.stream_only.cooldown_seconds,
                    "match_id": stream_match.match_id,
                    "home": stream_match.home_name,
                    "away": stream_match.away_name,
                }
            },
        )
        self.recorder.start()
        frame_iter = self.frame_sampler.frames()
        self._seed_pending_from_store(stream_match)

        try:
            while True:
                self.poll_iteration += 1
                self._drain_crop_futures()
                frame_ts, frame = next(frame_iter)
                event = self.score_change_detector.process(frame, frame_ts)
                if event is not None:
                    self._enqueue_stream_only_goal(stream_match, event)

                if event is not None or self.pending_jobs:
                    self._trigger_jobs(
                        match=stream_match,
                        frame_ts=frame_ts,
                        timer_minute=None,
                        timer_second=None,
                        timer_text="",
                        scoreboard=ScoreboardReading(None, None, None, None, 0.0),
                    )
        finally:
            self._drain_crop_futures()
            self.crop_executor.shutdown(wait=False, cancel_futures=False)
            self.recorder.stop()

    def _run_api_mode(self) -> None:
        self.logger.info(
            "orchestrator started",
            extra={
                "extra": {
                    "poll_interval_seconds": self.config.api.poll_interval_seconds,
                    "timer_tolerance_seconds": self.config.highlight.timer_match_tolerance_seconds,
                    "score_change_required": self.config.highlight.require_score_change_confirmation,
                }
            },
        )
        self.recorder.start()
        frame_iter = self.frame_sampler.frames()

        try:
            while True:
                self.poll_iteration += 1
                self._drain_crop_futures()
                live_matches = self.client.get_live_matches()
                if not live_matches:
                    self.logger.info(
                        "no live matches",
                        extra={"extra": {"poll_iteration": self.poll_iteration}},
                    )
                    time.sleep(self.config.api.poll_interval_seconds)
                    continue
                self.logger.info(
                    "live matches received",
                    extra={
                        "extra": {
                            "poll_iteration": self.poll_iteration,
                            "match_count": len(live_matches),
                            "match_ids": [m.match_id for m in live_matches],
                        }
                    },
                )

                frame_ts, frame, timer_reading, scoreboard_reading = self._read_ocr(frame_iter)
                resolved_match = self._select_match(live_matches, scoreboard_reading)
                if resolved_match is None:
                    self.logger.info(
                        "unable to resolve live match",
                        extra={
                            "extra": {
                                "poll_iteration": self.poll_iteration,
                                "ocr_home_label": scoreboard_reading.home_label,
                                "ocr_away_label": scoreboard_reading.away_label,
                                "ocr_home_score": scoreboard_reading.home_score,
                                "ocr_away_score": scoreboard_reading.away_score,
                                "ocr_confidence": scoreboard_reading.confidence,
                            }
                        },
                    )
                    time.sleep(self.config.api.poll_interval_seconds)
                    continue

                self.logger.info(
                    "match resolved",
                    extra={
                        "extra": {
                            "poll_iteration": self.poll_iteration,
                            "match_id": resolved_match.match_id,
                            "home": resolved_match.home_name,
                            "away": resolved_match.away_name,
                            "status": resolved_match.status,
                        }
                    },
                )
                self._seed_pending_from_store(resolved_match)
                self._detect_and_enqueue_new_goals(resolved_match, timer_minute=timer_reading.minute)
                self._trigger_jobs(
                    match=resolved_match,
                    frame_ts=frame_ts,
                    timer_minute=timer_reading.minute,
                    timer_second=timer_reading.second,
                    timer_text=timer_reading.raw_text,
                    scoreboard=scoreboard_reading,
                )

                time.sleep(self.config.api.poll_interval_seconds)
        finally:
            self._drain_crop_futures()
            self.crop_executor.shutdown(wait=False, cancel_futures=False)
            self.recorder.stop()

    def _build_stream_only_match(self) -> MatchSnapshot:
        match_id = int(self.config.stream_only.match_id)
        return MatchSnapshot(
            match_id=match_id,
            utc_date="",
            status="IN_PLAY",
            home_name=self.config.stream_only.home_name,
            home_short_name=self.config.stream_only.home_name,
            home_tla=self.config.stream_only.home_name[:3].upper(),
            away_name=self.config.stream_only.away_name,
            away_short_name=self.config.stream_only.away_name,
            away_tla=self.config.stream_only.away_name[:3].upper(),
            score_home=0,
            score_away=0,
            goals=[],
        )

    def _enqueue_stream_only_goal(self, match: MatchSnapshot, event: ScoreChangeEvent) -> None:
        goal_id = f"{match.match_id}_stream_{int(event.goal_ts_unix * 1000)}"
        seen = self.state_store.seen_goal_ids()
        if goal_id in seen or goal_id in self.pending_jobs:
            self.logger.debug(
                "stream-only goal already known",
                extra={"extra": {"goal_id": goal_id}},
            )
            return

        detected_at_utc = datetime.fromtimestamp(event.goal_ts_unix, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        goal_event = GoalEvent(
            goal_id=goal_id,
            source="stream-score-change-opencv",
            api_detected_at_utc=detected_at_utc,
            minute=0,
            injury_time=0,
            target_minute_label="stream",
            team="",
            scorer="",
            assist="",
            goal_type="SCORE_CHANGE",
            score_after_goal=Scoreline(home=0, away=0),
        )
        self.goal_store.append_or_update_goal(
            match_id=match.match_id,
            home_team=match.home_name,
            away_team=match.away_name,
            goal_event=goal_event,
        )
        self.state_store.add_seen_goal_id(goal_id)
        self.pending_jobs[goal_id] = PendingClipJob(
            match_id=match.match_id,
            goal_id=goal_id,
            minute=0,
            injury_time=0,
            expected_score_home=0,
            expected_score_away=0,
        )
        self.logger.info(
            "stream-only goal enqueued",
            extra={
                "extra": {
                    "goal_id": goal_id,
                    "match_id": match.match_id,
                    "goal_ts_unix": round(event.goal_ts_unix, 3),
                    "diff_value": round(event.diff_value, 3),
                    "changed_home": event.changed_home,
                    "changed_away": event.changed_away,
                    "pending_jobs": len(self.pending_jobs),
                }
            },
        )

    def _enqueue_crop_job(
        self,
        *,
        goal_id: str,
        match_id: int,
        home: str,
        away: str,
        raw_path: str,
        crop_path: str,
    ) -> None:
        if goal_id in self.crop_futures:
            return
        future = self.crop_executor.submit(self._run_crop_task, raw_path=raw_path, crop_path=crop_path)
        self.crop_future_meta[goal_id] = {
            "goal_id": goal_id,
            "match_id": match_id,
            "home": home,
            "away": away,
            "raw_path": raw_path,
            "crop_path": crop_path,
        }
        self.crop_futures[goal_id] = future
        self.logger.info(
            "crop job queued",
            extra={"extra": {"goal_id": goal_id, "match_id": match_id, "queued_jobs": len(self.crop_futures)}},
        )

    def _drain_crop_futures(self) -> None:
        if not self.crop_futures:
            return
        done_goal_ids = [goal_id for goal_id, future in self.crop_futures.items() if future.done()]
        for goal_id in done_goal_ids:
            future = self.crop_futures.pop(goal_id)
            meta = self.crop_future_meta.pop(goal_id, {})
            match_id = int(meta.get("match_id", 0))
            home = str(meta.get("home", ""))
            away = str(meta.get("away", ""))
            try:
                cropped_path = future.result()
            except Exception as exc:  # pragma: no cover - runtime path
                self.logger.error("crop job failed", extra={"extra": {"goal_id": goal_id, "error": str(exc)}})
                event = self.goal_store.find_goal(match_id, goal_id)
                if event is not None:
                    event["clip_status"] = "RAW_CREATED"
                    self._save_event_dict(match_id, home, away, event)
                continue

            event = self.goal_store.find_goal(match_id, goal_id)
            if event is None:
                continue
            event["clip_status"] = "CREATED"
            event["cropped_clip_path"] = cropped_path
            self._save_event_dict(match_id, home, away, event)
            self.logger.info("crop job completed", extra={"extra": {"goal_id": goal_id, "crop": cropped_path}})

    def _run_crop_task(self, *, raw_path: str, crop_path: str) -> str:
        return self.cropper.crop_clip(
            input_path=raw_path,
            output_path=crop_path,
            aspect_ratio=self.config.crop.aspect_ratio,
            enabled=self.config.crop.enabled,
        )

    def _read_ocr(self, frame_iter):
        frame_ts = time.time()
        frame = None
        timer_reading = self.timer_ocr.read(self._empty_frame())
        scoreboard_reading = ScoreboardReading(None, None, None, None, 0.0)

        try:
            frame_ts, frame = next(frame_iter)
            timer_reading = self.timer_ocr.read(frame)
            scoreboard_reading = self.scoreboard_ocr.read(frame)
        except Exception as exc:  # pragma: no cover - runtime path
            self.logger.warning("OCR read failed", extra={"extra": {"error": str(exc)}})
        else:
            self.logger.debug(
                "OCR frame processed",
                extra={
                    "extra": {
                        "frame_ts_unix": round(frame_ts, 3),
                        "timer_raw": (timer_reading.raw_text or "").strip(),
                        "timer_minute": timer_reading.minute,
                        "timer_second": timer_reading.second,
                        "timer_confidence": timer_reading.confidence,
                        "scoreboard_home_label": scoreboard_reading.home_label,
                        "scoreboard_away_label": scoreboard_reading.away_label,
                        "scoreboard_home_score": scoreboard_reading.home_score,
                        "scoreboard_away_score": scoreboard_reading.away_score,
                        "scoreboard_confidence": scoreboard_reading.confidence,
                    }
                },
            )

        return frame_ts, frame, timer_reading, scoreboard_reading

    @staticmethod
    def _empty_frame() -> object:
        class _Frame:
            shape = (100, 100, 3)

            def __getitem__(self, item):
                return self

        return _Frame()

    def _select_match(self, live_matches: list[MatchSnapshot], scoreboard: ScoreboardReading) -> Optional[MatchSnapshot]:
        if self.config.manual_match_id is not None:
            for match in live_matches:
                if match.match_id == self.config.manual_match_id:
                    self.state_store.set_locked_match(match.match_id, 100.0)
                    self.logger.info(
                        "manual match id matched live fixtures",
                        extra={
                            "extra": {
                                "manual_match_id": self.config.manual_match_id,
                                "home": match.home_name,
                                "away": match.away_name,
                            }
                        },
                    )
                    return match
            self.logger.warning(
                "manual match id not present in live fixtures",
                extra={
                    "extra": {
                        "manual_match_id": self.config.manual_match_id,
                        "available_match_ids": [m.match_id for m in live_matches],
                    }
                },
            )
            return None

        locked_match_id, locked_confidence = self.state_store.get_locked_match()
        if locked_match_id is not None:
            for match in live_matches:
                if match.match_id == locked_match_id:
                    self.logger.debug(
                        "using previously locked match",
                        extra={
                            "extra": {
                                "match_id": locked_match_id,
                                "lock_confidence": locked_confidence,
                            }
                        },
                    )
                    return match
            self.logger.warning(
                "previously locked match no longer live",
                extra={"extra": {"locked_match_id": locked_match_id, "lock_confidence": locked_confidence}},
            )

        candidate_scores = self.match_resolver.score_candidates(scoreboard, live_matches)
        self.logger.debug(
            "match resolver scoring snapshot",
            extra={
                "extra": {
                    "home_label": scoreboard.home_label,
                    "away_label": scoreboard.away_label,
                    "top_candidates": [
                        {
                            "match_id": c.match_id,
                            "home": c.home_name,
                            "away": c.away_name,
                            "score": round(c.score, 3),
                        }
                        for c in candidate_scores[:5]
                    ],
                }
            },
        )
        resolution = self.match_resolver.resolve(scoreboard=scoreboard, live_matches=live_matches)
        if resolution.match is not None:
            self.state_store.set_locked_match(resolution.match.match_id, resolution.confidence)
            self.logger.info(
                "match lock acquired",
                extra={
                    "extra": {
                        "match_id": resolution.match.match_id,
                        "confidence": round(resolution.confidence, 3),
                    }
                },
            )
        else:
            self.logger.info(
                "match lock not acquired",
                extra={"extra": {"best_confidence": round(resolution.confidence, 3)}},
            )
        return resolution.match

    def _seed_pending_from_store(self, match: MatchSnapshot) -> None:
        processed = self.state_store.processed_goal_ids()
        seeded = 0
        for event in self.goal_store.list_goals(match.match_id):
            goal_id = event.get("goal_id")
            if not goal_id or goal_id in processed:
                continue
            if event.get("clip_status") == "CREATED":
                self.state_store.mark_processed(goal_id)
                continue
            if event.get("clip_status") == "RAW_CREATED":
                raw_path = event.get("raw_clip_path")
                crop_path = event.get("cropped_clip_path")
                if isinstance(raw_path, str) and raw_path and Path(raw_path).exists():
                    if not isinstance(crop_path, str) or not crop_path:
                        _, crop_path = self._build_output_paths(
                            match,
                            PendingClipJob(
                                match_id=match.match_id,
                                goal_id=goal_id,
                                minute=int(event.get("minute", 0)),
                                injury_time=int(event.get("injury_time", 0)),
                                expected_score_home=0,
                                expected_score_away=0,
                            ),
                        )
                    self._enqueue_crop_job(
                        goal_id=goal_id,
                        match_id=match.match_id,
                        home=match.home_name,
                        away=match.away_name,
                        raw_path=raw_path,
                        crop_path=str(crop_path),
                    )
                    self.state_store.mark_processed(goal_id)
                    continue
            if goal_id in self.pending_jobs:
                continue
            score_after = event.get("score_after_goal", {})
            self.pending_jobs[goal_id] = PendingClipJob(
                match_id=match.match_id,
                goal_id=goal_id,
                minute=int(event.get("minute", 0)),
                injury_time=int(event.get("injury_time", 0)),
                expected_score_home=int(score_after.get("home", 0)),
                expected_score_away=int(score_after.get("away", 0)),
            )
            seeded += 1
        if seeded > 0:
            self.logger.info(
                "seeded pending jobs from stored goals",
                extra={"extra": {"match_id": match.match_id, "seeded_jobs": seeded, "pending_jobs": len(self.pending_jobs)}},
            )

    def _detect_and_enqueue_new_goals(self, match: MatchSnapshot, timer_minute: Optional[int]) -> None:
        seen = self.state_store.seen_goal_ids()
        detected = self.goal_detector.detect_new_goals(match, seen)
        seen_after_official = set(seen)
        seen_after_official.update(item.event.goal_id for item in detected)

        previous_score = self.state_store.get_match_score(match.match_id)
        fallback_detected = self.goal_detector.detect_score_delta_goals(
            match=match,
            seen_goal_ids=seen_after_official,
            previous_score=previous_score,
            inferred_minute=timer_minute,
        )
        detected.extend(fallback_detected)

        self.state_store.set_match_score(match.match_id, match.score_home, match.score_away)

        if fallback_detected:
            self.logger.warning(
                "score-delta fallback goals detected",
                extra={
                    "extra": {
                        "match_id": match.match_id,
                        "count": len(fallback_detected),
                        "previous_score": previous_score,
                        "new_score": {"home": match.score_home, "away": match.score_away},
                    }
                },
            )
        if not detected:
            self.logger.debug("no new goals detected in poll", extra={"extra": {"match_id": match.match_id}})
            return
        for goal in detected:
            self.goal_store.append_or_update_goal(
                match_id=goal.match_id,
                home_team=goal.home_team,
                away_team=goal.away_team,
                goal_event=goal.event,
            )
            self.state_store.add_seen_goal_id(goal.event.goal_id)
            self.pending_jobs[goal.event.goal_id] = PendingClipJob(
                match_id=goal.match_id,
                goal_id=goal.event.goal_id,
                minute=goal.event.minute,
                injury_time=goal.event.injury_time,
                expected_score_home=goal.event.score_after_goal.home,
                expected_score_away=goal.event.score_after_goal.away,
            )
            self.logger.info(
                "new goal detected",
                extra={
                    "extra": {
                        "goal_id": goal.event.goal_id,
                        "match_id": goal.match_id,
                        "minute": goal.event.minute,
                        "injury_time": goal.event.injury_time,
                        "score_after_goal": {
                            "home": goal.event.score_after_goal.home,
                            "away": goal.event.score_after_goal.away,
                        },
                    }
                },
            )

    def _trigger_jobs(
        self,
        *,
        match: MatchSnapshot,
        frame_ts: float,
        timer_minute: Optional[int],
        timer_second: Optional[int],
        timer_text: str,
        scoreboard: ScoreboardReading,
    ) -> None:
        processed = self.state_store.processed_goal_ids()
        self.logger.debug(
            "evaluating pending clip jobs",
            extra={
                "extra": {
                    "match_id": match.match_id,
                    "pending_jobs": len(self.pending_jobs),
                    "processed_goals": len(processed),
                    "timer_minute": timer_minute,
                    "timer_second": timer_second,
                    "timer_text": timer_text.strip(),
                    "scoreboard_home": scoreboard.home_score,
                    "scoreboard_away": scoreboard.away_score,
                }
            },
        )

        for goal_id, job in list(self.pending_jobs.items()):
            if goal_id in processed:
                self.logger.debug("removing already processed pending job", extra={"extra": {"goal_id": goal_id}})
                self.pending_jobs.pop(goal_id, None)
                continue

            raw_path, crop_path = self._build_output_paths(match, job)
            event = self.goal_store.find_goal(match.match_id, goal_id)
            if event is None:
                self.logger.warning("pending goal missing in goal store", extra={"extra": {"goal_id": goal_id}})
                continue

            goal_ts: Optional[float] = None
            if timer_minute is None:
                fallback_ts = _parse_utc_iso_to_unix(event.get("api_detected_at_utc"))
                if fallback_ts is None:
                    self.logger.debug(
                        "job skipped: timer unavailable and api_detected_at_utc missing/invalid",
                        extra={"extra": {"goal_id": goal_id}},
                    )
                    continue
                if frame_ts < fallback_ts + self.config.highlight.post_goal_seconds:
                    self.logger.debug(
                        "job waiting for post-goal window in OCR-less mode",
                        extra={
                            "extra": {
                                "goal_id": goal_id,
                                "frame_ts": round(frame_ts, 3),
                                "required_after_ts": round(fallback_ts + self.config.highlight.post_goal_seconds, 3),
                            }
                        },
                    )
                    continue
                goal_ts = fallback_ts
                self.logger.warning(
                    "triggering clip without OCR timer",
                    extra={
                        "extra": {
                            "goal_id": goal_id,
                            "source": event.get("source"),
                            "goal_ts_unix": round(goal_ts, 3),
                        }
                    },
                )
            else:
                if not self._timer_matches_goal(timer_minute, timer_second, job.minute, job.injury_time):
                    self.logger.debug(
                        "job skipped: timer does not match goal minute window",
                        extra={
                            "extra": {
                                "goal_id": goal_id,
                                "goal_minute": job.minute,
                                "goal_injury_time": job.injury_time,
                                "timer_minute": timer_minute,
                                "timer_second": timer_second,
                            }
                        },
                    )
                    continue
                if self.config.highlight.require_score_change_confirmation:
                    if scoreboard.home_score is None or scoreboard.away_score is None:
                        self.logger.debug(
                            "job skipped: score confirmation required but OCR score missing",
                            extra={"extra": {"goal_id": goal_id}},
                        )
                        continue
                    if scoreboard.home_score != job.expected_score_home or scoreboard.away_score != job.expected_score_away:
                        self.logger.debug(
                            "job skipped: OCR score does not match goal expected score",
                            extra={
                                "extra": {
                                    "goal_id": goal_id,
                                    "ocr_home": scoreboard.home_score,
                                    "ocr_away": scoreboard.away_score,
                                    "expected_home": job.expected_score_home,
                                    "expected_away": job.expected_score_away,
                                }
                            },
                        )
                        continue
                goal_ts = frame_ts

            window = compute_clip_window(
                goal_ts,
                self.config.highlight.pre_goal_seconds,
                self.config.highlight.post_goal_seconds,
            )

            if self.config.dry_run:
                event["ocr_goal_time"] = timer_text.strip() or None
                event["stream_goal_ts_unix"] = goal_ts
                event["clip_status"] = "DRY_RUN"
                event["raw_clip_path"] = raw_path
                event["cropped_clip_path"] = crop_path
                self._save_event_dict(match.match_id, match.home_name, match.away_name, event)
                self.state_store.mark_processed(goal_id)
                self.pending_jobs.pop(goal_id, None)
                self.logger.info(
                    "dry-run goal trigger recorded",
                    extra={
                        "extra": {
                            "goal_id": goal_id,
                            "goal_ts_unix": round(goal_ts, 3),
                            "clip_window_start": round(window.start_ts, 3),
                            "clip_window_end": round(window.end_ts, 3),
                        }
                    },
                )
                continue

            segments = self.rolling_buffer.get_segments_for_window(window.start_ts, window.end_ts)
            if not segments:
                self.logger.warning("no segments for goal window", extra={"extra": {"goal_id": goal_id}})
                continue

            try:
                self.clip_extractor.extract_clip(
                    segments=segments,
                    start_ts=window.start_ts,
                    end_ts=window.end_ts,
                    output_path=raw_path,
                )
            except Exception as exc:  # pragma: no cover - runtime path
                self.logger.error("raw clip generation failed", extra={"extra": {"goal_id": goal_id, "error": str(exc)}})
                continue

            event["ocr_goal_time"] = timer_text.strip() or None
            event["stream_goal_ts_unix"] = goal_ts
            event["clip_status"] = "RAW_CREATED"
            event["raw_clip_path"] = raw_path
            event["cropped_clip_path"] = None
            self._save_event_dict(match.match_id, match.home_name, match.away_name, event)

            self.state_store.mark_processed(goal_id)
            self.pending_jobs.pop(goal_id, None)
            self._enqueue_crop_job(
                goal_id=goal_id,
                match_id=match.match_id,
                home=match.home_name,
                away=match.away_name,
                raw_path=raw_path,
                crop_path=crop_path,
            )
            self.logger.info(
                "raw clip created; crop scheduled",
                extra={"extra": {"goal_id": goal_id, "raw": raw_path, "crop_target": crop_path}},
            )

    def _save_event_dict(self, match_id: int, home: str, away: str, event: dict) -> None:
        from app.models import GoalEvent, Scoreline

        goal_event = GoalEvent(
            goal_id=event["goal_id"],
            source=event.get("source", "football-data-v4"),
            api_detected_at_utc=event.get("api_detected_at_utc", ""),
            minute=int(event.get("minute", 0)),
            injury_time=int(event.get("injury_time", 0)),
            target_minute_label=event.get("target_minute_label", ""),
            team=event.get("team", ""),
            scorer=event.get("scorer", ""),
            assist=event.get("assist", ""),
            goal_type=event.get("goal_type", "REGULAR"),
            score_after_goal=Scoreline(
                home=int((event.get("score_after_goal") or {}).get("home", 0)),
                away=int((event.get("score_after_goal") or {}).get("away", 0)),
            ),
            ocr_goal_time=event.get("ocr_goal_time"),
            stream_goal_ts_unix=event.get("stream_goal_ts_unix"),
            clip_status=event.get("clip_status", "PENDING"),
            raw_clip_path=event.get("raw_clip_path"),
            cropped_clip_path=event.get("cropped_clip_path"),
        )
        self.goal_store.append_or_update_goal(
            match_id=match_id,
            home_team=home,
            away_team=away,
            goal_event=goal_event,
        )
        self.logger.debug(
            "goal event persisted",
            extra={
                "extra": {
                    "match_id": match_id,
                    "goal_id": goal_event.goal_id,
                    "clip_status": goal_event.clip_status,
                    "raw_clip_path": goal_event.raw_clip_path,
                    "cropped_clip_path": goal_event.cropped_clip_path,
                }
            },
        )

    def _build_output_paths(self, match: MatchSnapshot, job: PendingClipJob) -> tuple[str, str]:
        existing = self.goal_store.list_goals(match.match_id)
        goal_index = 1
        for idx, event in enumerate(existing, start=1):
            if event.get("goal_id") == job.goal_id:
                goal_index = idx
                break

        utc_day = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        home_slug = _slug(match.home_name)
        away_slug = _slug(match.away_name)
        base = (
            f"{utc_day}_match{match.match_id}_{home_slug}_vs_{away_slug}_"
            f"goal{goal_index}_{job.minute}_{job.injury_time}"
        )

        raw = str(Path(self.config.output.raw_dir) / f"{base}_raw.mp4")
        cropped = str(Path(self.config.output.cropped_dir) / f"{base}{self.config.crop.output_suffix}.mp4")
        self.logger.debug(
            "built output paths for goal",
            extra={"extra": {"goal_id": job.goal_id, "raw_path": raw, "cropped_path": cropped}},
        )
        return raw, cropped

    def _timer_matches_goal(
        self,
        timer_minute: int,
        timer_second: Optional[int],
        goal_minute: int,
        injury_time: int,
    ) -> bool:
        target_minute = goal_minute + injury_time
        if timer_second is None:
            timer_second = 0
        timer_total = timer_minute * 60 + timer_second
        lower = target_minute * 60 - self.config.highlight.timer_match_tolerance_seconds
        upper = target_minute * 60 + self.config.highlight.timer_match_tolerance_seconds
        return lower <= timer_total <= upper


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "", text.lower())
    return slug or "team"


def _parse_utc_iso_to_unix(value: object) -> Optional[float]:
    if not isinstance(value, str) or not value.strip():
        return None
    iso = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()
