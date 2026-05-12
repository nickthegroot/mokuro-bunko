"""WebDAV resources for mokuro-bunko.

Compatible with mokuro-reader's expected WebDAV structure.
The reader creates a /mokuro-reader/ folder on the server and stores:
  - volume-data.json and profiles.json (per-user progress/settings)
  - {SeriesTitle}/{Volume}.cbz (manga files, shared across users)

This module maps those virtual paths to a physical layout where manga
files are shared and per-user data is isolated.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, Callable, Optional

from wsgidav.dav_provider import DAVCollection, DAVError, DAVNonCollection
from wsgidav.util import join_uri

from mokuro_bunko.security import safe_resolve_under

if TYPE_CHECKING:
    from mokuro_bunko.database import Database
    from wsgidav.dav_provider import DAVProvider


class PathMapper:
    """Maps virtual WebDAV paths to physical filesystem paths.

    Virtual structure (compatible with mokuro-reader):
        /                                - Root (virtual)
        /mokuro-reader/                  - Reader root (virtual, merged view)
        /mokuro-reader/volume-data.json  - Per-user progress data
        /mokuro-reader/profiles.json     - Per-user profile settings
        /mokuro-reader/oneshots/         - Oneshots directory (optional)
        /mokuro-reader/{series}/         - Shared series folder
        /mokuro-reader/{series}/{file}   - Shared manga files (CBZ etc.)

    Physical structure:
        {storage_base}/library/          - Shared manga library (supports nesting)
        {storage_base}/library/{oneshots}/ - Oneshots directory (optional)
        {storage_base}/inbox/            - OCR upload queue
        {storage_base}/users/{username}/ - Per-user data

    Path flattening:
        Nested physical paths are flattened in virtual paths using '--' delimiter.
        Example: library/Favorites/SeriesA/ -> /mokuro-reader/Favorites--SeriesA/
        This avoids %2F encoding issues since WSGI decodes URLs before routing.
    """

    READER_ROOT = "mokuro-reader"
    ONESHOTS_PREFIX = "oneshots"
    PER_USER_FILES = frozenset({"volume-data.json", "profiles.json"})
    FLATTEN_DELIMITER = "--"

    def __init__(
        self,
        library_path: Path,
        oneshots_path: Optional[Path] = None,
        inbox_path: Optional[Path] = None,
        users_path: Optional[Path] = None,
    ) -> None:
        """Initialize path mapper.

        Args:
            library_path: Path to the library (should be resolved).
            oneshots_path: Optional path to the oneshots directory.
            inbox_path: Path to the inbox directory.
            users_path: Path to the users directory.
        """
        self.library_path = library_path
        self.oneshots_path = oneshots_path
        self.storage_base = library_path.parent
        self.inbox_path = inbox_path or (self.storage_base / "inbox")
        self.users_path = users_path or (self.storage_base / "users")

    def ensure_directories(self) -> None:
        """Create storage directories if they don't exist."""
        self.library_path.mkdir(parents=True, exist_ok=True)
        self.inbox_path.mkdir(parents=True, exist_ok=True)
        self.users_path.mkdir(parents=True, exist_ok=True)

    def ensure_user_directory(self, username: str) -> Path:
        """Ensure user directory exists and return its path."""
        user_dir = (self.users_path / username).resolve()
        if not user_dir.is_relative_to(self.users_path.resolve()):
            raise ValueError("Invalid username path")
        user_dir.mkdir(parents=True, exist_ok=True)
        return user_dir

    @staticmethod
    def _escape_component(component: str) -> str:
        """Escape delimiter in a path component to prevent ambiguity.

        Replaces '--' with '-\x00-' (using null byte which is illegal in filenames)
        to handle edge cases where folder names contain the delimiter.
        """
        return component.replace("--", "-\x00-")

    @staticmethod
    def _unescape_component(component: str) -> str:
        """Unescape a path component."""
        return component.replace("-\x00-", "--")

    def flatten_path(self, relative_parts: tuple[str, ...]) -> str:
        """Convert physical path components to a flattened virtual name.

        Args:
            relative_parts: Tuple of path components (e.g., ('Favorites', 'SeriesA')).

        Returns:
            Flattened name using delimiter (e.g., 'Favorites--SeriesA').
        """
        if not relative_parts:
            return ""
        escaped = [self._escape_component(p) for p in relative_parts]
        return self.FLATTEN_DELIMITER.join(escaped)

    def unflatten_path(self, flattened: str) -> tuple[str, ...]:
        """Convert a flattened virtual name back to path components.

        Args:
            flattened: Flattened name (e.g., 'Favorites--SeriesA').

        Returns:
            Tuple of path components (e.g., ('Favorites', 'SeriesA')).
        """
        if not flattened:
            return ()
        parts = flattened.split(self.FLATTEN_DELIMITER)
        return tuple(self._unescape_component(p) for p in parts)

    def is_flattened_name(self, name: str) -> bool:
        """Check if a name contains the flatten delimiter."""
        return self.FLATTEN_DELIMITER in name

    def get_user_file_path(self, username: str, filename: str) -> Optional[Path]:
        """Safely resolve a per-user file path under users/{username}/."""
        user_dir = (self.users_path / username).resolve()
        users_root = self.users_path.resolve()
        if not user_dir.is_relative_to(users_root):
            return None
        return user_dir / filename

    def is_per_user_file(self, virtual_path: str) -> bool:
        """Check if path is a per-user file (volume-data.json or profiles.json).

        These files live directly under /mokuro-reader/ and are mapped
        to each user's private directory.
        """
        virtual_path = "/" + virtual_path.strip("/")
        prefix = f"/{self.READER_ROOT}/"
        if virtual_path.startswith(prefix):
            relative = virtual_path[len(prefix):]
            return relative in self.PER_USER_FILES
        return False

    def is_reader_path(self, virtual_path: str) -> bool:
        """Check if path is under /mokuro-reader/."""
        virtual_path = "/" + virtual_path.strip("/")
        return (
            virtual_path == f"/{self.READER_ROOT}"
            or virtual_path.startswith(f"/{self.READER_ROOT}/")
        )

    def is_inbox_path(self, virtual_path: str) -> bool:
        """Check if path is under /inbox/."""
        virtual_path = "/" + virtual_path.strip("/")
        return virtual_path == "/inbox" or virtual_path.startswith("/inbox/")

    def virtual_to_physical(
        self,
        virtual_path: str,
        username: Optional[str] = None,
    ) -> Optional[Path]:
        """Convert virtual WebDAV path to physical filesystem path.

        Args:
            virtual_path: Virtual path from WebDAV request.
            username: Current user's username (for per-user file mapping).

        Returns:
            Physical filesystem path, or None if path is virtual-only.
        """
        virtual_path = "/" + virtual_path.strip("/")

        # Root and reader root are virtual
        if virtual_path == "/":
            return None
        if virtual_path == f"/{self.READER_ROOT}":
            return None

        # /mokuro-reader/* paths
        if virtual_path.startswith(f"/{self.READER_ROOT}/"):
            relative = virtual_path[len(f"/{self.READER_ROOT}/"):]

            # Per-user files map to user's private directory
            if relative in self.PER_USER_FILES:
                if username:
                    return self.get_user_file_path(username, relative)
                return None

            # Oneshots path
            if self.oneshots_path is not None and relative.startswith(f"{self.ONESHOTS_PREFIX}/"):
                oneshots_relative = relative[len(f"{self.ONESHOTS_PREFIX}/"):]
                # Unflatten nested paths in oneshots
                parts = oneshots_relative.split("/")
                physical_parts: list[str] = []
                for part in parts:
                    if self.FLATTEN_DELIMITER in part:
                        unflattened = self.unflatten_path(part)
                        physical_parts.extend(unflattened)
                    else:
                        physical_parts.append(part)
                physical_relative = "/".join(physical_parts)
                return safe_resolve_under(self.oneshots_path, physical_relative)

            # Check if this is a nested path (contains the delimiter)
            # Split by '/' first to get path components, then unflatten each
            parts = relative.split("/")
            physical_parts = []
            for part in parts:
                if self.FLATTEN_DELIMITER in part:
                    # This is a flattened nested path - unflatten it
                    unflattened = self.unflatten_path(part)
                    physical_parts.extend(unflattened)
                else:
                    physical_parts.append(part)

            physical_relative = "/".join(physical_parts)
            return safe_resolve_under(self.library_path, physical_relative)

        # /inbox paths
        if virtual_path == "/inbox" or virtual_path.startswith("/inbox/"):
            relative = virtual_path[6:].lstrip("/")  # Remove "/inbox"
            if relative:
                return safe_resolve_under(self.inbox_path, relative)
            return self.inbox_path.resolve()

        return None

    def physical_to_virtual(
        self,
        physical_path: Path,
        username: Optional[str] = None,
    ) -> Optional[str]:
        """Convert physical filesystem path to virtual WebDAV path.

        Args:
            physical_path: Physical filesystem path.
            username: Current user's username.

        Returns:
            Virtual WebDAV path, or None if not mappable.
        """
        physical_path = Path(physical_path).resolve()
        storage_base = self.storage_base.resolve()

        try:
            relative = physical_path.relative_to(storage_base)
        except ValueError:
            return None

        parts = relative.parts
        if not parts:
            return "/"

        # library/* -> /mokuro-reader/* (with flattening for nested paths)
        if parts[0] == "library":
            if len(parts) > 1:
                # Check if this is under the oneshots directory
                if (
                    self.oneshots_path is not None
                    and len(parts) >= 2
                    and parts[1] == self.oneshots_path.name
                ):
                    # This is a oneshots file/directory
                    oneshots_parts = parts[2:]  # Everything after library/{oneshots}/
                    if not oneshots_parts:
                        return f"/{self.READER_ROOT}/{self.ONESHOTS_PREFIX}"

                    # Flatten nested paths (e.g., foo/bar/baz.cbz -> foo--baz.cbz)
                    filename = oneshots_parts[-1]
                    dir_parts = oneshots_parts[:-1]
                    if dir_parts:
                        flattened_dirs = self.flatten_path(dir_parts)
                        return f"/{self.READER_ROOT}/{self.ONESHOTS_PREFIX}/{flattened_dirs}/{filename}"
                    else:
                        return f"/{self.READER_ROOT}/{self.ONESHOTS_PREFIX}/{filename}"

                # Flatten nested directory paths
                # For files: library/Favorites/SeriesA/vol1.cbz -> /mokuro-reader/Favorites--SeriesA/vol1.cbz
                # For folders: library/Favorites/SeriesA -> /mokuro-reader/Favorites--SeriesA
                all_parts = parts[1:]  # Everything after 'library/'

                # Check if this is a directory (folder) path
                physical_check = self.library_path.joinpath(*all_parts)
                if physical_check.is_dir():
                    # It's a directory - flatten all parts
                    flattened = self.flatten_path(all_parts)
                    return f"/{self.READER_ROOT}/{flattened}"
                else:
                    # It's a file or doesn't exist - keep filename separate
                    filename = all_parts[-1]  # Last component is the file/folder name
                    dir_parts = all_parts[:-1]  # Directory components to flatten

                    if dir_parts:
                        flattened_dirs = self.flatten_path(dir_parts)
                        return f"/{self.READER_ROOT}/{flattened_dirs}/{filename}"
                    else:
                        return f"/{self.READER_ROOT}/{filename}"
            return f"/{self.READER_ROOT}"

        # inbox/* -> /inbox/*
        if parts[0] == "inbox":
            return "/" + "/".join(parts)

        # users/{username}/{per-user-file} -> /mokuro-reader/{per-user-file}
        if parts[0] == "users" and len(parts) >= 3:
            filename = parts[2]
            if filename in self.PER_USER_FILES:
                return f"/{self.READER_ROOT}/{filename}"

        return None

    def get_path_type(self, virtual_path: str) -> str:
        """Determine the type of a virtual path.

        Returns:
            One of: "root", "reader_root", "progress", "library", "inbox", "unknown"
        """
        virtual_path = "/" + virtual_path.strip("/")

        if virtual_path == "/":
            return "root"

        if virtual_path == f"/{self.READER_ROOT}":
            return "reader_root"

        if virtual_path.startswith(f"/{self.READER_ROOT}/"):
            relative = virtual_path[len(f"/{self.READER_ROOT}/"):]
            if relative in self.PER_USER_FILES:
                return "progress"
            return "library"

        if virtual_path == "/inbox" or virtual_path.startswith("/inbox/"):
            return "inbox"

        return "unknown"


