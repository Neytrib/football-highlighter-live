from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen


@dataclass
class HighlighterSettings:
    config_path: str
    log_level: str = "INFO"
    log_file: str = "data/state/runtime.log"
    dry_run: bool = True
    stream_url: str = ""
    extra_args: tuple[str, ...] = ()


class HighlighterSupervisor:
    def __init__(self, settings: HighlighterSettings) -> None:
        self.settings = settings
        self.process: subprocess.Popen[str] | None = None

    def build_command(self) -> list[str]:
        command = [
            sys.executable,
            "-m",
            "app.main",
            "--config",
            self.settings.config_path,
            "--log-level",
            self.settings.log_level,
            "--log-file",
            self.settings.log_file,
        ]
        if self.settings.dry_run:
            command.append("--dry-run")
        if self.settings.stream_url:
            command.extend(["--stream-url", self.settings.stream_url])
        command.extend(self.settings.extra_args)
        return command

    def set_stream_url(self, stream_url: str) -> None:
        self.settings.stream_url = stream_url

    def status(self) -> dict[str, Any]:
        if self.process is None:
            return {"state": "stopped", "pid": None, "returncode": None}
        returncode = self.process.poll()
        if returncode is None:
            return {"state": "running", "pid": self.process.pid, "returncode": None}
        state = "stopped" if returncode == 0 else "error"
        return {"state": state, "pid": self.process.pid, "returncode": returncode}

    def start(self) -> dict[str, Any]:
        current = self.status()
        if current["state"] == "running":
            return current
        Path(self.settings.log_file).parent.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env.setdefault("PYTHONUNBUFFERED", "1")
        self.process = subprocess.Popen(
            self.build_command(),
            cwd=Path.cwd(),
            env=env,
            text=True,
            start_new_session=True,
        )
        return self.status()

    def stop(self, *, timeout_seconds: float = 8.0) -> dict[str, Any]:
        if self.process is None:
            return self.status()
        if self.process.poll() is not None:
            return self.status()
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
            self.process.wait(timeout=timeout_seconds)
        except ProcessLookupError:
            self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.process.wait(timeout=3)
        return self.status()

    def restart(self) -> dict[str, Any]:
        self.stop()
        return self.start()


class EngineSupervisor:
    REQUIRED_PORTS = ("6878/tcp", "8621/tcp", "8621/udp")

    def __init__(
        self,
        *,
        container_name: str = "football-acestream",
        image: str = "blaiseio/acelink",
        engine_url: str = "http://127.0.0.1:6878/webui/api/service?method=get_version",
    ) -> None:
        self.container_name = container_name
        self.image = image
        self.engine_url = engine_url

    def status(self) -> dict[str, Any]:
        if not self._docker_available():
            return {"state": "unavailable", "message": "Docker is not available"}
        inspect = self._run(["docker", "container", "inspect", self.container_name])
        if inspect.returncode != 0:
            return {"state": "stopped", "message": "AceStream container is not created"}
        running = self._run(["docker", "inspect", "-f", "{{.State.Running}}", self.container_name])
        if running.returncode != 0:
            return {"state": "error", "message": running.stderr.strip() or "Unable to inspect container"}
        if running.stdout.strip() != "true":
            return {"state": "stopped", "message": "AceStream container is stopped"}
        missing_ports = self._missing_required_ports()
        if missing_ports:
            return {
                "state": "misconfigured",
                "message": "AceStream container is missing required port bindings",
                "missingPorts": missing_ports,
            }
        version = self._engine_version()
        if version["ok"]:
            return {"state": "running", "message": "AceStream engine is ready", "version": version.get("body")}
        return {"state": "starting", "message": version.get("message", "Engine API is not ready")}

    def start(self) -> dict[str, Any]:
        if not self._docker_available():
            return {"state": "unavailable", "message": "Docker is not available"}
        inspect = self._run(["docker", "container", "inspect", self.container_name])
        if inspect.returncode == 0:
            if self._missing_required_ports():
                self._run(["docker", "stop", self.container_name], timeout=20)
                self._run(["docker", "rm", self.container_name], timeout=20)
                self._create_container()
            else:
                self._run(["docker", "start", self.container_name])
        else:
            self._create_container()
        return self._wait_ready()

    def stop(self) -> dict[str, Any]:
        if not self._docker_available():
            return {"state": "unavailable", "message": "Docker is not available"}
        self._run(["docker", "stop", self.container_name], timeout=20)
        return self.status()

    def restart(self) -> dict[str, Any]:
        if not self._docker_available():
            return {"state": "unavailable", "message": "Docker is not available"}
        if self._missing_required_ports():
            return self.start()
        self._run(["docker", "restart", self.container_name], timeout=30)
        return self._wait_ready()

    def _wait_ready(self) -> dict[str, Any]:
        for _ in range(20):
            status = self.status()
            if status["state"] == "running":
                return status
            time.sleep(1)
        return self.status()

    def _docker_available(self) -> bool:
        return self._run(["docker", "info"], timeout=5).returncode == 0

    def _engine_version(self) -> dict[str, Any]:
        try:
            with urlopen(self.engine_url, timeout=2) as response:
                body = response.read(4096).decode("utf-8", errors="replace")
        except URLError as exc:
            return {"ok": False, "message": str(exc)}
        except TimeoutError:
            return {"ok": False, "message": "Engine API timed out"}
        return {"ok": True, "body": body}

    def _create_container(self) -> subprocess.CompletedProcess[str]:
        return self._run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                self.container_name,
                "--platform=linux/amd64",
                "-p",
                "6878:6878",
                "-p",
                "8621:8621",
                "-p",
                "8621:8621/udp",
                self.image,
            ],
            timeout=30,
        )

    def _missing_required_ports(self) -> list[str]:
        ports = self._port_bindings()
        if ports is None:
            return []
        return [port for port in self.REQUIRED_PORTS if not ports.get(port)]

    def _port_bindings(self) -> dict[str, Any] | None:
        result = self._run(["docker", "inspect", "-f", "{{json .NetworkSettings.Ports}}", self.container_name])
        if result.returncode != 0:
            return None
        try:
            payload = json.loads(result.stdout.strip() or "{}")
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _run(command: list[str], *, timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            return subprocess.CompletedProcess(command, 1, "", str(exc))
