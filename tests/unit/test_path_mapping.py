"""Tests for virtual to physical path mapping."""

from __future__ import annotations

from pathlib import Path

import pytest

from mokuro_bunko.webdav.resources import PathMapper


@pytest.fixture
def mapper(temp_dir: Path) -> PathMapper:
    """Create a path mapper with temporary storage."""
    return PathMapper(temp_dir / "library")


class TestPathMapperInit:
    """Tests for PathMapper initialization."""

    def test_sets_storage_base(self, temp_dir: Path) -> None:
        """Test storage base path is set."""
        mapper = PathMapper(temp_dir / "library")
        assert mapper.storage_base == temp_dir

    def test_derived_paths(self, temp_dir: Path) -> None:
        """Test derived path properties."""
        mapper = PathMapper(temp_dir / "library")
        assert mapper.library_path == temp_dir / "library"
        assert mapper.inbox_path == temp_dir / "inbox"
        assert mapper.users_path == temp_dir / "users"

    def test_ensure_directories(self, temp_dir: Path) -> None:
        """Test directory creation."""
        mapper = PathMapper(temp_dir / "library")
        mapper.ensure_directories()

        assert mapper.library_path.exists()
        assert mapper.inbox_path.exists()
        assert mapper.users_path.exists()

    def test_ensure_user_directory(self, mapper: PathMapper) -> None:
        """Test user directory creation."""
        user_dir = mapper.ensure_user_directory("alice")
        assert user_dir == mapper.users_path / "alice"
        assert user_dir.exists()

    def test_class_constants(self) -> None:
        """Test class-level constants."""
        assert PathMapper.READER_ROOT == "mokuro-reader"
        assert PathMapper.PER_USER_FILES == frozenset({"volume-data.json", "profiles.json"})
        assert PathMapper.FLATTEN_DELIMITER == "--"


class TestPathFlattening:
    """Tests for nested path flattening/unflattening."""

    def test_flatten_single_component(self, mapper: PathMapper) -> None:
        """Flattening a single component returns it unchanged."""
        result = mapper.flatten_path(("SeriesA",))
        assert result == "SeriesA"

    def test_flatten_nested_path(self, mapper: PathMapper) -> None:
        """Flattening a nested path uses delimiter."""
        result = mapper.flatten_path(("Favorites", "SeriesA"))
        assert result == "Favorites--SeriesA"

    def test_flatten_deeply_nested(self, mapper: PathMapper) -> None:
        """Flattening works for deeply nested paths."""
        result = mapper.flatten_path(("A", "B", "C", "Series"))
        assert result == "A--B--C--Series"

    def test_flatten_empty(self, mapper: PathMapper) -> None:
        """Flattening empty tuple returns empty string."""
        result = mapper.flatten_path(())
        assert result == ""

    def test_flatten_with_delimiter_in_name(self, mapper: PathMapper) -> None:
        """Flattening escapes delimiter characters in component names."""
        result = mapper.flatten_path(("My--Series", "Volume1"))
        assert result == "My-\x00-Series--Volume1"

    def test_unflatten_single_component(self, mapper: PathMapper) -> None:
        """Unflattening a single component returns it as a tuple."""
        result = mapper.unflatten_path("SeriesA")
        assert result == ("SeriesA",)

    def test_unflatten_nested(self, mapper: PathMapper) -> None:
        """Unflattening restores nested path components."""
        result = mapper.unflatten_path("Favorites--SeriesA")
        assert result == ("Favorites", "SeriesA")

    def test_unflatten_deeply_nested(self, mapper: PathMapper) -> None:
        """Unflattening works for deeply nested paths."""
        result = mapper.unflatten_path("A--B--C--Series")
        assert result == ("A", "B", "C", "Series")

    def test_unflatten_empty(self, mapper: PathMapper) -> None:
        """Unflattening empty string returns empty tuple."""
        result = mapper.unflatten_path("")
        assert result == ()

    def test_unflatten_with_escaped_delimiter(self, mapper: PathMapper) -> None:
        """Unflattening restores escaped delimiters."""
        result = mapper.unflatten_path("My-\x00-Series--Volume1")
        assert result == ("My--Series", "Volume1")

    def test_roundtrip_simple(self, mapper: PathMapper) -> None:
        """Round-trip conversion preserves simple paths."""
        original = ("Favorites", "SeriesA")
        flattened = mapper.flatten_path(original)
        unflattened = mapper.unflatten_path(flattened)
        assert unflattened == original

    def test_roundtrip_with_delimiter(self, mapper: PathMapper) -> None:
        """Round-trip conversion preserves paths containing delimiter."""
        original = ("My--Series", "Vol--1")
        flattened = mapper.flatten_path(original)
        unflattened = mapper.unflatten_path(flattened)
        assert unflattened == original

    def test_is_flattened_name(self, mapper: PathMapper) -> None:
        """Check if name contains flatten delimiter."""
        assert mapper.is_flattened_name("Favorites--SeriesA") is True
        assert mapper.is_flattened_name("SeriesA") is False


