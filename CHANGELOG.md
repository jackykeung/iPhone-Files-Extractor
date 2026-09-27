# Changelog

All notable changes to **iPhone Files Extractor** are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.0] - 2026-09-28

### Added
- **Test suite** (`tests/test_files_extractor.py`): 52 tests covering every
  unit and function in the tool — `parse_plist_metadata`, `apple_to_unix`,
  `src_path`, `open_manifest_db`, `list_domains`, `query_files`,
  `Node`/`FileSystemIndex`, `_do_copy`, `_copy_one`, `read_smartfolders`, and
  `main`. Includes edge cases (garbage input, empty plist, symlink UID
  dereference, duplicate-name suffixing, hardlink-mode, missing source, etc.).
- **Deterministic coverage measurement** (see `tests/README.md`):
  100% function, 94% statement, 92% branch coverage. The remaining uncovered
  branches are defensive fallbacks only.
- `tests/README.md` documenting how to run the suite and the measured coverage.

### Changed
- `main()` now accepts an optional `argv` argument (defaults to `sys.argv`) so
  the whole CLI is directly testable and callable programmatically. Behaviour
  is unchanged when run from the shell.

### Fixed
- `read_smartfolders()` accessed the fileID via `r["fileID"]`, which only works
  when the SQLite connection has `row_factory=Row` set (as `open_manifest_db`
  does). It now reads `r[0]` by index, so the function is robust regardless of
  how the caller created the connection.

### Notes
- No change to extraction behaviour in this release; purely testability and
  robustness hardening plus an explicit CHANGELOG.

## [1.0.0] - 2026-09-28

### Added
- First release. Reconstructs the iPhone **Files app** folder tree (On My
  iPhone, AirDropped Inbox, iCloud Drive, `.Trash`) from an unencrypted backup.

### Features
- **Parallel copy** (`ProcessPoolExecutor`) with configurable `--workers`.
- **Incremental resume** — never re-copies or overwrites an existing identical file.
- **Proper binary-plist metadata** — `Birth`/`LastModified`/`Size`/`Target` via `plistlib`.
- **Preserves empty directories** (flags `2`), symlinks (`4`), hardlinks (`10`).
- **Postpones directory mtime** until after all children are written.
- **Files-domain aggregation** — `--local-only`, `--domain`, `--flat`.
- **Trash recovery** — `.Trash` folders preserved so deleted items are recoverable.
- **Duplicate-name safe** — colliding filenames get a ` (n)` suffix, never overwritten.
- `--restore-dates` restores real Birth + LastModified on macOS; `--link` hardlinks
  instead of copying to save disk; `--dry-run` scans only.
- Bundled `analysis.html` surveying the existing iOS-backup extractors and the
  technique improvements made in this tool.