class MokuroFileResource(DAVNonCollection):
    """WebDAV resource for files."""

    _VOLUME_SIDECAR_SUFFIXES = (".mokuro", ".mokuro.gz", ".webp", ".nocover")

    # Static property list — avoids calling getters to probe existence (8 calls
    # × 33k resources = 264k method calls saved on a Depth:infinity PROPFIND).
    _PROP_NAMES = [
        "{DAV:}resourcetype",
        "{DAV:}creationdate",
        "{DAV:}getcontentlength",
        "{DAV:}getcontenttype",
        "{DAV:}getlastmodified",
        "{DAV:}displayname",
        "{DAV:}getetag",
    ]

    def __init__(
        self,
        path: str,
        environ: dict[str, Any],
        file_path: Path,
    ) -> None:
        super().__init__(path, environ)
        self.file_path = file_path
        self._stat: Optional[os.stat_result] = None

    def _get_database(self) -> Optional["Database"]:
        db = self.environ.get("mokuro.db")
        if db is None:
            return None
        return db  # type: ignore[return-value]

    def _get_actor_username(self) -> Optional[str]:
        user_data = self.environ.get("mokuro.user")
        if isinstance(user_data, dict):
            username = user_data.get("username")
            if isinstance(username, str):
                return username
        username = self.environ.get("mokuro.username")
        if isinstance(username, str):
            return username
        return None

    def _relative_under_library(self) -> Optional[str]:
        provider = self.provider
        if not hasattr(provider, "path_mapper"):
            return None
        mapper: PathMapper = provider.path_mapper
        try:
            return str(self.file_path.resolve().relative_to(mapper.library_path.resolve()))
        except ValueError:
            return None

    def _get_mapper(self) -> Optional[PathMapper]:
        provider = self.provider
        if not hasattr(provider, "path_mapper"):
            return None
        return provider.path_mapper  # type: ignore[return-value]

    def _resolve_destination_path(self, dest_path: str) -> Optional[Path]:
        mapper = self._get_mapper()
        if mapper is None:
            return None
        source_type = mapper.get_path_type(self.path)
        dest_type = mapper.get_path_type(dest_path)
        if source_type not in {"library", "progress"} or dest_type != source_type:
            return None
        return mapper.virtual_to_physical(dest_path, self._get_actor_username())

    def _audit(self, action: str, *, details: Optional[dict[str, Any]] = None) -> None:
        db = self._get_database()
        if db is None:
            return
        rel = self._relative_under_library()
        if rel is not None:
            target_path = f"/{PathMapper.READER_ROOT}/{rel}"
            target_type = "library"
        else:
            target_path = self.path
            target_type = "progress" if self.path.split("/")[-1] in PathMapper.PER_USER_FILES else "webdav"
        db.log_audit_event(
            action=action,
            actor_username=self._get_actor_username(),
            target_type=target_type,
            target_path=target_path,
            details=details,
        )

    def _on_write_committed(self, existed_before: bool) -> None:
        db = self._get_database()
        actor = self._get_actor_username()
        rel = self._relative_under_library()
        if db is not None and rel is not None and actor:
            db.record_volume_upload(rel, actor, existed_before=existed_before)
        self._audit(
            "edit" if existed_before else "upload",
            details={"existed_before": existed_before},
        )

    def get_property_names(self, *, is_allprop: bool) -> list[str]:
        """Return static property list (no getter probing needed)."""
        return list(self._PROP_NAMES)

    def _get_stat(self) -> Optional[os.stat_result]:
        """Get cached stat result."""
        if self._stat is None:
            try:
                self._stat = os.stat(self.file_path)
            except OSError:
                pass
        return self._stat

    def get_content_length(self) -> Optional[int]:
        """Return file size."""
        stat_result = self._get_stat()
        if stat_result:
            return stat_result.st_size
        return None

    def get_content_type(self) -> Optional[str]:
        """Return content type based on extension."""
        suffix = self.file_path.suffix.lower()
        content_types = {
            ".cbz": "application/vnd.comicbook+zip",
            ".cbr": "application/vnd.comicbook-rar",
            ".zip": "application/zip",
            ".gz": "application/gzip",
            ".json": "application/json",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".gif": "image/gif",
            ".webp": "image/webp",
        }
        # Handle compound extensions
        name_lower = self.file_path.name.lower()
        if name_lower.endswith(".json.gz"):
            return "application/gzip"
        if name_lower.endswith(".mokuro.gz"):
            return "application/gzip"
        return content_types.get(suffix, "application/octet-stream")

    def get_creation_date(self) -> Optional[float]:
        """Return creation time."""
        stat_result = self._get_stat()
        if stat_result:
            return stat_result.st_ctime
        return None

    def get_display_name(self) -> str:
        """Return display name."""
        return self.file_path.name

    def get_etag(self) -> Optional[str]:
        """Return ETag based on mtime and size."""
        stat_result = self._get_stat()
        if stat_result:
            return f"{stat_result.st_mtime:.6f}-{stat_result.st_size}"
        return None

    def get_last_modified(self) -> Optional[float]:
        """Return last modified time."""
        stat_result = self._get_stat()
        if stat_result:
            return stat_result.st_mtime
        return None

    def support_etag(self) -> bool:
        return True

    def support_ranges(self) -> bool:
        return True

    def get_content(self) -> BinaryIO:
        """Return file content as file object."""
        try:
            return open(self.file_path, "rb")
        except OSError as e:
            raise DAVError(500, f"Cannot read file: {e}") from e

    def begin_write(self, content_type: Optional[str] = None) -> BinaryIO:
        """Begin writing to file, return file object."""
        existed_before = self.file_path.exists()
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        if self.file_path.suffix.lower() == ".cbz":
            writer: BinaryIO = _ValidatedCbzWriter(self.file_path)
        else:
            writer = open(self.file_path, "wb")
        return _AuditedWriter(
            writer,
            on_commit=lambda: self._on_write_committed(existed_before),
        )

    def delete(self) -> None:
        """Delete the file."""
        if not self.file_path.exists():
            return

        rel = self._relative_under_library()
        lower = self.file_path.name.lower()
        if lower.endswith(".cbz"):
            base = self.file_path.with_suffix("")
            for suffix in self._VOLUME_SIDECAR_SUFFIXES:
                sidecar = Path(f"{base}{suffix}")
                try:
                    sidecar.unlink(missing_ok=True)
                except OSError:
                    pass

        os.remove(self.file_path)

        db = self._get_database()
        if db is not None and rel is not None and lower.endswith(".cbz"):
            db.forget_volume_upload(rel)
        self._audit("delete")

    def handle_move(self, dest_path: str) -> bool:
        """Handle direct file moves natively without touching sibling sidecars."""
        dest_physical = self._resolve_destination_path(dest_path)
        if dest_physical is None:
            return False

        mapper = self._get_mapper()
        old_rel = self._relative_under_library()
        new_rel = None
        if mapper is not None:
            try:
                new_rel = str(
                    dest_physical.resolve().relative_to(mapper.library_path.resolve())
                )
            except ValueError:
                new_rel = None

        dest_physical.parent.mkdir(parents=True, exist_ok=True)
        os.replace(self.file_path, dest_physical)

        db = self._get_database()
        if db is not None and old_rel is not None and new_rel is not None:
            db.rename_volume_upload(old_rel, new_rel)

        self._audit("move", details={"destination": dest_path})
        return True

    def support_recursive_move(self, dest_path: str) -> bool:
        return False

    def copy_move_single(
        self,
        dest_path: str,
        is_move: bool,
    ) -> bool:
        """Copy or move this resource."""
        mapper = self._get_mapper()
        dest_physical = self._resolve_destination_path(dest_path)
        if mapper is not None and dest_physical is not None:
            dest_physical.parent.mkdir(parents=True, exist_ok=True)
            if is_move:
                os.replace(self.file_path, dest_physical)
            else:
                shutil.copy2(self.file_path, dest_physical)
            db = self._get_database()
            if db is not None:
                old_rel = self._relative_under_library()
                try:
                    new_rel = str(
                        dest_physical.resolve().relative_to(mapper.library_path.resolve())
                    )
                except ValueError:
                    new_rel = None
                if is_move and old_rel is not None and new_rel is not None:
                    db.rename_volume_upload(old_rel, new_rel)
            self._audit(
                "move" if is_move else "copy",
                details={"destination": dest_path},
            )
            return True
        return False