class TestIsPerUserFile:
    """Tests for is_per_user_file."""

    def test_volume_data_is_per_user(self, mapper: PathMapper) -> None:
        """Test volume-data.json is a per-user file."""
        assert mapper.is_per_user_file("/mokuro-reader/volume-data.json") is True

    def test_profiles_is_per_user(self, mapper: PathMapper) -> None:
        """Test profiles.json is a per-user file."""
        assert mapper.is_per_user_file("/mokuro-reader/profiles.json") is True

    def test_library_file_not_per_user(self, mapper: PathMapper) -> None:
        """Test library files are not per-user."""
        assert mapper.is_per_user_file("/mokuro-reader/manga.cbz") is False
        assert mapper.is_per_user_file("/mokuro-reader/series/vol1.cbz") is False

    def test_non_reader_path_not_per_user(self, mapper: PathMapper) -> None:
        """Test paths outside /mokuro-reader/ are not per-user."""
        assert mapper.is_per_user_file("/inbox/file.cbz") is False
        assert mapper.is_per_user_file("/volume-data.json") is False
        assert mapper.is_per_user_file("/") is False


class TestIsReaderPath:
    """Tests for is_reader_path."""

    def test_reader_root(self, mapper: PathMapper) -> None:
        """Test /mokuro-reader is a reader path."""
        assert mapper.is_reader_path("/mokuro-reader") is True

    def test_reader_subpath(self, mapper: PathMapper) -> None:
        """Test paths under /mokuro-reader/ are reader paths."""
        assert mapper.is_reader_path("/mokuro-reader/volume-data.json") is True
        assert mapper.is_reader_path("/mokuro-reader/series/vol1.cbz") is True

    def test_non_reader_path(self, mapper: PathMapper) -> None:
        """Test paths outside /mokuro-reader/ are not reader paths."""
        assert mapper.is_reader_path("/") is False
        assert mapper.is_reader_path("/inbox") is False
        assert mapper.is_reader_path("/inbox/file.cbz") is False


