from pathlib import Path

import pytest

from app.ui.channel_catalog import ChannelCatalog, ChannelCatalogError, parse_catalog_sources, quality_rank
from app.ui.stream_manager import stream_id_from_url


STREAM_ID = "e38b33c56332de27ff25df223cdf488b1ec6051f"
LOW_ID = "016d48fb89bb9505ab3f883db1bfb3a7c0a3eccc"


def test_channel_catalog_adds_and_sorts_by_quality(tmp_path: Path) -> None:
    catalog = ChannelCatalog(tmp_path / "channels.json")

    catalog.add_channel({"name": "Low", "streamId": LOW_ID, "language": "ru", "quality": "720p"})
    catalog.add_channel({"name": "High", "streamId": STREAM_ID, "language": "en", "quality": "1080p"})

    channels = catalog.list_payload()["channels"]
    assert [channel["name"] for channel in channels] == ["High", "Low"]
    assert stream_id_from_url(channels[0]["streamUrl"]) == STREAM_ID


def test_channel_catalog_rejects_invalid_stream(tmp_path: Path) -> None:
    catalog = ChannelCatalog(tmp_path / "channels.json")

    with pytest.raises(ChannelCatalogError):
        catalog.add_channel({"name": "Bad", "streamId": "not-valid"})


def test_channel_catalog_refresh_imports_json_and_upgrades_quality(tmp_path: Path) -> None:
    catalog = ChannelCatalog(tmp_path / "channels.json")
    catalog.add_channel({"name": "Manual", "streamId": STREAM_ID, "language": "en", "quality": "720p"})
    source = tmp_path / "source.json"
    source.write_text(
        """
        {
          "channels": [
            {
              "name": "Better",
              "stream": "acestream://e38b33c56332de27ff25df223cdf488b1ec6051f/",
              "language": "en",
              "quality": "1080p"
            },
            {
              "name": "Low",
              "streamId": "016d48fb89bb9505ab3f883db1bfb3a7c0a3eccc",
              "language": "ru",
              "quality": "720p"
            }
          ]
        }
        """,
        encoding="utf-8",
    )

    result = catalog.refresh_from_sources([str(source)])
    channels = catalog.list_payload()["channels"]

    assert result["added"] == 1
    assert result["updated"] == 1
    assert channels[0]["name"] == "Better"
    assert len(channels) == 2


def test_channel_catalog_delete(tmp_path: Path) -> None:
    catalog = ChannelCatalog(tmp_path / "channels.json")
    catalog.add_channel({"name": "One", "streamId": STREAM_ID})

    assert catalog.delete_channel(STREAM_ID)["deleted"] is True
    assert catalog.list_payload()["channels"] == []


def test_quality_rank_and_source_parsing() -> None:
    assert quality_rank("4K") > quality_rank("1080p") > quality_rank("720p")
    assert parse_catalog_sources("a.json, b.json\nc.json") == ["a.json", "b.json", "c.json"]
