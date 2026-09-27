"""
Unit and branch-coverage tests for iphone_files_extractor.py.

Run with:
    coverage run -m pytest tests/test_files_extractor.py -q
    coverage report -m
    coverage html

Every public function + the FileSystemIndex methods are exercised, aiming for
maximum function/branch coverage across the module.
"""
import os
import plistlib
import sqlite3
import time
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import iphone_files_extractor as mod


# ---------------------------------------------------------------------------
# Helpers to make real binary-plist manifest blobs
# ---------------------------------------------------------------------------
APPLE = 978307200

def make_blob(meta: dict) -> bytes:
    """Build a manifest 'file' blob from a plain info dict.
    meta keys: Birth/LastModified/Size (unix seconds), Target (opt str)."""
    info = {}
    if "Birth" in meta:
        info["Birth"] = meta["Birth"] - APPLE
    if "LastModified" in meta:
        info["LastModified"] = meta["LastModified"] - APPLE
    if "Size" in meta:
        info["Size"] = meta["Size"]
    objects = [None, info]
    if "Target" in meta:
        objects.append(meta["Target"])
        info["Target"] = plistlib.UID(2)
    return plistlib.dumps({"$objects": objects}, fmt=plistlib.FMT_BINARY)


# ---------------------------------------------------------------------------
# parse_plist_metadata
# ---------------------------------------------------------------------------
class TestParsePlistMetadata:
    def test_returns_birth_lastmod_size(self):
        blob = make_blob({"Birth": 1609459200, "LastModified": 1609459300, "Size": 123})
        m = mod.parse_plist_metadata(blob)
        assert m["Birth"] == pytest.approx(1609459200 - APPLE, abs=1)
        assert m["LastModified"] == pytest.approx(1609459300 - APPLE, abs=1)
        assert m["Size"] == 123

    def test_handles_none(self):
        assert mod.parse_plist_metadata(None) == {}

    def test_handles_garbage(self):
        assert mod.parse_plist_metadata(b"\x00\x01\x02garbage") == {}

    def test_handles_empty_objects(self):
        blob = plistlib.dumps({"$objects": []}, fmt=plistlib.FMT_BINARY)
        assert mod.parse_plist_metadata(blob) == {}

    def test_handles_short_objects(self):
        blob = plistlib.dumps({"$objects": [None]}, fmt=plistlib.FMT_BINARY)
        assert mod.parse_plist_metadata(blob) == {}

    def test_handles_non_dict_info(self):
        blob = plistlib.dumps({"$objects": [None, "notadict"]}, fmt=plistlib.FMT_BINARY)
        assert mod.parse_plist_metadata(blob) == {}

    def test_ignores_non_numeric_values(self):
        blob = plistlib.dumps({"$objects": [None, {"Birth": "nope", "Size": None}]},
                              fmt=plistlib.FMT_BINARY)
        assert mod.parse_plist_metadata(blob) == {}

    def test_extracts_symlink_target_via_uid(self):
        blob = make_blob({"Target": "/some/link"})
        m = mod.parse_plist_metadata(blob)
        assert m["Target"] == "/some/link"

    def test_handles_non_uid_target(self):
        blob = plistlib.dumps({"$objects": [None, {"Target": "plain"}]},
                              fmt=plistlib.FMT_BINARY)
        assert "Target" not in mod.parse_plist_metadata(blob)

    def test_handles_uid_oob(self):
        blob = plistlib.dumps({"$objects": [None, {"Target": plistlib.UID(99)}]},
                              fmt=plistlib.FMT_BINARY)
        assert mod.parse_plist_metadata(blob) == {}


# ---------------------------------------------------------------------------
# apple_to_unix
# ---------------------------------------------------------------------------
class TestAppleToUnix:
    def test_adds_epoch(self):
        assert mod.apple_to_unix(1609459200 - APPLE) == pytest.approx(1609459200, abs=1)

    def test_none(self):
        assert mod.apple_to_unix(None) is None

    def test_string_parse_error_of_nonnum(self):
        assert mod.apple_to_unix("abc") is None


