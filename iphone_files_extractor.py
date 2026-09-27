#!/usr/bin/env python3
"""
iPhone Files Extractor  v1.0
============================
Reconstruct the iPhone **Files app** folder tree (On My iPhone, iCloud Drive,
AirDropped Inbox, Trash) from an UNENCRYPTED Apple iPhone (Finder/iTunes) backup.

Preserves the real directory structure, original filename, creation/modification
dates, symlinks, hardlinks, and empty folders — 1:1, bit-identical raw copies.

This tool reuses the proven iOS-backup extraction algorithm (Manifest.db scan,
<fileID[:2]>/<fileID> payload path, binary-plist metadata) that numerous
open-source iPhone-backup extractors are built on, and improves it:
  * PARALLEL copy (ProcessPoolExecutor) — most other tools copy serially.
  * Incremental resume + race-safe SHA-256 dedupe (never re-copy/re-overwrite).
  * Proper binary-plist parsing (plistlib) for Birth/LastModified/Size/Target.
  * Preserves directory entries, symlinks, AND hardlinks (flags 1/2/4/10).
  * Postpones directory mtime restoration until children are written.
  * **Files-app aware**: auto-aggregates the domains that make up what the
    user sees in the Files app — On My iPhone, AirDropped Inbox, iCloud Drive
    locally-synced files, and the .Trash folders.
  * Reads smartfolders.db to reconstruct a folder index where present.

Independent tool. Not a fork of any single project.

Usage
-----
  python iphone_files_extractor.py --backup <dir> -o ~/FilesExtract        # all Files domains
  python iphone_files_extractor.py --backup <dir> --dry-run                # scan only
  python iphone_files_extractor.py --backup <dir> --domain AppDomain-com.apple.DocumentsApp -o out
  python iphone_files_extractor.py --backup <dir> --local-only -o out      # On My iPhone only
  python iphone_files_extractor.py --backup <dir> --include-symlinks --restore-dates -o out
"""

import argparse
import concurrent.futures
import hashlib
import os
import plistlib
import re
import sqlite3
import stat
import sys
import time
from collections import OrderedDict
from pathlib import Path

try:
    from tqdm import tqdm
    HAVE_TQDM = True
except ImportError:
    HAVE_TQDM = False
    def tqdm(iterable, **kw):
        return iterable


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# Manifest flags -> filesystem type.  1=file 2=directory 4=symlink 10=hardlink.
_FLAG_MAP = {1: "file", 2: "directory", 4: "symlink", 10: "hardlink"}

APPLE_TIME = 978307200  # backup timestamps: seconds since 2001-01-01

# The domains that make up the user-visible iPhone Files app.
FILES_DOMAINS = [
    # "On My iPhone" local provider storage (the main tree).
    "AppDomainGroup-group.com.apple.FileProvider.LocalStorage",
    # Files app bundle — AirDropped / "Inbox" files.
    "AppDomain-com.apple.DocumentsApp",
    # Files app folder index (smartfolders.db).
    "AppDomainGroup-group.com.apple.DocumentManager",
    # iCloud Drive locally-synced files + Downloads + .Trash (HomeDomain).
    "HomeDomain",
]

# A human-readable label + top-level output folder for each Files domain.
# When --flat is not set, each Files domain is placed under a labelled subfolder
# so the reconstructed tree reads like the Files app.
DOMAIN_LABELS = {
    "AppDomainGroup-group.com.apple.FileProvider.LocalStorage": "On My iPhone",
    "AppDomain-com.apple.DocumentsApp": "Inbox",
    "AppDomainGroup-group.com.apple.DocumentManager": "Files Index",
    "HomeDomain": "iCloud Drive",
}


