import os
from pathlib import Path

from app.config import AppConfig
from app.ui.channel_catalog import ChannelCatalog
from app.ui.clip_library import ClipLibrary
from app.ui.live_preview import LivePreviewSupervisor
from app.ui.server import UiContext, make_handler, seed_channel_catalog, select_live_stream, select_recording_stream
from app.ui.supervisor import HighlighterSettings, HighlighterSupervisor


class _Engine:
    def status(self):
        return {"state": "stopped"}


class _Highlighter:
    def __init__(self, state="stopped"):
        self.state = state
        self.stream_url = ""
        self.started = 0
        self.restarted = 0

    def set_stream_url(self, stream_url):
        self.stream_url = stream_url

    def status(self):
        return {"state": self.state, "pid": None, "returncode": None}

    def start(self):
        self.started += 1
        self.state = "running"
        return self.status()

    def restart(self):
        self.restarted += 1
        self.state = "running"
        return self.status()


def _make_context(tmp_path, cfg=None):
    cfg = cfg or AppConfig()
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

    return UiContext(
        config=cfg,
        clip_library=ClipLibrary(cfg),
        channel_catalog=ChannelCatalog(tmp_path / "channels.json"),
        channel_sources=[],
        highlighter=HighlighterSupervisor(HighlighterSettings(config_path="configs/config.yaml")),
        live_preview=LivePreviewSupervisor(
            stream_config=cfg.stream,
            segment_dir=str(tmp_path / "tmp" / "live-preview"),
        ),
        engine=_Engine(),  # type: ignore[arg-type]
        host="127.0.0.1",
        port=0,
        log_file=str(tmp_path / "runtime.log"),
        env_file=str(tmp_path / ".env"),
        started_at=0,
    )


def test_status_endpoint_serves_json_without_api_token(tmp_path) -> None:
    cfg = AppConfig()
    cfg.stream_url = "http://127.0.0.1:6878/ace/getstream?id=abc"
    context = _make_context(tmp_path, cfg)
    handler = object.__new__(make_handler(context))
    payload = handler._status_payload()

    assert payload["server"]["state"] == "running"
    assert "secret-token" not in str(payload)


def test_seed_channel_catalog_imports_only_when_empty(tmp_path) -> None:
    catalog = ChannelCatalog(tmp_path / "channels.json")
    seed = tmp_path / "seed.json"
    seed.write_text(
        """
        {
          "channels": [
            {
              "name": "Seeded",
              "stream": "acestream://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/",
              "language": "en",
              "quality": "1080p"
            }
          ]
        }
        """,
        encoding="utf-8",
    )

    result = seed_channel_catalog(catalog, str(seed))
    skipped = seed_channel_catalog(catalog, str(seed))

    assert result is not None
    assert result["added"] == 1
    assert skipped is None
    assert [channel["name"] for channel in catalog.list_payload()["channels"]] == ["Seeded"]