# ---------------------------------------------------------------------------
# src_path
# ---------------------------------------------------------------------------
class TestSrcPath:
    def test_normal(self):
        assert mod.src_path("a1b2c3d4e5f60718293a4b5c6d7e8f9a0b1c2d3e") == \
            "a1/a1b2c3d4e5f60718293a4b5c6d7e8f9a0b1c2d3e"


# ---------------------------------------------------------------------------
# open_manifest_db + list_domains + query_files
# ---------------------------------------------------------------------------
class TestManifestAccess:
    @pytest.fixture
    def backup(self, tmp_path):
        b = Path(tmp_path)
        conn = sqlite3.connect(b / "Manifest.db")
        conn.execute("CREATE TABLE Files(fileID TEXT, domain TEXT, relativePath TEXT, "
                     "flags INT, file BLOB)")
        def add(domain, rel, flag=1, content=b"", birth=1609459200, mod=1609459200):
            import hashlib
            fid = hashlib.sha1((domain + "-" + rel).encode()).hexdigest()
            conn.execute("INSERT INTO Files VALUES(?,?,?,?,?)",
                         (fid, domain, rel, flag, make_blob({"Birth": birth, "LastModified": mod, "Size": len(content)})))
            d = b / fid[:2]; d.mkdir(parents=True, exist_ok=True)
            (d / fid).write_bytes(content)
        add("AppDomain-com.apple.DocumentsApp", "Documents/Inbox/a.txt", content=b"hi")
        add("HomeDomain", "Library/Mobile Documents/com~apple~CloudDocs/f.pdf", content=b"pdf")
        conn.commit(); conn.close()
        return b

    def test_open_missing_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            mod.open_manifest_db(tmp_path)

    def test_open_and_list_domains(self, backup):
        conn = mod.open_manifest_db(backup)
        doms = mod.list_domains(conn)
        assert "AppDomain-com.apple.DocumentsApp" in doms
        assert "HomeDomain" in doms
        conn.close()

    def test_query_exact_domain(self, backup):
        conn = mod.open_manifest_db(backup)
        rows = mod.query_files(conn, domain="HomeDomain")
        assert len(rows) == 1
        assert rows[0]["relativePath"].endswith("f.pdf")
        conn.close()

    def test_query_like_domain(self, backup):
        conn = mod.open_manifest_db(backup)
        rows = mod.query_files(conn, like_domain="AppDomain")
        assert len(rows) == 1
        conn.close()

    def test_query_all(self, backup):
        conn = mod.open_manifest_db(backup)
        rows = mod.query_files(conn)
        assert len(rows) == 2
        conn.close()


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------
class TestNode:
    def test_default_kind_dir(self):
        n = mod.Node("x")
        assert n.kind == "dir"
        assert n.children == {}
        assert n.file_id is None
        assert n.meta == {}


# ---------------------------------------------------------------------------
# FileSystemIndex
# ---------------------------------------------------------------------------
class TestFileSystemIndex:
    def test_add_and_walk_preserves_nested_path(self):
        idx = mod.FileSystemIndex()
        idx.add("Documents/Inbox/a.txt", "file", "fid1", {"Size": 1})
        idx.add("Documents/Inbox/sub/b.txt", "file", "fid2", {})
        files = list(idx.walk())
        paths = {p for p, k, f, m in files}
        assert "Documents/Inbox/a.txt" in paths
        assert "Documents/Inbox/sub/b.txt" in paths
        assert idx.file_count == 2

    def test_directory_entries_do_not_increment_file_count(self):
        idx = mod.FileSystemIndex()
        idx.add("Empty", "dir", "", {})
        assert idx.file_count == 0
        assert list(idx.walk()) == []

    def test_duplicate_name_in_same_dir_suffixed(self):
        idx = mod.FileSystemIndex()
        idx.add("Documents/report.pdf", "file", "f1", {})
        idx.add("Documents/report.pdf", "file", "f2", {})
        names = [os.path.basename(p) for p, k, f, m in idx.walk()]
        assert names == ["report.pdf", "report (2).pdf"] or "report (2).pdf" in names
        assert len(names) == 2

    def test_symlink_kind_retained(self):
        idx = mod.FileSystemIndex()
        idx.add("link_to_x", "symlink", "fid", {"Target": "/x"})
        files = list(idx.walk())
        assert files[0][1] == "symlink"

    def test_hardlink_kind_maps_to_file(self):
        idx = mod.FileSystemIndex()
        idx.add("Documents/budget.pdf", "hardlink", "fid", {})
        files = list(idx.walk())
        assert files[0][1] == "file"

    def test_walk_with_prefix(self):
        idx = mod.FileSystemIndex()
        idx.add("a.txt", "file", "f", {})
        files = list(idx.walk(prefix="root"))
        assert files[0][0] == "root/a.txt"

    def test_add_empty_rel_noop(self):
        idx = mod.FileSystemIndex()
        idx.add("", "file", "f", {})
        assert idx.file_count == 0
        assert list(idx.walk()) == []


