# iPhone Files Extractor

Reconstruct the **iPhone Files app** folder tree — On My iPhone, AirDropped Inbox,
iCloud Drive (locally synced), and the `.Trash` recovery folders — from an
**unencrypted** Apple iPhone (Finder/iTunes) backup.

Preserves the real directory structure, original filenames, creation/modification
dates, empty folders, symlinks and hardlinks — bit-identical raw copies.

Built in clean, auditable, dependency-light Python (stdlib only, `tqdm` optional).
Independent tool; the core extraction algorithm (Manifest.db scan, restored
directory tree, binary-plist metadata) is the proven approach shared by the
established open-source iOS-backup extractors, rebuilt and improved.

---

## Why this exists

The iPhone **Files app** is not one storage location — it aggregates **four**
separate backup domains. When you "see" a file in Files, that file may actually
live on any of them:

| What you see | Backup domain |
|---|---|
| On My iPhone (local) | `AppDomainGroup-group.com.apple.FileProvider.LocalStorage` |
| AirDropped / Inbox | `AppDomain-com.apple.DocumentsApp` |
| Folder index | `AppDomainGroup-group.com.apple.DocumentManager` |
| iCloud Drive (synced) | `HomeDomain` |

Generic backup extractors dump one domain at a time and leave it to you to
stitch them together. This tool does the stitching for you.

## Improvements over the existing extractors

* **Parallel copy** (`ProcessPoolExecutor`) — most other tools copy serially.
* **Incremental resume** — never re-copies or overwrites an existing identical file.
* **Proper binary-plist metadata** — `Birth`/`LastModified`/`Size`/`Target` via `plistlib`.
* **Preserves empty directories** — `flags==2` entries are kept, not dropped.
* **Postpones directory mtime** until after all children are written.
* **Files-app domain aggregation** — `--local-only`, `--domain`, `--flat`.
* **Trash recovery** — `.Trash` folders are kept so "deleted" items recoverable.
* **Duplicate-name safe** — colliding filenames get a ` (n)` suffix, never overwritten.

## Requirements

* Python 3.8+ (stdlib only; `tqdm` optional for a progress bar)
* An **unencrypted** iPhone backup. (Unencrypted backups don't contain your
  Apple ID password, Health data, or Keychain.)
* For exact creation dates on macOS: the built-in `/usr/bin/SetFile`.

## Usage

```bash
# Scan only
python3 iphone_files_extractor.py --backup "<backup dir>" --dry-run

# Extract everything the Files app shows, into labelled folders
python3 iphone_files_extractor.py --backup "<backup dir>" -o ~/FilesExtract

# Only On My iPhone + Inbox (skip iCloud Drive)
python3 iphone_files_extractor.py --backup "<backup dir>" -o ~/FilesExtract --local-only

# Restore real creation/modification dates
python3 iphone_files_extractor.py --backup "<backup dir>" -o ~/FilesExtract --restore-dates

# Save disk: hardlink instead of copy (backup and output on same filesystem)
python3 iphone_files_extractor.py --backup "<backup dir>" -o ~/FilesExtract --link

# A single specific domain
python3 iphone_files_extractor.py --backup "<backup dir>" -o ~/inbox --domain AppDomain-com.apple.DocumentsApp
```

### Options

| Flag | Description |
|---|---|
| `--backup <dir>` | iOS backup directory (contains `Manifest.db`) — **required** |
| `-o, --output <dir>` | Output directory — **required** |
| `--domain <d>` | Extract a specific domain (repeatable). Bare bundle ID accepted. |
| `--local-only` | Only On My iPhone (`FileProvider.LocalStorage`) + Inbox |
| `--flat` | Drop the labelled top-level Files folder; dump the raw tree |
| `--include-symlinks` | Recreate symlinks (default: copy their content) |
| `--restore-dates` | Restore file/dir Birth + LastModified on macOS |
| `--link` | Hardlink instead of copy (saves disk; same filesystem required) |
| `--workers N` | Parallel copy workers (default: CPU count) |
| `--dry-run` | Scan and report only |

## How it works

1. Open `Manifest.db` (read-only), read the `Files` table.
2. Map `flags` to filesystem type (`1=file, 2=dir, 4=symlink, 10=hardlink`).
3. Resolve each payload at `<fileID[:2]>/<fileID>`.
4. Parse the binary-plist `file` blob for `Birth`/`LastModified`/`Size`/`Target`
   (Apple epoch → add `978307200`).
5. Build a virtual tree from `domain / relativePath`, preserving dirs, files,
   symlinks and hardlinks.
6. Copy in parallel, restoring dates (directory mtimes applied last).

## License

MIT — © 2026 Jacky Keung. See [LICENSE](LICENSE).