def test_select_recording_stream_starts_stopped_highlighter(tmp_path) -> None:
    context = _make_context(tmp_path)
    highlighter = _Highlighter("stopped")
    context.highlighter = highlighter  # type: ignore[assignment]

    payload = select_recording_stream(context, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")

    assert payload["stream"]["id"] == "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    assert payload["highlighter"]["state"] == "running"
    assert payload["startedHighlighter"] is True
    assert payload["restartedHighlighter"] is False
    assert highlighter.started == 1
    assert highlighter.restarted == 0
    assert highlighter.stream_url == context.config.stream_url
    assert "STREAM_URL=" in Path(context.env_file).read_text(encoding="utf-8")


def test_select_recording_stream_restarts_running_highlighter(tmp_path) -> None:
    context = _make_context(tmp_path)
    highlighter = _Highlighter("running")
    context.highlighter = highlighter  # type: ignore[assignment]

    payload = select_recording_stream(context, "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")

    assert payload["highlighter"]["state"] == "running"
    assert payload["startedHighlighter"] is False
    assert payload["restartedHighlighter"] is True
    assert highlighter.started == 0
    assert highlighter.restarted == 1


def test_select_recording_stream_can_skip_starting_highlighter(tmp_path) -> None:
    context = _make_context(tmp_path)
    highlighter = _Highlighter("stopped")
    context.highlighter = highlighter  # type: ignore[assignment]

    payload = select_recording_stream(
        context,
        "cccccccccccccccccccccccccccccccccccccccc",
        start_highlighter=False,
    )

    assert payload["highlighter"]["state"] == "stopped"
    assert payload["startedHighlighter"] is False
    assert highlighter.started == 0


def test_select_live_stream_starts_local_preview_for_new_stream(tmp_path) -> None:
    context = _make_context(tmp_path)
    started_urls = []

    def fake_start(stream_url):
        started_urls.append(stream_url)
        return {"state": "running", "configured": True, "url": stream_url}

    context.live_preview.start = fake_start  # type: ignore[method-assign]

    payload = select_live_stream(
        context,
        "dddddddddddddddddddddddddddddddddddddddd",
        recording_segment_dir=tmp_path / "recording-segments",
        recording_hls_dir=tmp_path / "recording-hls",
    )

    assert started_urls == [
        "http://127.0.0.1:6878/ace/getstream?id=dddddddddddddddddddddddddddddddddddddddd&pid=football-highlighter-recorder&use_stop_notifications=1"
    ]
    assert payload["live"]["state"] == "running"
    assert payload["live"]["hlsPlaybackUrl"] == "/api/live/hls/stream.m3u8"


def test_select_live_stream_mirrors_recording_buffer_for_recording_stream(tmp_path) -> None:
    context = _make_context(tmp_path)
    context.config.stream_url = (
        "http://127.0.0.1:6878/ace/getstream?id=eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
        "&pid=football-highlighter-recorder&use_stop_notifications=1"
    )
    mirrored = []

    def fake_use_existing(stream_url, segment_dir, hls_dir):
        mirrored.append((stream_url, Path(segment_dir), Path(hls_dir)))
        return {"state": "mirroring", "configured": True, "url": stream_url}

    context.live_preview.use_existing = fake_use_existing  # type: ignore[method-assign]

    payload = select_live_stream(
        context,
        "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        recording_segment_dir=tmp_path / "recording-segments",
        recording_hls_dir=tmp_path / "recording-hls",
    )

    assert mirrored == [
        (
            context.config.stream_url,
            tmp_path / "recording-segments",
            tmp_path / "recording-hls",
        )
    ]
    assert payload["live"]["state"] == "mirroring"
    assert payload["live"]["hlsPlaybackUrl"] == "/api/live/hls/stream.m3u8"


def test_live_latest_payload_uses_newest_segment(tmp_path) -> None:
    context = _make_context(tmp_path)
    segment_dir = Path(context.config.output.tmp_dir) / "segments"
    hls_dir = Path(context.config.output.tmp_dir) / "hls"
    segment_dir.mkdir(parents=True)
    hls_dir.mkdir(parents=True)
    old_segment = segment_dir / "segment_20260606T190000.mp4"
    new_segment = segment_dir / "segment_20260606T190002.mp4"
    (hls_dir / "stream.m3u8").write_text("#EXTM3U\n", encoding="utf-8")
    old_segment.write_bytes(b"old")
    new_segment.write_bytes(b"newer")
    os.utime(old_segment, (100, 100))
    os.utime(new_segment, (200, 200))

    handler = object.__new__(make_handler(context))
    payload = handler._live_latest_payload()

    assert payload["available"] is True
    assert payload["name"] == new_segment.name
    assert payload["size"] == 5
    assert payload["mediaUrl"] == f"/api/live/segment?name={new_segment.name}"
    assert payload["frameUrl"] == f"/api/live/frame?name={new_segment.name}"
    assert payload["hlsAvailable"] is True
    assert payload["hlsReady"] is False
    assert payload["hlsSegmentCount"] == 0
    assert payload["playbackMode"] == "segment"
    assert payload["hlsUrl"] == "/api/live/hls/stream.m3u8"


def test_live_latest_payload_marks_local_hls_ready_after_startup_segments(tmp_path) -> None:
    context = _make_context(tmp_path)
    context.config.stream.hls_startup_segments = 2
    hls_dir = Path(context.config.output.tmp_dir) / "hls"
    hls_dir.mkdir(parents=True)
    (hls_dir / "stream.m3u8").write_text(
        "#EXTM3U\n#EXT-X-TARGETDURATION:2\nlive_00001.ts\nlive_00002.ts\n",
        encoding="utf-8",
    )

    handler = object.__new__(make_handler(context))
    payload = handler._live_latest_payload()

    assert payload["available"] is False
    assert payload["hlsAvailable"] is True
    assert payload["hlsReady"] is True
    assert payload["hlsSegmentCount"] == 2
    assert payload["playbackMode"] == "local_hls"
    assert payload["segmentSeconds"] == context.config.stream.segment_seconds
    assert payload["startupTargetSeconds"] == context.config.stream.live_startup_target_seconds


def test_live_latest_payload_defers_local_hls_until_startup_window(tmp_path) -> None:
    context = _make_context(tmp_path)
    context.config.stream.hls_startup_segments = 2
    segment_dir = Path(context.config.output.tmp_dir) / "segments"
    hls_dir = Path(context.config.output.tmp_dir) / "hls"
    segment_dir.mkdir(parents=True)
    hls_dir.mkdir(parents=True)
    segment = segment_dir / "segment_20260606T190002.mp4"
    segment.write_bytes(b"segment")
    os.utime(segment, (200, 200))
    (hls_dir / "stream.m3u8").write_text(
        "#EXTM3U\n#EXT-X-TARGETDURATION:2\nlive_00001.ts\n",
        encoding="utf-8",
    )

    handler = object.__new__(make_handler(context))
    payload = handler._live_latest_payload()

    assert payload["available"] is True
    assert payload["hlsAvailable"] is True
    assert payload["hlsReady"] is False
    assert payload["hlsSegmentCount"] == 1
    assert payload["playbackMode"] == "segment"
    assert payload["mediaUrl"] == f"/api/live/segment?name={segment.name}"


def test_live_latest_payload_uses_mirrored_recording_buffer(tmp_path) -> None:
    context = _make_context(tmp_path)
    stream_url = "http://127.0.0.1:6878/ace/getstream?id=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    recording_dir = Path(context.config.output.tmp_dir) / "segments"
    preview_dir = Path(context.config.output.tmp_dir) / "live-preview"
    recording_hls_dir = Path(context.config.output.tmp_dir) / "hls"
    preview_hls_dir = preview_dir / "hls"
    recording_dir.mkdir(parents=True)
    preview_dir.mkdir(parents=True)
    recording_hls_dir.mkdir(parents=True)
    preview_hls_dir.mkdir(parents=True)
    recording_segment = recording_dir / "segment_20260606T190003.mp4"
    preview_segment = preview_dir / "segment_20260606T190004.mp4"
    (recording_hls_dir / "stream.m3u8").write_text("#EXTM3U\n", encoding="utf-8")
    (preview_hls_dir / "stream.m3u8").write_text("#EXTM3U\n", encoding="utf-8")
    recording_segment.write_bytes(b"recording")
    preview_segment.write_bytes(b"preview")
    os.utime(preview_segment, (400, 400))

    context.live_preview.use_existing(stream_url, recording_dir, recording_hls_dir)
    fresh_time = context.live_preview.started_at
    os.utime(recording_segment, (fresh_time, fresh_time))
    os.utime(recording_hls_dir / "stream.m3u8", (fresh_time, fresh_time))
    handler = object.__new__(make_handler(context))
    payload = handler._live_latest_payload()

    assert context.live_preview.status()["state"] == "mirroring"
    assert context.live_preview.status()["hlsDir"] == str(recording_hls_dir)
    assert payload["available"] is True
    assert payload["name"] == recording_segment.name
    assert payload["size"] == 9
    assert payload["hlsAvailable"] is True
    assert payload["hlsReady"] is False
    assert payload["playbackMode"] == "segment"


def test_live_latest_payload_ignores_pre_switch_mirrored_segment(tmp_path) -> None:
    context = _make_context(tmp_path)
    stream_url = "http://127.0.0.1:6878/ace/getstream?id=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    recording_dir = Path(context.config.output.tmp_dir) / "segments"
    recording_hls_dir = Path(context.config.output.tmp_dir) / "hls"
    recording_dir.mkdir(parents=True)
    recording_hls_dir.mkdir(parents=True)
    old_segment = recording_dir / "segment_20260606T190003.mp4"
    old_segment.write_bytes(b"old")

    context.live_preview.use_existing(stream_url, recording_dir, recording_hls_dir)
    old_time = context.live_preview.started_at - 10
    os.utime(old_segment, (old_time, old_time))

    handler = object.__new__(make_handler(context))
    payload = handler._live_latest_payload()

    assert payload["available"] is False
    assert payload["hlsAvailable"] is False
    assert payload["hlsReady"] is False
    assert payload["playbackMode"] == "warming"


def test_live_latest_payload_ignores_pre_switch_hls_playlist(tmp_path) -> None:
    context = _make_context(tmp_path)
    context.config.stream.hls_startup_segments = 2
    stream_url = "http://127.0.0.1:6878/ace/getstream?id=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    recording_dir = Path(context.config.output.tmp_dir) / "segments"
    recording_hls_dir = Path(context.config.output.tmp_dir) / "hls"
    recording_dir.mkdir(parents=True)
    recording_hls_dir.mkdir(parents=True)
    playlist = recording_hls_dir / "stream.m3u8"
    playlist.write_text("#EXTM3U\nlive_00001.ts\nlive_00002.ts\n", encoding="utf-8")

    context.live_preview.use_existing(stream_url, recording_dir, recording_hls_dir)
    old_time = context.live_preview.started_at - 10
    os.utime(playlist, (old_time, old_time))

    handler = object.__new__(make_handler(context))
    payload = handler._live_latest_payload()

    assert payload["available"] is False
    assert payload["hlsAvailable"] is False
    assert payload["hlsReady"] is False
    assert payload["hlsSegmentCount"] == 0
    assert payload["playbackMode"] == "warming"


def test_live_latest_payload_reports_external_hls_without_segments(tmp_path) -> None:
    context = _make_context(tmp_path)
    stream_url = "http://127.0.0.1:6878/ace/getstream?id=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    context.live_preview.use_external_hls(
        stream_url,
        "http://127.0.0.1:6878/ace/m/hash/session.m3u8",
    )

    handler = object.__new__(make_handler(context))
    payload = handler._live_latest_payload()

    assert context.live_preview.status()["state"] == "external"
    assert payload["available"] is False
    assert payload["hlsAvailable"] is True
    assert payload["hlsReady"] is True
    assert payload["hlsSegmentCount"] is None
    assert payload["playbackMode"] == "external_hls"
    assert payload["hlsUrl"] == "/api/live/hls/stream.m3u8"
