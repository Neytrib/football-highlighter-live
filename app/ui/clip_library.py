from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import shutil
from typing import Any, Iterable
from urllib.parse import quote

from app.config import AppConfig


MEDIA_EXTENSIONS = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".ts"}
SAFE_CATEGORY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}$")


class ClipLibraryError(ValueError):
    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class ClipRoot:
    key: str
    label: str
    path: Path
    custom: bool = False


class ClipLibrary:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.builtin_roots = {
            "raw": ClipRoot("raw", "Uncropped", Path(config.output.raw_dir).resolve()),
            "cropped": ClipRoot("cropped", "Cropped", Path(config.output.cropped_dir).resolve()),
            "uncertain": ClipRoot("uncertain", "Uncertain", Path(config.output.uncertain_dir).resolve()),
            "var": ClipRoot("var", "VAR", Path(config.output.var_dir).resolve()),
        }
        self.custom_root = Path(config.output.custom_categories_dir).resolve()
        self._ensure_roots()

    def _ensure_roots(self) -> None:
        for root in self.builtin_roots.values():
            root.path.mkdir(parents=True, exist_ok=True)
        self.custom_root.mkdir(parents=True, exist_ok=True)

    def categories(self) -> list[dict[str, Any]]:
        builtin = [
            {"key": root.key, "label": root.label, "custom": False, "count": self._count_media(root.path)}
            for root in self.builtin_roots.values()
        ]
        custom = [
            {
                "key": f"custom:{path.name}",
                "label": path.name,
                "custom": True,
                "count": self._count_media(path),
            }
            for path in sorted(self.custom_root.iterdir(), key=lambda item: item.name.lower())
            if path.is_dir()
        ]
        return builtin + custom

    def list_clips(self) -> dict[str, Any]:
        clips: list[dict[str, Any]] = []
        for root in self.builtin_roots.values():
            clips.extend(self._clips_in_root(root))
        for category_dir in self._custom_category_dirs():
            root = ClipRoot(f"custom:{category_dir.name}", category_dir.name, category_dir, custom=True)
            clips.extend(self._clips_in_root(root))
        clips.sort(key=lambda item: item["mtime"], reverse=True)
        return {"categories": self.categories(), "clips": clips}

    def create_category(self, name: str) -> dict[str, Any]:
        category = self._safe_category_name(name)
        path = (self.custom_root / category).resolve()
        self._ensure_inside(path, self.custom_root)
        if path.exists() and not path.is_dir():
            raise ClipLibraryError("A file already uses that category name", status=409)
        path.mkdir(parents=True, exist_ok=True)
        return {"key": f"custom:{category}", "label": category, "custom": True, "count": self._count_media(path)}

    def rename_clip(self, root_key: str, relative_path: str, new_name: str) -> dict[str, Any]:
        source = self.resolve_clip(root_key, relative_path)
        safe_name = self._safe_file_name(new_name)
        if Path(safe_name).suffix == "":
            safe_name = f"{safe_name}{source.suffix}"
        target = (source.parent / safe_name).resolve()
        self._ensure_inside(target, self._root_for_key(root_key).path)
        if target.exists():
            raise ClipLibraryError("A clip already uses that name", status=409)
        source.rename(target)
        return self._clip_payload(root_key, target, self._root_for_key(root_key))

    def delete_clip(self, root_key: str, relative_path: str) -> dict[str, Any]:
        source = self.resolve_clip(root_key, relative_path)
        source.unlink()
        return {"deleted": True, "root": root_key, "path": relative_path}

    def move_clip(self, root_key: str, relative_path: str, category: str) -> dict[str, Any]:
        source = self.resolve_clip(root_key, relative_path)
        category_name = self._safe_category_name(category)
        target_dir = (self.custom_root / category_name).resolve()
        self._ensure_inside(target_dir, self.custom_root)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = (target_dir / source.name).resolve()
        self._ensure_inside(target, target_dir)
        if target.exists():
            raise ClipLibraryError("A clip already exists in that category", status=409)
        shutil.move(str(source), str(target))
        return self._clip_payload(f"custom:{category_name}", target, ClipRoot(f"custom:{category_name}", category_name, target_dir, True))

    def resolve_clip(self, root_key: str, relative_path: str) -> Path:
        root = self._root_for_key(root_key)
        if not relative_path or Path(relative_path).is_absolute():
            raise ClipLibraryError("Clip path must be relative")
        path = (root.path / relative_path).resolve()
        self._ensure_inside(path, root.path)
        if not path.exists() or not path.is_file():
            raise ClipLibraryError("Clip not found", status=404)
        if path.suffix.lower() not in MEDIA_EXTENSIONS:
            raise ClipLibraryError("Unsupported media file", status=415)
        return path

    def resolve_media(self, root_key: str, relative_path: str) -> Path:
        return self.resolve_clip(root_key, relative_path)

    def _root_for_key(self, root_key: str) -> ClipRoot:
        if root_key in self.builtin_roots:
            return self.builtin_roots[root_key]
        if root_key.startswith("custom:"):
            category = self._safe_category_name(root_key.split(":", 1)[1])
            path = (self.custom_root / category).resolve()
            self._ensure_inside(path, self.custom_root)
            if not path.exists() or not path.is_dir():
                raise ClipLibraryError("Category not found", status=404)
            return ClipRoot(root_key, category, path, custom=True)
        raise ClipLibraryError("Unknown clip root", status=404)

    def _clips_in_root(self, root: ClipRoot) -> list[dict[str, Any]]:
        if not root.path.exists():
            return []
        return [
            self._clip_payload(root.key, path, root)
            for path in self._iter_media(root.path)
        ]

    def _clip_payload(self, root_key: str, path: Path, root: ClipRoot) -> dict[str, Any]:
        rel = path.resolve().relative_to(root.path).as_posix()
        stat = path.stat()
        return {
            "id": f"{root_key}:{rel}",
            "root": root_key,
            "category": root.label,
            "custom": root.custom,
            "name": path.name,
            "path": rel,
            "extension": path.suffix.lower(),
            "size": stat.st_size,
            "mtime": stat.st_mtime,
            "mediaUrl": f"/media?root={quote(root_key)}&path={quote(rel)}",
        }

    def _custom_category_dirs(self) -> Iterable[Path]:
        if not self.custom_root.exists():
            return []
        return [
            path
            for path in sorted(self.custom_root.iterdir(), key=lambda item: item.name.lower())
            if path.is_dir()
        ]

    def _iter_media(self, root: Path) -> Iterable[Path]:
        return sorted(
            (path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )

    def _count_media(self, root: Path) -> int:
        if not root.exists():
            return 0
        return sum(1 for _ in self._iter_media(root))

    @staticmethod
    def _ensure_inside(path: Path, root: Path) -> None:
        try:
            path.resolve().relative_to(root.resolve())
        except ValueError as exc:
            raise ClipLibraryError("Path escapes the clip library") from exc

    @staticmethod
    def _safe_file_name(name: str) -> str:
        value = (name or "").strip()
        if not value or value in {".", ".."}:
            raise ClipLibraryError("File name is required")
        if "/" in value or "\\" in value:
            raise ClipLibraryError("File name cannot include path separators")
        if Path(value).name != value or ".." in Path(value).parts:
            raise ClipLibraryError("Unsafe file name")
        return value

    @staticmethod
    def _safe_category_name(name: str) -> str:
        value = (name or "").strip()
        if not value or value in {".", ".."}:
            raise ClipLibraryError("Category name is required")
        if "/" in value or "\\" in value:
            raise ClipLibraryError("Category cannot include path separators")
        if not SAFE_CATEGORY_RE.match(value):
            raise ClipLibraryError("Category can use letters, numbers, spaces, dots, underscores, and hyphens")
        return value
