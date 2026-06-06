from app.config import AppConfig
from app.ui import live_preview as live_preview_module
from app.ui.live_preview import LivePreviewSupervisor


class _FakeRecorder:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


def test_live_preview_start_clears_stale_local_hls(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(live_preview_module, "StreamRecorder", _FakeRecorder)
    cfg = AppConfig()
    supervisor = LivePreviewSupervisor(
        stream_config=cfg.stream,
        segment_dir=str(tmp_path / "live-preview"),
        hls_dir=str(tmp_path / "live-preview" / "hls"),
    )
    hls_dir = tmp_path / "live-preview" / "hls"
    hls_dir.mkdir(parents=True)
    stale_paths = [
        hls_dir / "stream.m3u8",
        hls_dir / "stream.m3u8.tmp",
        hls_dir / "live_00001.ts",
        hls_dir / "live_00002.ts.tmp",
    ]
    keep_path = hls_dir / "keep.txt"
    for path in stale_paths:
        path.write_text("stale", encoding="utf-8")
    keep_path.write_text("keep", encoding="utf-8")

    supervisor.start("http://127.0.0.1:6878/ace/getstream?id=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")

    assert all(not path.exists() for path in stale_paths)
    assert keep_path.exists()
