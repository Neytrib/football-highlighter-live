from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import threading
from time import time
from typing import Any, Iterable
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from app.ui.stream_manager import StreamInputError, normalize_stream_input


class ChannelCatalogError(ValueError):
    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class ChannelRefreshResult:
    added: int
    updated: int
    skipped: int
    sources: int
    message: str


def parse_catalog_sources(value: str | Iterable[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_items = value.replace("\n", ",").split(",")
    else:
        raw_items = list(value)
    return [str(item).strip() for item in raw_items if str(item).strip()]


def quality_rank(value: Any) -> int:
    text = str(value or "").lower()
    known = {
        "8k": 4320,
        "4k": 2160,
        "uhd": 2160,
        "fhd": 1080,
        "hd": 720,
        "sd": 480,
    }
    for label, rank in known.items():
        if label in text:
            return rank
    digits = "".join(char for char in text if char.isdigit())
    if not digits:
        return 0
    try:
        return int(digits[:4])
    except ValueError:
        return 0


class ChannelCatalog:
    def __init__(self, path: str | Path, *, engine_base_url: str = "http://127.0.0.1:6878") -> None:
        self.path = Path(path)
        self.engine_base_url = engine_base_url
        self._lock = threading.Lock()
        self._last_refresh: dict[str, Any] = {
            "at": None,
            "message": "No catalog refresh yet",
            "added": 0,
            "updated": 0,
            "skipped": 0,
            "sources": 0,
        }

    def list_payload(self) -> dict[str, Any]:
        with self._lock:
            channels = self._sorted_channels(self._read_store_unlocked().get("channels", []))
            return {"channels": channels, "refresh": self._last_refresh.copy()}

    def add_channel(self, payload: dict[str, Any]) -> dict[str, Any]:
        channel = self._channel_from_payload(payload, source_default="manual")
        with self._lock:
            store = self._read_store_unlocked()
            channels = store.setdefault("channels", [])
            existing = self._find_channel(channels, channel["streamId"])
            if existing is None:
                channels.append(channel)
            else:
                self._merge_channel(existing, channel, prefer_better_quality=False)
            store["channels"] = self._sorted_channels(channels)
            self._write_store_unlocked(store)
        return channel

    def delete_channel(self, stream_id: str) -> dict[str, Any]:
        stream_id = str(stream_id or "").strip().lower()
        if not stream_id:
            raise ChannelCatalogError("Channel ID is required")
        with self._lock:
            store = self._read_store_unlocked()
            channels = store.get("channels", [])
            next_channels = [channel for channel in channels if channel.get("streamId") != stream_id]
            if len(next_channels) == len(channels):
                raise ChannelCatalogError("Channel not found", status=404)
            store["channels"] = next_channels
            self._write_store_unlocked(store)
        return {"deleted": True, "streamId": stream_id}

    def refresh_from_sources(self, sources: Iterable[str]) -> dict[str, Any]:
        source_list = parse_catalog_sources(sources)
        added = 0
        updated = 0
        skipped = 0
        if not source_list:
            result = ChannelRefreshResult(
                added=0,
                updated=0,
                skipped=0,
                sources=0,
                message="No channel catalog sources configured",
            )
            self._remember_refresh(result)
            return self._last_refresh.copy()

        with self._lock:
            store = self._read_store_unlocked()
            channels = store.setdefault("channels", [])
            for source in source_list:
                try:
                    for item in self._load_source(source):
                        try:
                            incoming = self._channel_from_payload(item, source_default=source)
                        except ChannelCatalogError:
                            skipped += 1
                            continue
                        existing = self._find_channel(channels, incoming["streamId"])
                        if existing is None:
                            channels.append(incoming)
                            added += 1
                        elif self._merge_channel(existing, incoming, prefer_better_quality=True):
                            updated += 1
                        else:
                            skipped += 1
                except ChannelCatalogError:
                    skipped += 1
            store["channels"] = self._sorted_channels(channels)
            self._write_store_unlocked(store)

        result = ChannelRefreshResult(
            added=added,
            updated=updated,
            skipped=skipped,
            sources=len(source_list),
            message=f"Catalog refresh: {added} added, {updated} upgraded, {skipped} skipped",
        )
        self._remember_refresh(result)
        return self._last_refresh.copy()

    def _remember_refresh(self, result: ChannelRefreshResult) -> None:
        self._last_refresh = {
            "at": int(time()),
            "message": result.message,
            "added": result.added,
            "updated": result.updated,
            "skipped": result.skipped,
            "sources": result.sources,
        }

    def _channel_from_payload(self, payload: dict[str, Any], *, source_default: str) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ChannelCatalogError("Channel must be an object")
        raw_stream = payload.get("streamId") or payload.get("stream") or payload.get("url") or payload.get("id")
        try:
            selection = normalize_stream_input(str(raw_stream or ""), engine_base_url=self.engine_base_url)
        except StreamInputError as exc:
            raise ChannelCatalogError(str(exc), status=exc.status) from exc

        language = self._normalize_language(payload.get("language") or payload.get("lang") or "other")
        quality = str(payload.get("quality") or payload.get("resolution") or "").strip()
        now = int(time())
        name = str(payload.get("name") or payload.get("title") or selection.stream_id[:10]).strip()
        if not name:
            raise ChannelCatalogError("Channel name is required")

        return {
            "streamId": selection.stream_id,
            "streamUrl": selection.stream_url,
            "name": name[:120],
            "language": language,
            "quality": quality[:40],
            "qualityRank": quality_rank(quality),
            "source": str(payload.get("source") or source_default or "manual").strip()[:160],
            "createdAt": int(payload.get("createdAt") or now),
            "updatedAt": now,
        }

    @staticmethod
    def _normalize_language(value: Any) -> str:
        text = str(value or "").strip().lower()
        if text in {"ru", "rus", "russian", "рус", "русский"}:
            return "ru"
        if text in {"en", "eng", "english"}:
            return "en"
        return "other"

    @staticmethod
    def _find_channel(channels: list[dict[str, Any]], stream_id: str) -> dict[str, Any] | None:
        for channel in channels:
            if channel.get("streamId") == stream_id:
                return channel
        return None

    @staticmethod
    def _merge_channel(existing: dict[str, Any], incoming: dict[str, Any], *, prefer_better_quality: bool) -> bool:
        if prefer_better_quality and incoming.get("qualityRank", 0) <= existing.get("qualityRank", 0):
            return False
        created_at = existing.get("createdAt") or incoming.get("createdAt")
        existing.update(incoming)
        existing["createdAt"] = created_at
        return True

    @staticmethod
    def _sorted_channels(channels: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(
            channels,
            key=lambda channel: (
                -int(channel.get("qualityRank") or 0),
                str(channel.get("language") or ""),
                str(channel.get("name") or "").lower(),
            ),
        )

    def _load_source(self, source: str) -> list[dict[str, Any]]:
        parsed = urlparse(source)
        try:
            if parsed.scheme in {"http", "https"}:
                request = Request(source, headers={"User-Agent": "FootballHighlighterLocalUI/1.0"})
                with urlopen(request, timeout=6) as response:
                    raw = response.read(1_000_000).decode("utf-8")
            else:
                raw = Path(source).expanduser().read_text(encoding="utf-8")
        except (OSError, URLError) as exc:
            raise ChannelCatalogError(f"Could not read channel catalog source: {source}") from exc

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ChannelCatalogError(f"Channel catalog source is not JSON: {source}") from exc

        if isinstance(payload, dict):
            items = payload.get("channels", [])
        else:
            items = payload
        if not isinstance(items, list):
            raise ChannelCatalogError("Channel catalog JSON must be a list or contain a channels list")
        return [item for item in items if isinstance(item, dict)]

    def _read_store_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"channels": []}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ChannelCatalogError(f"Channel store is invalid JSON: {self.path}") from exc
        if not isinstance(payload, dict):
            raise ChannelCatalogError("Channel store must be a JSON object")
        payload.setdefault("channels", [])
        return payload

    def _write_store_unlocked(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


class ChannelRefreshWorker:
    def __init__(self, catalog: ChannelCatalog, sources: Iterable[str], *, interval_seconds: int = 60) -> None:
        self.catalog = catalog
        self.sources = parse_catalog_sources(sources)
        self.interval_seconds = max(10, int(interval_seconds))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None or not self.sources:
            return
        self._thread = threading.Thread(target=self._run, name="channel-catalog-refresh", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.catalog.refresh_from_sources(self.sources)
            except Exception:
                pass
            self._stop.wait(self.interval_seconds)