class TestVirtualToPhysical:
    """Tests for virtual_to_physical conversion."""

    def test_root_returns_none(self, mapper: PathMapper) -> None:
        """Test root path returns None (virtual only)."""
        assert mapper.virtual_to_physical("/") is None
        assert mapper.virtual_to_physical("") is None

    def test_reader_root_returns_none(self, mapper: PathMapper) -> None:
        """Test reader root path returns None (virtual only)."""
        assert mapper.virtual_to_physical("/mokuro-reader") is None

    def test_reader_library_file(self, mapper: PathMapper) -> None:
        """Test /mokuro-reader/manga.cbz maps to library/manga.cbz."""
        result = mapper.virtual_to_physical("/mokuro-reader/manga.cbz")
        assert result == mapper.library_path / "manga.cbz"

    def test_reader_series_file(self, mapper: PathMapper) -> None:
        """Test /mokuro-reader/series/vol1.cbz maps to library/series/vol1.cbz."""
        result = mapper.virtual_to_physical("/mokuro-reader/series/vol1.cbz")
        assert result == mapper.library_path / "series" / "vol1.cbz"

    def test_flattened_nested_series(self, mapper: PathMapper) -> None:
        """Test flattened nested path maps correctly."""
        # /mokuro-reader/Favorites--SeriesA/vol1.cbz
        # should map to library/Favorites/SeriesA/vol1.cbz
        result = mapper.virtual_to_physical("/mokuro-reader/Favorites--SeriesA/vol1.cbz")
        assert result == mapper.library_path / "Favorites" / "SeriesA" / "vol1.cbz"

    def test_flattened_deeply_nested(self, mapper: PathMapper) -> None:
        """Test deeply nested flattened path maps correctly."""
        result = mapper.virtual_to_physical("/mokuro-reader/A--B--C--Series/vol1.cbz")
        assert result == mapper.library_path / "A" / "B" / "C" / "Series" / "vol1.cbz"

    def test_per_user_file_with_user(self, mapper: PathMapper) -> None:
        """Test per-user file maps to user directory when username provided."""
        result = mapper.virtual_to_physical("/mokuro-reader/volume-data.json", username="alice")
        assert result == mapper.users_path / "alice" / "volume-data.json"

        result = mapper.virtual_to_physical("/mokuro-reader/profiles.json", username="alice")
        assert result == mapper.users_path / "alice" / "profiles.json"

    def test_per_user_file_without_user(self, mapper: PathMapper) -> None:
        """Test per-user file without user returns None."""
        result = mapper.virtual_to_physical("/mokuro-reader/volume-data.json")
        assert result is None

        result = mapper.virtual_to_physical("/mokuro-reader/profiles.json")
        assert result is None

    def test_inbox_root(self, mapper: PathMapper) -> None:
        """Test inbox root path."""
        result = mapper.virtual_to_physical("/inbox")
        assert result == mapper.inbox_path

    def test_inbox_file(self, mapper: PathMapper) -> None:
        """Test inbox file path."""
        result = mapper.virtual_to_physical("/inbox/upload.cbz")
        assert result == mapper.inbox_path / "upload.cbz"

    def test_unknown_path(self, mapper: PathMapper) -> None:
        """Test unknown path returns None."""
        result = mapper.virtual_to_physical("/unknown/path")
        assert result is None

    def test_library_path_traversal_rejected(self, mapper: PathMapper) -> None:
        """Traversal outside library root is rejected."""
        result = mapper.virtual_to_physical("/mokuro-reader/../../escape.txt")
        assert result is None

    def test_inbox_path_traversal_rejected(self, mapper: PathMapper) -> None:
        """Traversal outside inbox root is rejected."""
        result = mapper.virtual_to_physical("/inbox/../../escape.txt")
        assert result is None

    def test_path_normalization(self, mapper: PathMapper) -> None:
        """Test path normalization."""
        # With trailing slash
        assert mapper.virtual_to_physical("/mokuro-reader/manga.cbz") == mapper.library_path / "manga.cbz"
        # Without leading slash
        assert mapper.virtual_to_physical("mokuro-reader/manga.cbz") == mapper.library_path / "manga.cbz"


