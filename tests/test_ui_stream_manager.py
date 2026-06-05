from pathlib import Path

import pytest

from app.ui.stream_manager import StreamInputError, normalize_stream_input, stream_id_from_url, write_stream_url_env


STREAM_ID = "e38b33c56332de27ff25df223cdf488b1ec6051f"
STREAM_URL = f"http://127.0.0.1:6878/ace/getstream?id={STREAM_ID}"


def test_normalize_stream_input_accepts_bare_id() -> None:
    selection = normalize_stream_input(STREAM_ID)

    assert selection.stream_id == STREAM_ID
    assert selection.stream_url == STREAM_URL


def test_normalize_stream_input_accepts_acestream_link() -> None:
    selection = normalize_stream_input(f"acestream://{STREAM_ID}/")

    assert selection.stream_id == STREAM_ID
    assert selection.stream_url == STREAM_URL


def test_normalize_stream_input_accepts_getstream_url_and_id_query() -> None:
    assert normalize_stream_input(STREAM_URL).stream_id == STREAM_ID
    assert normalize_stream_input(f"id={STREAM_ID}").stream_url == STREAM_URL


def test_normalize_stream_input_rejects_bad_values() -> None:
    for value in ["", "not-an-id", "../escape", "acestream://notvalid"]:
        with pytest.raises(StreamInputError):
            normalize_stream_input(value)


def test_stream_id_from_url_returns_empty_for_missing_or_invalid_id() -> None:
    assert stream_id_from_url(STREAM_URL) == STREAM_ID
    assert stream_id_from_url("http://127.0.0.1:6878/ace/getstream") == ""
    assert stream_id_from_url("http://127.0.0.1:6878/ace/getstream?id=nope") == ""


def test_write_stream_url_env_replaces_only_stream_url(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "FOOTBALL_DATA_API_TOKEN=keep-me\nSTREAM_URL=http://old\nOTHER=value\n",
        encoding="utf-8",
    )

    write_stream_url_env(str(env_path), STREAM_URL)

    content = env_path.read_text(encoding="utf-8")
    assert "FOOTBALL_DATA_API_TOKEN=keep-me" in content
    assert f"STREAM_URL={STREAM_URL}" in content
    assert "OTHER=value" in content
    assert "http://old" not in content
