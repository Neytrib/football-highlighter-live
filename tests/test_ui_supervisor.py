import subprocess

from app.ui.supervisor import EngineSupervisor, HighlighterSettings, HighlighterSupervisor


def test_highlighter_command_includes_runtime_flags() -> None:
    stream_url = "http://127.0.0.1:6878/ace/getstream?id=e38b33c56332de27ff25df223cdf488b1ec6051f"
    supervisor = HighlighterSupervisor(
        HighlighterSettings(
            config_path="configs/config.yaml",
            log_level="DEBUG",
            log_file="data/state/runtime.log",
            dry_run=True,
            stream_url=stream_url,
            extra_args=("--stream-only",),
        )
    )

    command = supervisor.build_command()

    assert command[1:3] == ["-m", "app.main"]
    assert "--config" in command
    assert "configs/config.yaml" in command
    assert "--log-level" in command
    assert "DEBUG" in command
    assert "--log-file" in command
    assert "data/state/runtime.log" in command
    assert "--dry-run" in command
    assert "--stream-url" in command
    assert stream_url in command
    assert "--stream-only" in command


def test_highlighter_live_mode_omits_dry_run() -> None:
    supervisor = HighlighterSupervisor(
        HighlighterSettings(config_path="configs/config.yaml", dry_run=False)
    )

    assert "--dry-run" not in supervisor.build_command()


def test_highlighter_status_without_process_is_stopped() -> None:
    supervisor = HighlighterSupervisor(HighlighterSettings(config_path="configs/config.yaml"))

    assert supervisor.status()["state"] == "stopped"


def test_engine_status_running(monkeypatch) -> None:
    engine = EngineSupervisor(container_name="football-acestream")

    def fake_run(command, *, timeout=10.0):
        if command[:2] == ["docker", "info"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == ["docker", "container", "inspect"]:
            return subprocess.CompletedProcess(command, 0, "[]", "")
        if command[:3] == ["docker", "inspect", "-f"]:
            return subprocess.CompletedProcess(command, 0, "true\n", "")
        return subprocess.CompletedProcess(command, 1, "", "unexpected")

    monkeypatch.setattr(engine, "_run", fake_run)
    monkeypatch.setattr(engine, "_engine_version", lambda: {"ok": True, "body": '{"version": "3.2.3"}'})

    status = engine.status()

    assert status["state"] == "running"
    assert "version" in status


def test_engine_status_when_docker_missing(monkeypatch) -> None:
    engine = EngineSupervisor(container_name="football-acestream")
    monkeypatch.setattr(
        engine,
        "_run",
        lambda command, timeout=10.0: subprocess.CompletedProcess(command, 1, "", "missing"),
    )

    assert engine.status()["state"] == "unavailable"