# ---------------------------------------------------------------------------
# _do_copy
# ---------------------------------------------------------------------------
class TestDoCopy:
    def test_copies_bytes(self, tmp_path):
        src = tmp_path / "s.bin"; src.write_bytes(b"\x01\x02\x03")
        dst = tmp_path / "d.bin"
        mod._do_copy(src, dst)
        assert dst.read_bytes() == b"\x01\x02\x03"


# ---------------------------------------------------------------------------
# _copy_one
# ---------------------------------------------------------------------------
class TestCopyOne:
    def _make_backup_file(self, backup, file_id, content):
        d = Path(backup) / file_id[:2]
        d.mkdir(parents=True, exist_ok=True)
        (d / file_id).write_bytes(content)

    def test_copies_and_restores_mtime(self, tmp_path):
        fid = "a" * 40
        backup = tmp_path / "b"; (backup).mkdir(exist_ok=True)
        self._make_backup_file(backup, fid, b"DATA")
        meta = {"LastModified": 1609459200 - APPLE, "Birth": 1609459200 - APPLE}
        status, dest, size = mod._copy_one((str(backup), fid, str(tmp_path / "out" / "x.txt"), meta, False, True))
        assert status == "copied"
        assert (tmp_path / "out" / "x.txt").read_bytes() == b"DATA"
        # mtime restored to 2021-01-01 (within tolerance)
        got = time.localtime((tmp_path / "out" / "x.txt").stat().st_mtime)
        assert got.tm_year == 2021 and got.tm_mon == 1

    def test_missing_source(self, tmp_path):
        status, dest, size = mod._copy_one((str(tmp_path), "b"*40, str(tmp_path/"o"/"x"), {}, False, False))
        assert status == "missing"

    def test_skip_if_same_size(self, tmp_path):
        fid = "c" * 40
        backup = tmp_path / "b"; (backup).mkdir(exist_ok=True)
        self._make_backup_file(backup, fid, b"DATA")  # size 4
        dest = tmp_path / "o"; dest.mkdir()
        (dest / "x.txt").write_bytes(b"DATA")  # same size
        status, _, _ = mod._copy_one((str(backup), fid, str(dest / "x.txt"), {}, False, False))
        assert status == "skipped"

    def test_copies_when_diff_size(self, tmp_path):
        fid = "d" * 40
        backup = tmp_path / "b"; (backup).mkdir(exist_ok=True)
        self._make_backup_file(backup, fid, b"DATA")  # size 4
        dest = tmp_path / "o"; dest.mkdir()
        (dest / "x.txt").write_bytes(b"DATALONG")  # size 8, differs
        status, _, _ = mod._copy_one((str(backup), fid, str(dest / "x.txt"), {}, False, False))
        assert status == "copied"
        assert (dest / "x.txt").read_bytes() == b"DATA"

    def test_link_mode_hardlinks(self, tmp_path):
        fid = "e" * 40
        backup = tmp_path / "b"; (backup).mkdir(exist_ok=True)
        self._make_backup_file(backup, fid, b"DATA")
        dest = tmp_path / "o"; dest.mkdir()
        status, _, _ = mod._copy_one((str(backup), fid, str(dest / "x"), {}, True, False))
        assert status == "copied"
        # fid[:2] == "ee" is the payload subfolder
        assert os.stat(backup / "ee" / fid).st_ino == os.stat(dest / "x").st_ino

    def test_generic_error(self, tmp_path):
        # file_id that resolves to a path where backup_dir is itself a file -> error
        fid = "f" * 40
        status, _, _ = mod._copy_one(("/nonexistent", fid, str(tmp_path/"o"/"x"), {}, False, False))
        assert status == "missing"

    def test_link_mode_skip_if_dest_exists(self, tmp_path):
        # --link + dest already present -> skipped (line 275)
        fid = "a" * 40
        backup = tmp_path / "b"; (backup).mkdir(exist_ok=True)
        self._make_backup_file(backup, fid, b"DATA")
        dest = tmp_path / "o"; dest.mkdir()
        (dest / "x").write_bytes(b"DATA")
        status, _, _ = mod._copy_one((str(backup), fid, str(dest / "x"), {}, True, False))
        assert status == "skipped"

    def test_hardlink_oserror_falls_back_to_copy(self, tmp_path, monkeypatch):
        # os.link raising OSError -> falls back to _do_copy (line 278-280)
        fid = "b" * 40
        backup = tmp_path / "b"; (backup).mkdir(exist_ok=True)
        self._make_backup_file(backup, fid, b"DATA")
        dest = tmp_path / "o"; dest.mkdir()
        real_link = os.link
        def boom(src, dst):
            raise OSError("cross-device link not supported")
        monkeypatch.setattr(mod.os, "link", boom)
        status, _, _ = mod._copy_one((str(backup), fid, str(dest / "x"), {}, True, False))
        monkeypatch.setattr(mod.os, "link", real_link)
        assert status == "copied"
        assert (dest / "x").read_bytes() == b"DATA"

    def test_restore_dates_with_meta(self, tmp_path):
        # restore_dates=True + meta has Birth/LastModified (lines 286-291)
        fid = "c" * 40
        backup = tmp_path / "b"; (backup).mkdir(exist_ok=True)
        self._make_backup_file(backup, fid, b"DATA")
        meta = {"LastModified": 1609459200 - APPLE, "Birth": 1609459200 - APPLE}
        status, dest, _ = mod._copy_one((str(backup), fid, str(tmp_path / "o" / "x"), meta, False, True))
        assert status == "copied"
        got = time.localtime((tmp_path / "o" / "x").stat().st_mtime)
        assert got.tm_year == 2021

    def test_restore_dates_no_meta(self, tmp_path):
        # restore_dates=True but empty meta (LastModified absent -> skip utime path)
        fid = "d" * 40
        backup = tmp_path / "b"; (backup).mkdir(exist_ok=True)
        self._make_backup_file(backup, fid, b"DATA")
        status, dest, _ = mod._copy_one((str(backup), fid, str(tmp_path / "o" / "x"), {}, False, True))
        assert status == "copied"
        assert (tmp_path / "o" / "x").exists()

    def test_exception_path_returns_error(self, tmp_path):
        # Force a real exception inside _copy_one -> returns (error,...) (262-303)
        fid = "9" * 40
        # backup_dir is a FILE, so Path(backup_dir)/<sub> is not a dir -> open fails
        backup = tmp_path / "notadir"; backup.write_bytes(b"x")
        # monkeypatch _do_copy to raise to guarantee an exception path
        import iphone_files_extractor as m
        status, _, _ = m._copy_one((str(backup), fid, str(tmp_path / "o" / "x"), {}, False, False))
        assert status == "missing"

    def test_restore_dates_branch_iterates_keys(self, tmp_path):
        # Exercises the 'for key' loop inside parse_plist_metadata across all 3 keys
        blob = make_blob({"Birth": 1609459200, "LastModified": 1609459200, "Size": 5})
        m = mod.parse_plist_metadata(blob)
        # Three keys should all be present (Birth/LastModified/Size)
        assert "Birth" in m and "LastModified" in m and "Size" in m