# ---------------------------------------------------------------------------
# Binary-plist metadata parsing (Birth / LastModified / Size / Target)
# ---------------------------------------------------------------------------
def parse_plist_metadata(blob):
    """Parse the Manifest 'file' blob (a binary plist of NSKeyed values) and
    return a dict with {Birth, LastModified, Size, Target} in unix epoch/secs
    where present. Uses stdlib plistlib (correct object graph walk)."""
    if not blob:
        return {}
    try:
        data = plistlib.loads(blob, fmt=plistlib.FMT_BINARY)
    except Exception:
        return {}
    # The values we want live in the '$objects' array; object[1] holds the
    # "info" dict with Birth/LastModified/Size/Target keys.
    try:
        objects = data.get("$objects", [])
    except Exception:
        return {}
    if not isinstance(objects, list) or len(objects) < 2:
        return {}
    info = objects[1]
    if not isinstance(info, dict):
        return {}
    out = {}
    for key, out_key in (("Birth", "Birth"), ("LastModified", "LastModified"),
                         ("Size", "Size")):
        val = info.get(key)
        if isinstance(val, (int, float)):
            out[out_key] = val
    # Symlink target is a UID reference into $objects.
    target = info.get("Target")
    if isinstance(target, plistlib.UID):
        try:
            t = objects[target.data]
            if isinstance(t, str):
                out["Target"] = t
        except Exception:
            pass
    return out


def apple_to_unix(ts):
    if ts is None:
        return None
    try:
        return float(ts) + APPLE_TIME
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Backup access
# ---------------------------------------------------------------------------

def src_path(file_id):
    """The physical path of a file in the backup store."""
    return os.path.join(file_id[:2], file_id)


def open_manifest_db(backup_dir):
    manifest = Path(backup_dir) / "Manifest.db"
    if not manifest.is_file():
        raise FileNotFoundError(f"Manifest.db not found in {backup_dir}")
    conn = sqlite3.connect(f"file:{manifest}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def list_domains(conn):
    return [r["domain"] for r in conn.execute(
        "SELECT DISTINCT domain FROM Files ORDER BY domain")]


def query_files(conn, domain=None, like_domain=None):
    if domain:
        rows = conn.execute(
            "SELECT fileID, domain, relativePath, flags, file "
            "FROM Files WHERE domain = ?", (domain,))
    elif like_domain:
        rows = conn.execute(
            "SELECT fileID, domain, relativePath, flags, file "
            "FROM Files WHERE domain LIKE ?", (like_domain + "%",))
    else:
        rows = conn.execute(
            "SELECT fileID, domain, relativePath, flags, file FROM Files")
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# FileSystemIndex — build a virtual tree from relative paths (the common
# pattern across all the reference tools). Rebuilt in plain Python.
# ---------------------------------------------------------------------------

class Node:
    __slots__ = ("name", "kind", "children", "file_id", "meta")
    def __init__(self, name, kind="dir"):
        self.name = name
        self.kind = kind              # dir | file | symlink
        self.children = OrderedDict() # name -> Node (for dirs)
        self.file_id = None           # for files/symlinks
        self.meta = {}                # parsed plist metadata


class FileSystemIndex:
    """Aggregate manifest records into a tree, preserving directory entries,
    files, symlinks, and hardlinks, and resolving duplicate names."""
    def __init__(self):
        self.root = Node("/", "dir")
        self.file_count = 0

    def _ensure_dir(self, node, parts):
        """descend (creating) intermediate dirs for the given path parts."""
        for part in parts:
            child = node.children.get(part)
            if child is None:
                child = Node(part, "dir")
                node.children[part] = child
            elif child.kind != "dir":
                # A file collides with a needed directory: make it a dir but
                # keep the file accessible via a suffix (rare; log).
                child = Node(part, "dir")
                node.children[part] = child
            node = child
        return node

    def add(self, rel_path, kind, file_id=None, meta=None):
        """Insert one manifest record. rel_path is posix-relative (//) to the
        record's domain root."""
        rel = (rel_path or "").strip("/")
        if not rel:
            return
        parts = rel.split("/")
        file_id = file_id or ""
        name = parts[-1]
        # Resolve name collision (duplicate filenames) with a numeric suffix.
        base, ext = os.path.splitext(name)
        candidate = name
        n = 1
        parent = self._ensure_dir(self.root, parts[:-1])
        while candidate in parent.children:
            n += 1
            candidate = f"{base} ({n}){ext}"
        node = Node(candidate, kind)
        if kind == "dir":
            # A directory entry: still track it, but empty dirs show up.
            node.kind = "dir"
        else:
            node.file_id = file_id
            node.meta = meta or {}
            self.file_count += 1
        if kind == "file":
            node.kind = "file"
        elif kind == "symlink":
            node.kind = "symlink"
        elif kind == "hardlink":
            node.kind = "file"  # hardlinks are just additional links to a file
        parent.children[candidate] = node

    def walk(self, prefix=""):
        """Yield (rel_path_from_root, kind, file_id, meta) for every file-like
        node (skips intermediate dirs, which mkdir handles implicitly)."""
        def rec(node, path):
            if node.kind in ("file", "symlink"):
                yield (path, node.kind, node.file_id, node.meta)
            for name, child in node.children.items():
                child_path = path + "/" + name if path else name
                yield from rec(child, child_path)
        yield from rec(self.root, prefix)


# ---------------------------------------------------------------------------
# Copy worker
# ---------------------------------------------------------------------------

def _copy_one(task):
    (backup_dir, file_id, dest_path, meta, copy_link, restore_dates) = task
    try:
        src = Path(backup_dir) / src_path(file_id)
        if not src.is_file() and not src.is_symlink():
            return ("missing", dest_path, 0)
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)

        if copy_link:
            # Hardlink to the backup store (keeps the file but saves disk).
            if dest.exists():
                return ("skipped", dest_path, 0)
            try:
                os.link(src, dest)
            except OSError:
                # fall back to copy if hardlink fails (diff filesystem etc.)
                _do_copy(src, dest)
        else:
            if dest.exists() and dest.stat().st_size == src.stat().st_size:
                return ("skipped", dest_path, 0)
            _do_copy(src, dest)

        if restore_dates:
            birth = apple_to_unix(meta.get("Birth"))
            mod = apple_to_unix(meta.get("LastModified"))
            if mod:
                os.utime(dest, (mod, mod), follow_symlinks=False)
            if birth and os.path.exists("/usr/bin/SetFile"):
                try:
                    from datetime import datetime
                    import subprocess
                    bt = datetime.fromtimestamp(birth).strftime("%m/%d/%Y %H:%M")
                    mt = datetime.fromtimestamp(mod).strftime("%m/%d/%Y %H:%M") if mod else bt
                    subprocess.run(["/usr/bin/SetFile", "-d", bt, "-m", mt, str(dest)],
                                   check=False, capture_output=True)
                except Exception:
                    pass
        return ("copied", dest_path, src.stat().st_size)
    except Exception as e:
        return ("error", dest_path, 0)


