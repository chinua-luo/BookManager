from __future__ import annotations

import errno
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bookmanager.exporting import STAGING_PREFIX, ExportProblem, export_folder
from bookmanager.naming import StructuredFileName, build_structured_file_name
from bookmanager.store import LibraryStore


def _parts(
    main_title: str = "Main Title",
    *,
    series_abbr: str = "ABC",
    number: str = "7",
    subtitle: str = "Sub",
    edition: str = "2",
    authors: str = "Author",
    extension: str = ".txt",
) -> StructuredFileName:
    return StructuredFileName(
        series_abbr=series_abbr,
        number=number,
        main_title=main_title,
        subtitle=subtitle,
        edition=edition,
        authors=authors,
        extension=extension,
    )


class ExportingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.env = mock.patch.dict(os.environ, {"BOOKMANAGER_DIAGNOSTIC_HOME": str(self.root / "diag")})
        self.env.start()
        self.store = LibraryStore(self.root / "data")
        self.out = self.root / "out"
        self.out.mkdir()
        self.series = self.store.create_folder(1, "Series")
        self.volume = self.store.create_folder(self.series, "Volume")
        self.empty = self.store.create_folder(self.volume, "Empty")
        self._sources = 0

    def tearDown(self) -> None:
        self.store.close()
        self.env.stop()
        self.temporary.cleanup()

    # -- helpers ---------------------------------------------------------
    def _source(self, content: str) -> Path:
        self._sources += 1
        path = self.root / "incoming" / f"source-{self._sources}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def _add(
        self,
        folder_id: int,
        content: str,
        parts: StructuredFileName | None = None,
    ) -> int:
        return self.store.add_local_file(self._source(content), folder_id, parts or _parts())

    def _staging_leftovers(self, directory: Path) -> list[str]:
        return sorted(entry.name for entry in directory.iterdir() if entry.name.startswith(STAGING_PREFIX))

    def _diagnostic_text(self) -> str:
        logs = self.store.home / "logs"
        if not logs.is_dir():
            return ""
        return "\n".join(path.read_text(encoding="utf-8") for path in logs.glob("*.jsonl"))

    # -- snapshot --------------------------------------------------------
    def test_snapshot_carries_tree_blobs_and_missing_files(self) -> None:
        item_id = self._add(self.volume, "volume content")
        item = self.store.get_item(item_id)

        snapshot = self.store.folder_export_snapshot(self.series)

        self.assertEqual(snapshot.folder.id, self.series)
        self.assertEqual([entry.relative_parts for entry in snapshot.folders], [("Volume",), ("Volume", "Empty")])
        self.assertEqual([entry.name for entry in snapshot.folders], ["Volume", "Empty"])
        self.assertEqual(snapshot.missing_files, ())
        self.assertEqual(len(snapshot.items), 1)
        entry = snapshot.items[0]
        self.assertEqual(entry.folder_id, self.volume)
        self.assertEqual(entry.relative_parts, ("Volume",))
        self.assertEqual(entry.stored_path, item.stored_path)
        self.assertEqual(entry.size, item.size)
        self.assertEqual(self.store.folder_stats(self.series), snapshot.stats)

        item.stored_path.unlink()
        after = self.store.folder_export_snapshot(self.series)
        self.assertEqual(after.missing_files, (item.stored_path,))

    def test_snapshot_unknown_folder_keeps_existing_error(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self.store.folder_export_snapshot(9999)
        self.assertIn("does not exist", str(caught.exception))

    # -- success paths ---------------------------------------------------
    def test_export_success_copies_files_and_reports_counts(self) -> None:
        item_id = self._add(self.series, "alpha content")
        item = self.store.get_item(item_id)

        result = export_folder(self.store, self.series, self.out)

        # The export reports the resolved target, so a symlinked temp dir such as
        # macOS /var -> /private/var compares consistently.
        target = self.out.resolve() / "Series"
        self.assertEqual(result.target_path, target)
        self.assertEqual(result.file_count, 1)
        self.assertEqual(result.folder_count, 2)
        self.assertEqual(result.copied_bytes, item.size)
        copied = target / build_structured_file_name(_parts())
        self.assertTrue(copied.is_file())
        self.assertEqual(copied.read_text(encoding="utf-8"), "alpha content")
        self.assertEqual(self._staging_leftovers(self.out), [])

    def test_export_mirrors_multi_level_and_empty_folders(self) -> None:
        self._add(self.volume, "nested content")
        deep = self.store.create_folder(self.empty, "Deep")

        result = export_folder(self.store, self.series, self.out)

        target = self.out / "Series"
        self.assertTrue((target / "Volume").is_dir())
        self.assertTrue((target / "Volume" / "Empty").is_dir())
        self.assertTrue((target / "Volume" / "Empty" / "Deep").is_dir())
        self.assertEqual(list((target / "Volume" / "Empty" / "Deep").iterdir()), [])
        self.assertEqual(result.folder_count, 3)
        self.assertTrue((target / "Volume" / build_structured_file_name(_parts())).is_file())

    def test_export_preserves_tree_without_any_files(self) -> None:
        self.store.create_folder(self.empty, "Deep")

        result = export_folder(self.store, self.series, self.out)

        target = self.out / "Series"
        self.assertTrue((target / "Volume" / "Empty" / "Deep").is_dir())
        self.assertEqual(result.file_count, 0)
        self.assertEqual(result.folder_count, 3)
        self.assertEqual(result.copied_bytes, 0)
        self.assertEqual(self._staging_leftovers(self.out), [])

    def test_export_uses_full_structured_name(self) -> None:
        parts = _parts(
            "Deep Learning",
            series_abbr="dl",
            number="12",
            subtitle="Second Sub",
            edition="3",
            authors="Goodfellow",
            extension=".pdf",
        )
        source = self.root / "incoming" / "deep learning.pdf"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"%PDF-1.4 fake")
        self.store.add_local_file(source, self.series, parts)

        export_folder(self.store, self.series, self.out)

        expected = build_structured_file_name(parts)
        self.assertEqual(expected, "DL012 Deep Learning - Second Sub - 3rd Edition _ Goodfellow.pdf")
        self.assertTrue((self.out / "Series" / expected).is_file())

    def test_export_suffixes_duplicate_file_names(self) -> None:
        # Two items per folder whose rendered names differ only by case collide on
        # a case-insensitive filesystem, so the second must get a numbered suffix.
        self._add(self.series, "upper", _parts("Alpha"))
        self._add(self.series, "lower", _parts("alpha"))

        export_folder(self.store, self.series, self.out)

        target = self.out / "Series"
        base = build_structured_file_name(_parts("Alpha"))
        second = build_structured_file_name(_parts("alpha"))
        self.assertTrue((target / base).is_file())
        self.assertTrue((target / f"{Path(second).stem} (2){Path(second).suffix}").is_file(), list(target.iterdir()))

    def test_export_falls_back_to_display_name_for_invalid_parts(self) -> None:
        item_id = self._add(self.series, "legacy content")
        self.store.conn.execute(
            "UPDATE items SET name_data = ?, display_name = ? WHERE id = ?",
            (
                '{"series_abbr": "", "number": "", "main_title": "", "subtitle": "", '
                '"edition": "1", "authors": "", "extension": ""}',
                "Legacy:Name",
                item_id,
            ),
        )
        self.store.conn.commit()

        export_folder(self.store, self.series, self.out)

        # The fallback sanitizes characters that are illegal in file names and
        # appends the stored blob suffix exactly once.
        self.assertTrue((self.out / "Series" / "Legacy_Name.txt").is_file(), list((self.out / "Series").iterdir()))

    def test_export_fallback_does_not_double_the_suffix(self) -> None:
        item_id = self._add(self.series, "legacy content")
        self.store.conn.execute(
            "UPDATE items SET name_data = ?, display_name = ? WHERE id = ?",
            (
                '{"series_abbr": "", "number": "", "main_title": "", "subtitle": "", '
                '"edition": "1", "authors": "", "extension": ""}',
                "Legacy.txt",
                item_id,
            ),
        )
        self.store.conn.commit()

        export_folder(self.store, self.series, self.out)

        self.assertTrue((self.out / "Series" / "Legacy.txt").is_file())

    def test_export_copies_each_mirrored_item_once(self) -> None:
        item_id = self._add(self.series, "shared")
        self.store.mirror_item(item_id, self.volume)
        extra = self.store.create_folder(self.series, "Extra")
        self.store.mirror_item(item_id, extra)

        result = export_folder(self.store, self.series, self.out)

        target = self.out / "Series"
        base = build_structured_file_name(_parts())
        self.assertTrue((target / base).is_file())
        self.assertTrue((target / "Volume" / base).is_file())
        self.assertTrue((target / "Extra" / base).is_file())
        self.assertEqual(result.file_count, 3)
        self.assertEqual(result.copied_bytes, self.store.get_item(item_id).size * 3)

    # -- conflict and preflight -----------------------------------------
    def test_export_refuses_existing_target_folder(self) -> None:
        self._add(self.series, "content")
        existing = self.out / "Series"
        existing.mkdir()
        marker = existing / "keep.txt"
        marker.write_text("keep", encoding="utf-8")

        with self.assertRaises(ExportProblem) as caught:
            export_folder(self.store, self.series, self.out)

        error = caught.exception
        self.assertEqual(error.export_error_type, "export_conflict")
        self.assertEqual(error.http_status, 409)
        self.assertIn("同名", str(error))
        self.assertTrue(marker.is_file())
        self.assertEqual(self._staging_leftovers(self.out), [])

    def test_export_missing_unknown_target_folder(self) -> None:
        self._add(self.series, "content")
        with self.assertRaises(ExportProblem) as caught:
            export_folder(self.store, self.series, "")
        self.assertEqual(caught.exception.export_error_type, "export_target_invalid")
        self.assertEqual(caught.exception.http_status, 400)

        with self.assertRaises(ExportProblem) as caught:
            export_folder(self.store, self.series, self.root / "does-not-exist")
        self.assertEqual(caught.exception.export_error_type, "export_target_invalid")
        self.assertEqual(caught.exception.http_status, 400)

    def test_export_refuses_target_inside_library_home(self) -> None:
        self._add(self.series, "content")
        for parent in (self.store.home, self.store.home / "exports"):
            if parent.name == "exports":
                parent.mkdir(parents=True, exist_ok=True)
            with self.assertRaises(ExportProblem) as caught:
                export_folder(self.store, self.series, parent)
            self.assertEqual(caught.exception.export_error_type, "export_target_invalid")
            self.assertEqual(caught.exception.http_status, 400)
            self.assertFalse((parent / "Series").exists())

    def test_export_refuses_target_that_is_a_file(self) -> None:
        self._add(self.series, "content")
        parent = self.root / "plain-file"
        parent.write_text("not a directory", encoding="utf-8")

        with self.assertRaises(ExportProblem) as caught:
            export_folder(self.store, self.series, parent)

        self.assertEqual(caught.exception.export_error_type, "export_target_invalid")

    def test_export_refuses_folder_name_traversal(self) -> None:
        self._add(self.volume, "content")
        self.store.conn.execute("UPDATE folders SET name = ? WHERE id = ?", ("..", self.empty))
        self.store.conn.commit()

        with self.assertRaises(ExportProblem) as caught:
            export_folder(self.store, self.series, self.out)

        self.assertEqual(caught.exception.export_error_type, "export_failed")
        self.assertFalse((self.out / "Series").exists())
        self.assertEqual(self._staging_leftovers(self.out), [])

    def test_export_refuses_source_name_with_traversal(self) -> None:
        self._add(self.series, "content")
        self.store.conn.execute("UPDATE folders SET name = ? WHERE id = ?", ("..", self.series))
        self.store.conn.commit()

        with self.assertRaises(ExportProblem) as caught:
            export_folder(self.store, self.series, self.out)

        self.assertEqual(caught.exception.export_error_type, "export_failed")
        self.assertEqual(self._staging_leftovers(self.out), [])

    @unittest.skipIf(os.name == "nt", "Windows symlink creation requires optional privileges")
    def test_export_refuses_symlink_named_target(self) -> None:
        self._add(self.series, "content")
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        (self.out / "Series").symlink_to(elsewhere, target_is_directory=True)

        with self.assertRaises(ExportProblem) as caught:
            export_folder(self.store, self.series, self.out)

        self.assertEqual(caught.exception.export_error_type, "export_conflict")
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_export_missing_source_blob(self) -> None:
        item_id = self._add(self.series, "vanishing")
        self.store.get_item(item_id).stored_path.unlink()

        with self.assertRaises(ExportProblem) as caught:
            export_folder(self.store, self.series, self.out)

        error = caught.exception
        self.assertEqual(error.export_error_type, "export_source_missing")
        self.assertEqual(error.http_status, 400)
        self.assertIn("缺失", str(error))
        self.assertNotIn(str(self.root), str(error))
        self.assertFalse((self.out / "Series").exists())
        self.assertEqual(self._staging_leftovers(self.out), [])

    @unittest.skipIf(os.name == "nt", "Windows symlink creation requires optional privileges")
    def test_export_refuses_symlinked_source_blob(self) -> None:
        item_id = self._add(self.series, "linked")
        stored_path = self.store.get_item(item_id).stored_path
        real = self.root / "real.txt"
        real.write_text("real content", encoding="utf-8")
        stored_path.unlink()
        stored_path.symlink_to(real)

        with self.assertRaises(ExportProblem) as caught:
            export_folder(self.store, self.series, self.out)

        self.assertEqual(caught.exception.export_error_type, "export_source_missing")
        self.assertIn("软链接", str(caught.exception))
        self.assertFalse((self.out / "Series").exists())

    def test_export_unknown_folder_is_missing(self) -> None:
        with self.assertRaises(ExportProblem) as caught:
            export_folder(self.store, 424242, self.out)
        self.assertEqual(caught.exception.export_error_type, "export_folder_missing")
        self.assertEqual(caught.exception.http_status, 404)

    # -- failure handling ------------------------------------------------
    def test_export_cleans_staging_after_unexpected_failure(self) -> None:
        self._add(self.series, "content")

        with mock.patch("bookmanager.exporting.clone_or_copy", side_effect=RuntimeError("boom")):
            with self.assertRaises(ExportProblem) as caught:
                export_folder(self.store, self.series, self.out)

        self.assertEqual(caught.exception.export_error_type, "export_failed")
        self.assertEqual(caught.exception.http_status, 500)
        self.assertNotIn(str(self.root), str(caught.exception))
        self.assertEqual(self._staging_leftovers(self.out), [])
        self.assertFalse((self.out / "Series").exists())
        self.assertIn("export.failed", self._diagnostic_text())

    def test_export_keeps_disk_full_error_for_response_mapping(self) -> None:
        self._add(self.series, "content")

        with mock.patch(
            "bookmanager.exporting.clone_or_copy",
            side_effect=OSError(errno.ENOSPC, "No space left on device"),
        ):
            with self.assertRaises(OSError) as caught:
                export_folder(self.store, self.series, self.out)

        self.assertEqual(caught.exception.errno, errno.ENOSPC)
        self.assertNotIsInstance(caught.exception, ExportProblem)
        self.assertEqual(self._staging_leftovers(self.out), [])

    def test_export_keeps_permission_error_for_response_mapping(self) -> None:
        self._add(self.series, "content")

        with mock.patch(
            "bookmanager.exporting.clone_or_copy",
            side_effect=PermissionError(errno.EACCES, "Permission denied"),
        ):
            with self.assertRaises(PermissionError):
                export_folder(self.store, self.series, self.out)

        self.assertEqual(self._staging_leftovers(self.out), [])

    def test_export_survives_staging_cleanup_failure(self) -> None:
        self._add(self.series, "content")

        with mock.patch("bookmanager.exporting.clone_or_copy", side_effect=RuntimeError("boom")):
            with mock.patch("bookmanager.exporting.shutil.rmtree", side_effect=OSError(errno.EACCES, "denied")):
                with self.assertRaises(ExportProblem) as caught:
                    export_folder(self.store, self.series, self.out)

        self.assertEqual(caught.exception.export_error_type, "export_failed")
        self.assertIn("export.cleanup_failed", self._diagnostic_text())

    @unittest.skipIf(not hasattr(os, "geteuid") or os.geteuid() == 0, "root/windows ignores directory write bits")
    def test_export_refuses_unwritable_target(self) -> None:
        self._add(self.series, "content")
        locked = self.root / "locked"
        locked.mkdir()
        locked.chmod(0o500)
        try:
            with self.assertRaises(ExportProblem) as caught:
                export_folder(self.store, self.series, locked)
        finally:
            locked.chmod(0o700)

        self.assertEqual(caught.exception.export_error_type, "export_target_invalid")


if __name__ == "__main__":
    unittest.main()
