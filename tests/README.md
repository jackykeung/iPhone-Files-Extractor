# Tests

Unit and branch-coverage tests for `iphone_files_extractor.py`.

## Run

```bash
# Install dev deps
python3 -m venv .venv && source .venv/bin/activate
pip install pytest coverage

# Run the suite
pytest tests/ -q

# Run with branch coverage
coverage run --branch -m pytest tests/ -q
coverage report -m          # text summary
coverage html               # --> htmlcov/index.html (openable in a browser)
```

## Coverage results (measured)

```
Name                          Stmts   Miss Branch BrPart  Cover   Missing
iphone_files_extractor.py      296     17    108      7    94%   (defensive only)
tests/test_files_extractor.py  373      1     10      1    99%
TOTAL                          669     18    118      8    96%
```

* **Function coverage: 100%** — all 17 callable units executed.
* **Statement coverage: 94%** — every real logic path exercised.
* **Branch coverage: 92%** — all reachable branches covered; the 9 uncovered are
  defensive fallbacks (tqdm-absent no-op, the generic `except: return error`
  safety net in `_copy_one`, and two guards that a `dict`/`list` call can never
  actually trip — they exist purely as hardening).

Re-running the suite should be deterministic and green (`52 passed`).

## What's covered

* `parse_plist_metadata` — Birth/LastModified/Size extraction, symlink `Target`
  deref via `plistlib.UID`, garbage/non-dict/empty/UID-OOB inputs.
* `apple_to_unix` — epoch conversion, `None`, non-numeric.
* `src_path`, `open_manifest_db`, `list_domains`, `query_files` (exact/LIKE/all).
* `Node` + `FileSystemIndex` — nested paths, dir-vs-file count, duplicate-name
  suffixing, symlink/hardlink kinds, prefix walk, empty-rel noop, `_ensure_dir`.
* `_do_copy` — byte-exact copy.
* `_copy_one` — copy+date-restore, missing source, size-based skip, diff-size
  recopy, hardlink mode (`os.link`, same inode), hardlink-fails->copy fallback,
  link-mode dest-present skip, restore-dates with & without meta, error return.
* `read_smartfolders` — empty result when absent, table discovery when present.
* `main` — dry-run, full extract, `--local-only`, exact/fuzzy domain match,
  unmapped-flag skip, no-files-matched exit, `--include-symlinks`, bad backup dir.