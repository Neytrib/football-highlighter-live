from pathlib import Path

from app.config import load_config


def test_stream_only_config_loads_and_overrides(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                "stream_only:",
                "  enabled: true",
                "  score_roi: [0.1, 0.2, 0.3, 0.4]",
                "  home_score_roi: [0.15, 0.2, 0.1, 0.4]",
                "  away_score_roi: [0.35, 0.2, 0.1, 0.4]",
                "  auto_locate_score_rois: true",
                "  auto_locate_frames: 42",
                "  change_threshold: 17.5",
                "  stable_threshold: 5.0",
                "  stable_frames: 4",
                "  cooldown_seconds: 12",
                "  match_id: 99",
                "  home_name: home_stream",
                "  away_name: away_stream",
            ]
        ),
        encoding="utf-8",
    )

    cfg = load_config(str(config_path))
    assert cfg.stream_only.enabled is True
    assert cfg.stream_only.score_roi == (0.1, 0.2, 0.3, 0.4)
    assert cfg.stream_only.home_score_roi == (0.15, 0.2, 0.1, 0.4)
    assert cfg.stream_only.away_score_roi == (0.35, 0.2, 0.1, 0.4)
    assert cfg.stream_only.auto_locate_score_rois is True
    assert cfg.stream_only.auto_locate_frames == 42
    assert cfg.stream_only.change_threshold == 17.5
    assert cfg.stream_only.stable_threshold == 5.0
    assert cfg.stream_only.stable_frames == 4
    assert cfg.stream_only.cooldown_seconds == 12
    assert cfg.stream_only.match_id == 99
    assert cfg.stream_only.home_name == "home_stream"
    assert cfg.stream_only.away_name == "away_stream"

    override = load_config(str(config_path), stream_only_override=False)
    assert override.stream_only.enabled is False
