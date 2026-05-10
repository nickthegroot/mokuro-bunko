"""Shared cached index of the library filesystem tree."""

from __future__ import annotations

import os
import threading
import time
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# Delimiter for flattening nested paths (matches PathMapper.FLATTEN_DELIMITER)
FLATTEN_DELIMITER = "--"


@dataclass(frozen=True)
class VolumeSnapshot:
    """Indexed metadata for a single logical volume stem."""

    name: str
    has_cbz: bool
    has_mokuro: bool
    has_mokuro_gz: bool
    cover: Optional[str]


@dataclass(frozen=True)
class SeriesSnapshot:
    """Indexed metadata for a single series folder.

    The name is a flattened representation of nested paths.
    Example: "Favorites/SeriesA" -> "Favorites--SeriesA"
    """

    name: str
    cover: Optional[str]
    volumes: tuple[VolumeSnapshot, ...]
    # Physical path relative to library root (for internal use)
    _physical_path: str


@dataclass(frozen=True)
class LibrarySnapshot:
    """Immutable snapshot returned by the shared library index."""

    series: tuple[SeriesSnapshot, ...]
    pending_ocr: tuple[tuple[str, str], ...]
    pending_thumbnails: int

    def series_by_name(self, name: str) -> Optional[SeriesSnapshot]:
        """Return a named series snapshot when present."""
        for series in self.series:
            if series.name == name:
                return series
        return None


def cbz_language_is_ja_or_null(cbz_path: Path) -> bool:
    """Return True if ComicInfo.xml LanguageISO is ja, missing, or unreadable."""
    try:
        with zipfile.ZipFile(cbz_path, "r") as zf:
            for name in zf.namelist():
                if name.lower() == "comicinfo.xml":
                    with zf.open(name) as f:
                        tree = ET.parse(f)
                        root = tree.getroot()
                        language = root.find("LanguageISO")
                        return language is None or language.text == "ja"
    except (zipfile.BadZipFile, OSError, ET.ParseError):
        return True
    return True


def _escape_component(component: str) -> str:
    """Escape delimiter in a path component to prevent ambiguity.

    Replaces '--' with '-\x00-' (using null byte which is illegal in filenames)
    to handle edge cases where folder names contain the delimiter.
    """
    return component.replace("--", "-\x00-")


def _unescape_component(component: str) -> str:
    """Unescape a path component."""
    return component.replace("-\x00-", "--")


def flatten_path(parts: tuple[str, ...]) -> str:
    """Convert path components to a flattened name using delimiter.

    Args:
        parts: Tuple of path components (e.g., ('Favorites', 'SeriesA')).

    Returns:
        Flattened name (e.g., 'Favorites--SeriesA').
    """
    if not parts:
        return ""
    escaped = [_escape_component(p) for p in parts]
    return FLATTEN_DELIMITER.join(escaped)


def unflatten_path(flattened: str) -> tuple[str, ...]:
    """Convert a flattened name back to path components.

    Args:
        flattened: Flattened name (e.g., 'Favorites--SeriesA').

    Returns:
        Tuple of path components (e.g., ('Favorites', 'SeriesA')).
    """
    if not flattened:
        return ()
    parts = flattened.split(FLATTEN_DELIMITER)
    return tuple(_unescape_component(p) for p in parts)



