from __future__ import annotations

import argparse
import logging

from app.config import load_config
from app.logging_setup import parse_log_level, setup_logging
from app.pipeline.orchestrator import Orchestrator


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Goal Highlighter")
    parser.add_argument("--config", default="configs/config.yaml", help="Path to YAML config")
    parser.add_argument("--stream-url", default=None, help="Override stream URL")
    parser.add_argument("--manual-match-id", type=int, default=None, help="Manual match id override")
    parser.add_argument("--dry-run", action="store_true", help="Disable clipping and only record events")
    parser.add_argument("--stream-only", action="store_true", help="Detect confirmed score-value changes from stream only (no football-data API)")
    parser.add_argument("--input-video", default=None, help="Run score detection and clipping against a local video file")
    parser.add_argument("--calibrate-score-rois", default=None, help="Write score ROI preview images to this directory and exit")
    parser.add_argument("--log-level", default="DEBUG", help="Console log level (DEBUG, INFO, WARNING, ERROR)")
    parser.add_argument("--log-file", default="data/state/runtime.log", help="Path to JSON log file")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    level = parse_log_level(args.log_level)
    setup_logging(level, log_file=args.log_file, file_level=logging.DEBUG)
    logger = logging.getLogger(__name__)
    cfg = load_config(
        args.config,
        stream_url_override=args.stream_url,
        manual_match_id_override=args.manual_match_id,
        dry_run_override=True if args.dry_run else None,
        stream_only_override=True if args.stream_only or args.input_video else None,
    )
    logger.info(
        "application config loaded",
        extra={
            "extra": {
                "config_path": args.config,
                "stream_url_set": bool(cfg.stream_url),
                "manual_match_id": cfg.manual_match_id,
                "dry_run": cfg.dry_run,
                "stream_only": cfg.stream_only.enabled,
                "log_file": args.log_file,
            }
        },
    )

    orchestrator = Orchestrator(cfg)
    if args.calibrate_score_rois:
        written = orchestrator.write_score_calibration_previews(
            output_dir=args.calibrate_score_rois,
            input_video=args.input_video,
        )
        logger.info("calibration complete", extra={"extra": {"files": written}})
        return
    if args.input_video:
        orchestrator.run_input_video(args.input_video)
        return
    orchestrator.run_forever()


if __name__ == "__main__":
    main()