class MokuroFolderResource(DAVCollection):
    """WebDAV resource for folders (both virtual and physical)."""

    # Static property list for folders (no getcontentlength).
    _PROP_NAMES = [
        "{DAV:}resourcetype",
        "{DAV:}creationdate",
        "{DAV:}getlastmodified",
        "{DAV:}displayname",
        "{DAV:}getetag",
    ]

    def __init__(
        self,
        path: str,
        environ: dict[str, Any],
        folder_path: Optional[Path],
        path_mapper: PathMapper,
        is_virtual: bool = False,
    ) -> None:
        super().__init__(path, environ)
        self.folder_path = folder_path
        self.path_mapper = path_mapper
        self.is_virtual = is_virtual
        self._stat: Optional[os.stat_result] = None
        self._scandir_cache: Optional[dict[str, os.DirEntry[str]]] = None

    def get_property_names(self, *, is_allprop: bool) -> list[str]:
        """Return static property list (no getter probing needed)."""
        return list(self._PROP_NAMES)

    def _get_stat(self) -> Optional[os.stat_result]:
        """Get cached stat result."""
        if self._stat is None and self.folder_path:
            try:
                self._stat = os.stat(self.folder_path)
            except OSError:
                pass
        return self._stat

    def _get_username(self) -> Optional[str]:
        """Get current username from environ."""
        user_data = self.environ.get("mokuro.user")
        if user_data:
            return user_data.get("username")
        return None

    def _get_database(self) -> Optional["Database"]:
        db = self.environ.get("mokuro.db")
        if db is None:
            return None
        return db  # type: ignore[return-value]

    def _get_actor_username(self) -> Optional[str]:
        user_data = self.environ.get("mokuro.user")
        if isinstance(user_data, dict):
            username = user_data.get("username")
            if isinstance(username, str):
                return username
        username = self.environ.get("mokuro.username")
        if isinstance(username, str):
            return username
        return None

    def _relative_under_library(self) -> Optional[str]:
        if self.folder_path is None:
            return None
        try:
            return str(self.folder_path.resolve().relative_to(self.path_mapper.library_path.resolve()))
        except ValueError:
            return None

    def _audit(self, action: str, *, details: Optional[dict[str, Any]] = None) -> None:
        db = self._get_database()
        if db is None:
            return
        rel = self._relative_under_library()
        target_path = f"/{PathMapper.READER_ROOT}/{rel}" if rel else self.path
        db.log_audit_event(
            action=action,
            actor_username=self._get_actor_username(),
            target_type="library_folder" if rel else "webdav_folder",
            target_path=target_path,
            details=details,
        )

    def _resolve_member_path(self, name: str) -> Optional[Path]:
        """Resolve a child resource safely under this physical folder."""
        if self.folder_path is None:
            return None
        return safe_resolve_under(self.folder_path, name)

    def _resolve_destination_path(self, dest_path: str) -> Optional[Path]:
        if self.path_mapper.get_path_type(dest_path) != "library":
            return None
        return self.path_mapper.virtual_to_physical(dest_path, self._get_actor_username())

    def _get_library_volume_paths(self) -> list[str]:
        """Collect CBZ paths relative to the library root before a folder move."""
        if self.folder_path is None:
            return []

        library_root = self.path_mapper.library_path.resolve()
        volume_paths: list[str] = []
        for candidate in self.folder_path.rglob("*"):
            if candidate.suffix.lower() != ".cbz":
                continue
            try:
                volume_paths.append(str(candidate.resolve().relative_to(library_root)))
            except ValueError:
                continue
        return volume_paths

    def get_creation_date(self) -> Optional[float]:
        stat_result = self._get_stat()
        if stat_result:
            return stat_result.st_ctime
        return datetime.now().timestamp()

    def get_display_name(self) -> str:
        if self.path == "/":
            return "mokuro-bunko"
        if self.folder_path:
            return self.folder_path.name
        return self.path.rstrip("/").split("/")[-1] or "root"

    def get_directory_info(self) -> Optional[dict[str, Any]]:
        return None

    def get_etag(self) -> Optional[str]:
        stat_result = self._get_stat()
        if stat_result:
            return f"{stat_result.st_mtime:.6f}"
        return None

    def get_last_modified(self) -> Optional[float]:
        stat_result = self._get_stat()
        if stat_result:
            return stat_result.st_mtime
        return datetime.now().timestamp()

    def get_member_names(self) -> list[str]:
        """Return list of member names."""
        normalized = self.path.rstrip("/") or "/"
        username = self._get_username()

        # Root: show mokuro-reader
        if normalized == "/":
            return [PathMapper.READER_ROOT]

        # /mokuro-reader: merge per-user files + shared library contents
        if normalized == f"/{PathMapper.READER_ROOT}":
            members: set[str] = set()

            # Per-user JSON files (only if they exist for this user)
            if username:
                for name in PathMapper.PER_USER_FILES:
                    file_path = self.path_mapper.get_user_file_path(username, name)
                    if file_path and file_path.exists():
                        members.add(name)

            # Oneshots directory (if configured and has files)
            if self.path_mapper.oneshots_path is not None:
                oneshots_root = self.path_mapper.oneshots_path
                if oneshots_root.exists() and oneshots_root.is_dir():
                    has_cbz = any(
                        f.is_file() and f.suffix.lower() == ".cbz"
                        for f in oneshots_root.rglob("*")
                        if f.is_file()
                    )
                    if has_cbz:
                        members.add(PathMapper.ONESHOTS_PREFIX)

            # Shared library contents - list all CBZ-containing directories
            # Flatten nested paths; keep single-level paths as-is
            try:
                library_root = self.path_mapper.library_path

                # Collect directories containing CBZ files
                cbz_dirs: set[Path] = set()
                for root, dirs, files in os.walk(library_root):
                    # Skip hidden directories
                    dirs[:] = [d for d in dirs if not d.startswith(".")]

                    # Skip oneshots directory - it's handled separately
                    if self.path_mapper.oneshots_path is not None:
                        try:
                            current_rel = Path(root).relative_to(library_root)
                            if str(current_rel).startswith(self.path_mapper.oneshots_path.name):
                                dirs.clear()  # Don't descend into oneshots
                                continue
                        except ValueError:
                            pass

                    # Check if this directory has CBZ files
                    has_cbz = any(f.lower().endswith(".cbz") for f in files)
                    if has_cbz:
                        cbz_dirs.add(Path(root))

                # Convert to member names
                for cbz_dir in cbz_dirs:
                    try:
                        rel_path = cbz_dir.relative_to(library_root)
                    except ValueError:
                        continue

                    parts = rel_path.parts
                    if len(parts) == 0:
                        # Root library - add CBZ files directly
                        for f in cbz_dir.iterdir():
                            if f.is_file() and f.suffix.lower() == ".cbz":
                                members.add(f.name)
                    elif len(parts) == 1:
                        # Single-level directory (e.g., "series") - add as-is
                        members.add(parts[0])
                    else:
                        # Nested directory - flatten (e.g., "Favorites/SeriesA" -> "Favorites--SeriesA")
                        flattened = self.path_mapper.flatten_path(parts)
                        members.add(flattened)

            except OSError:
                pass

            return sorted(members)

        # /mokuro-reader/oneshots: list CBZ files with flattening
        if normalized == f"/{PathMapper.READER_ROOT}/{PathMapper.ONESHOTS_PREFIX}":
            if self.path_mapper.oneshots_path is None:
                return []
            oneshots_root = self.path_mapper.oneshots_path
            oneshots_members: set[str] = set()
            try:
                for root, dirs, files in os.walk(oneshots_root):
                    dirs[:] = [d for d in sorted(dirs) if not d.startswith(".")]
                    for f in sorted(files):
                        if not f.lower().endswith(".cbz"):
                            continue
                        rel_path = Path(root).relative_to(oneshots_root)
                        volume_name = f[:-len(".cbz")]
                        if rel_path.parts:
                            flattened = self.path_mapper.flatten_path(
                                (*rel_path.parts, volume_name)
                            )
                        else:
                            flattened = volume_name
                        oneshots_members.add(f"{flattened}.cbz")
            except OSError:
                pass
            return sorted(oneshots_members)

        # Physical folder: list filesystem contents
        if self.folder_path:
            try:
                cache = {}
                with os.scandir(self.folder_path) as it:
                    for entry in it:
                        cache[entry.name] = entry
                self._scandir_cache = cache
                return list(cache.keys())
            except OSError:
                pass

        return []

    def _resource_from_entry(
        self,
        member_path: str,
        entry: os.DirEntry[str],
    ) -> DAVCollection | DAVNonCollection:
        """Create a resource from a cached DirEntry, pre-populating stat."""
        physical = Path(entry.path)
        if entry.is_dir(follow_symlinks=True):
            res = MokuroFolderResource(
                member_path, self.environ, physical, self.path_mapper,
            )
        else:
            res = MokuroFileResource(member_path, self.environ, physical)
        try:
            res._stat = entry.stat(follow_symlinks=True)
        except OSError:
            pass
        return res

    def get_member(self, name: str) -> Optional[DAVCollection | DAVNonCollection]:
        """Get a member resource by name."""
        member_path = join_uri(self.path, name)
        normalized = self.path.rstrip("/") or "/"
        username = self._get_username()

        # Root members
        if normalized == "/":
            if name == PathMapper.READER_ROOT:
                return MokuroFolderResource(
                    f"/{PathMapper.READER_ROOT}",
                    self.environ,
                    None,
                    self.path_mapper,
                    is_virtual=True,
                )
            return None

        # /mokuro-reader members
        if normalized == f"/{PathMapper.READER_ROOT}":
            # Per-user files
            if name in PathMapper.PER_USER_FILES:
                if username:
                    file_path = self.path_mapper.get_user_file_path(username, name)
                    if not file_path:
                        return None
                    return MokuroFileResource(
                        f"/{PathMapper.READER_ROOT}/{name}",
                        self.environ,
                        file_path,
                    )
                return None

            # Oneshots directory
            if name == PathMapper.ONESHOTS_PREFIX:
                if self.path_mapper.oneshots_path is None:
                    return None
                return MokuroFolderResource(
                    member_path,
                    self.environ,
                    self.path_mapper.oneshots_path,
                    self.path_mapper,
                )

            # Check if this is a flattened nested path (e.g., "Favorites--SeriesA")
            # Unflatten and resolve
            if self.path_mapper.FLATTEN_DELIMITER in name:
                unflattened_parts = self.path_mapper.unflatten_path(name)
                physical = self.path_mapper.library_path.joinpath(*unflattened_parts)
                if physical.is_dir():
                    return MokuroFolderResource(
                        member_path,
                        self.environ,
                        physical,
                        self.path_mapper,
                    )
                elif physical.exists():
                    return MokuroFileResource(member_path, self.environ, physical)
                return None

            # Fast path: use cached scandir entry (no stat/resolve needed)
            if self._scandir_cache and name in self._scandir_cache:
                return self._resource_from_entry(
                    member_path, self._scandir_cache[name],
                )

            # Oneshots folder lookup (unflatten nested paths)
            oneshots_path = self.path_mapper.oneshots_path
            if self.folder_path is not None and self.folder_path == oneshots_path:
                if oneshots_path is not None and self.path_mapper.FLATTEN_DELIMITER in name:
                    unflattened_parts = self.path_mapper.unflatten_path(name)
                    physical = oneshots_path.joinpath(*unflattened_parts)
                    if physical.exists():
                        if physical.is_dir():
                            return MokuroFolderResource(
                                member_path,
                                self.environ,
                                physical,
                                self.path_mapper,
                            )
                        return MokuroFileResource(member_path, self.environ, physical)
                    return None
                # Fallback for flat path
                if oneshots_path is not None:
                    physical = safe_resolve_under(oneshots_path, name)
                    if physical is not None and physical.exists():
                        if physical.is_dir():
                            return MokuroFolderResource(
                                member_path,
                                self.environ,
                                physical,
                                self.path_mapper,
                            )
                        return MokuroFileResource(member_path, self.environ, physical)
                    return MokuroFileResource(member_path, self.environ, physical) if physical else None

            # Fallback for uncached lookups (flat path)
            physical = safe_resolve_under(self.path_mapper.library_path, name)
            if physical is None:
                return None
            if physical.is_dir():
                return MokuroFolderResource(
                    member_path,
                    self.environ,
                    physical,
                    self.path_mapper,
                )
            elif physical.exists():
                return MokuroFileResource(member_path, self.environ, physical)
            # Return resource for non-existent file (supports PUT)
            return MokuroFileResource(member_path, self.environ, physical)

        # Physical folder members
        if self.folder_path:
            # Fast path: use cached scandir entry
            if self._scandir_cache and name in self._scandir_cache:
                return self._resource_from_entry(
                    member_path, self._scandir_cache[name],
                )

            # Fallback for uncached lookups
            member_physical = self._resolve_member_path(name)
            if member_physical is None:
                return None
            if member_physical.exists():
                if member_physical.is_dir():
                    return MokuroFolderResource(
                        member_path,
                        self.environ,
                        member_physical,
                        self.path_mapper,
                    )
                else:
                    return MokuroFileResource(
                        member_path,
                        self.environ,
                        member_physical,
                    )
            # Return resource for non-existent file (supports PUT)
            return MokuroFileResource(member_path, self.environ, member_physical)

        return None

    def create_empty_resource(self, name: str) -> DAVNonCollection:
        """Create an empty file resource for PUT."""
        member_path = join_uri(self.path, name)
        normalized = self.path.rstrip("/") or "/"
        username = self._get_username()

        # /mokuro-reader: per-user files or library files
        if normalized == f"/{PathMapper.READER_ROOT}":
            if name in PathMapper.PER_USER_FILES:
                if username:
                    file_path = self.path_mapper.get_user_file_path(username, name)
                    if not file_path:
                        raise ValueError("Invalid username path")
                    file_path.parent.mkdir(parents=True, exist_ok=True)
                    return MokuroFileResource(
                        f"/{PathMapper.READER_ROOT}/{name}",
                        self.environ,
                        file_path,
                    )
                raise ValueError("Authentication required to create per-user files")
            # Library file
            file_path = safe_resolve_under(self.path_mapper.library_path, name)
            if file_path is None:
                raise DAVError(403, "Forbidden")
            file_path.parent.mkdir(parents=True, exist_ok=True)
            return MokuroFileResource(member_path, self.environ, file_path)

        # Physical folder
        if self.folder_path:
            file_path = self._resolve_member_path(name)
            if file_path is None:
                raise DAVError(403, "Forbidden")
            return MokuroFileResource(member_path, self.environ, file_path)

        raise ValueError(f"Cannot create resource at {member_path}")

    def create_collection(self, name: str) -> MokuroFolderResource:
        """Create a subdirectory (MKCOL)."""
        member_path = join_uri(self.path, name)
        normalized = self.path.rstrip("/") or "/"

        # /mokuro-reader: create series folder in shared library
        if normalized == f"/{PathMapper.READER_ROOT}":
            new_dir = safe_resolve_under(self.path_mapper.library_path, name)
            if new_dir is None:
                raise DAVError(403, "Forbidden")
            new_dir.mkdir(parents=True, exist_ok=True)
            self._audit("mkdir", details={"path": member_path})
            return MokuroFolderResource(
                member_path,
                self.environ,
                new_dir,
                self.path_mapper,
            )

        # Physical folder
        if self.folder_path:
            new_dir = self._resolve_member_path(name)
            if new_dir is None:
                raise DAVError(403, "Forbidden")
            new_dir.mkdir(parents=True, exist_ok=True)
            self._audit("mkdir", details={"path": member_path})
            return MokuroFolderResource(
                member_path,
                self.environ,
                new_dir,
                self.path_mapper,
            )

        raise ValueError(f"Cannot create collection at {member_path}")

    def copy_move_single(
        self,
        dest_path: str,
        is_move: bool,
    ) -> bool:
        """Create the destination collection for generic COPY/MOVE handling."""
        dest_physical = self._resolve_destination_path(dest_path)
        if dest_physical is None:
            return False
        dest_physical.mkdir(parents=True, exist_ok=True)
        return True

    def support_recursive_move(self, dest_path: str) -> bool:
        return (
            self.folder_path is not None
            and self._resolve_destination_path(dest_path) is not None
        )

    def move_recursive(self, dest_path: str) -> list[tuple[str, DAVError]]:
        """Move a folder tree atomically, preserving OCR sidecars and ownership."""
        if self.folder_path is None:
            raise DAVError(403, "Forbidden")

        dest_physical = self._resolve_destination_path(dest_path)
        if dest_physical is None:
            raise DAVError(403, "Forbidden")

        old_rel_prefix = self._relative_under_library()
        new_rel_prefix = None
        try:
            new_rel_prefix = str(
                dest_physical.resolve().relative_to(self.path_mapper.library_path.resolve())
            )
        except ValueError:
            new_rel_prefix = None

        volume_paths = self._get_library_volume_paths()
        dest_physical.parent.mkdir(parents=True, exist_ok=True)
        os.replace(self.folder_path, dest_physical)

        db = self._get_database()
        if db is not None and old_rel_prefix is not None and new_rel_prefix is not None:
            for old_rel in volume_paths:
                suffix = old_rel[len(old_rel_prefix):].lstrip("/")
                new_rel = f"{new_rel_prefix}/{suffix}" if suffix else new_rel_prefix
                db.rename_volume_upload(old_rel, new_rel)

        self._audit("move", details={"destination": dest_path})
        return []

    def delete(self) -> None:
        """Delete this folder."""
        if self.folder_path and self.folder_path.exists():
            db = self._get_database()
            rel = self._relative_under_library()
            if db is not None and rel:
                db.forget_volume_uploads_under_prefix(rel)
            shutil.rmtree(self.folder_path)
            self._audit("delete")

    def support_recursive_delete(self) -> bool:
        return True


