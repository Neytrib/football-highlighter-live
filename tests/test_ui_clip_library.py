from pathlib import Path

import pytest

from app.config import AppConfig
from app.ui.clip_library import ClipLibrary, ClipLibraryError


def _config(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.output.raw_dir = str(tmp_path / "clips_raw")
    cfg.output.cropped_dir = str(tmp_path / "clips_cropped")
    cfg.output.uncertain_dir = str(tmp_path / "clips_uncertain")
    cfg.output.var_dir = str(tmp_path / "clips_var")
    cfg.output.custom_categories_dir = str(tmp_path / "clips_categories")
    cfg.output.goals_dir = str(tmp_path / "goals")
    cfg.output.state_dir = str(tmp_path / "state")
    cfg.output.tmp_dir = str(tmp_path / "tmp")
    cfg.score_ocr.temp_dir = str(tmp_path / "score_ocr")
    cfg.ensure_output_dirs()
    return cfg


def _write(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"video")
    return path


def test_lists_builtin_and_custom_clips(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _write(Path(cfg.output.raw_dir) / "raw.mp4")
    _write(Path(cfg.output.cropped_dir) / "crop.mp4")
    _write(Path(cfg.output.uncertain_dir) / "maybe.mp4")
    _write(Path(cfg.output.var_dir) / "var.mp4")
    _write(Path(cfg.output.custom_categories_dir) / "Best" / "best.mp4")
    _write(Path(cfg.output.raw_dir) / "notes.txt")

    payload = ClipLibrary(cfg).list_clips()

    roots = {clip["root"] for clip in payload["clips"]}
    assert roots == {"raw", "cropped", "uncertain", "var", "custom:Best"}
    assert all(clip["extension"] == ".mp4" for clip in payload["clips"])


def test_create_category_rejects_path_traversal(tmp_path: Path) -> None:
    library = ClipLibrary(_config(tmp_path))

    with pytest.raises(ClipLibraryError):
        library.create_category("../escape")
    with pytest.raises(ClipLibraryError):
        library.create_category("/tmp/escape")
    with pytest.raises(ClipLibraryError):
        library.create_category("nested/category")


def test_rename_clip_stays_inside_root(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    source = _write(Path(cfg.output.raw_dir) / "goal.mp4")
    library = ClipLibrary(cfg)

    renamed = library.rename_clip("raw", "goal.mp4", "better-goal")

    assert not source.exists()
    assert (Path(cfg.output.raw_dir) / "better-goal.mp4").exists()
    assert renamed["name"] == "better-goal.mp4"

    with pytest.raises(ClipLibraryError):
        library.rename_clip("raw", "better-goal.mp4", "../outside.mp4")


def test_delete_clip_rejects_external_paths(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    source = _write(Path(cfg.output.raw_dir) / "goal.mp4")
    outside = _write(tmp_path / "outside.mp4")
    library = ClipLibrary(cfg)

    with pytest.raises(ClipLibraryError):
        library.delete_clip("raw", "../outside.mp4")

    assert outside.exists()
    library.delete_clip("raw", "goal.mp4")
    assert not source.exists()


def test_move_clip_to_custom_category_and_conflict(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _write(Path(cfg.output.raw_dir) / "goal.mp4")
    _write(Path(cfg.output.custom_categories_dir) / "Favorites" / "existing.mp4")
    library = ClipLibrary(cfg)

    moved = library.move_clip("raw", "goal.mp4", "Favorites")

    assert moved["root"] == "custom:Favorites"
    assert (Path(cfg.output.custom_categories_dir) / "Favorites" / "goal.mp4").exists()

    _write(Path(cfg.output.raw_dir) / "existing.mp4")
    with pytest.raises(ClipLibraryError):
        library.move_clip("raw", "existing.mp4", "Favorites")


def test_resolve_media_rejects_unknown_root_and_non_media(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _write(Path(cfg.output.raw_dir) / "notes.txt")
    library = ClipLibrary(cfg)

    with pytest.raises(ClipLibraryError):
        library.resolve_media("missing", "clip.mp4")
    with pytest.raises(ClipLibraryError):
        library.resolve_media("raw", "notes.txt")
