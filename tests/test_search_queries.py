from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bookmanager.naming import StructuredFileName
from bookmanager.store import LibraryStore


class SearchQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.env = mock.patch.dict(os.environ, {"BOOKMANAGER_DIAGNOSTIC_HOME": str(self.root / "diag")})
        self.env.start()
        self.store = LibraryStore(self.root / "data")
        self.mirror_folder = self.store.create_folder(1, "Mirror")
        self.alpha = self.add("Alpha 100% Complete", subtitle="Intro", authors="Alice")
        self.alpha_mirror = self.store.mirror_item(self.alpha, self.mirror_folder)
        self.beta = self.add("Beta 1000 Done", authors="Bob")
        self.gamma = self.add("Gamma", subtitle="Advanced", authors="Alice", tags=("Topic: Testing",))
        self.delta = self.add("Delta", authors="Alice", tags=("Topic: Other",))

    def tearDown(self) -> None:
        self.store.close()
        self.env.stop()
        self.temporary.cleanup()

    def add(self, title: str, *, subtitle: str = "", authors: str = "", tags=()) -> int:
        source = self.root / f"{len(list(self.root.glob('source-*')))}.txt"
        source.write_text(f"content for {title}", encoding="utf-8")
        parts = StructuredFileName("", "", title, subtitle, "1", authors, ".txt")
        return self.store.add_local_file(source, 1, parts, tags=tags)

    def names(self, results) -> list[str]:
        return [item.display_name for _path, item in results]

    def test_unique_search_filters_in_sql_and_groups_mirrors(self) -> None:
        results, truncated = self.store.search_unique_documents("100%")
        self.assertEqual({item.id for _path, item in results}, {self.alpha, self.alpha_mirror})
        self.assertFalse(truncated)
        self.assertEqual({path for path, _item in results}, {"Library", "Library / Mirror"})

    def test_search_is_case_insensitive_and_matches_every_field(self) -> None:
        self.assertEqual(len(self.store.search_unique_documents("alpha")[0]), 2)
        self.assertEqual(len(self.store.search_unique_documents(subtitle="INTRO")[0]), 2)
        author_hits, truncated = self.store.search_unique_documents(authors="alice")
        self.assertEqual(len(author_hits), 4)
        self.assertFalse(truncated)

    def test_wildcards_in_user_input_are_literal(self) -> None:
        self.assertEqual(self.store.search_unique_documents("100_")[0], [])
        hits, _truncated = self.store.search_mirror_items("100%")
        self.assertEqual({item.id for _path, item in hits}, {self.alpha, self.alpha_mirror})
        # The underscore must not behave as a single-character wildcard.
        underscore_doc = self.add("Under_score")
        self.add("UnderXscore")
        self.assertEqual(len(self.store.search_unique_documents("under_score")[0]), 1)
        self.assertIsNotNone(self.store.get_item(underscore_doc))

    def test_truncated_flag_reports_omitted_matches(self) -> None:
        results, truncated = self.store.search_mirror_items(authors="alice", limit=2)
        self.assertEqual(len(results), 2)
        self.assertTrue(truncated)
        results, truncated = self.store.search_mirror_items(authors="bob", limit=2)
        self.assertEqual(len(results), 1)
        self.assertFalse(truncated)

    def test_tag_search_uses_normalized_names_and_reports_truncation(self) -> None:
        hits, truncated = self.store.search_tagged_items("TOPIC", limit=1)
        self.assertEqual(len(hits), 1)
        self.assertTrue(truncated)
        hits, truncated = self.store.search_tagged_items("topic: testing")
        self.assertEqual(self.names(hits), [self.store.get_item(self.gamma).display_name])
        self.assertFalse(truncated)
        self.assertEqual(self.store.search_tagged_items("")[0], [])


if __name__ == "__main__":
    unittest.main()