class TestPhysicalToVirtual:
    """Tests for physical_to_virtual conversion."""

    def test_library_file(self, mapper: PathMapper) -> None:
        """Test library file maps to /mokuro-reader/."""
        physical = mapper.library_path / "manga.cbz"
        result = mapper.physical_to_virtual(physical)
        assert result == "/mokuro-reader/manga.cbz"

    def test_library_nested_file(self, mapper: PathMapper) -> None:
        """Test nested library file maps to /mokuro-reader/ with flattening."""
        # library/favorites/series-a/volume-1.cbz -> /mokuro-reader/favorites--series-a/volume-1.cbz
        physical = mapper.library_path / "favorites" / "series-a" / "volume-1.cbz"
        result = mapper.physical_to_virtual(physical)
        assert result == "/mokuro-reader/favorites--series-a/volume-1.cbz"

    def test_library_deeply_nested_file(self, mapper: PathMapper) -> None:
        """Test deeply nested library file is flattened correctly."""
        physical = mapper.library_path / "A" / "B" / "C" / "vol1.cbz"
        result = mapper.physical_to_virtual(physical)
        # Directories are flattened, filename stays separate
        assert result == "/mokuro-reader/A--B--C/vol1.cbz"

    def test_library_series_folder(self, mapper: PathMapper) -> None:
        """Test nested series folder is flattened."""
        # Create the directory so is_dir() check works
        physical = mapper.library_path / "Favorites" / "SeriesA"
        physical.mkdir(parents=True)
        result = mapper.physical_to_virtual(physical)
        # Folders get fully flattened
        assert result == "/mokuro-reader/Favorites--SeriesA"

    def test_library_flat_series(self, mapper: PathMapper) -> None:
        """Test flat series folder (no nesting) stays flat."""
        physical = mapper.library_path / "SeriesA" / "vol1.cbz"
        result = mapper.physical_to_virtual(physical)
        assert result == "/mokuro-reader/SeriesA/vol1.cbz"

    def test_library_root(self, mapper: PathMapper) -> None:
        """Test library root maps to /mokuro-reader."""
        result = mapper.physical_to_virtual(mapper.library_path)
        assert result == "/mokuro-reader"

    def test_inbox_path(self, mapper: PathMapper) -> None:
        """Test inbox path conversion (not flattened)."""
        physical = mapper.inbox_path / "upload.cbz"
        result = mapper.physical_to_virtual(physical)
        assert result == "/inbox/upload.cbz"

    def test_user_per_user_file(self, mapper: PathMapper) -> None:
        """Test user per-user file maps to /mokuro-reader/."""
        physical = mapper.users_path / "alice" / "volume-data.json"
        result = mapper.physical_to_virtual(physical)
        assert result == "/mokuro-reader/volume-data.json"

        physical = mapper.users_path / "alice" / "profiles.json"
        result = mapper.physical_to_virtual(physical)
        assert result == "/mokuro-reader/profiles.json"

    def test_storage_root(self, mapper: PathMapper) -> None:
        """Test storage root returns /."""
        result = mapper.physical_to_virtual(mapper.storage_base)
        assert result == "/"

    def test_outside_storage(self, mapper: PathMapper) -> None:
        """Test path outside storage returns None."""
        result = mapper.physical_to_virtual(Path("/tmp/other"))
        assert result is None


class TestGetPathType:
    """Tests for get_path_type."""

    def test_root(self, mapper: PathMapper) -> None:
        """Test root path type."""
        assert mapper.get_path_type("/") == "root"
        assert mapper.get_path_type("") == "root"

    def test_reader_root(self, mapper: PathMapper) -> None:
        """Test reader root path type."""
        assert mapper.get_path_type("/mokuro-reader") == "reader_root"

    def test_progress_files(self, mapper: PathMapper) -> None:
        """Test per-user progress files path type."""
        assert mapper.get_path_type("/mokuro-reader/volume-data.json") == "progress"
        assert mapper.get_path_type("/mokuro-reader/profiles.json") == "progress"

    def test_library_files(self, mapper: PathMapper) -> None:
        """Test library file path types."""
        assert mapper.get_path_type("/mokuro-reader/manga.cbz") == "library"
        assert mapper.get_path_type("/mokuro-reader/series/vol1.cbz") == "library"
        assert mapper.get_path_type("/mokuro-reader/series") == "library"
        # Flattened nested paths
        assert mapper.get_path_type("/mokuro-reader/Favorites--SeriesA") == "library"
        assert mapper.get_path_type("/mokuro-reader/Favorites--SeriesA/vol1.cbz") == "library"

    def test_inbox(self, mapper: PathMapper) -> None:
        """Test inbox path types."""
        assert mapper.get_path_type("/inbox") == "inbox"
        assert mapper.get_path_type("/inbox/file.cbz") == "inbox"

    def test_unknown(self, mapper: PathMapper) -> None:
        """Test unknown path type."""
        assert mapper.get_path_type("/other") == "unknown"
        assert mapper.get_path_type("/something/else") == "unknown"
        assert mapper.get_path_type("/library") == "unknown"
        assert mapper.get_path_type("/users/alice") == "unknown"


