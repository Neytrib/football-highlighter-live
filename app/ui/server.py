from __future__ import annotations

import argparse
from dataclasses import dataclass
import errno
import json
import mimetypes
import os
from pathlib import Path
import re
import subprocess
from time import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse
from urllib.request import Request, urlopen
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.config import AppConfig, load_config
from app.ui.channel_catalog import ChannelCatalog, ChannelCatalogError, ChannelRefreshWorker, parse_catalog_sources
from app.ui.clip_library import ClipLibrary, ClipLibraryError
from app.ui.live_preview import LivePreviewSupervisor
from app.ui.logs import read_recent_logs
from app.ui.stream_manager import StreamInputError, normalize_stream_input, stream_id_from_url, write_stream_url_env
from app.ui.supervisor import EngineSupervisor, HighlighterSettings, HighlighterSupervisor


STATIC_DIR = Path(__file__).parent / "static"
DEFAULT_CHANNEL_SEED = "configs/channels.json"
LIVE_SWITCH_FRESHNESS_GRACE_SECONDS = 2.0


@dataclass
class UiContext:
    config: AppConfig
    clip_library: ClipLibrary
    channel_catalog: ChannelCatalog
    channel_sources: list[str]
    highlighter: HighlighterSupervisor
    live_preview: LivePreviewSupervisor
    engine: EngineSupervisor
    host: str
    port: int
    log_file: str
    env_file: str
    started_at: float


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


def select_recording_stream(
    context: UiContext,
    stream_value: str,
    *,
    start_highlighter: bool = True,
    restart_highlighter: bool = True,
) -> dict[str, Any]:
    selection = normalize_stream_input(stream_value)
    context.config.stream_url = selection.stream_url
    context.highlighter.set_stream_url(selection.stream_url)
    write_stream_url_env(context.env_file, selection.stream_url)

    highlighter_status = context.highlighter.status()
    restarted = False
    started = False
    if highlighter_status["state"] == "running" and restart_highlighter:
        highlighter_status = context.highlighter.restart()
        restarted = True
    elif highlighter_status["state"] != "running" and start_highlighter:
        highlighter_status = context.highlighter.start()
        started = highlighter_status["state"] == "running"

    return {
        "stream": {
            "configured": bool(context.config.stream_url),
            "id": selection.stream_id,
            "url": context.config.stream_url,
            "playbackUrl": context.config.stream_url,
        },
        "highlighter": highlighter_status,
        "startedHighlighter": started,
        "restartedHighlighter": restarted,
    }


def select_live_stream(
    context: UiContext,
    stream_value: str,
    *,
    recording_segment_dir: str | Path,
    recording_hls_dir: str | Path,
) -> dict[str, Any]:
    selection = normalize_stream_input(stream_value)
    recording_id = stream_id_from_url(context.config.stream_url)
    if context.config.stream_url and (selection.stream_url == context.config.stream_url or selection.stream_id == recording_id):
        live_status = context.live_preview.use_existing(
            selection.stream_url,
            recording_segment_dir,
            recording_hls_dir,
        )
    else:
        live_status = context.live_preview.start(selection.stream_url)
    live_status["hlsPlaybackUrl"] = "/api/live/hls/stream.m3u8"
    return {"live": live_status}


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
    parser.add_argument("--channel-catalog-sources", default="", help="Comma-separated lawful JSON channel catalog sources")
    parser.add_argument("--channel-seed", default=DEFAULT_CHANNEL_SEED, help="Initial JSON channel catalog for an empty local catalog")
    parser.add_argument("--channel-refresh-seconds", type=int, default=60, help="Channel catalog refresh interval")
    return parser


