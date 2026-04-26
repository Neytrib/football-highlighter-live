from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

import yaml
from dotenv import load_dotenv


@dataclass
class ApiConfig:
    poll_interval_seconds: int = 6
    unfold_goals: bool = True
    timeout_seconds: int = 10
    max_retries: int = 5
    backoff_base_seconds: float = 1.5


@dataclass
class StreamConfig:
    read_fps_for_ocr: float = 2.0
    reconnect_delay_seconds: int = 3
    rolling_buffer_seconds: int = 300
    segment_seconds: int = 2


@dataclass
class HighlightConfig:
    pre_goal_seconds: int = 45
    post_goal_seconds: int = 30
    timer_match_tolerance_seconds: int = 2
    require_score_change_confirmation: bool = True


@dataclass
class VisionConfig:
    timer_roi: tuple[float, float, float, float] = (0.0, 0.0, 0.28, 0.12)
    scoreboard_roi: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 0.16)
    ocr_confidence_min: float = 0.55


@dataclass
class StreamOnlyConfig:
    enabled: bool = False
    score_roi: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 0.22)
    home_score_roi: tuple[float, float, float, float] = (0.46, 0.0, 0.06, 0.14)
    away_score_roi: tuple[float, float, float, float] = (0.52, 0.0, 0.06, 0.14)
    auto_locate_score_rois: bool = True
    auto_locate_frames: int = 30
    change_threshold: float = 18.0
    stable_threshold: float = 6.0
    stable_frames: int = 3
    cooldown_seconds: int = 15
    match_id: int = 0
    home_name: str = "stream_home"
    away_name: str = "stream_away"


@dataclass
class CropConfig:
    enabled: bool = True
    aspect_ratio: str = "1:1"
    detector_model_path: str = "models/soccer_yolov8s.pt"
    detection_frame_stride: int = 3
    smoothing_alpha: float = 0.25
    target_class_names: tuple[str, ...] = ("ball",)
    min_confidence: float = 0.15
    background_workers: int = 1
    output_suffix: str = "_crop1x1"


@dataclass
class OutputConfig:
    raw_dir: str = "data/clips_raw"
    cropped_dir: str = "data/clips_cropped"
    goals_dir: str = "data/goals"
    state_dir: str = "data/state"
    tmp_dir: str = "data/tmp"


@dataclass
class AppConfig:
    api: ApiConfig = field(default_factory=ApiConfig)
    stream: StreamConfig = field(default_factory=StreamConfig)
    highlight: HighlightConfig = field(default_factory=HighlightConfig)
    vision: VisionConfig = field(default_factory=VisionConfig)
    stream_only: StreamOnlyConfig = field(default_factory=StreamOnlyConfig)
    crop: CropConfig = field(default_factory=CropConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    football_data_api_token: str = ""
    football_data_base_url: str = "https://api.football-data.org/v4"
    stream_url: str = ""
    manual_match_id: Optional[int] = None
    dry_run: bool = False

    def ensure_output_dirs(self) -> None:
        for path in [
            self.output.raw_dir,
            self.output.cropped_dir,
            self.output.goals_dir,
            self.output.state_dir,
            self.output.tmp_dir,
        ]:
            Path(path).mkdir(parents=True, exist_ok=True)


def _merge_dataclass(section: Any, values: Dict[str, Any]) -> Any:
    for key, value in values.items():
        if hasattr(section, key):
            setattr(section, key, value)
    return section


def _read_yaml(path: str) -> Dict[str, Any]:
    cfg_path = Path(path)
    if not cfg_path.exists():
        return {}
    with cfg_path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError("Config YAML root must be a mapping")
    return data


def load_config(
    config_path: str,
    *,
    stream_url_override: Optional[str] = None,
    manual_match_id_override: Optional[int] = None,
    dry_run_override: Optional[bool] = None,
    stream_only_override: Optional[bool] = None,
) -> AppConfig:
    load_dotenv()
    raw = _read_yaml(config_path)

    cfg = AppConfig()
    if "api" in raw:
        cfg.api = _merge_dataclass(cfg.api, raw["api"])
    if "stream" in raw:
        cfg.stream = _merge_dataclass(cfg.stream, raw["stream"])
    if "highlight" in raw:
        cfg.highlight = _merge_dataclass(cfg.highlight, raw["highlight"])
    if "vision" in raw:
        vision = raw["vision"].copy()
        if "timer_roi" in vision:
            vision["timer_roi"] = tuple(vision["timer_roi"])
        if "scoreboard_roi" in vision:
            vision["scoreboard_roi"] = tuple(vision["scoreboard_roi"])
        cfg.vision = _merge_dataclass(cfg.vision, vision)
    if isinstance(raw.get("stream_only"), dict):
        stream_only = raw["stream_only"].copy()
        if "score_roi" in stream_only:
            stream_only["score_roi"] = tuple(stream_only["score_roi"])
        if "home_score_roi" in stream_only:
            stream_only["home_score_roi"] = tuple(stream_only["home_score_roi"])
        if "away_score_roi" in stream_only:
            stream_only["away_score_roi"] = tuple(stream_only["away_score_roi"])
        if "score_roi" in stream_only and "home_score_roi" not in stream_only and "away_score_roi" not in stream_only:
            sx, sy, sw, sh = stream_only["score_roi"]
            half = sw / 2.0
            stream_only["home_score_roi"] = (sx, sy, half, sh)
            stream_only["away_score_roi"] = (sx + half, sy, half, sh)
        cfg.stream_only = _merge_dataclass(cfg.stream_only, stream_only)
    elif isinstance(raw.get("stream_only"), bool):
        cfg.stream_only.enabled = bool(raw["stream_only"])
    if "crop" in raw:
        crop = raw["crop"].copy()
        if "target_class_names" in crop:
            crop["target_class_names"] = tuple(str(x) for x in crop["target_class_names"])
        cfg.crop = _merge_dataclass(cfg.crop, crop)
    if "output" in raw:
        cfg.output = _merge_dataclass(cfg.output, raw["output"])

    cfg.football_data_api_token = os.getenv("FOOTBALL_DATA_API_TOKEN", cfg.football_data_api_token)
    cfg.football_data_base_url = os.getenv("FOOTBALL_DATA_BASE_URL", cfg.football_data_base_url)
    cfg.stream_url = os.getenv("STREAM_URL", cfg.stream_url)

    if stream_url_override:
        cfg.stream_url = stream_url_override
    if manual_match_id_override is not None:
        cfg.manual_match_id = manual_match_id_override
    elif isinstance(raw.get("manual_match_id"), int):
        cfg.manual_match_id = raw["manual_match_id"]

    if dry_run_override is not None:
        cfg.dry_run = dry_run_override
    elif isinstance(raw.get("dry_run"), bool):
        cfg.dry_run = raw["dry_run"]

    if stream_only_override is not None:
        cfg.stream_only.enabled = stream_only_override
    elif isinstance(raw.get("stream_only_enabled"), bool):
        cfg.stream_only.enabled = raw["stream_only_enabled"]

    cfg.ensure_output_dirs()
    return cfg
