from __future__ import annotations

import argparse
from dataclasses import dataclass
import errno
import json
import mimetypes
from pathlib import Path
from time import time
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.config import AppConfig, load_config
from app.ui.clip_library import ClipLibrary, ClipLibraryError
from app.ui.logs import read_recent_logs
from app.ui.stream_manager import StreamInputError, normalize_stream_input, stream_id_from_url, write_stream_url_env
from app.ui.supervisor import EngineSupervisor, HighlighterSettings, HighlighterSupervisor


STATIC_DIR = Path(__file__).parent / "static"


@dataclass
class UiContext:
    config: AppConfig
    clip_library: ClipLibrary
    highlighter: HighlighterSupervisor
    engine: EngineSupervisor
    host: str
    port: int
    log_file: str
    env_file: str
    started_at: float


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Football Highlighter local UI")
    parser.add_argument("--config", default="configs/config.yaml", help="Path to YAML config")
    parser.add_argument("--host", default="127.0.0.1", help="UI host")
    parser.add_argument("--port", type=int, default=5174, help="UI port")
    parser.add_argument("--log-level", default="INFO", help="Highlighter log level")
    parser.add_argument("--log-file", default="data/state/runtime.log", help="Highlighter JSON log file")
    parser.add_argument("--env-file", default=".env", help="Local env file to update when stream ID changes")
    parser.add_argument("--dry-run", action="store_true", help="Start highlighter in dry-run mode")
    parser.add_argument("--live-clips", action="store_true", help="Start highlighter with clipping enabled")
    parser.add_argument("--no-auto-start", action="store_true", help="Serve UI without starting the highlighter")
    parser.add_argument("--engine-url", default="http://127.0.0.1:6878/webui/api/service?method=get_version")
    parser.add_argument("--engine-container", default="football-acestream")
    parser.add_argument("--engine-image", default="blaiseio/acelink")
    return parser