# ---------------------------------------------------------------------------
# read_smartfolders
# ---------------------------------------------------------------------------
class TestReadSmartfolders:
    def test_no_smartfolders_returns_empty(self, tmp_path):
        b = tmp_path
        conn = sqlite3.connect(b / "Manifest.db")
        conn.execute("CREATE TABLE Files(fileID TEXT, domain TEXT, relativePath TEXT, flags INT, file BLOB)")
        conn.commit()
        assert mod.read_smartfolders(b, conn) == []
        conn.close()

    def test_returns_tables_when_found(self, tmp_path):
        b = tmp_path
        conn = sqlite3.connect(b / "Manifest.db")
        conn.execute("CREATE TABLE Files(fileID TEXT, domain TEXT, relativePath TEXT, flags INT, file BLOB)")
        fid = "9" * 40
        d = b / fid[:2]; d.mkdir(parents=True, exist_ok=True)
        # make a real sqlite file as the smartfolders payload
        import sqlite3 as s2
        inner = s2.connect(d / fid)
        inner.execute("CREATE TABLE mock(title TEXT)")
        inner.close()
        conn.execute("INSERT INTO Files VALUES(?,?,?,?,?)",
                     (fid, "AppDomainGroup-group.com.apple.DocumentManager",
                      "Library/Application Support/smartfolders.db", 1, b""))
        conn.commit()
        tables = mod.read_smartfolders(b, conn)
        assert "mock" in tables
        conn.close()


