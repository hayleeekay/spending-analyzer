"""Storage directory and profile setup."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import shutil

from .storage import load_json, save_json


FORMAT_VERSION = 1
MARKER_NAME = ".spending-analyzer.json"
PROFILE_FOLDERS = ("imports", "archive", "data", "rules", "reports")
LEGACY_FOLDER_NAMES = {
    "inbox": "imports",
    "processed": "archive",
    "data": "data",
    "rules": "rules",
    "reports": "reports",
}


class LegacyWorkspaceError(ValueError):
    """Raised when an older single-workspace layout needs explicit migration."""


def _visible_entries(path: Path) -> list[Path]:
    if not path.exists():
        return []
    return sorted(entry for entry in path.iterdir() if entry.name != ".DS_Store")


def has_legacy_workspace(storage_dir: Path) -> bool:
    return any((storage_dir / name).exists() for name in LEGACY_FOLDER_NAMES)


def initialize_storage(storage_dir: Path) -> bool:
    """Validate or create an analyzer storage directory.

    Returns True when a new storage directory was initialized.
    """
    marker = storage_dir / MARKER_NAME
    if marker.exists():
        try:
            metadata = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Could not read Spending Analyzer marker: {marker}") from exc
        if not isinstance(metadata, dict) or metadata.get("format_version") != FORMAT_VERSION:
            version = metadata.get("format_version") if isinstance(metadata, dict) else None
            raise ValueError(
                f"Unsupported Spending Analyzer storage format in {marker}: "
                f"{version!r}"
            )
        profiles_dir = storage_dir / "profiles"
        if profiles_dir.is_symlink():
            raise ValueError(f"Profiles folder cannot be a symbolic link: {profiles_dir}")
        profiles_dir.mkdir(exist_ok=True)
        return False

    if has_legacy_workspace(storage_dir):
        raise LegacyWorkspaceError(
            f"An older workspace was found at {storage_dir}. "
            "Convert it explicitly with --migrate-profile NAME."
        )

    entries = _visible_entries(storage_dir)
    if entries:
        names = ", ".join(entry.name for entry in entries[:5])
        raise ValueError(
            f"Refusing to initialize the non-empty directory {storage_dir}. "
            f"It does not look like Spending Analyzer storage (found: {names})."
        )

    storage_dir.mkdir(parents=True, exist_ok=True)
    profiles_dir = storage_dir / "profiles"
    try:
        profiles_dir.mkdir()
        save_json(marker, {"format_version": FORMAT_VERSION})
    except Exception:
        marker.with_suffix(marker.suffix + ".tmp").unlink(missing_ok=True)
        if profiles_dir.exists() and not any(profiles_dir.iterdir()):
            profiles_dir.rmdir()
        raise
    return True


def profile_names(storage_dir: Path) -> list[str]:
    profiles_dir = storage_dir / "profiles"
    if not profiles_dir.exists():
        return []
    return sorted(
        path.name
        for path in profiles_dir.iterdir()
        if path.is_dir() and not path.is_symlink()
    )


def initialize_profile(storage_dir: Path, name: str) -> tuple[Path, bool]:
    profile_dir = storage_dir / "profiles" / name
    if profile_dir.is_symlink():
        raise ValueError(f"Profile folder cannot be a symbolic link: {profile_dir}")
    created = not profile_dir.exists()
    for folder in PROFILE_FOLDERS:
        path = profile_dir / folder
        if path.is_symlink():
            raise ValueError(f"Managed profile folder cannot be a symbolic link: {path}")
        if path.exists() and not path.is_dir():
            raise ValueError(f"Profile path must be a folder: {path}")
    profile_dir.mkdir(parents=True, exist_ok=True)
    for folder in PROFILE_FOLDERS:
        path = profile_dir / folder
        path.mkdir(exist_ok=True)
    return profile_dir, created


def migrate_legacy_workspace(storage_dir: Path, name: str) -> Path:
    """Move an older root workspace into an explicitly named profile."""
    if (storage_dir / MARKER_NAME).exists():
        raise ValueError(f"Storage is already using profiles: {storage_dir}")
    if not has_legacy_workspace(storage_dir):
        raise ValueError(f"No older workspace was found at {storage_dir}")

    allowed = set(LEGACY_FOLDER_NAMES) | {".DS_Store"}
    unexpected = [entry.name for entry in storage_dir.iterdir() if entry.name not in allowed]
    if unexpected:
        raise ValueError(
            "Legacy conversion stopped because the storage directory also contains: "
            + ", ".join(sorted(unexpected))
        )
    linked = [name for name in LEGACY_FOLDER_NAMES if (storage_dir / name).is_symlink()]
    if linked:
        raise ValueError(
            "Legacy conversion cannot move symbolic links: " + ", ".join(linked)
        )

    profiles_dir = storage_dir / "profiles"
    profile_dir = profiles_dir / name
    if profile_dir.exists() or profile_dir.is_symlink():
        raise ValueError(f"Migration destination already exists: {profile_dir}")

    profiles_dir.mkdir()
    profile_dir.mkdir()
    moved: list[tuple[Path, Path]] = []
    created_folders: list[Path] = []
    original_imports: list[dict] | None = None
    imports_path = profile_dir / "data" / "imports.json"
    try:
        for old_name, new_name in LEGACY_FOLDER_NAMES.items():
            source = storage_dir / old_name
            if source.exists():
                destination = profile_dir / new_name
                shutil.move(str(source), destination)
                moved.append((source, destination))
        for folder in PROFILE_FOLDERS:
            path = profile_dir / folder
            if not path.exists():
                path.mkdir()
                created_folders.append(path)
        if imports_path.exists():
            original_imports = load_json(imports_path, [])
            if not isinstance(original_imports, list) or not all(
                isinstance(record, dict) for record in original_imports
            ):
                raise ValueError(f"Import history must be a JSON list: {imports_path}")
            updated_imports = deepcopy(original_imports)
            for record in updated_imports:
                archived_path = record.get("archived_path")
                if isinstance(archived_path, str) and (
                    archived_path.startswith("processed/")
                    or archived_path.startswith("processed\\")
                ):
                    record["archived_path"] = "archive" + archived_path[len("processed"):]
            if updated_imports != original_imports:
                save_json(imports_path, updated_imports)
        save_json(storage_dir / MARKER_NAME, {"format_version": FORMAT_VERSION})
    except BaseException:
        (storage_dir / f"{MARKER_NAME}.tmp").unlink(missing_ok=True)
        try:
            if original_imports is not None and imports_path.exists():
                save_json(imports_path, original_imports)
        finally:
            for source, destination in reversed(moved):
                if destination.exists() and not source.exists():
                    shutil.move(str(destination), source)
            for path in reversed(created_folders):
                if path.exists() and not any(path.iterdir()):
                    path.rmdir()
            if profile_dir.exists() and not any(profile_dir.iterdir()):
                profile_dir.rmdir()
            if profiles_dir.exists() and not any(profiles_dir.iterdir()):
                profiles_dir.rmdir()
        raise
    return profile_dir