class _ValidatedCbzWriter:
    """Temporary CBZ writer that validates archive integrity before committing."""

    def __init__(self, destination: Path) -> None:
        self.destination = destination
        self.destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{destination.name}.upload-",
            suffix=".tmp",
            dir=str(destination.parent),
        )
        os.close(fd)
        self.temp_path = Path(temp_name)
        self._file = open(self.temp_path, "wb")
        self._closed = False

    def write(self, data: bytes) -> int:
        return self._file.write(data)

    def flush(self) -> None:
        self._file.flush()

    def fileno(self) -> int:
        return self._file.fileno()

    def tell(self) -> int:
        return self._file.tell()

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._file.seek(offset, whence)

    def truncate(self, size: Optional[int] = None) -> int:
        if size is None:
            return self._file.truncate()
        return self._file.truncate(size)

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._file.flush()
            os.fsync(self._file.fileno())
        except OSError:
            pass
        finally:
            self._file.close()

        if not self._is_valid_cbz(self.temp_path):
            try:
                self.temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise DAVError(400, "Invalid or corrupted CBZ upload")

        os.replace(self.temp_path, self.destination)
        # mkstemp creates with 0o600; apply umask-derived permissions instead.
        # On Windows, umask/chmod have no effect on NTFS permissions.
        if os.name != "nt":
            umask = os.umask(0)
            os.umask(umask)
            os.chmod(self.destination, 0o666 & ~umask)

    def writable(self) -> bool:
        return True

    def __enter__(self) -> "_ValidatedCbzWriter":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if exc_type is not None:
            try:
                self._file.close()
            finally:
                try:
                    self.temp_path.unlink(missing_ok=True)
                except OSError:
                    pass
            self._closed = True
            return
        self.close()

    @staticmethod
    def _is_valid_cbz(path: Path) -> bool:
        try:
            with zipfile.ZipFile(path, "r") as zf:
                # Ensure archive structure and entry CRCs are valid.
                return zf.testzip() is None
        except (zipfile.BadZipFile, OSError, EOFError):
            return False


class _AuditedWriter:
    """File wrapper that triggers callback only after successful close."""

    def __init__(self, inner: BinaryIO, on_commit: Callable[[], None]) -> None:
        self._inner = inner
        self._on_commit = on_commit
        self._committed = False

    def __getattr__(self, item: str) -> Any:
        return getattr(self._inner, item)

    def close(self) -> None:
        if self._committed:
            return
        self._inner.close()
        self._committed = True
        self._on_commit()

    def __enter__(self) -> "_AuditedWriter":
        self._inner.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if exc_type is not None:
            self._inner.__exit__(exc_type, exc, tb)
            return
        self.close()