def make_handler(context: UiContext) -> type[BaseHTTPRequestHandler]:
    class UiRequestHandler(BaseHTTPRequestHandler):
        server_version = "FootballHighlighterUI/1.0"

        def do_GET(self) -> None:
            try:
                parsed = urlparse(self.path)
                if parsed.path == "/api/status":
                    self._send_json(self._status_payload())
                    return
                if parsed.path == "/api/logs":
                    query = parse_qs(parsed.query)
                    limit = int(query.get("limit", ["80"])[0])
                    self._send_json({"logs": read_recent_logs(context.log_file, limit=limit)})
                    return
                if parsed.path == "/api/clips":
                    self._send_json(context.clip_library.list_clips())
                    return
                if parsed.path == "/media":
                    self._serve_media(parsed.query)
                    return
                self._serve_static(parsed.path)
            except ClipLibraryError as exc:
                self._send_json({"error": str(exc)}, status=exc.status)
            except Exception as exc:  # pragma: no cover - last-resort HTTP guard
                self._send_json({"error": str(exc)}, status=500)

        def do_POST(self) -> None:
            try:
                parsed = urlparse(self.path)
                payload = self._read_json()
                if parsed.path == "/api/categories":
                    self._send_json(context.clip_library.create_category(str(payload.get("name", ""))))
                    return
                if parsed.path == "/api/clips/rename":
                    self._send_json(
                        context.clip_library.rename_clip(
                            str(payload.get("root", "")),
                            str(payload.get("path", "")),
                            str(payload.get("newName", "")),
                        )
                    )
                    return
                if parsed.path == "/api/clips/delete":
                    self._send_json(
                        context.clip_library.delete_clip(
                            str(payload.get("root", "")),
                            str(payload.get("path", "")),
                        )
                    )
                    return
                if parsed.path == "/api/clips/move":
                    self._send_json(
                        context.clip_library.move_clip(
                            str(payload.get("root", "")),
                            str(payload.get("path", "")),
                            str(payload.get("category", "")),
                        )
                    )
                    return
                if parsed.path == "/api/stream":
                    selection = normalize_stream_input(
                        str(payload.get("streamId") or payload.get("stream") or payload.get("url") or ""),
                    )
                    context.config.stream_url = selection.stream_url
                    context.highlighter.set_stream_url(selection.stream_url)
                    write_stream_url_env(context.env_file, selection.stream_url)
                    highlighter_status = context.highlighter.status()
                    restarted = False
                    if highlighter_status["state"] == "running" and payload.get("restartHighlighter", True):
                        highlighter_status = context.highlighter.restart()
                        restarted = True
                    self._send_json(
                        {
                            "stream": self._stream_payload(),
                            "highlighter": highlighter_status,
                            "restartedHighlighter": restarted,
                        }
                    )
                    return
                if parsed.path == "/api/engine/start":
                    self._send_json({"engine": context.engine.start()})
                    return
                if parsed.path == "/api/engine/stop":
                    self._send_json({"engine": context.engine.stop()})
                    return
                if parsed.path == "/api/engine/restart":
                    self._send_json({"engine": context.engine.restart()})
                    return
                if parsed.path == "/api/highlighter/start":
                    self._send_json({"highlighter": context.highlighter.start()})
                    return
                if parsed.path == "/api/highlighter/stop":
                    self._send_json({"highlighter": context.highlighter.stop()})
                    return
                if parsed.path == "/api/highlighter/restart":
                    self._send_json({"highlighter": context.highlighter.restart()})
                    return
                self._send_json({"error": "Not found"}, status=404)
            except ClipLibraryError as exc:
                self._send_json({"error": str(exc)}, status=exc.status)
            except StreamInputError as exc:
                self._send_json({"error": str(exc)}, status=exc.status)
            except json.JSONDecodeError:
                self._send_json({"error": "Invalid JSON body"}, status=400)
            except Exception as exc:  # pragma: no cover - last-resort HTTP guard
                self._send_json({"error": str(exc)}, status=500)

        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"[ui] {self.address_string()} - {fmt % args}")

        def _status_payload(self) -> dict[str, Any]:
            stream = self._stream_payload()
            return {
                "server": {
                    "state": "running",
                    "host": context.host,
                    "port": context.port,
                    "url": f"http://{context.host}:{context.port}",
                    "uptimeSeconds": max(0, int(time() - context.started_at)),
                },
                "engine": context.engine.status(),
                "highlighter": context.highlighter.status(),
                "stream": stream,
                "mode": {
                    "dryRun": bool(context.config.dry_run),
                    "streamOnly": bool(context.config.stream_only.enabled),
                },
            }

        def _stream_payload(self) -> dict[str, Any]:
            stream_url = context.config.stream_url
            return {
                "configured": bool(stream_url),
                "id": stream_id_from_url(stream_url),
                "url": stream_url,
                "playbackUrl": stream_url,
            }

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length <= 0:
                return {}
            body = self.rfile.read(length).decode("utf-8")
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise json.JSONDecodeError("Expected JSON object", body, 0)
            return payload

        def _send_json(self, payload: dict[str, Any] | list[Any], *, status: int = 200) -> None:
            encoded = json.dumps(payload, ensure_ascii=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(encoded)

        def _serve_static(self, request_path: str) -> None:
            relative = "index.html" if request_path in {"", "/"} else unquote(request_path).lstrip("/")
            if relative.startswith("api/") or relative.startswith("media"):
                self._send_json({"error": "Not found"}, status=404)
                return
            path = (STATIC_DIR / relative).resolve()
            try:
                path.relative_to(STATIC_DIR.resolve())
            except ValueError:
                self._send_json({"error": "Not found"}, status=404)
                return
            if not path.exists() or not path.is_file():
                path = STATIC_DIR / "index.html"
            self._send_file(path)

        def _serve_media(self, query_string: str) -> None:
            query = parse_qs(query_string)
            root = query.get("root", [""])[0]
            relative_path = query.get("path", [""])[0]
            media_path = context.clip_library.resolve_media(root, relative_path)
            self._send_file(media_path, ranged=True)

        def _send_file(self, path: Path, *, ranged: bool = False) -> None:
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            if path.suffix.lower() == ".ts":
                content_type = "video/mp2t"
            total = path.stat().st_size
            range_header = self.headers.get("Range") if ranged else None
            if range_header and range_header.startswith("bytes="):
                start, end = self._parse_range(range_header, total)
                length = end - start + 1
                self.send_response(206)
                self.send_header("Content-Type", content_type)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
                self.send_header("Content-Length", str(length))
                self.end_headers()
                with path.open("rb") as fh:
                    fh.seek(start)
                    self.wfile.write(fh.read(length))
                return

            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(total))
            if ranged:
                self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            with path.open("rb") as fh:
                self.wfile.write(fh.read())

        @staticmethod
        def _parse_range(range_header: str, total: int) -> tuple[int, int]:
            raw = range_header.removeprefix("bytes=").split(",", 1)[0]
            start_raw, _, end_raw = raw.partition("-")
            start = int(start_raw) if start_raw else 0
            end = int(end_raw) if end_raw else total - 1
            start = max(0, min(start, total - 1))
            end = max(start, min(end, total - 1))
            return start, end

    return UiRequestHandler


def bind_server(context: UiContext, *, attempts: int = 20) -> ReusableThreadingHTTPServer:
    last_error: OSError | None = None
    for port in range(context.port, context.port + max(1, attempts)):
        context.port = port
        try:
            return ReusableThreadingHTTPServer((context.host, port), make_handler(context))
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise
            last_error = exc
    if last_error is not None:
        raise last_error
    raise OSError("Unable to bind UI server")


def main() -> None:
    parser = build_parser()
    args, highlighter_args = parser.parse_known_args()

    dry_run_override = None
    if args.dry_run:
        dry_run_override = True
    elif args.live_clips:
        dry_run_override = False

    config = load_config(args.config, dry_run_override=dry_run_override)
    settings = HighlighterSettings(
        config_path=args.config,
        log_level=args.log_level,
        log_file=args.log_file,
        dry_run=config.dry_run,
        stream_url=config.stream_url,
        extra_args=tuple(highlighter_args),
    )
    context = UiContext(
        config=config,
        clip_library=ClipLibrary(config),
        highlighter=HighlighterSupervisor(settings),
        engine=EngineSupervisor(
            container_name=args.engine_container,
            image=args.engine_image,
            engine_url=args.engine_url,
        ),
        host=args.host,
        port=args.port,
        log_file=args.log_file,
        env_file=args.env_file,
        started_at=time(),
    )

    if not args.no_auto_start:
        context.highlighter.start()

    server = bind_server(context)
    print(f"Football Highlighter UI: http://{context.host}:{context.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        context.highlighter.stop()
        server.server_close()


if __name__ == "__main__":
    main()
