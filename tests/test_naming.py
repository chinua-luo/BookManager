from __future__ import annotations

import unittest

from bookmanager.naming import (
    NamingValidationError,
    StructuredFileName,
    build_structured_file_name,
    chinese_number,
    format_edition,
    infer_structured_file_name,
    normalize_extension,
    normalize_structured_file_name,
    parse_edition_number,
    parse_structured_file_name,
    split_author_segment,
    split_leading_series_title,
    split_series_number,
    title_case,
)


def parts(**overrides: str) -> StructuredFileName:
    values = {
        "series_abbr": "",
        "number": "",
        "main_title": "Main Title",
        "subtitle": "",
        "edition": "1",
        "authors": "",
        "extension": ".pdf",
    }
    values.update(overrides)
    return StructuredFileName(**values)


class TitleCaseTests(unittest.TestCase):
    def test_keeps_acronyms_and_brand_spellings(self) -> None:
        self.assertEqual(title_case("AI and ML"), "AI and ML")
        self.assertEqual(title_case("iOS programming"), "iOS Programming")
        self.assertEqual(title_case("HTML and CSS"), "HTML and CSS")

    def test_handles_contractions_and_possessives(self) -> None:
        self.assertEqual(title_case("don't stop"), "Don't Stop")
        self.assertEqual(title_case("what's new in AI"), "What's New in AI")
        self.assertEqual(title_case("O'Reilly media"), "O'Reilly Media")

    def test_lowercases_small_words_only_in_the_middle(self) -> None:
        self.assertEqual(title_case("the lord of the rings"), "The Lord of the Rings")
        self.assertEqual(title_case("a tale of two cities"), "A Tale of Two Cities")

    def test_preserves_whitespace_and_empty_input(self) -> None:
        self.assertEqual(title_case("  multiple   spaces  "), "Multiple   Spaces")
        self.assertEqual(title_case("   "), "")


class ValidationTests(unittest.TestCase):
    def test_rejects_control_characters(self) -> None:
        for value in ("bad\nnewline", "tab\tcharacter", "null\x00byte", "delete\x7fchar"):
            with self.subTest(value=value):
                with self.assertRaises(NamingValidationError) as raised:
                    build_structured_file_name(parts(main_title=value))
                self.assertEqual(raised.exception.field, "mainTitle")
                self.assertIn("控制字符", str(raised.exception))

    def test_rejects_windows_forbidden_characters(self) -> None:
        with self.assertRaises(NamingValidationError):
            build_structured_file_name(parts(main_title="a/b"))
        with self.assertRaises(NamingValidationError):
            build_structured_file_name(parts(authors="a:b"))

    def test_rejects_reserved_delimiters_inside_fields(self) -> None:
        with self.assertRaises(NamingValidationError) as raised:
            build_structured_file_name(parts(main_title="Title - Subtitle"))
        self.assertEqual(raised.exception.field, "mainTitle")

    def test_rejects_empty_combined_name(self) -> None:
        with self.assertRaises(NamingValidationError) as raised:
            build_structured_file_name(parts(main_title="", edition="1"))
        self.assertIn("不能为空", str(raised.exception))

    def test_rejects_invalid_edition(self) -> None:
        with self.assertRaises(NamingValidationError) as raised:
            build_structured_file_name(parts(edition="second"))
        self.assertEqual(raised.exception.field, "edition")
        with self.assertRaises(NamingValidationError):
            build_structured_file_name(parts(edition="0"))


class NormalizationTests(unittest.TestCase):
    def test_normalizes_series_number_edition_and_extension(self) -> None:
        normalized = normalize_structured_file_name(parts(series_abbr="qft", number="7", extension="PDF"))
        self.assertEqual(normalized.series_abbr, "QFT")
        self.assertEqual(normalized.number, "007")
        self.assertEqual(normalized.extension, ".pdf")
        self.assertEqual(normalized.edition_language, "英文")
        self.assertEqual(normalize_extension("epub"), ".epub")
        self.assertEqual(normalize_extension(""), "")

    def test_normalizes_edition_language(self) -> None:
        self.assertEqual(normalize_structured_file_name(parts(edition_language="中文")).edition_language, "中文")
        self.assertEqual(normalize_structured_file_name(parts(edition_language="英文")).edition_language, "英文")
        self.assertEqual(normalize_structured_file_name(parts(edition_language="未知")).edition_language, "英文")


class EditionTests(unittest.TestCase):
    def test_formats_english_and_chinese_editions(self) -> None:
        self.assertEqual(format_edition("1"), "")
        self.assertEqual(format_edition("2"), "2nd Edition")
        self.assertEqual(format_edition("3"), "3rd Edition")
        self.assertEqual(format_edition("11"), "11th Edition")
        self.assertEqual(format_edition("2", "中文"), "第二版")
        self.assertEqual(format_edition("12", "中文"), "第十二版")

    def test_parses_edition_labels(self) -> None:
        self.assertEqual(parse_edition_number("2nd Edition"), "2")
        self.assertEqual(parse_edition_number("第二版"), "2")
        self.assertEqual(parse_edition_number("3"), "3")
        self.assertEqual(chinese_number(2), "二")
        self.assertEqual(chinese_number(12), "十二")
        self.assertEqual(chinese_number(102), "一百零二")


class BuildAndParseTests(unittest.TestCase):
    def test_builds_canonical_name(self) -> None:
        self.assertEqual(
            build_structured_file_name(parts(
                series_abbr="QFT",
                number="007",
                main_title="Quantum Fields",
                subtitle="A Course",
                edition="2",
                authors="Weinberg",
                extension=".pdf",
            )),
            "QFT007 Quantum Fields - A Course - 2nd Edition _ Weinberg.pdf",
        )
        self.assertEqual(build_structured_file_name(parts(main_title="Author", authors="", extension=".pdf")), "Author.pdf")
        self.assertEqual(
            build_structured_file_name(parts(main_title="", authors="Author", extension=".pdf")),
            "Author.pdf",
        )

    def test_round_trips_canonical_names(self) -> None:
        for name in (
            "QFT007 Quantum Fields - A Course - 2nd Edition _ Weinberg.pdf",
            "UTM Elementary Probability Theory _ Someone.pdf",
            "Plain Book Title.pdf",
        ):
            parsed = parse_structured_file_name(name)
            self.assertIsNotNone(parsed)
            self.assertEqual(build_structured_file_name(parsed), name)

    def test_infers_structured_name_from_file_name(self) -> None:
        inferred = infer_structured_file_name("QFT007 Quantum Fields - A Course - 2nd Edition _ Weinberg.pdf")
        self.assertEqual(inferred.series_abbr, "QFT")
        self.assertEqual(inferred.number, "007")
        self.assertEqual(inferred.main_title, "Quantum Fields")
        self.assertEqual(inferred.subtitle, "A Course")
        self.assertEqual(inferred.edition, "2")
        self.assertEqual(inferred.authors, "Weinberg")

    def test_splits_series_number_and_author_segments(self) -> None:
        self.assertEqual(split_series_number("QFT007"), ("QFT", "007"))
        self.assertEqual(split_series_number("Plain Title"), ("Plain Title", ""))
        self.assertEqual(split_author_segment("Title - Subtitle _ Author"), ("Title - Subtitle", "Author"))
        self.assertEqual(split_author_segment("Title"), ("Title", ""))
        self.assertEqual(split_leading_series_title("QFT007 Quantum Fields"), ("QFT", "007", "Quantum Fields"))
        self.assertEqual(split_leading_series_title("Plain Title"), ("", "", ""))


if __name__ == "__main__":
    unittest.main()