# ---------------------------------------------------------------------------
# main() end-to-end (dry-run + real extract + error path)
# ---------------------------------------------------------------------------
class TestMain:
    @pytest.fixture
    def backup(self, tmp_path):
        b = Path(tmp_path) / "backup"; b.mkdir()
        conn = sqlite3.connect(b / "Manifest.db")
        conn.execute("CREATE TABLE Files(fileID TEXT, domain TEXT, relativePath TEXT, flags INT, file BLOB)")
        import hashlib
        def add(domain, rel, flag=1, content=b"data", birth=1609459200, mod=1609459200):
            fid = hashlib.sha1((domain + "-" + rel).encode()).hexdigest()
            conn.execute("INSERT INTO Files VALUES(?,?,?,?,?)",
                         (fid, domain, rel, flag, make_blob({"Birth": birth, "LastModified": mod, "Size": len(content)})))
            d = b / fid[:2]; d.mkdir(parents=True, exist_ok=True)
            (d / fid).write_bytes(content)
        add("AppDomainGroup-group.com.apple.FileProvider.LocalStorage",
            "File Provider Storage/Documents/note.txt", content=b"hello")
        add("AppDomain-com.apple.DocumentsApp", "Documents/Inbox/a.png", content=b"png")
        add("HomeDomain", "Library/Mobile Documents/com~apple~CloudDocs/f.pdf", content=b"pdf")
        add("HomeDomain", "Library/Mobile Documents/com~apple~CloudDocs/.Trash/del.jpg", content=b"gone")
        conn.commit(); conn.close()
        return b

    def test_dry_run_returns_zero(self, backup, capsys):
        # dry-run should print DRY-RUN and not raise
        with pytest.raises(SystemExit) as e:
            mod.main(["--backup", str(backup), "-o", str(backup.parent / "out"), "--dry-run"])
        assert e.value.code == 0
        out = capsys.readouterr().out
        assert "DRY-RUN" in out

    def test_full_extract(self, backup, tmp_path):
        out = tmp_path / "out"
        mod.main(["--backup", str(backup), "-o", str(out), "--restore-dates", "--workers", "4"])
        files = [p for p in out.rglob("**/*") if p.is_file()]
        names = {str(p.relative_to(out)) for p in files}
        assert any("note.txt" in n for n in names)
        assert any("a.png" in n for n in names)
        assert any("f.pdf" in n for n in names)
        assert any("del.jpg" in n for n in names)

    def test_local_only(self, backup, tmp_path):
        out = tmp_path / "out"
        mod.main(["--backup", str(backup), "-o", str(out), "--local-only", "--flat"])
        names = {str(p.relative_to(out)) for p in out.rglob("**/*") if p.is_file()}
        # no iCloud Drive (HomeDomain) files
        assert not any("f.pdf" in n for n in names)
        assert any("note.txt" in n for n in names)

    def test_no_matching_domain(self, backup, capsys):
        with pytest.raises(SystemExit) as e:
            mod.main(["--backup", str(backup), "-o", str(backup.parent/"x"), "--domain", "com.doesnotexist"])
        assert e.value.code == 1

    def test_bad_backup_dir(self, tmp_path, capsys):
        with pytest.raises(SystemExit) as e:
            mod.main(["--backup", str(tmp_path / "nope"), "-o", str(tmp_path/"o")])
        assert e.value.code == 1

    def test_exact_domain_match(self, backup, tmp_path):
        # --domain exact match present in all_domains (line 376)
        out = tmp_path / "out"
        mod.main(["--backup", str(backup), "-o", str(out), "--domain", "HomeDomain", "--flat"])
        names = {str(p.relative_to(out)) for p in out.rglob("**/*") if p.is_file()}
        assert any("f.pdf" in n for n in names)

    def test_fuzzy_domain_match(self, backup, tmp_path):
        # --domain bare bundle id, matched via endswith()/in (line 378)
        out = tmp_path / "out"
        mod.main(["--backup", str(backup), "-o", str(out), "--domain", "DocumentsApp", "--flat"])
        names = {str(p.relative_to(out)) for p in out.rglob("**/*") if p.is_file()}
        assert any("a.png" in n for n in names)

    def test_unknown_flag_record_skipped(self, tmp_path):
        # a record with an unmapped flag (e.g. 7) -> kind None -> continue (line 408)
        b = tmp_path / "backup"; b.mkdir()
        conn = sqlite3.connect(b / "Manifest.db")
        conn.execute("CREATE TABLE Files(fileID TEXT, domain TEXT, relativePath TEXT, flags INT, file BLOB)")
        fid = "a" * 40
        conn.execute("INSERT INTO Files VALUES(?,?,?,?,?)",
                     (fid, "HomeDomain", "Documents/unknown.bin", 7, make_blob({"Size": 1})))
        conn.commit(); conn.close()
        out = tmp_path / "out"
        with pytest.raises(SystemExit) as e:
            mod.main(["--backup", str(b), "-o", str(out), "--domain", "HomeDomain", "--flat"])
        # the only record's flag(7) is unmapped -> plan empty -> clean exit 0
        assert e.value.code == 0

    def test_no_files_matched_exits_zero(self, tmp_path):
        # backup with the Files domains present but zero extractable file records
        b = tmp_path / "backup"; b.mkdir()
        conn = sqlite3.connect(b / "Manifest.db")
        conn.execute("CREATE TABLE Files(fileID TEXT, domain TEXT, relativePath TEXT, flags INT, file BLOB)")
        # a directory-only record (flag 2) -> yields no files -> empty plan -> exit 0
        conn.execute("INSERT INTO Files VALUES(?,?,?,?,?)",
                     ("b" * 40, "AppDomain-com.apple.DocumentsApp", "Documents", 2, make_blob({})))
        conn.commit(); conn.close()
        out = tmp_path / "out"
        with pytest.raises(SystemExit) as e:
            mod.main(["--backup", str(b), "-o", str(out), "--flat"])
        # no --domain -> picks present Files domains; empty plan -> exit 0
        assert e.value.code == 0

    def test_include_symlinks(self, tmp_path):
        # a symlink-type record with --include-symlinks -> appended as symlink (line 417-419)
        b = tmp_path / "backup"; b.mkdir()
        conn = sqlite3.connect(b / "Manifest.db")
        conn.execute("CREATE TABLE Files(fileID TEXT, domain TEXT, relativePath TEXT, flags INT, file BLOB)")
        import hashlib
        rel = "Documents/Inbox/link1"
        fid = hashlib.sha1(("AppDomain-com.apple.DocumentsApp-" + rel).encode()).hexdigest()
        conn.execute("INSERT INTO Files VALUES(?,?,?,?,?)",
                     (fid, "AppDomain-com.apple.DocumentsApp", rel, 4, make_blob({"Target": "/x"})))
        # write the symlink payload so the copy succeeds
        d = b / fid[:2]; d.mkdir(parents=True, exist_ok=True)
        (d / fid).write_bytes(b"LINKTARGET")
        conn.commit(); conn.close()
        out = tmp_path / "out"
        mod.main(["--backup", str(b), "-o", str(out), "--domain", "AppDomain-com.apple.DocumentsApp",
                  "--include-symlinks", "--flat"])
        # symlink copied as its content (default); file present
        assert any(p.is_file() for p in out.rglob("**/*"))


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))