class LibraryIndexCache:
    """Time-based cached scanner for `storage/library`."""

    def __init__(self, library_path: Path, ttl: float = 30.0) -> None:
        self.library_path = library_path
        self.ttl = ttl
        self._lock = threading.Lock()
        self._snapshot: Optional[LibrarySnapshot] = None
        self._snapshot_time = 0.0

    def invalidate(self) -> None:
        """Drop current snapshot so next read rescans the filesystem."""
        with self._lock:
            self._snapshot = None
            self._snapshot_time = 0.0

    def get_snapshot(self) -> LibrarySnapshot:
        """Return a recent snapshot, rescanning when stale."""
        now = time.monotonic()
        with self._lock:
            if self._snapshot is not None and (now - self._snapshot_time) < self.ttl:
                return self._snapshot

        snapshot = self._scan_library()
        with self._lock:
            self._snapshot = snapshot
            self._snapshot_time = time.monotonic()
            return snapshot

    def _scan_library(self) -> LibrarySnapshot:
        """Scan the entire library tree once (recursive walk) and build an immutable snapshot."""
        if not self.library_path.is_dir():
            return LibrarySnapshot(series=(), pending_ocr=(), pending_thumbnails=0)

        series_items: list[SeriesSnapshot] = []
        pending_ocr: list[tuple[float, str, str]] = []
        pending_thumbnails = 0

        try:
            library_str = str(self.library_path)
            for dirpath, dirnames, filenames_list in os.walk(library_str):
                # Ignore hidden subdirectories while still traversing non-hidden paths.
                dirnames[:] = [d for d in sorted(dirnames) if not d.startswith(".")]
                filenames = set(filenames_list)
                current_dir = Path(dirpath)
                try:
                    series_name = current_dir.relative_to(self.library_path).as_posix()
                except ValueError:
                    continue

                if series_name.startswith("."):
                    continue

                # Index logical volumes from CBZ files only, so sidecar-only stems
                # left behind by plain filesystem operations do not become phantom volumes.
                volume_names: set[str] = set()

                for file_name in sorted(filenames):
                    lower_name = file_name.lower()
                    if lower_name.endswith(".cbz"):
                        volume_names.add(file_name[:-len(".cbz")])

                volumes: list[VolumeSnapshot] = []
                series_cover: Optional[str] = None

                for volume_name in sorted(volume_names):
                    has_cbz = f"{volume_name}.cbz" in filenames
                    has_mokuro = f"{volume_name}.mokuro" in filenames
                    has_mokuro_gz = f"{volume_name}.mokuro.gz" in filenames
                    has_webp = f"{volume_name}.webp" in filenames
                    cover = f"{series_name}/{volume_name}.webp" if has_webp else None

                    if series_cover is None and cover is not None:
                        series_cover = cover

                    volumes.append(
                        VolumeSnapshot(
                            name=volume_name,
                            has_cbz=has_cbz,
                            has_mokuro=has_mokuro,
                            has_mokuro_gz=has_mokuro_gz,
                            cover=cover,
                        )
                    )

                    if has_cbz and not has_mokuro and not has_mokuro_gz:
                        cbz_path = current_dir / f"{volume_name}.cbz"
                        pending_ocr.append((self._created_timestamp(cbz_path), series_name, volume_name))
                    if has_cbz and f"{volume_name}.webp" not in filenames and f"{volume_name}.nocover" not in filenames:
                        pending_thumbnails += 1

                if volumes:
                    # Flatten the series path for WebDAV compatibility
                    series_parts = tuple(series_name.split("/"))
                    flattened_name = flatten_path(series_parts)
                    series_items.append(
                        SeriesSnapshot(
                            name=flattened_name,
                            cover=series_cover,
                            volumes=tuple(volumes),
                            _physical_path=series_name,
                        )
                    )
        except OSError:
            return LibrarySnapshot(series=(), pending_ocr=(), pending_thumbnails=0)

        pending_ocr.sort(key=lambda item: (item[0], item[1], item[2]))
        return LibrarySnapshot(
            series=tuple(series_items),
            pending_ocr=tuple((series_name, volume_name) for _, series_name, volume_name in pending_ocr),
            pending_thumbnails=pending_thumbnails,
        )

    @staticmethod
    def _created_timestamp(path: Path) -> float:
        """Best-effort creation time for FIFO ordering."""
        try:
            st = path.stat()
        except OSError:
            return 0.0
        birth = getattr(st, "st_birthtime", None)
        if birth is not None:
            return float(birth)
        return float(st.st_mtime)