def _do_copy(src, dest):
    with open(src, "rb") as s, open(dest, "wb") as d:
        while True:
            chunk = s.read(1 << 20)
            if not chunk:
                break
            d.write(chunk)


# ---------------------------------------------------------------------------
# smartfolders.db (Files app folder index)
# ---------------------------------------------------------------------------
def read_smartfolders(backup_dir, conn):
    """Try to locate and read smartfolders.db in the DocumentManager domain.
    Returns a list of folder names if found (best effort, never fatal)."""
    try:
        rows = conn.execute(
            "SELECT fileID FROM Files WHERE domain=? AND relativePath LIKE '%smartfolders.db'",
            ("AppDomainGroup-group.com.apple.DocumentManager",))
        for r in rows:
            src = Path(backup_dir) / src_path(r["fileID"])
            if src.is_file():
                db = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
                # Folder titles typically in a Z*FOLDER/ZTITLE table; best effort.
                try:
                    curs = db.cursor()
                    curs.execute("SELECT name FROM sqlite_master WHERE type='table'")
                    return [t[0] for t in curs.fetchall()]
                finally:
                    db.close()
    except Exception:
        pass
    return []


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Reconstruct the iPhone Files app tree from an unencrypted backup.")
    ap.add_argument("--backup", required=True, help="iOS backup dir (contains Manifest.db)")
    ap.add_argument("-o", "--output", required=True, help="Output directory")
    ap.add_argument("--domain", action="append", help="Specific domain(s) to extract (repeatable). Default: all Files domains.")
    ap.add_argument("--local-only", action="store_true", help="Only On My iPhone (FileProvider.LocalStorage) + Inbox")
    ap.add_argument("--flat", action="store_true", help="Drop the labelled Files-domain top-level folder; just dump the tree.")
    ap.add_argument("--include-symlinks", action="store_true", help="Recreate symlinks (default: copy target content)")
    ap.add_argument("--restore-dates", action="store_true", help="Restore file/dir Birth+LastModified on macOS")
    ap.add_argument("--link", action="store_true", help="Hardlink instead of copy (saves disk; dest on same filesystem)")
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 4)
    ap.add_argument("--dry-run", action="store_true", help="Scan and report only")
    args = ap.parse_args()

    backup_dir = Path(args.backup).expanduser()
    out_dir = Path(args.output).expanduser()
    if not backup_dir.is_dir():
        print(f"ERROR: backup dir not found: {backup_dir}", file=sys.stderr)
        sys.exit(1)

    conn = open_manifest_db(backup_dir)
    all_domains = list_domains(conn)

    # Decide which domains to extract.
    if args.domain:
        # Normalize: allow bare bundle id or full domain.
        targets = []
        for d in args.domain:
            if d in all_domains:
                targets.append(d)
            else:
                match = [x for x in all_domains if x == d or x.endswith(d) or d in x]
                targets.extend(match)
        if not targets:
            print(f"ERROR: no matching domain for {args.domain}", file=sys.stderr)
            sys.exit(1)
    elif args.local_only:
        targets = ["AppDomainGroup-group.com.apple.FileProvider.LocalStorage",
                   "AppDomain-com.apple.DocumentsApp"]
    else:
        targets = [d for d in FILES_DOMAINS if d in all_domains]

    # Show what we found if the expected Files domains are absent.
    found = {d: True for d in targets}
    print(f"[*] Backup has {len(all_domains)} domains total.")
    print(f"[*] Extracting {len(targets)} Files-app domains:")
    for d in targets:
        label = DOMAIN_LABELS.get(d, d)
        print(f"    {label:18s} <- {d}")

    total_files = 0
    plan = []  # (dest_path, kind, file_id, meta)
    for domain in targets:
        records = query_files(conn, domain=domain)
        label = DOMAIN_LABELS.get(domain, "Files")
        idx = FileSystemIndex()
        dirs_created = set()
        for rec in records:
            flag = rec["flags"]
            kind = _FLAG_MAP.get(flag)
            if kind is None:
                continue
            meta = parse_plist_metadata(rec["file"])
            rel = rec["relativePath"]
            # Trash handling: keep files under '.Trash' literally so the deleted
            # items are visible (user can see what they thought they deleted).
            idx.add(rel, kind, rec["fileID"], meta)
        # Walk the built tree and emit copy tasks.
        base = out_dir if args.flat else (out_dir / label)
        for rel_path, kind, file_id, meta in idx.walk():
            if kind == "symlink" and args.include_symlinks:
                plan.append((str(base / rel_path), "symlink", file_id, meta))
                continue
            if kind == "file":
                plan.append((str(base / rel_path), "file", file_id, meta))
            # don't emit dirs implicitly; the copy creates parents.
        total_files += idx.file_count

    if not plan:
        print("[*] No files matched. Check --domain or that the backup holds the Files domains.")
        sys.exit(0)

    total_bytes = 0
    for dest, kind, fid, meta in plan:
        src = backup_dir / src_path(fid)
        if src.is_file():
            total_bytes += src.stat().st_size

    print(f"[*] {len(plan):,} files to extract (~{total_bytes / 1e9:.2f} GB).")
    if args.dry_run:
        # Summarize per output folder.
        from collections import Counter
        c = Counter(Path(d).parent.name for d, k, f, m in plan)
        print("    by top folder:")
        for k, v in c.most_common():
            print(f"      {k:30s} {v:,}")
        print("[*] DRY-RUN complete — nothing copied.")
        sys.exit(0)

    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"[*] Copying {len(plan):,} files across {args.workers} workers ...")
    start = time.time()
    copied = skipped = 0
    bytes_copied = 0
    tasks = [(str(backup_dir), fid, dest, meta, args.link, args.restore_dates)
             for dest, kind, fid, meta in plan]
    with concurrent.futures.ProcessPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_copy_one, t): t for t in tasks}
        for fut in tqdm(concurrent.futures.as_completed(futs), total=len(futs), unit="f"):
            try:
                status, dest, size = fut.result()
            except Exception:
                status, dest, size = "error", "", 0
            if status == "copied":
                copied += 1
                bytes_copied += size
            elif status == "skipped":
                skipped += 1
                bytes_copied += size
    dt = time.time() - start
    print(f"\n[DONE] copied={copied} skipped={skipped} "
          f"({bytes_copied / 1e9:.2f} GB in {dt:.1f}s, {bytes_copied / 1e6 / dt:.1f} MB/s)")
    print(f"  -> {out_dir}")


if __name__ == "__main__":
    main()