def seed_channel_catalog(catalog: ChannelCatalog, sources: str | list[str]) -> dict[str, Any] | None:
    seed_sources = parse_catalog_sources(sources)
    if not seed_sources or catalog.list_payload().get("channels"):
        return None
    return catalog.refresh_from_sources(seed_sources)


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
                if parsed.path == "/api/channels":
                    self._send_json(context.channel_catalog.list_payload())
                    return
                if parsed.path == "/api/live/latest":
                    self._send_json(self._live_latest_payload())
                    return
                if parsed.path == "/api/live/status":
                    self._send_json({"live": context.live_preview.status()})
                    return
                if parsed.path == "/api/live/segment":
                    self._serve_live_segment(parsed.query)
                    return
                if parsed.path == "/api/live/frame":
                    self._serve_live_frame(parsed.query)
                    return
                if parsed.path == "/api/live/hls/stream.m3u8" or parsed.path.startswith("/api/live/hls/"):
                    self._serve_live_hls_asset(parsed.path)
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
                    self._send_json(
                        select_recording_stream(
                            context,
                            str(payload.get("streamId") or payload.get("stream") or payload.get("url") or ""),
                            start_highlighter=bool(payload.get("startHighlighter", True)),
                            restart_highlighter=bool(payload.get("restartHighlighter", True)),
                        )
                    )
                    return
                if parsed.path == "/api/channels":
                    self._send_json({"channel": context.channel_catalog.add_channel(payload)})
                    return
                if parsed.path == "/api/channels/delete":
                    self._send_json(
                        context.channel_catalog.delete_channel(str(payload.get("streamId") or payload.get("id") or ""))
                    )
                    return
                if parsed.path == "/api/channels/refresh":
                    sources = parse_catalog_sources(payload.get("sources") or context.channel_sources)
                    self._send_json({"refresh": context.channel_catalog.refresh_from_sources(sources)})
                    return
                if parsed.path == "/api/live":
                    self._send_json(
                        select_live_stream(
                            context,
                            str(payload.get("streamId") or payload.get("stream") or payload.get("url") or ""),
                            recording_segment_dir=self._recording_segment_dir(),
                            recording_hls_dir=self._recording_hls_dir(),
                        )
                    )
                    return
                if parsed.path == "/api/live/stop":
                    self._send_json({"live": context.live_preview.stop()})
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
            except ChannelCatalogError as exc:
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
            live = context.live_preview.status()
            if live["configured"] or stream["configured"]:
                live["hlsPlaybackUrl"] = "/api/live/hls/stream.m3u8"
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
                "live": live,
                "channels": {
                    "count": len(context.channel_catalog.list_payload().get("channels", [])),
                    "sources": len(context.channel_sources),
                },
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

        def _resolve_acestream_hls_url(self, stream_id: str) -> str:
            if not stream_id:
                return ""
            url = (
                "http://127.0.0.1:6878/ace/manifest.m3u8"
                f"?id={quote(stream_id)}&format=json&pid=football-highlighter-live"
            )
            try:
                with urlopen(Request(url, headers={"User-Agent": "football-highlighter/1.0"}), timeout=18) as response:
                    payload = json.loads(response.read(4096).decode("utf-8", errors="replace"))
            except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError):
                return ""

            response_payload = payload.get("response") if isinstance(payload, dict) else None
            if not isinstance(response_payload, dict):
                return ""
            playback_url = str(response_payload.get("playback_url") or "")
            return playback_url if self._is_allowed_engine_url(playback_url) else ""

        def _is_allowed_engine_url(self, url: str) -> bool:
            parsed = urlparse(url)
            host = parsed.hostname or ""
            return parsed.scheme == "http" and host in {"127.0.0.1", "localhost"} and (parsed.port or 80) == 6878

        def _engine_proxy_url(self, url: str) -> str:
            return f"/api/live/hls/proxy?url={quote(url, safe='')}"

        def _rewrite_engine_m3u8(self, body: bytes, base_url: str) -> bytes:
            text = body.decode("utf-8", errors="replace")

            def replace_uri(match: re.Match[str]) -> str:
                absolute = urljoin(base_url, match.group(1))
                return f'URI="{self._engine_proxy_url(absolute)}"'

            lines: list[str] = []
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("#EXT-X-MAP:"):
                    lines.append(re.sub(r'URI="([^"]+)"', replace_uri, line))
                elif stripped and not stripped.startswith("#"):
                    lines.append(self._engine_proxy_url(urljoin(base_url, stripped)))
                else:
                    lines.append(line)
            return ("\n".join(lines) + "\n").encode("utf-8")

        def _fetch_engine_asset(self, url: str) -> tuple[bytes, str]:
            if not self._is_allowed_engine_url(url):
                raise ValueError("Engine URL is not allowed")
            with urlopen(Request(url, headers={"User-Agent": "football-highlighter/1.0"}), timeout=20) as response:
                content_type = response.headers.get_content_type() or "application/octet-stream"
                return response.read(), content_type

        def _send_bytes(self, body: bytes, content_type: str, *, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _live_latest_payload(self) -> dict[str, Any]:
            hls_path = self._live_hls_dir() / "stream.m3u8"
            external_hls_url = context.live_preview.external_hls_url()
            fresh_after = self._live_fresh_after()
            hls_available, hls_ready, hls_segment_count = self._hls_state(
                hls_path,
                bool(external_hls_url),
                fresh_after,
            )
            latest, latest_stat = self._find_latest_segment()
            if latest is None or latest_stat is None:
                return {
                    "available": False,
                    "message": "Starting live preview",
                    "playbackMode": "external_hls" if external_hls_url else ("local_hls" if hls_ready else "warming"),
                    "hlsAvailable": hls_available,
                    "hlsReady": hls_ready,
                    "hlsSegmentCount": hls_segment_count,
                    "hlsUrl": "/api/live/hls/stream.m3u8",
                    "startedAt": context.live_preview.started_at or None,
                    "segmentSeconds": context.config.stream.segment_seconds,
                    "startupTargetSeconds": context.config.stream.live_startup_target_seconds,
                }

            encoded_name = quote(latest.name)

            return {
                "available": True,
                "playbackMode": "external_hls" if external_hls_url else ("local_hls" if hls_ready else "segment"),
                "name": latest.name,
                "size": latest_stat.st_size,
                "mtime": latest_stat.st_mtime,
                "ageSeconds": max(0, int(time() - latest_stat.st_mtime)),
                "mediaUrl": f"/api/live/segment?name={encoded_name}",
                "frameUrl": f"/api/live/frame?name={encoded_name}",
                "hlsAvailable": hls_available,
                "hlsReady": hls_ready,
                "hlsSegmentCount": hls_segment_count,
                "hlsUrl": "/api/live/hls/stream.m3u8",
                "startedAt": context.live_preview.started_at or None,
                "segmentSeconds": context.config.stream.segment_seconds,
                "startupTargetSeconds": context.config.stream.live_startup_target_seconds,
            }

        def _hls_state(self, hls_path: Path, external_hls: bool, fresh_after: float) -> tuple[bool, bool, int | None]:
            if external_hls:
                return True, True, None
            if not hls_path.exists() or not hls_path.is_file():
                return False, False, 0
            try:
                if hls_path.stat().st_mtime < fresh_after:
                    return False, False, 0
            except OSError:
                return False, False, 0
            segment_count = self._hls_segment_count(hls_path)
            required_segments = max(1, int(context.config.stream.hls_startup_segments))
            return True, segment_count >= required_segments, segment_count

        def _live_fresh_after(self) -> float:
            if not context.live_preview.is_active() or not context.live_preview.started_at:
                return 0.0
            return max(0.0, context.live_preview.started_at - LIVE_SWITCH_FRESHNESS_GRACE_SECONDS)

        @staticmethod
        def _hls_segment_count(hls_path: Path) -> int:
            try:
                text = hls_path.read_text(encoding="utf-8")
            except OSError:
                return 0
            return sum(1 for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#"))

        def _serve_live_hls_asset(self, request_path: str) -> None:
            external_hls_url = context.live_preview.external_hls_url()
            if external_hls_url:
                self._serve_engine_hls_asset(request_path)
                return

            name = unquote(request_path.removeprefix("/api/live/hls/")).strip("/")
            if "/" in name or "\\" in name or name in {"", ".", ".."} or name.endswith(".tmp"):
                self._send_json({"error": "Not found"}, status=404)
                return

            allowed = (
                name == "stream.m3u8"
                or name == "init.mp4"
                or (name.startswith("live_") and name.endswith((".m4s", ".ts")))
            )
            if not allowed:
                self._send_json({"error": "Not found"}, status=404)
                return

            hls_dir = self._live_hls_dir()
            path = (hls_dir / name).resolve()
            try:
                path.relative_to(hls_dir)
            except ValueError:
                self._send_json({"error": "Not found"}, status=404)
                return
            if not path.exists() or not path.is_file():
                self._send_json({"error": "Not found"}, status=404)
                return
            self._send_file(path, ranged=True)

        def _serve_engine_hls_asset(self, request_path: str) -> None:
            external_hls_url = context.live_preview.external_hls_url()
            if request_path == "/api/live/hls/stream.m3u8":
                try:
                    body, _content_type = self._fetch_engine_asset(external_hls_url)
                except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
                    self._send_json({"error": f"Engine HLS unavailable: {exc}"}, status=502)
                    return
                rewritten = self._rewrite_engine_m3u8(body, external_hls_url)
                self._send_bytes(rewritten, "application/vnd.apple.mpegurl")
                return

            if request_path != "/api/live/hls/proxy":
                self._send_json({"error": "Not found"}, status=404)
                return

            query = parse_qs(urlparse(self.path).query)
            url = query.get("url", [""])[0]
            try:
                body, content_type = self._fetch_engine_asset(url)
            except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
                self._send_json({"error": f"Engine HLS asset unavailable: {exc}"}, status=502)
                return
            suffix = Path(urlparse(url).path).suffix.lower()
            if suffix == ".m3u8":
                body = self._rewrite_engine_m3u8(body, url)
                content_type = "application/vnd.apple.mpegurl"
            elif suffix == ".ts":
                content_type = "video/mp2t"
            elif suffix == ".m4s":
                content_type = "video/iso.segment"
            elif suffix == ".mp4":
                content_type = "video/mp4"
            self._send_bytes(body, content_type)

        def _serve_live_segment(self, query_string: str) -> None:
            query = parse_qs(query_string)
            name = query.get("name", [""])[0]
            if "/" in name or "\\" in name or not name.startswith("segment_") or not name.endswith(".mp4"):
                self._send_json({"error": "Not found"}, status=404)
                return

            segment_dir = self._live_segment_dir()
            path = (segment_dir / name).resolve()
            try:
                path.relative_to(segment_dir)
            except ValueError:
                self._send_json({"error": "Not found"}, status=404)
                return
            if not path.exists() or not path.is_file():
                self._send_json({"error": "Not found"}, status=404)
                return
            try:
                if path.stat().st_mtime < self._live_fresh_after():
                    self._send_json({"error": "Not found"}, status=404)
                    return
            except OSError:
                self._send_json({"error": "Not found"}, status=404)
                return
            self._send_file(path, ranged=True)

        def _serve_live_frame(self, query_string: str) -> None:
            query = parse_qs(query_string)
            name = query.get("name", [""])[0]
            path = self._resolve_live_segment_name(name)
            if path is None:
                self._send_json({"error": "Not found"}, status=404)
                return

            result = subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-ss",
                    "0.5",
                    "-i",
                    str(path),
                    "-frames:v",
                    "1",
                    "-f",
                    "mjpeg",
                    "pipe:1",
                ],
                capture_output=True,
                timeout=6,
            )
            if result.returncode != 0 or not result.stdout:
                self._send_json({"error": "Frame unavailable"}, status=503)
                return

            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(result.stdout)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(result.stdout)

        def _find_latest_segment(self) -> tuple[Path | None, os.stat_result | None]:
            segment_dir = self._live_segment_dir()
            if not segment_dir.exists():
                return None, None

            now = time()
            fresh_after = self._live_fresh_after()
            candidates: list[tuple[Path, os.stat_result]] = []
            for path in segment_dir.glob("segment_*.mp4"):
                if not path.is_file():
                    continue
                stat = path.stat()
                if stat.st_size <= 0:
                    continue
                if stat.st_mtime < fresh_after:
                    continue
                candidates.append((path, stat))

            if not candidates:
                return None, None

            candidates.sort(key=lambda item: item[1].st_mtime, reverse=True)
            for path, stat in candidates:
                if now - stat.st_mtime >= 1.0:
                    return path, stat
            return candidates[0]

        def _resolve_live_segment_name(self, name: str) -> Path | None:
            if "/" in name or "\\" in name or not name.startswith("segment_") or not name.endswith(".mp4"):
                return None

            segment_dir = self._live_segment_dir()
            path = (segment_dir / name).resolve()
            try:
                path.relative_to(segment_dir)
            except ValueError:
                return None
            if not path.exists() or not path.is_file():
                return None
            try:
                if path.stat().st_mtime < self._live_fresh_after():
                    return None
            except OSError:
                return None
            return path

        def _live_segment_dir(self) -> Path:
            return context.live_preview.segment_dir_for_preview(self._recording_segment_dir())

        def _live_hls_dir(self) -> Path:
            return context.live_preview.hls_dir_for_preview(self._recording_hls_dir())

        def _recording_segment_dir(self) -> Path:
            return (Path(context.config.output.tmp_dir) / "segments").resolve()

        def _recording_hls_dir(self) -> Path:
            return (Path(context.config.output.tmp_dir) / "hls").resolve()

        def _send_file(self, path: Path, *, ranged: bool = False) -> None:
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            if path.suffix.lower() == ".ts":
                content_type = "video/mp2t"
            if path.suffix.lower() == ".m3u8":
                content_type = "application/vnd.apple.mpegurl"
            if path.suffix.lower() == ".m4s":
                content_type = "video/iso.segment"
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
                if path.suffix.lower() in {".m3u8", ".m4s", ".mp4"}:
                    self.send_header("Cache-Control", "no-store")
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
            if path.suffix.lower() in {".m3u8", ".m4s", ".mp4"}:
                self.send_header("Cache-Control", "no-store")
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
    channel_sources = parse_catalog_sources(args.channel_catalog_sources or os.getenv("CHANNEL_CATALOG_URLS", ""))
    channel_catalog = ChannelCatalog(Path(config.output.state_dir) / "channels.json")
    seed_channel_catalog(channel_catalog, os.getenv("CHANNEL_CATALOG_SEED", args.channel_seed))
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
        channel_catalog=channel_catalog,
        channel_sources=channel_sources,
        highlighter=HighlighterSupervisor(settings),
        live_preview=LivePreviewSupervisor(
            stream_config=config.stream,
            segment_dir=str(Path(config.output.tmp_dir) / "live-preview"),
        ),
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

    refresh_worker = ChannelRefreshWorker(
        channel_catalog,
        channel_sources,
        interval_seconds=int(os.getenv("CHANNEL_REFRESH_SECONDS", str(args.channel_refresh_seconds))),
    )
    refresh_worker.start()

    server = bind_server(context)
    print(f"Football Highlighter UI: http://{context.host}:{context.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        refresh_worker.stop()
        context.live_preview.stop()
        context.highlighter.stop()
        server.server_close()


if __name__ == "__main__":
    main()
