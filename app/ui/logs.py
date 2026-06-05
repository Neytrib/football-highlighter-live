from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def read_recent_logs(log_file: str, *, limit: int = 80) -> list[dict[str, Any]]:
    path = Path(log_file)
    if not path.exists() or not path.is_file():
        return []

    safe_limit = max(1, min(int(limit), 500))
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()
    for line in lines[-safe_limit * 3:]:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return list(reversed(records[-safe_limit:]))
