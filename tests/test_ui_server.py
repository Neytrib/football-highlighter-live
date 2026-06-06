from app.config import AppConfig
from app.ui.channel_catalog import ChannelCatalog
from app.ui.clip_library import ClipLibrary
from app.ui.server import UiContext, make_handler
from app.ui.supervisor import HighlighterSettings, HighlighterSupervisor


class _Engine:
    def status(self):
        return {"state": "stopped"}


def test_status_endpoint_serves_json_without_api_token(tmp_path) -> None:
    cfg = AppConfig()
    cfg.stream_url = "http://127.0.0.1:6878/ace/getstream?id=abc"
    cfg.football_data_api_token = "secret-token"
    cfg.output.raw_dir = str(tmp_path / "raw")
    cfg.output.cropped_dir = str(tmp_path / "cropped")
    cfg.output.uncertain_dir = str(tmp_path / "uncertain")
    cfg.output.var_dir = str(tmp_path / "var")
    cfg.output.custom_categories_dir = str(tmp_path / "categories")
    cfg.output.state_dir = str(tmp_path / "state")
    cfg.output.goals_dir = str(tmp_path / "goals")
    cfg.output.tmp_dir = str(tmp_path / "tmp")
    cfg.score_ocr.temp_dir = str(tmp_path / "score_ocr")
    cfg.ensure_output_dirs()

    context = UiContext(
        config=cfg,
        clip_library=ClipLibrary(cfg),
        channel_catalog=ChannelCatalog(tmp_path / "channels.json"),
        channel_sources=[],
        highlighter=HighlighterSupervisor(HighlighterSettings(config_path="configs/config.yaml")),
        engine=_Engine(),  # type: ignore[arg-type]
        host="127.0.0.1",
        port=0,
        log_file=str(tmp_path / "runtime.log"),
        env_file=str(tmp_path / ".env"),
        started_at=0,
    )
    handler = object.__new__(make_handler(context))
    payload = handler._status_payload()

    assert payload["server"]["state"] == "running"
    assert "secret-token" not in str(payload)
