import json
from pathlib import Path

from app.ui.logs import read_recent_logs


def test_read_recent_logs_handles_missing_file(tmp_path: Path) -> None:
    assert read_recent_logs(str(tmp_path / "missing.log")) == []


def test_read_recent_logs_parses_recent_json_and_skips_bad_lines(tmp_path: Path) -> None:
    log_file = tmp_path / "runtime.log"
    log_file.write_text(
        "\n".join(
            [
                json.dumps({"level": "INFO", "message": "one"}),
                "not json",
                json.dumps({"level": "WARNING", "message": "two"}),
                json.dumps(["not", "dict"]),
                json.dumps({"level": "ERROR", "message": "three"}),
            ]
        ),
        encoding="utf-8",
    )

    records = read_recent_logs(str(log_file), limit=2)

    assert [record["message"] for record in records] == ["three", "two"]