class TestPathMappingConsistency:
    """Tests for round-trip path mapping consistency."""

    def test_library_roundtrip(self, mapper: PathMapper) -> None:
        """Test library path round-trip."""
        # Flat paths should round-trip correctly
        virtual = "/mokuro-reader/flat-series/vol1.cbz"
        physical = mapper.virtual_to_physical(virtual)
        assert physical is not None
        back = mapper.physical_to_virtual(physical)
        assert back == virtual

    def test_nested_library_roundtrip(self, mapper: PathMapper) -> None:
        """Test nested library path round-trip with flattening."""
        # Nested paths use flattening
        virtual = "/mokuro-reader/Favorites--SeriesA/vol1.cbz"
        physical = mapper.virtual_to_physical(virtual)
        assert physical is not None
        assert physical == mapper.library_path / "Favorites" / "SeriesA" / "vol1.cbz"
        back = mapper.physical_to_virtual(physical)
        assert back == virtual

    def test_inbox_roundtrip(self, mapper: PathMapper) -> None:
        """Test inbox path round-trip."""
        virtual = "/inbox/upload.cbz"
        physical = mapper.virtual_to_physical(virtual)
        assert physical is not None
        back = mapper.physical_to_virtual(physical)
        assert back == virtual

    def test_per_user_file_roundtrip(self, mapper: PathMapper) -> None:
        """Test per-user file round-trip."""
        virtual = "/mokuro-reader/volume-data.json"
        physical = mapper.virtual_to_physical(virtual, username="alice")
        assert physical is not None
        back = mapper.physical_to_virtual(physical)
        assert back == virtual


class TestNestedPathEdgeCases:
    """Tests for edge cases in nested path handling."""

    def test_path_with_spaces(self, mapper: PathMapper) -> None:
        """Paths with spaces are handled correctly."""
        virtual = "/mokuro-reader/My Series--Volume 1/file.cbz"
        physical = mapper.virtual_to_physical(virtual)
        assert physical == mapper.library_path / "My Series" / "Volume 1" / "file.cbz"

    def test_path_with_special_chars(self, mapper: PathMapper) -> None:
        """Paths with special characters are handled correctly."""
        virtual = "/mokuro-reader/Series[1]--Vol.1/chapter.cbz"
        physical = mapper.virtual_to_physical(virtual)
        assert physical == mapper.library_path / "Series[1]" / "Vol.1" / "chapter.cbz"

    def test_delimiter_in_folder_name(self, mapper: PathMapper) -> None:
        """Folders containing the delimiter are escaped correctly."""
        # Create the directory structure to test physical->virtual round-trip
        (mapper.library_path / "My--Series" / "Volume1").mkdir(parents=True)
        
        # A folder literally named "My--Series" should work
        # In virtual path, it's escaped as My-\x00-Series
        virtual = "/mokuro-reader/My-\x00-Series--Volume1/file.cbz"
        physical = mapper.virtual_to_physical(virtual)
        # This should unflatten to ("My--Series", "Volume1")
        assert physical == mapper.library_path / "My--Series" / "Volume1" / "file.cbz"
