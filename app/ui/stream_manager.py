from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from urllib.parse import parse_qs, urlparse


STREAM_ID_RE = re.compile(r"^[A-Fa-f0-9]{32,64}$")


class StreamInputError(ValueError):
    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class StreamSelection:
    stream_id: str
    stream_url: str


def stream_id_from_url(stream_url: str) -> str:
    parsed = urlparse(stream_url or "")
    query = parse_qs(parsed.query)
    stream_id = query.get("id", [""])[0].strip()
    return stream_id if STREAM_ID_RE.match(stream_id) else ""


def normalize_stream_input(value: str, *, engine_base_url: str = "http://127.0.0.1:6878") -> StreamSelection:
    raw = (value or "").strip()
    if not raw:
        raise StreamInputError("Stream ID is required")

    stream_id = raw
    parsed = urlparse(raw)
    if parsed.scheme == "acestream":
        stream_id = (parsed.netloc or parsed.path).strip("/")
    elif parsed.scheme in {"http", "https"}:
        stream_id = parse_qs(parsed.query).get("id", [""])[0].strip()
    elif raw.startswith("id="):
        stream_id = raw.split("=", 1)[1].split("&", 1)[0].strip()
    elif "id=" in raw:
        stream_id = parse_qs(urlparse(raw).query).get("id", [""])[0].strip()

    if not STREAM_ID_RE.match(stream_id):
        raise StreamInputError("Stream ID must be a 32-64 character hex AceStream ID")

    return StreamSelection(
        stream_id=stream_id,
        stream_url=f"{engine_base_url.rstrip('/')}/ace/getstream?id={stream_id}",
    )


def write_stream_url_env(env_path: str, stream_url: str) -> None:
    path = Path(env_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    next_lines: list[str] = []
    replaced = False
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("STREAM_URL="):
            next_lines.append(f"STREAM_URL={stream_url}")
            replaced = True
        else:
            next_lines.append(line)
    if not replaced:
        if next_lines and next_lines[-1] != "":
            next_lines.append("")
        next_lines.append(f"STREAM_URL={stream_url}")
    path.write_text("\n".join(next_lines) + "\n", encoding="utf-8")
